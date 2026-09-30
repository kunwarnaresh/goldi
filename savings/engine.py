"""Scheme Posting Engine - the single writer of scheme money movements.

Every customer payment, allocation, benefit accrual, redemption, refund, forfeiture, credit transfer and their
reversals goes through ``SchemePostingEngine``. In one database transaction it:

* locks the enrolment,
* writes the immutable document (payment / redemption / ...) and the customer scheme ledger entry,
* updates the enrolment's running balances (always equal to the ledger column sums),
* records the accounting event (``SchemePostingEntry``) and, when G/L posting is on, posts it through the
  ERP finance engine - there is no separate accounting engine here,
* writes the audit trail.

Balances are never merged: contribution, benefit, redemption, refund, forfeiture and credit each have their own
ledger column.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone as dj_timezone

from .benefit import ZERO, d, installment_eligible_amount, money
from .models import (
    SchemeEnrollment, SchemeInstallment, SchemeLedgerEntry, SchemeNotification, SchemePayment, SchemePaymentAllocation,
    SchemePostingEntry, SchemeRedemption, SchemeRedemptionLine,
)
from .security import ConfirmationRequired, SchemeError, audit, get_setup, next_number

ACCOUNT_LABELS = {
    'collection': 'Cash / bank collection', 'contribution_liability': 'Scheme contribution liability',
    'benefit_liability': 'Scheme benefit liability', 'benefit_expense': 'Scheme benefit expense', 'penalty_income': 'Late payment income',
    'forfeiture_income': 'Forfeiture / cancellation income', 'redemption_settlement': 'Redemption settlement',
    'customer_credit': 'Customer purchase credit',
}
LEDGER_BUCKETS = ('contribution', 'advance', 'penalty', 'benefit', 'contribution_redeemed', 'benefit_redeemed', 'refund',
                  'contribution_forfeited', 'benefit_forfeited', 'credit_transfer')
ENROLLMENT_FIELDS = {  # ledger column -> enrolment running balance
    'contribution': 'contribution_paid', 'advance': 'unallocated_advance', 'penalty': 'penalty_paid', 'benefit': 'benefit_approved',
    'contribution_redeemed': 'contribution_redeemed', 'benefit_redeemed': 'benefit_redeemed', 'refund': 'contribution_refunded',
    'contribution_forfeited': 'contribution_forfeited', 'benefit_forfeited': 'benefit_forfeited', 'credit_transfer': 'contribution_credited',
}


def lock(actor, enrollment):
    return SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)


def staff_name(user):
    return (user.get_full_name() or user.get_username())[:150] if user else ''


def add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, 28))


# ---------------------------------------------------------------------------
# Installment and enrolment status
# ---------------------------------------------------------------------------

def refresh_installment(inst, rules, as_of, reminder_days):
    """Date-driven status of one installment. Never cancels anything - missed installments just become overdue."""
    if inst.status in ('WAIVED', 'ADJUSTED', 'CANCELLED'):
        return inst.status
    paid, scheduled = d(inst.paid_amount), d(inst.scheduled_amount)
    grace_end = inst.due_date + timedelta(days=int(rules.get('grace_days') or 0))
    if scheduled and paid >= scheduled:
        status = 'PAID'
    elif as_of > grace_end:
        status = 'OVERDUE'
    elif paid > 0:
        status = 'PARTIALLY_PAID'
    elif as_of > inst.due_date:
        status = 'GRACE'
    elif as_of >= inst.due_date - timedelta(days=reminder_days):
        status = 'DUE'
    else:
        status = 'UPCOMING'
    inst.status = status
    if status != 'PAID':
        inst.late_days = max((as_of - inst.due_date).days, 0)
    return status


def derive_status(enrollment, installments, as_of):
    if enrollment.status not in SchemeEnrollment.CONTRIBUTING:
        return enrollment.status
    statuses = [i.status for i in installments]
    if all(s in SchemeInstallment.SETTLED for s in statuses):
        return 'COMPLETED'
    if 'OVERDUE' in statuses:
        return 'OVERDUE'
    if 'PARTIALLY_PAID' in statuses:
        return 'PARTIALLY_PAID'
    if 'DUE' in statuses or 'GRACE' in statuses:
        return 'PAYMENT_DUE'
    return 'ACTIVE'


def refresh_enrollment(enrollment, as_of=None, setup=None):
    """Recompute installment and enrolment statuses for a date. Idempotent."""
    as_of = as_of or dj_timezone.localdate()
    setup = setup or get_setup(enrollment.tenant)
    installments = list(enrollment.installments.all())
    for inst in installments:
        before = (inst.status, inst.late_days)
        refresh_installment(inst, enrollment.rules, as_of, setup.reminder_days)
        if (inst.status, inst.late_days) != before:
            inst.save(update_fields=['status', 'late_days', 'updated_at'])
    status = derive_status(enrollment, installments, as_of)
    if status != enrollment.status:
        enrollment.status = status
        enrollment.save(update_fields=['status', 'updated_at'])
    return installments


def queue_notification(enrollment, event, message, dedupe_key):
    """Hand a message to the Goldio notification engine's queue. Duplicate keys are ignored, so jobs can re-run."""
    for channel, recipient in (('SMS', enrollment.mobile), ('WHATSAPP', enrollment.mobile), ('EMAIL', enrollment.email)):
        if recipient:
            SchemeNotification.objects.get_or_create(
                tenant=enrollment.tenant, dedupe_key=dedupe_key[:120], channel=channel,
                defaults={'enrollment': enrollment, 'event': event, 'recipient': recipient, 'message': message},
            )


