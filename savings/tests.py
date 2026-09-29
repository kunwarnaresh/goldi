"""Jewellery Savings acceptance tests - the spec's Gold Jewellery 11+1 scenario plus redemption, missed/partial payment,
cancellation/refund, controls and accounting (spec section 79)."""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import TestCase

from erp.models import Company, Customer, FinanceVoucher, GLAccount, PaymentMethod, SalesInvoice
from inventory.models import Location, TenantMembership
from inventory.services import Actor
from inventory.tenancy import create_tenant_for_user

from savings import services as svc
from savings.engine import SchemePostingEngine, reconcile
from savings.models import (
    BenefitCalculation, SavingsRole, SchemeEnrollment, SchemeInstallment, SchemeLedgerEntry, SchemePayment, SchemePostingEntry,
    SchemeVersion,
)
from savings.security import ConfirmationRequired, SchemeError, get_setup

D = Decimal
TEN_K = D('10000')


class SavingsFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner@goldio.test', password='pw-12345678')
        self.tenant = create_tenant_for_user(self.owner, 'Goldio Savings')
        self.admin = Actor(self.tenant, self.owner)
        self.manager = self._member('manager', 'MANAGER')
        self.operator = self._member('operator', 'OPERATOR')
        self.finance = self._member('finance', 'FINANCE')
        self.delhi = Location.objects.create(tenant=self.tenant, code='DEL', name='Delhi Store')
        self.mumbai = Location.objects.create(tenant=self.tenant, code='MUM', name='Mumbai Store')
        self.cash = PaymentMethod.objects.create(code='CASH', name='Cash', method_type='cash')
        self.upi = PaymentMethod.objects.create(code='UPI', name='UPI', method_type='upi')
        self.rahul = Customer.objects.create(name='Rahul Sharma', phone='9811000001', email='rahul@example.com', pan='ABCDE1234F')

    def _member(self, username, role):
        user = User.objects.create_user(username, password='pw-12345678')
        TenantMembership.objects.create(tenant=self.tenant, user=user, role='member', all_locations=True)
        SavingsRole.objects.create(tenant=self.tenant, user=user, role=role)
        return Actor(self.tenant, user)

    def make_scheme(self, code='GJ11', template='11+1', product_rules=(), **rules):
        scheme = svc.create_scheme(self.admin, code=code, name='Gold Jewellery 11+1 Plan', template=template, **rules)
        version = scheme.versions.get()
        if product_rules:
            svc.set_product_rules(self.admin, version, product_rules)
        svc.transition_version(self.admin, version, 'submit')
        svc.transition_version(self.manager, version, 'approve')
        svc.transition_version(self.manager, version, 'activate')
        scheme.refresh_from_db()
        return scheme

    def enroll(self, scheme, customer=None, start=date(2026, 1, 1), **kw):
        e = svc.enroll(self.operator, scheme=scheme, customer=customer or self.rahul, start_date=start, branch=self.delhi,
                       confirm_duplicate=True, **kw)
        svc.accept_agreement(self.operator, e, method='PHYSICAL', signature_reference='Signed form #1')
        svc.approve_enrollment(self.manager, e)
        e.refresh_from_db()
        return e

    def pay(self, e, amount, day, method=None, **kw):
        return svc.collect_payment(self.operator, e, amount=amount, payment_method=method or self.cash, payment_date=day,
                                   location=kw.pop('location', self.delhi), confirm_duplicate=True, **kw)

    def pay_all(self, e, months=11):
        for inst in e.installments.order_by('installment_no')[:months]:
            self.pay(e, inst.scheduled_amount, inst.due_date)
        e.refresh_from_db()
        return e

    def matured(self, e, day=date(2026, 12, 5), approved=None, reason=''):
        calc = svc.mature(self.manager, e, as_of=day)
        svc.approve_benefit(self.manager, calc, approved_benefit=approved, reason=reason)
        e.refresh_from_db()
        return e, calc

    def jewellery(self, value, making=0, **kw):
        return [dict(description='Gold necklace', item_code='NK-22', metal='GOLD', category='Necklace', line_value=D(value),
                     making_charge=D(making), **kw)]

    def assert_consistent(self, e):
        e.refresh_from_db()
        self.assertEqual(reconcile(e), [])
        for doc in SchemePostingEntry.objects.filter(enrollment=e).values('document_no').annotate(d=Sum('debit'), c=Sum('credit')):
            self.assertEqual(doc['d'], doc['c'], doc['document_no'])