# ---------------------------------------------------------------------------
# Posting engine
# ---------------------------------------------------------------------------

class SchemePostingEngine:
    def __init__(self, actor, enrollment, *, posting_date=None, location=None):
        self.actor, self.user, self.tenant = actor, actor.user, actor.tenant
        self.enrollment = lock(actor, enrollment)
        self.setup = get_setup(actor.tenant)
        self.posting_date = posting_date or dj_timezone.localdate()
        self.location = location

    # -- ledger + accounting primitives ------------------------------------------------------------------------

    def ledger(self, entry_type, document_type, document_no, description, **buckets):
        e = self.enrollment
        for bucket, amount in buckets.items():
            if bucket not in LEDGER_BUCKETS:
                raise ValueError(bucket)
            field = ENROLLMENT_FIELDS[bucket]
            setattr(e, field, money(getattr(e, field) + d(amount)))
        for field in ('contribution_paid', 'unallocated_advance', 'benefit_approved'):
            if getattr(e, field) < 0:
                raise SchemeError('This would make a scheme balance negative.')
        if e.contribution_balance < 0 or e.benefit_balance < 0:
            raise SchemeError('This would take the scheme below zero - the entitlement is already used.')
        e.save()
        return SchemeLedgerEntry.objects.create(
            tenant=self.tenant, company=e.company, enrollment=e, entry_date=self.posting_date, entry_type=entry_type,
            document_type=document_type, document_no=document_no, description=description[:250],
            entitlement_balance=e.available_entitlement, location=self.location, staff=self.user, staff_name=staff_name(self.user),
            created_by=self.user, **{k: money(v) for k, v in buckets.items()},
        )

    def _account(self, role, *, method_type='', bank_account=None, invoice=None):
        s = self.setup
        if role == 'collection':
            if bank_account is not None and bank_account.gl_account_id:
                return bank_account.gl_account
            if method_type and method_type != 'cash' and s.bank_clearing_account_id:
                return s.bank_clearing_account
            if s.cash_account_id:
                return s.cash_account
            from erp import services as erp_services
            try:
                return erp_services.get_posting_setup(self._company()).default_cash_account
            except ValueError:
                return None
        if role == 'redemption_settlement':
            if s.redemption_settlement_account_id:
                return s.redemption_settlement_account
            if invoice is not None:
                from erp import services as erp_services
                try:
                    group = erp_services._resolve_customer_posting_group(invoice.customer)
                except ValueError:
                    return None
                return getattr(group, 'receivable_account', None)
            return None
        return getattr(s, f'{role}_account', None)

    def _company(self):
        if self.setup.company_id:
            return self.setup.company
        if self.enrollment.company_id:
            return self.enrollment.company
        from erp import services as erp_services
        return erp_services.get_default_company()

    def account(self, document, document_type, document_no, lines, **resolve):
        """lines: [(account_role, signed amount)] - positive = debit. Records the event, posts to the G/L when enabled."""
        lines = [(role, money(amount)) for role, amount in lines if money(amount) != 0]
        if not lines:
            return None
        if sum((a for _, a in lines), ZERO) != 0:
            raise SchemeError('Internal error: scheme posting is not balanced.')
        accounts = {role: self._account(role, **resolve) for role, _ in lines}
        posted = self.setup.gl_posting_enabled
        voucher = None
        if posted:
            missing = sorted({ACCOUNT_LABELS[r] for r, a in accounts.items() if a is None})
            if missing:
                raise SchemeError(f'Jewellery savings posting setup is missing G/L accounts: {", ".join(missing)}.')
            from erp import services as erp_services
            voucher_lines = []
            for number, (role, amount) in enumerate(lines, 1):
                line = {'line_no': number, 'account': accounts[role],
                        'description': f'{self.enrollment.account_no} {document_no} {ACCOUNT_LABELS[role]}'[:250]}
                line['debit_amount' if amount > 0 else 'credit_amount'] = abs(amount)
                voucher_lines.append(line)
            try:
                voucher = erp_services._post_document_voucher(
                    company=self._company(), voucher_type_code='scheme_journal', user=self.user, lines=voucher_lines,
                    narration=f'Jewellery savings {self.enrollment.account_no} - {document_type} {document_no}',
                    document_no=document_no, voucher_date=self.posting_date, source_doc=document, source_doc_type=f'scheme_{document_type}',
                )
            except ValueError as exc:
                raise SchemeError(f'G/L posting failed: {exc}')
        for role, amount in lines:
            SchemePostingEntry.objects.create(
                tenant=self.tenant, company=self.enrollment.company, document_type=document_type, document_no=document_no,
                enrollment=self.enrollment, posting_date=self.posting_date, account_role=role, account=accounts[role],
                debit=amount if amount > 0 else ZERO, credit=-amount if amount < 0 else ZERO,
                status='POSTED' if posted else 'RECORDED', finance_voucher=voucher, created_by=self.user,
            )
        return voucher

    def benefit_accrued(self):
        return self.setup.benefit_treatment == 'ACCRUE_ON_APPROVAL'

    # -- collections --------------------------------------------------------------------------------------------

    def _penalty_for(self, inst):
        rules = self.enrollment.rules
        rule, value = rules.get('late_payment_rule'), d(rules.get('late_payment_value'))
        grace_end = inst.due_date + timedelta(days=int(rules.get('grace_days') or 0))
        if inst.penalty_amount or self.posting_date <= grace_end:
            return ZERO
        if rule == 'FIXED':
            return money(value)
        if rule == 'PERCENT':
            return money(d(inst.scheduled_amount) * value / 100)
        return ZERO

    def _apply(self, inst, amount, allocation_type, payment, penalty=ZERO):
        rules = self.enrollment.rules
        grace_end = inst.due_date + timedelta(days=int(rules.get('grace_days') or 0))
        inst.paid_amount = money(inst.paid_amount + amount)
        inst.penalty_amount = money(inst.penalty_amount + penalty)
        inst.first_payment_date = inst.first_payment_date or self.posting_date
        if inst.paid_amount >= inst.scheduled_amount:
            inst.settled_date = self.posting_date
            inst.paid_within_grace = self.posting_date <= grace_end
            inst.late_days = max((self.posting_date - inst.due_date).days, 0)
            inst.status = 'PAID'
        inst.eligible_amount = installment_eligible_amount(rules, inst)
        inst.save()
        SchemePaymentAllocation.objects.create(
            tenant=self.tenant, company=self.enrollment.company, payment=payment, installment=inst, allocation_type=allocation_type,
            amount=amount, penalty_amount=penalty, within_grace=self.posting_date <= grace_end, created_by=self.user,
        )

    def _plan_allocation(self, amount):
        """[(installment, amount, type, penalty)], advance, penalty - raises if the scheme rules reject the payment."""
        e, rules = self.enrollment, self.enrollment.rules
        open_insts = [i for i in e.installments.order_by('installment_no') if i.room > 0]
        if not open_insts:
            if rules.get('advance_mode') == 'UNALLOCATED':
                return [], amount, ZERO
            raise SchemeError('Every installment of this scheme is already paid.')
        month_end = add_months(self.posting_date.replace(day=1), 1) - timedelta(days=1)
        partial_ok = rules.get('partial_payment_allowed')
        advance_mode = rules.get('advance_mode')
        max_advance = int(rules.get('max_advance_installments') or 0)
        plan, remaining, penalty_total, future_count = [], d(amount), ZERO, 0
        for index, inst in enumerate(open_insts):
            if remaining <= 0:
                break
            if index == 0 or inst.due_date <= month_end:
                kind = 'ARREAR' if inst.due_date < self.posting_date else 'CURRENT'
            else:
                if advance_mode != 'ALLOCATE_FUTURE' or (max_advance and future_count >= max_advance):
                    break
                kind = 'FUTURE'
                future_count += 1
            penalty = self._penalty_for(inst) if kind == 'ARREAR' else ZERO
            if penalty:
                if remaining <= penalty:
                    raise SchemeError(f'Installment {inst.installment_no} is overdue: a late payment charge of ₹{penalty} applies before contribution.')
                remaining -= penalty
                penalty_total += penalty
            take = min(remaining, inst.room)
            if not partial_ok and take < inst.outstanding:
                if penalty:
                    remaining += penalty
                    penalty_total -= penalty
                break
            plan.append((inst, money(take), kind, penalty))
            remaining -= take
        advance = money(remaining)
        if advance > 0:
            if advance_mode == 'UNALLOCATED':
                pass
            elif not partial_ok and not plan:
                raise SchemeError(f'Partial payment is not allowed: pay at least ₹{open_insts[0].outstanding} for installment {open_insts[0].installment_no}.')
            elif advance_mode == 'NOT_ALLOWED':
                raise SchemeError(f'Amount exceeds the installment(s) currently due; advance payment is not allowed on this scheme. '
                                  f'Maximum today: ₹{money(amount - advance)}.')
            elif not partial_ok:
                raise SchemeError(f'Partial payment is not allowed: ₹{advance} cannot settle a whole installment.')
            else:
                raise SchemeError(f'Amount exceeds the total outstanding contribution by ₹{advance}.')
        return plan, advance, penalty_total

    def collect(self, *, amount, payment_method=None, method_type='', bank_account=None, reference_no='', idempotency_key='',
                remarks='', confirm_duplicate=False):
        e = self.enrollment
        amount = money(amount)
        if idempotency_key:
            existing = SchemePayment.objects.filter(tenant=self.tenant, idempotency_key=idempotency_key).first()
            if existing:
                if existing.enrollment_id != e.pk or existing.amount != amount:
                    raise SchemeError('This idempotency key was already used for a different payment.')
                return existing
        if e.status not in SchemeEnrollment.CONTRIBUTING:
            raise SchemeError(f'{e.account_no} is {e.get_status_display().lower()}; installments cannot be collected.')
        if amount <= 0:
            raise SchemeError('Enter an amount greater than zero.')
        method_type = method_type or (payment_method.method_type if payment_method else '')
        if not method_type:
            raise SchemeError('Select a payment method.')
        if reference_no and SchemePayment.objects.filter(tenant=self.tenant, reference_no=reference_no, status='POSTED',
                                                         method_type=method_type).exists():
            raise SchemeError(f'A scheme payment with reference {reference_no} is already posted (duplicate payment).')
        same_day = SchemePayment.objects.filter(tenant=self.tenant, enrollment=e, amount=amount, payment_date=self.posting_date,
                                                status='POSTED').exists()
        if same_day and not confirm_duplicate:
            raise ConfirmationRequired(f'₹{amount} was already collected on {e.account_no} today. Is this a second payment?')
        self._check_location('collect_at_any_location')

        plan, advance, penalty = self._plan_allocation(amount)
        contribution = money(amount - penalty)
        payment = SchemePayment.objects.create(
            tenant=self.tenant, company=e.company, receipt_no=next_number(self.tenant, 'SCHEME_RECEIPT'), enrollment=e,
            payment_date=self.posting_date, amount=amount, contribution_amount=contribution, penalty_amount=penalty,
            advance_amount=advance, payment_method=payment_method, method_type=method_type, bank_account=bank_account,
            reference_no=reference_no, location=self.location, collected_by=self.user, collected_by_name=staff_name(self.user),
            idempotency_key=idempotency_key, remarks=remarks[:250], created_by=self.user,
        )
        for inst, take, kind, pen in plan:
            self._apply(inst, take, kind, payment, pen)
        described = ', '.join(f'#{i.installment_no} ₹{t}' for i, t, _, _ in plan) or 'no installment'
        self.ledger('ADVANCE' if not plan else 'PAYMENT', 'receipt', payment.receipt_no,
                    f'Receipt {payment.receipt_no}: {described}' + (f', advance ₹{advance}' if advance else '') + (f', late charge ₹{penalty}' if penalty else ''),
                    contribution=contribution, advance=advance, penalty=penalty)
        payment.finance_voucher = self.account(payment, 'receipt', payment.receipt_no,
                                               [('collection', amount), ('contribution_liability', -contribution), ('penalty_income', -penalty)],
                                               method_type=method_type, bank_account=bank_account)
        if payment.finance_voucher:
            payment.save(update_fields=['finance_voucher', 'updated_at'])
        refresh_enrollment(e, self.posting_date, self.setup)
        audit(self.actor, 'collect', 'SCHEME_PAYMENT', payment.receipt_no, enrollment=e,
              new={'amount': amount, 'contribution': contribution, 'advance': advance, 'penalty': penalty, 'method': method_type})
        queue_notification(e, 'PAYMENT', f'We have received ₹{amount} for your Jewellery Scheme Account {e.account_no}. Receipt {payment.receipt_no}.',
                           f'payment:{payment.receipt_no}')
        return payment

    def apply_advance(self):
        """Move unallocated advance into installments whose month has arrived. Idempotent; no money moves."""
        e = self.enrollment
        if e.unallocated_advance <= 0 or e.status not in SchemeEnrollment.CONTRIBUTING:
            return ZERO
        # Oldest money first; each allocation points at the payment the advance really came from, so reversals stay exact.
        sources = []
        for payment in e.payments.filter(status='POSTED', advance_amount__gt=0).order_by('id'):
            left = money(payment.contribution_amount - (payment.allocations.aggregate(s=Sum('amount'))['s'] or ZERO))
            if left > 0:
                sources.append([payment, left])
        month_end = add_months(self.posting_date.replace(day=1), 1) - timedelta(days=1)
        applied, documents = ZERO, []
        for inst in e.installments.filter(due_date__lte=month_end).order_by('installment_no'):
            need = inst.room
            available = sum((s[1] for s in sources), ZERO)
            if need <= 0 or available <= 0:
                continue
            if not e.rules.get('partial_payment_allowed') and available < inst.outstanding:
                break
            for source in sources:
                take = min(source[1], need)
                if take <= 0:
                    continue
                self._apply(inst, money(take), 'ADVANCE_APPLIED', source[0])
                source[1] -= take
                need -= take
                applied += take
                documents.append(source[0].receipt_no)
                inst.refresh_from_db()
                if need <= 0:
                    break
        if applied:
            self.ledger('ALLOCATION', 'receipt', documents[0], f'Advance ₹{money(applied)} from {", ".join(dict.fromkeys(documents))} applied to due installment(s)',
                        advance=-applied)
            refresh_enrollment(e, self.posting_date, self.setup)
        return money(applied)

    def reverse_payment(self, payment, *, reason):
        e = self.enrollment
        payment = SchemePayment.objects.select_for_update().get(pk=payment.pk, tenant=self.tenant)
        if payment.enrollment_id != e.pk:
            raise SchemeError('Payment belongs to another scheme account.')
        if payment.status != 'POSTED':
            raise SchemeError(f'{payment.receipt_no} is {payment.get_status_display().lower()}.')
        if not (reason or '').strip():
            raise SchemeError('A reason is required to reverse a payment.')
        if e.status not in SchemeEnrollment.CONTRIBUTING:
            raise SchemeError('The scheme has matured; reverse the benefit (or redemptions) before reversing payments.')
        allocations = list(payment.allocations.select_related('installment'))
        allocated = sum((a.amount for a in allocations), ZERO)
        still_unallocated = money(payment.contribution_amount - allocated)
        if still_unallocated > e.unallocated_advance:
            raise SchemeError('Part of this payment is no longer held as advance; reverse the later allocation first.')
        reversal = SchemePayment.objects.create(
            tenant=self.tenant, company=e.company, receipt_no=next_number(self.tenant, 'SCHEME_REVERSAL'), enrollment=e,
            payment_date=self.posting_date, amount=-payment.amount, contribution_amount=-payment.contribution_amount,
            penalty_amount=-payment.penalty_amount, advance_amount=-payment.advance_amount, payment_method=payment.payment_method,
            method_type=payment.method_type, bank_account=payment.bank_account, reference_no=payment.reference_no,
            location=self.location, collected_by=self.user, collected_by_name=staff_name(self.user), remarks=reason[:250],
            status='REVERSAL', reversal_of=payment, created_by=self.user,
        )
        for alloc in allocations:
            inst = alloc.installment
            inst.paid_amount = money(inst.paid_amount - alloc.amount)
            inst.penalty_amount = money(inst.penalty_amount - alloc.penalty_amount)
            if inst.paid_amount < inst.scheduled_amount:
                inst.settled_date, inst.paid_within_grace = None, False
                inst.status = 'PARTIALLY_PAID' if inst.paid_amount > 0 else 'DUE'
            if inst.paid_amount <= 0:
                inst.first_payment_date = None
            inst.eligible_amount = installment_eligible_amount(e.rules, inst)
            inst.save()
        self.ledger('PAYMENT_REVERSAL', 'receipt', reversal.receipt_no, f'Reversal of {payment.receipt_no}: {reason}',
                    contribution=-payment.contribution_amount, advance=-still_unallocated, penalty=-payment.penalty_amount)
        reversal.finance_voucher = self.account(reversal, 'receipt_reversal', reversal.receipt_no,
                                                [('collection', -payment.amount), ('contribution_liability', payment.contribution_amount),
                                                 ('penalty_income', payment.penalty_amount)],
                                                method_type=payment.method_type, bank_account=payment.bank_account)
        if reversal.finance_voucher:
            reversal.save(update_fields=['finance_voucher', 'updated_at'])
        payment.status, payment.reversed_by = 'REVERSED', reversal
        payment.save(update_fields=['status', 'reversed_by', 'updated_at'])
        if e.status == 'COMPLETED':
            e.status = 'ACTIVE'
            e.save(update_fields=['status', 'updated_at'])
        refresh_enrollment(e, self.posting_date, self.setup)
        same_day = payment.payment_date == self.posting_date
        audit(self.actor, 'reverse_payment', 'SCHEME_PAYMENT', payment.receipt_no, enrollment=e,
              new={'reversal': reversal.receipt_no, 'amount': payment.amount, 'same_day': same_day}, reason=reason)
        if same_day and self.setup.same_day_reversal_alert:
            audit(self.actor, 'alert_same_day_reversal', 'SCHEME_PAYMENT', payment.receipt_no, enrollment=e, reason='Same-day reversal')
        return reversal

    # -- benefit ------------------------------------------------------------------------------------------------

    def accrue_benefit(self, calculation, amount, *, description):
        amount = money(amount)
        self.ledger('BENEFIT_ACCRUAL', 'benefit', calculation.calculation_no, description, benefit=amount)
        if self.benefit_accrued():
            self.account(calculation, 'benefit', calculation.calculation_no,
                         [('benefit_expense', amount), ('benefit_liability', -amount)])

    def reverse_benefit(self, calculation, *, reason):
        e = self.enrollment
        amount = money(calculation.approved_benefit or 0)
        if e.benefit_redeemed:
            raise SchemeError('Part of the benefit has been redeemed; reverse those redemptions first.')
        self.ledger('BENEFIT_REVERSAL', 'benefit', calculation.calculation_no, f'Benefit reversal: {reason}', benefit=-amount)
        if self.benefit_accrued():
            self.account(calculation, 'benefit_reversal', calculation.calculation_no,
                         [('benefit_liability', amount), ('benefit_expense', -amount)])

    # -- redemption ---------------------------------------------------------------------------------------------

    def _check_location(self, flag):
        scheme = self.enrollment.scheme
        if self.location is None or getattr(scheme, flag):
            return
        allowed = set(scheme.locations.values_list('pk', flat=True))
        if self.enrollment.branch_id:
            allowed.add(self.enrollment.branch_id)
        if allowed and self.location.pk not in allowed:
            raise SchemeError(f'{self.location.code} is not a participating location for {scheme.code}.')

    @staticmethod
    def line_eligible(rules, line):
        """(eligible, reason) of one jewellery line under the scheme's product rules."""
        def matches(rule):
            scope, value = rule['scope'], (rule.get('value') or '').strip().lower()
            if scope == 'ALL':
                return True
            if scope == 'TAG':
                return value in [t.strip().lower() for t in (line.get('tags') or '').split(',')]
            field = {'METAL': 'metal', 'CATEGORY': 'category', 'COLLECTION': 'collection', 'SKU': 'item_code', 'LOCATION': 'location'}[scope]
            return (line.get(field) or '').strip().lower() == value
        product_rules = rules.get('product_rules') or []
        for rule in product_rules:
            if rule['rule_type'] == 'EXCLUDE' and matches(rule):
                return False, f'Excluded ({rule["scope"].lower()} {rule.get("value") or ""})'.strip()
        includes = [r for r in product_rules if r['rule_type'] == 'INCLUDE']
        if includes and not any(matches(r) for r in includes):
            return False, 'Not in the scheme\'s eligible products'
        return True, ''

    def redeem(self, *, lines, invoice_value=None, sales_invoice=None, invoice_reference='', amount=None, idempotency_key='', reason=''):
        e, rules = self.enrollment, self.enrollment.rules
        if idempotency_key:
            existing = SchemeRedemption.objects.filter(tenant=self.tenant, idempotency_key=idempotency_key).first()
            if existing:
                return existing
        early = e.status in SchemeEnrollment.CONTRIBUTING and rules.get('redeem_before_maturity')
        if e.status not in SchemeEnrollment.REDEEMABLE and not early:
            raise SchemeError(f'{e.account_no} is {e.get_status_display().lower()}; only schemes with an approved benefit can be redeemed.')
        if e.redemption_valid_until and self.posting_date > e.redemption_valid_until:
            raise SchemeError(f'The redemption window ended on {e.redemption_valid_until}.')
        self._check_location('redeem_at_any_location')
        if sales_invoice is not None:
            if sales_invoice.customer_id != e.customer_id:
                raise SchemeError('The invoice belongs to a different customer.')
            if SchemeRedemption.objects.filter(tenant=self.tenant, sales_invoice=sales_invoice, status='POSTED').exists():
                raise SchemeError(f'Invoice {sales_invoice.invoice_no} already has a scheme redemption (duplicate redemption).')
            invoice_value = sales_invoice.balance_amount if invoice_value is None else invoice_value
            invoice_reference = invoice_reference or sales_invoice.invoice_no
        max_count = int(rules.get('max_redemptions') or 0)
        if max_count and e.redemption_count >= max_count:
            raise SchemeError(f'This scheme allows {max_count} redemption(s) and they have been used.')
        if e.redemption_count and not rules.get('partial_redemption_allowed'):
            raise SchemeError('Multiple redemptions are not allowed on this scheme.')
        if not lines:
            raise SchemeError('Add the jewellery being purchased.')

        eligible_value = making_eligible = ZERO
        prepared = []
        for line in lines:
            value, making = money(line.get('line_value')), money(line.get('making_charge'))
            ok, why = self.line_eligible(rules, line)
            prepared.append((line, value, making, ok, why))
            if ok:
                usable = value if rules.get('making_charge_eligible') else value - making
                eligible_value += usable
                making_eligible += making if rules.get('making_charge_eligible') else ZERO
        lines_total = money(sum((p[1] for p in prepared), ZERO))
        invoice_value = money(invoice_value if invoice_value is not None else lines_total)
        eligible_value = min(money(eligible_value), invoice_value)
        if eligible_value <= 0:
            raise SchemeError('None of the selected jewellery is eligible under this scheme.')

        contribution_available = e.contribution_balance
        benefit_available = ZERO if early else e.benefit_balance
        application = rules.get('benefit_application')
        if application == 'MAKING_CHARGE':
            benefit_cap = min(benefit_available, making_eligible)
        elif application == 'DISCOUNT':
            benefit_cap = min(benefit_available, money(eligible_value * d(rules.get('discount_cap_percent')) / 100))
        else:
            benefit_cap = benefit_available
        limit = eligible_value
        if amount is not None:
            limit = min(limit, money(amount))
        max_one = d(rules.get('max_redemption_amount'))
        if max_one:
            limit = min(limit, max_one)
        contribution_use = min(contribution_available, limit)
        benefit_use = min(benefit_cap, limit - contribution_use)
        total = money(contribution_use + benefit_use)
        if total <= 0:
            raise SchemeError('There is no entitlement available to redeem.')
        if total < d(rules.get('min_redemption_amount')):
            raise SchemeError(f'Minimum redemption is ₹{money(d(rules.get("min_redemption_amount")))}.')
        remaining_after = money(e.available_entitlement - total)
        if (not rules.get('partial_redemption_allowed') and remaining_after > 0 and application in ('MONETARY', 'CREDIT')
                and rules.get('remaining_balance_rule', 'KEEP') == 'KEEP'):
            raise SchemeError(f'Partial redemption is not allowed: the purchase must use the full entitlement of ₹{e.available_entitlement}.')

        redemption = SchemeRedemption.objects.create(
            tenant=self.tenant, company=e.company, redemption_no=next_number(self.tenant, 'SCHEME_REDEMPTION'), enrollment=e,
            redemption_date=self.posting_date, location=self.location, sales_invoice=sales_invoice, invoice_reference=invoice_reference,
            invoice_value=invoice_value, eligible_value=eligible_value, making_charge_value=money(sum((p[2] for p in prepared), ZERO)),
            contribution_applied=money(contribution_use), benefit_applied=money(benefit_use), amount=total,
            balance_payable=money(invoice_value - total), staff=self.user, staff_name=staff_name(self.user),
            idempotency_key=idempotency_key, reason=reason[:250], created_by=self.user,
        )
        for line, value, making, ok, why in prepared:
            SchemeRedemptionLine.objects.create(
                tenant=self.tenant, company=e.company, redemption=redemption, description=(line.get('description') or 'Jewellery')[:200],
                item_code=line.get('item_code', '')[:60], metal=line.get('metal', '')[:20], category=line.get('category', '')[:100],
                collection=line.get('collection', '')[:100], tags=line.get('tags', '')[:200], line_value=value, making_charge=making,
                eligible=ok, ineligible_reason=why[:150], created_by=self.user,
            )
        self.ledger('REDEMPTION', 'redemption', redemption.redemption_no,
                    f'Redeemed against {invoice_reference or "jewellery purchase"} (invoice ₹{invoice_value})',
                    contribution_redeemed=contribution_use, benefit_redeemed=benefit_use)
        benefit_role = 'benefit_liability' if self.benefit_accrued() else 'benefit_expense'
        redemption.finance_voucher = self.account(
            redemption, 'redemption', redemption.redemption_no,
            [('contribution_liability', contribution_use), (benefit_role, benefit_use), ('redemption_settlement', -total)],
            invoice=sales_invoice)
        if redemption.finance_voucher:
            redemption.save(update_fields=['finance_voucher', 'updated_at'])
        if sales_invoice is not None:
            self._settle_invoice(sales_invoice, total)
        e.redemption_count += 1
        if not early:
            e.status = 'REDEEMED' if e.available_entitlement <= 0 else 'PARTIALLY_REDEEMED'
            if e.status == 'REDEEMED':
                e.closed_at = dj_timezone.now()
        e.save()
        audit(self.actor, 'redeem', 'SCHEME_REDEMPTION', redemption.redemption_no, enrollment=e,
              new={'amount': total, 'contribution': contribution_use, 'benefit': benefit_use, 'invoice': invoice_reference})
        queue_notification(e, 'REDEMPTION', f'₹{total} from your Jewellery Scheme {e.account_no} has been redeemed against '
                                            f'{invoice_reference or "your purchase"}. Available: ₹{e.available_entitlement}.',
                           f'redemption:{redemption.redemption_no}')
        return redemption

    def _settle_invoice(self, invoice, amount):
        from erp.models import SalesInvoice
        invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)
        invoice.paid_amount = money(invoice.paid_amount + amount)
        if invoice.paid_amount <= 0:
            invoice.payment_status = 'unpaid'
        elif invoice.paid_amount < invoice.total_amount:
            invoice.payment_status = 'partially_paid'
        elif invoice.paid_amount == invoice.total_amount:
            invoice.payment_status = 'paid'
        else:
            invoice.payment_status = 'overpaid'
        invoice.save(update_fields=['paid_amount', 'payment_status'])

    def reverse_redemption(self, redemption, *, reason):
        e = self.enrollment
        redemption = SchemeRedemption.objects.select_for_update().get(pk=redemption.pk, tenant=self.tenant)
        if redemption.enrollment_id != e.pk or redemption.status != 'POSTED':
            raise SchemeError(f'{redemption.redemption_no} cannot be reversed.')
        if not (reason or '').strip():
            raise SchemeError('A reason is required to reverse a redemption.')
        if e.status in ('CANCELLED', 'REFUNDED', 'CLOSED'):
            raise SchemeError(f'{e.account_no} is {e.get_status_display().lower()}.')
        reversal = SchemeRedemption.objects.create(
            tenant=self.tenant, company=e.company, redemption_no=next_number(self.tenant, 'SCHEME_REDEMPTION'), enrollment=e,
            redemption_date=self.posting_date, location=self.location, sales_invoice=redemption.sales_invoice,
            invoice_reference=redemption.invoice_reference, invoice_value=-redemption.invoice_value, eligible_value=-redemption.eligible_value,
            contribution_applied=-redemption.contribution_applied, benefit_applied=-redemption.benefit_applied, amount=-redemption.amount,
            balance_payable=ZERO, staff=self.user, staff_name=staff_name(self.user), status='REVERSAL', reversal_of=redemption,
            reason=reason[:250], created_by=self.user,
        )
        self.ledger('REDEMPTION_REVERSAL', 'redemption', reversal.redemption_no, f'Reversal of {redemption.redemption_no}: {reason}',
                    contribution_redeemed=-redemption.contribution_applied, benefit_redeemed=-redemption.benefit_applied)
        benefit_role = 'benefit_liability' if self.benefit_accrued() else 'benefit_expense'
        reversal.finance_voucher = self.account(
            reversal, 'redemption_reversal', reversal.redemption_no,
            [('contribution_liability', -redemption.contribution_applied), (benefit_role, -redemption.benefit_applied),
             ('redemption_settlement', redemption.amount)], invoice=redemption.sales_invoice)
        if reversal.finance_voucher:
            reversal.save(update_fields=['finance_voucher', 'updated_at'])
        if redemption.sales_invoice_id:
            self._settle_invoice(redemption.sales_invoice, -redemption.amount)
        redemption.status, redemption.reversed_by = 'REVERSED', reversal
        redemption.save(update_fields=['status', 'reversed_by', 'updated_at'])
        e.redemption_count = max(e.redemption_count - 1, 0)
        if e.status in ('REDEEMED', 'PARTIALLY_REDEEMED'):
            e.status = 'PARTIALLY_REDEEMED' if e.redemption_count else 'BENEFIT_APPROVED'
            e.closed_at = None
        e.save()
        audit(self.actor, 'reverse_redemption', 'SCHEME_REDEMPTION', redemption.redemption_no, enrollment=e,
              new={'reversal': reversal.redemption_no, 'amount': redemption.amount}, reason=reason)
        return reversal

    # -- forfeiture, credit, refund, adjustment ----------------------------------------------------------------

    def forfeit(self, document, document_no, *, contribution=ZERO, benefit=ZERO, description):
        contribution, benefit = money(contribution), money(benefit)
        if not contribution and not benefit:
            return
        self.ledger('FORFEITURE', 'forfeiture', document_no, description, contribution_forfeited=contribution, benefit_forfeited=benefit)
        lines = [('contribution_liability', contribution), ('forfeiture_income', -contribution)]
        if self.benefit_accrued():
            lines += [('benefit_liability', benefit), ('benefit_expense', -benefit)]
        self.account(document, 'forfeiture', document_no, lines)

    def credit_transfer(self, document, document_no, *, description):
        e = self.enrollment
        contribution, benefit = e.contribution_balance, e.benefit_balance
        self.ledger('CREDIT_TRANSFER', 'credit', document_no, description, credit_transfer=contribution, benefit_forfeited=benefit)
        lines = [('contribution_liability', contribution), ('customer_credit', -contribution)]
        if self.benefit_accrued():
            lines += [('benefit_liability', benefit), ('benefit_expense', -benefit)]
        self.account(document, 'credit', document_no, lines)
        return contribution

    def pay_refund(self, refund):
        e = self.enrollment
        if refund.contribution_refund + refund.deduction > e.contribution_balance:
            raise SchemeError(f'Refund exceeds the contribution balance of ₹{e.contribution_balance}.')
        if refund.benefit_reversal > e.benefit_balance:
            raise SchemeError('Benefit reversal exceeds the remaining benefit.')
        self.ledger('REFUND', 'refund', refund.refund_no, f'Refund {refund.refund_no}: {refund.reason}',
                    refund=refund.contribution_refund, contribution_forfeited=refund.deduction, benefit_forfeited=refund.benefit_reversal)
        lines = [('contribution_liability', refund.contribution_refund + refund.deduction), ('collection', -refund.contribution_refund),
                 ('forfeiture_income', -refund.deduction)]
        if self.benefit_accrued():
            lines += [('benefit_liability', refund.benefit_reversal), ('benefit_expense', -refund.benefit_reversal)]
        return self.account(refund, 'refund', refund.refund_no, lines, method_type=refund.method_type, bank_account=refund.bank_account)

    def post_adjustment(self, adjustment):
        amount, kind = money(adjustment.amount), adjustment.adjustment_type
        desc = f'{adjustment.get_adjustment_type_display()} {adjustment.adjustment_no}: {adjustment.reason}'
        if kind == 'WAIVER':
            inst = adjustment.installment
            if inst is None or inst.enrollment_id != self.enrollment.pk:
                raise SchemeError('Select the installment to waive.')
            inst.status, inst.eligible_amount = 'WAIVED', ZERO
            inst.save(update_fields=['status', 'eligible_amount', 'updated_at'])
            self.ledger('ADJUSTMENT', 'adjustment', adjustment.adjustment_no, desc)
            refresh_enrollment(self.enrollment, self.posting_date, self.setup)
            return
        if kind == 'BENEFIT':
            self.ledger('BENEFIT_ADJUSTMENT', 'adjustment', adjustment.adjustment_no, desc, benefit=amount)
            if self.benefit_accrued():
                self.account(adjustment, 'adjustment', adjustment.adjustment_no, [('benefit_expense', amount), ('benefit_liability', -amount)])
        elif kind == 'PENALTY':
            self.ledger('ADJUSTMENT', 'adjustment', adjustment.adjustment_no, desc, penalty=amount, contribution=-amount)
            self.account(adjustment, 'adjustment', adjustment.adjustment_no, [('contribution_liability', amount), ('penalty_income', -amount)])
        else:  # CONTRIBUTION / CORRECTION: customer balance changes without cash - contra is forfeiture/adjustment income
            self.ledger('ADJUSTMENT', 'adjustment', adjustment.adjustment_no, desc, contribution=amount, advance=amount)
            self.account(adjustment, 'adjustment', adjustment.adjustment_no, [('forfeiture_income', amount), ('contribution_liability', -amount)])


def ledger_totals(enrollment):
    """Column sums of the scheme ledger - these must equal the enrolment's running balances."""
    sums = SchemeLedgerEntry.objects.filter(enrollment=enrollment).aggregate(**{b: Sum(b) for b in LEDGER_BUCKETS})
    return {b: money(sums[b] or 0) for b in LEDGER_BUCKETS}


def reconcile(enrollment):
    """[(bucket, ledger, balance)] where the running balance disagrees with the ledger - empty when consistent."""
    totals = ledger_totals(enrollment)
    return [(b, totals[b], getattr(enrollment, f)) for b, f in ENROLLMENT_FIELDS.items() if totals[b] != getattr(enrollment, f)]


def as_decimal(value, default=None):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except Exception:  # noqa: BLE001 - any unparsable input means "not a number"
        return default


atomic = transaction.atomic