class GoldJewellery11Plus1Tests(SavingsFixture):
    def test_end_to_end_11_plus_1(self):
        scheme = self.make_scheme()
        e = self.enroll(scheme)
        # Enrolment, agreement, schedule (tests 1-5)
        self.assertEqual(e.account_no, 'GSP-000001')
        self.assertEqual(e.status, 'ACTIVE')
        self.assertEqual((e.planned_contribution, e.expected_benefit, e.expected_entitlement), (D('110000'), TEN_K, D('120000')))
        self.assertEqual(e.installments.count(), 11)
        self.assertEqual(list(e.installments.values_list('due_date', flat=True)[:2]), [date(2026, 1, 5), date(2026, 2, 5)])
        self.assertEqual(e.expected_maturity_date, date(2026, 12, 5))
        self.assertEqual(e.rules['benefit_type'], 'ONE_INSTALLMENT')
        # Collect 11 installments (tests 6, 8, 16)
        e = self.pay_all(e)
        self.assertEqual(e.status, 'COMPLETED')
        self.assertEqual((e.contribution_paid, e.benefit_approved, e.available_entitlement), (D('110000'), 0, D('110000')))
        self.assertFalse(SchemeInstallment.objects.filter(enrollment=e).exclude(status='PAID').exists())
        # Maturity waits for the maturity date
        with self.assertRaisesMessage(SchemeError, 'Maturity date is 2026-12-05'):
            svc.mature(self.manager, e, as_of=date(2026, 11, 20))
        e, calc = self.matured(e)
        # Benefit engine + approval (tests 17-18)
        self.assertEqual((calc.paid_contribution, calc.eligible_contribution, calc.calculated_benefit, calc.eligibility),
                         (D('110000'), D('110000'), TEN_K, 'ELIGIBLE'))
        self.assertEqual(e.status, 'BENEFIT_APPROVED')
        self.assertEqual((e.contribution_balance, e.benefit_balance, e.available_entitlement), (D('110000'), TEN_K, D('120000')))
        self.assertEqual(e.redemption_valid_until, date(2027, 12, 5))
        # Redeem against a ₹1,45,000 jewellery invoice (tests 20, 23)
        r = svc.redeem(self.operator, e, lines=self.jewellery(145000), invoice_reference='INV-9001', location=self.mumbai)
        self.assertEqual((r.amount, r.contribution_applied, r.benefit_applied, r.balance_payable), (D('120000'), D('110000'), TEN_K, D('25000')))
        e.refresh_from_db()
        self.assertEqual(e.status, 'REDEEMED')
        self.assertEqual(e.available_entitlement, 0)
        # Ledger keeps every balance separate (test 40)
        types = list(e.ledger.values_list('entry_type', flat=True))
        self.assertEqual(types.count('PAYMENT'), 11)
        self.assertIn('BENEFIT_ACCRUAL', types)
        self.assertIn('REDEMPTION', types)
        self.assert_consistent(e)
        self.assertEqual(e.ledger.aggregate(v=Sum('benefit'))['v'], TEN_K)
        self.assertEqual(e.ledger.aggregate(v=Sum('contribution'))['v'], D('110000'))

    def test_partial_and_multiple_redemption_and_reversal(self):
        e, _ = self.matured(self.pay_all(self.enroll(self.make_scheme())))
        svc.redeem(self.operator, e, lines=self.jewellery(50000))
        second = svc.redeem(self.operator, e, lines=self.jewellery(45000))
        e.refresh_from_db()
        self.assertEqual((e.status, e.available_entitlement, e.redemption_count), ('PARTIALLY_REDEEMED', D('25000'), 2))
        # Contribution is used first; benefit remains separately identifiable
        self.assertEqual((e.contribution_balance, e.benefit_balance), (D('15000'), TEN_K))
        # Purchase below entitlement keeps the balance (test 24) - then reverse the second redemption (test 25)
        with self.assertRaises(PermissionDenied):
            svc.reverse_redemption(self.operator, second, reason='Wrong item')
        svc.reverse_redemption(self.manager, second, reason='Wrong item')
        e.refresh_from_db()
        self.assertEqual((e.available_entitlement, e.redemption_count), (D('70000'), 1))
        second.refresh_from_db()
        self.assertEqual(second.status, 'REVERSED')
        with self.assertRaises(SchemeError):
            svc.reverse_redemption(self.manager, second, reason='Again')
        self.assert_consistent(e)

    def test_redemption_limits_and_duplicate_invoice(self):
        scheme = self.make_scheme(max_redemptions=1, remaining_balance_rule='FORFEIT')
        e, _ = self.matured(self.pay_all(self.enroll(scheme)))
        invoice = SalesInvoice.objects.create(invoice_no='SI-1', customer=self.rahul, total_amount=D('100000'))
        svc.redeem(self.operator, e, lines=self.jewellery(100000), sales_invoice=invoice)
        invoice.refresh_from_db()
        self.assertEqual((invoice.paid_amount, invoice.payment_status), (D('100000'), 'paid'))
        e.refresh_from_db()
        # Only one redemption allowed: the ₹20,000 remainder is forfeited per the scheme rule, not refunded
        self.assertEqual((e.status, e.available_entitlement, e.contribution_forfeited, e.benefit_forfeited), ('CLOSED', 0, D('10000'), TEN_K))
        other = self.matured(self.pay_all(self.enroll(self.make_scheme(code='GJ12'))))[0]
        with self.assertRaisesMessage(SchemeError, 'different customer'):
            invoice2 = SalesInvoice.objects.create(invoice_no='SI-2', customer=Customer.objects.create(name='X'), total_amount=D('5000'))
            svc.redeem(self.operator, other, lines=self.jewellery(5000), sales_invoice=invoice2)
        invoice3 = SalesInvoice.objects.create(invoice_no='SI-3', customer=self.rahul, total_amount=D('5000'))
        svc.redeem(self.operator, other, lines=self.jewellery(5000), sales_invoice=invoice3)
        with self.assertRaisesMessage(SchemeError, 'duplicate redemption'):
            svc.redeem(self.operator, other, lines=self.jewellery(5000), sales_invoice=invoice3)

    def test_product_eligibility_and_making_charge_benefit(self):
        scheme = self.make_scheme(template='MAKING', code='MK', product_rules=[{'rule_type': 'EXCLUDE', 'scope': 'TAG', 'value': 'coin'}])
        self.assertEqual(scheme.current_version().benefit_application, 'MAKING_CHARGE')
        e = self.enroll(scheme, installment_amount=5000)
        self.assertEqual(e.rules['product_rules'], [{'rule_type': 'EXCLUDE', 'scope': 'TAG', 'value': 'coin'}])
        e, _ = self.matured(self.pay_all(e))
        self.assertEqual(e.available_entitlement, D('60000'))
        lines = self.jewellery(70000, making=4000) + [dict(description='Gold coin', line_value=D('20000'), making_charge=D('0'), tags='coin')]
        r = svc.redeem(self.operator, e, lines=lines)
        # Benefit usable only against making charges (₹4,000); contribution against the eligible ₹70,000
        self.assertEqual((r.eligible_value, r.contribution_applied, r.benefit_applied), (D('70000'), D('55000'), D('4000')))
        self.assertEqual(r.lines.filter(eligible=False).count(), 1)
        self.assertEqual(r.balance_payable, D('31000'))


class InstallmentRuleTests(SavingsFixture):
    def test_missed_installment_must_be_paid(self):
        e = self.enroll(self.make_scheme())
        insts = list(e.installments.order_by('installment_no'))
        for i in (0, 1, 3):
            self.pay(e, TEN_K, insts[i].due_date)
        svc.run_daily(self.admin, date(2026, 4, 20))
        insts[2].refresh_from_db()
        self.assertEqual(insts[2].status, 'OVERDUE')
        e.refresh_from_db()
        self.assertEqual(e.status, 'OVERDUE')  # never auto-cancelled (test 12)
        result = svc.calculate_benefit(e, date(2026, 12, 5))
        self.assertEqual((result.eligibility, result.benefit, result.installments_missed), ('PENDING', 0, 8))

    def test_missed_installment_reduces_or_forfeits_benefit(self):
        reduce = self.make_scheme(code='RED', missed_installment_rule='REDUCE_BENEFIT', missed_reduction_percent=D('10'))
        e = self.enroll(reduce)
        for inst in e.installments.order_by('installment_no'):
            if inst.installment_no != 3:
                self.pay(e, TEN_K, inst.due_date)
        calc = svc.mature(self.manager, e, as_of=date(2026, 12, 5))
        self.assertEqual((calc.eligibility, calc.calculated_benefit, calc.installments_missed), ('REDUCED', D('9000'), 1))
        forfeit = self.make_scheme(code='FOR', missed_installment_rule='INELIGIBLE')
        e2 = self.enroll(forfeit, customer=Customer.objects.create(name='Priya', phone='9811000002'))
        for inst in e2.installments.order_by('installment_no'):
            if inst.installment_no != 3:
                self.pay(e2, TEN_K, inst.due_date)
        calc2 = svc.mature(self.manager, e2, as_of=date(2026, 12, 5))
        self.assertEqual((calc2.eligibility, calc2.calculated_benefit), ('NOT_ELIGIBLE', 0))

    def test_grace_period_late_payment_and_on_time_eligibility(self):
        scheme = self.make_scheme(late_payment_rule='FIXED', late_payment_value=D('200'), benefit_eligibility='ON_TIME')
        e = self.enroll(scheme)
        first = e.installments.get(installment_no=1)
        # Within grace (due 5th + 10 days): no charge (test 13)
        p1 = self.pay(e, TEN_K, date(2026, 1, 15))
        self.assertEqual(p1.penalty_amount, 0)
        first.refresh_from_db()
        self.assertTrue(first.paid_within_grace)
        # After grace: fixed ₹200 charge taken first, separate from contribution (test 14)
        p2 = self.pay(e, D('10200'), date(2026, 2, 20))
        self.assertEqual((p2.penalty_amount, p2.contribution_amount), (D('200'), TEN_K))
        second = e.installments.get(installment_no=2)
        self.assertEqual((second.status, second.paid_within_grace, second.eligible_amount), ('PAID', False, 0))
        e.refresh_from_db()
        self.assertEqual((e.contribution_paid, e.penalty_paid), (D('20000'), D('200')))
        for inst in e.installments.filter(installment_no__gt=2).order_by('installment_no'):
            self.pay(e, TEN_K, inst.due_date)
        calc = svc.mature(self.manager, e, as_of=date(2026, 12, 5))
        self.assertEqual((calc.installments_late, calc.eligibility, calc.calculated_benefit), (1, 'NOT_ELIGIBLE', 0))  # test 15

    def test_partial_final_payment(self):
        e = self.enroll(self.make_scheme())
        insts = list(e.installments.order_by('installment_no'))
        for inst in insts[:10]:
            self.pay(e, TEN_K, inst.due_date)
        self.pay(e, D('7000'), insts[10].due_date)  # test 10
        insts[10].refresh_from_db()
        self.assertEqual((insts[10].status, insts[10].outstanding), ('PARTIALLY_PAID', D('3000')))
        with self.assertRaisesMessage(SchemeError, 'must be paid first'):
            svc.mature(self.manager, e, as_of=date(2026, 12, 5))
        self.pay(e, D('3000'), date(2026, 12, 1))
        e, calc = self.matured(e)
        self.assertEqual(calc.calculated_benefit, TEN_K)

    def test_partial_payment_not_allowed(self):
        e = self.enroll(self.make_scheme(partial_payment_allowed=False, advance_mode='NOT_ALLOWED'))
        with self.assertRaisesMessage(SchemeError, 'Partial payment is not allowed'):
            self.pay(e, D('6000'), date(2026, 1, 5))
        with self.assertRaisesMessage(SchemeError, 'advance payment is not allowed'):
            self.pay(e, D('20000'), date(2026, 1, 5))

    def test_advance_payment_allocated_to_future_installments(self):
        e = self.enroll(self.make_scheme())
        p = self.pay(e, D('30000'), date(2026, 1, 5))  # test 9
        self.assertEqual(list(p.allocations.values_list('allocation_type', flat=True)), ['CURRENT', 'FUTURE', 'FUTURE'])
        self.assertEqual(e.installments.filter(status='PAID').count(), 3)

    def test_unallocated_advance_applied_by_daily_job(self):
        e = self.enroll(self.make_scheme(advance_mode='UNALLOCATED'))
        p = self.pay(e, D('25000'), date(2026, 1, 5))
        e.refresh_from_db()
        self.assertEqual((p.advance_amount, e.unallocated_advance, e.contribution_paid), (D('15000'), D('15000'), D('25000')))
        svc.run_daily(self.admin, date(2026, 2, 1))
        svc.run_daily(self.admin, date(2026, 2, 1))  # idempotent
        e.refresh_from_db()
        self.assertEqual(e.unallocated_advance, D('5000'))
        self.assertEqual(e.installments.get(installment_no=2).status, 'PAID')
        self.assert_consistent(e)

    def test_payment_reversal(self):
        e = self.enroll(self.make_scheme())
        p = self.pay(e, D('20000'), date(2026, 1, 5))
        with self.assertRaises(PermissionDenied):
            svc.reverse_payment(self.operator, p, reason='Cheque bounced')
        reversal = svc.reverse_payment(self.manager, p, reason='Cheque bounced')  # test 11
        p.refresh_from_db()
        e.refresh_from_db()
        self.assertEqual((p.status, reversal.amount, e.contribution_paid), ('REVERSED', D('-20000'), 0))
        self.assertFalse(e.installments.filter(status='PAID').exists())
        self.assertEqual(SchemePayment.objects.filter(enrollment=e).count(), 2)  # original + reversal - never deleted
        with self.assertRaises(ValidationError):
            p.delete()
        with self.assertRaises(SchemeError):
            svc.reverse_payment(self.manager, p, reason='Twice')
        self.assert_consistent(e)


class ControlTests(SavingsFixture):
    def test_scheme_version_is_frozen_and_snapshot_kept(self):
        scheme = self.make_scheme()
        version = scheme.current_version()
        version.benefit_value = D('5')
        with self.assertRaises(ValidationError):
            version.save()
        e = self.enroll(scheme)
        v2 = svc.new_version(self.admin, scheme, change_note='Percentage benefit')
        svc.save_version(self.admin, v2, benefit_type='PERCENT_ELIGIBLE', benefit_value=D('5'))
        svc.transition_version(self.admin, v2, 'submit')
        with self.assertRaisesMessage(SchemeError, 'Maker-checker'):
            svc.transition_version(self.admin, v2, 'approve')
        svc.transition_version(self.manager, v2, 'approve')
        svc.transition_version(self.manager, v2, 'activate')
        self.assertEqual(scheme.current_version(), v2)
        e.refresh_from_db()
        self.assertEqual((e.version.version_no, e.rules['benefit_type']), (1, 'ONE_INSTALLMENT'))

    def test_benefit_override_controls(self):
        e = self.pay_all(self.enroll(self.make_scheme(max_override_percent=D('10'))))
        calc = svc.mature(self.operator, e, as_of=date(2026, 12, 5))
        with self.assertRaises(PermissionDenied):  # test 31
            svc.approve_benefit(self.operator, calc, approved_benefit=D('9500'), reason='Goodwill')
        with self.assertRaisesMessage(SchemeError, 'reason is required'):
            svc.approve_benefit(self.manager, calc, approved_benefit=D('9500'))
        with self.assertRaisesMessage(SchemeError, 'above the allowed'):
            svc.approve_benefit(self.manager, calc, approved_benefit=D('5000'), reason='Too much')
        svc.approve_benefit(self.manager, calc, approved_benefit=D('9500'), reason='Late KYC')
        calc.refresh_from_db()
        e.refresh_from_db()
        self.assertEqual((calc.calculated_benefit, calc.approved_benefit, e.benefit_approved), (TEN_K, D('9500'), D('9500')))
        calc.calculated_benefit = D('1')
        with self.assertRaises(ValidationError):
            calc.save()
        # Benefit reversal: original kept, reversal posted, fresh calculation queued
        fresh = svc.reverse_benefit(self.manager, BenefitCalculation.objects.get(pk=calc.pk), reason='Recalculate')
        e.refresh_from_db()
        self.assertEqual((e.status, e.benefit_approved, fresh.status), ('MATURED', 0, 'CALCULATED'))
        self.assertTrue(e.ledger.filter(entry_type='BENEFIT_REVERSAL').exists())

    def test_duplicate_payment_and_idempotency(self):
        e = self.enroll(self.make_scheme())
        svc.collect_payment(self.operator, e, amount=TEN_K, payment_method=self.upi, reference_no='UPI-777', payment_date=date(2026, 1, 5))
        with self.assertRaisesMessage(SchemeError, 'duplicate payment'):  # test 33
            svc.collect_payment(self.operator, e, amount=TEN_K, payment_method=self.upi, reference_no='UPI-777', payment_date=date(2026, 1, 6))
        with self.assertRaises(ConfirmationRequired):
            svc.collect_payment(self.operator, e, amount=TEN_K, payment_method=self.cash, payment_date=date(2026, 1, 5))
        first = svc.collect_payment(self.operator, e, amount=TEN_K, payment_method=self.cash, payment_date=date(2026, 2, 5), idempotency_key='k-1')
        again = svc.collect_payment(self.operator, e, amount=TEN_K, payment_method=self.cash, payment_date=date(2026, 2, 5), idempotency_key='k-1')
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(SchemePayment.objects.filter(enrollment=e).count(), 2)

    def test_duplicate_customer_warning_and_limits(self):
        scheme = self.make_scheme()
        self.enroll(scheme)
        with self.assertRaisesMessage(ConfirmationRequired, 'already has 1 active scheme'):
            svc.enroll(self.operator, scheme=scheme, customer=self.rahul)
        setup = get_setup(self.tenant)
        setup.allow_multiple_active_schemes = False
        setup.save()
        with self.assertRaisesMessage(SchemeError, 'multiple schemes are not allowed'):
            svc.enroll(self.operator, scheme=scheme, customer=self.rahul, confirm_duplicate=True)

    def test_enrollment_maker_checker_and_kyc(self):
        scheme = self.make_scheme(kyc_required=True)
        with self.assertRaisesMessage(SchemeError, 'KYC is required'):
            svc.enroll(self.operator, scheme=scheme, customer=Customer.objects.create(name='No PAN'))
        e = svc.enroll(self.manager, scheme=scheme, customer=self.rahul)
        self.assertEqual(e.kyc_reference, '••••••234F')
        svc.accept_agreement(self.manager, e, method='OTP')
        with self.assertRaisesMessage(SchemeError, 'Maker-checker'):
            svc.approve_enrollment(self.manager, e)
        with self.assertRaises(PermissionDenied):
            svc.approve_enrollment(self.operator, e)

    def test_tenant_isolation(self):
        e = self.enroll(self.make_scheme())
        stranger = User.objects.create_user('other@shop.test', password='pw-12345678')
        other = Actor(create_tenant_for_user(stranger, 'Other Jeweller'), stranger)
        with self.assertRaises(SchemeEnrollment.DoesNotExist):  # test 35
            svc.collect_payment(other, e, amount=TEN_K, payment_method=self.cash)
        self.assertFalse(svc.find_accounts(other, e.account_no))
        self.assertEqual(svc.find_accounts(self.admin, e.account_no)[0], e)


class CancellationRefundTests(SavingsFixture):
    def test_cancel_refund_and_close(self):
        scheme = self.make_scheme(cancellation_charge_type='PERCENT', cancellation_charge_value=D('2'))
        e = self.enroll(scheme)
        for inst in e.installments.order_by('installment_no')[:5]:
            self.pay(e, TEN_K, inst.due_date)
        cancellation = svc.request_cancellation(self.operator, e, reason='Customer relocating')  # test 26
        self.assertEqual((cancellation.cancellation_charge, cancellation.refundable_amount), (D('1000'), D('49000')))  # test 27
        with self.assertRaises(PermissionDenied):
            svc.decide_cancellation(self.operator, cancellation)
        svc.decide_cancellation(self.manager, cancellation)
        e.refresh_from_db()
        self.assertEqual(e.status, 'CANCELLED')
        self.assertTrue(e.installments.filter(status='CANCELLED').exists())
        refund = e.refunds.get()
        with self.assertRaises(PermissionDenied):  # test 32
            svc.pay_refund(self.operator, refund, payment_method=self.cash)
        with self.assertRaisesMessage(SchemeError, 'must be approved'):
            svc.pay_refund(self.finance, refund, payment_method=self.cash)
        svc.decide_refund(self.manager, refund)  # test 28
        svc.pay_refund(self.finance, refund, payment_method=self.cash)  # test 29
        e.refresh_from_db()
        self.assertEqual((e.status, e.contribution_refunded, e.contribution_forfeited, e.available_entitlement), ('REFUNDED', D('49000'), D('1000'), 0))
        self.assert_consistent(e)

    def test_benefit_is_never_refunded_as_cash(self):
        e, _ = self.matured(self.pay_all(self.enroll(self.make_scheme())))
        cancellation = svc.request_cancellation(self.operator, e, reason='Changed mind')
        self.assertEqual((cancellation.refundable_amount, cancellation.benefit_forfeited), (D('110000'), TEN_K))
        svc.decide_cancellation(self.manager, cancellation)
        refund = e.refunds.get()
        self.assertEqual((refund.contribution_refund, refund.benefit_reversal, refund.refund_amount), (D('110000'), TEN_K, D('110000')))
        svc.decide_refund(self.finance, refund)
        svc.pay_refund(self.finance, refund, payment_method=self.cash)
        e.refresh_from_db()
        self.assertEqual((e.contribution_refunded, e.benefit_forfeited, e.available_entitlement), (D('110000'), TEN_K, 0))

    def test_adjustment_journal_needs_a_checker(self):
        e = self.enroll(self.make_scheme())
        adj = svc.create_adjustment(self.manager, e, adjustment_type='WAIVER', reason='Hospitalised', reference='MGR-1',
                                    installment=e.installments.get(installment_no=1))
        with self.assertRaisesMessage(SchemeError, 'Maker-checker'):
            svc.decide_adjustment(self.manager, adj)
        svc.decide_adjustment(self.finance, adj)
        self.assertEqual(e.installments.get(installment_no=1).status, 'WAIVED')


class AccountingTests(SavingsFixture):
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_code='GLD', company_name='Goldio Jewellers', status='active')
        acct = lambda code, name, kind: GLAccount.objects.create(company=self.company, account_code=code, account_name=name, account_type=kind)
        setup = get_setup(self.tenant)
        setup.company, setup.gl_posting_enabled = self.company, True
        setup.cash_account = acct('1100', 'Cash', 'asset')
        setup.contribution_liability_account = acct('2310', 'Scheme contribution liability', 'liability')
        setup.benefit_liability_account = acct('2320', 'Scheme benefit liability', 'liability')
        setup.benefit_expense_account = acct('5410', 'Scheme benefit cost', 'expense')
        setup.redemption_settlement_account = acct('1200', 'Sales settlement clearing', 'asset')
        setup.forfeiture_income_account = acct('4910', 'Scheme forfeiture income', 'revenue')
        setup.penalty_income_account = acct('4920', 'Scheme late fees', 'revenue')
        setup.save()
        self.accounts = setup

    def balance(self, account):
        from erp.models import FinanceVoucherLine
        t = FinanceVoucherLine.objects.filter(account=account, voucher__status='posted').aggregate(d=Sum('debit_amount'), c=Sum('credit_amount'))
        return (t['d'] or 0) - (t['c'] or 0)

    def test_postings_for_payment_benefit_redemption_refund(self):
        e = self.pay_all(self.enroll(self.make_scheme()))
        s = self.accounts
        self.assertEqual(self.balance(s.cash_account), D('110000'))  # test 36
        self.assertEqual(self.balance(s.contribution_liability_account), D('-110000'))
        e, _ = self.matured(e)
        self.assertEqual(self.balance(s.benefit_expense_account), TEN_K)  # test 37
        self.assertEqual(self.balance(s.benefit_liability_account), -TEN_K)
        svc.redeem(self.operator, e, lines=self.jewellery(100000))  # test 38
        self.assertEqual(self.balance(s.contribution_liability_account), -TEN_K)
        self.assertEqual(self.balance(s.benefit_liability_account), -TEN_K)
        self.assertEqual(self.balance(s.redemption_settlement_account), D('-100000'))
        e.refresh_from_db()
        svc.close_enrollment(self.manager, e, disposition='REFUND', reason='Customer wants the rest back')
        refund = e.refunds.get()
        svc.decide_refund(self.manager, refund)
        svc.pay_refund(self.finance, refund, payment_method=self.cash)  # test 39
        self.assertEqual(self.balance(s.contribution_liability_account), 0)
        self.assertEqual(self.balance(s.benefit_liability_account), 0)
        self.assertEqual(self.balance(s.benefit_expense_account), 0)  # unused benefit reversed, never paid out
        self.assertEqual(self.balance(s.cash_account), D('100000'))
        self.assertTrue(FinanceVoucher.objects.filter(voucher_type__code='scheme_journal').exists())
        self.assert_consistent(e)
        self.assertFalse(SchemePostingEntry.objects.filter(enrollment=e, status='RECORDED').exists())

    def test_missing_accounts_block_posting(self):
        setup = get_setup(self.tenant)
        setup.contribution_liability_account = None
        setup.save()
        e = self.enroll(self.make_scheme())
        with self.assertRaisesMessage(SchemeError, 'missing G/L accounts'):
            self.pay(e, TEN_K, date(2026, 1, 5))
        self.assertFalse(SchemeLedgerEntry.objects.filter(enrollment=e, entry_type='PAYMENT').exists())


class BenefitEngineTests(SavingsFixture):
    def test_tiered_and_percentage_benefits(self):
        tiered = self.make_scheme(template='TIERED', code='TIER')
        e = self.pay_all(self.enroll(tiered, installment_amount=10000))
        self.assertEqual(svc.calculate_benefit(e, date(2026, 12, 5)).benefit, D('11000.00'))  # ₹1,10,000 -> 10% tier
        e2 = self.pay_all(self.enroll(tiered, customer=Customer.objects.create(name='Asha'), installment_amount=5000))
        self.assertEqual(svc.calculate_benefit(e2, date(2026, 12, 5)).benefit, D('3850.00'))  # ₹55,000 -> 7% tier
        pct = self.make_scheme(template='6+1', code='SIX')
        e3 = self.pay_all(self.enroll(pct, customer=Customer.objects.create(name='Neha'), installment_amount=5000), months=6)
        self.assertEqual(e3.installments.count(), 6)
        result = svc.calculate_benefit(e3, date(2026, 7, 5))
        self.assertEqual((result.eligible_contribution, result.benefit), (D('30000.00'), D('2400.00')))  # 8% of eligible
        self.assertEqual(e3.expected_maturity_date, date(2026, 7, 5))
