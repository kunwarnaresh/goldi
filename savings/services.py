"""Jewellery Savings workflows: scheme setup and versioning (maker-checker), enrolment and agreement, collection,
maturity and benefit approval, redemption, cancellation, refund, adjustment, nominees and the scheduled jobs.

Every money movement is delegated to ``savings.engine.SchemePostingEngine`` and every benefit figure to
``savings.benefit``; this module only validates, orchestrates and audits.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

from inventory.models import Location
from inventory.services import Actor
from inventory.tenancy import require_location

from . import benefit as bf
from .benefit import ZERO, d, money
from .engine import SchemePostingEngine, add_months, queue_notification, refresh_enrollment, staff_name
from .models import (
    BenefitCalculation, JewellerySavingsScheme, SchemeAdjustment, SchemeCancellation, SchemeEnrollment, SchemeInstallment,
    SchemeNominee, SchemePayment, SchemeProductRule, SchemeRedemption, SchemeRefund, SchemeVersion,
)
from .security import ConfirmationRequired, SchemeError, audit, get_setup, mask, maker_checker, next_number, require


def _get(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=getattr(pk, 'pk', pk)).first()
    if obj is None:
        raise SchemeError(f'{model._meta.verbose_name.title()} not found.')
    return obj


def _reload(enrollment):
    return SchemeEnrollment.objects.get(pk=enrollment.pk)


def _own(actor, obj):
    if obj is not None and obj.tenant_id != actor.tenant.id:
        raise SchemeError('Not found.')
    return obj


def _location(actor, location, action='view'):
    if location is None:
        return None
    _own(actor, location)
    require_location(actor.tenant, actor.user, location, action)
    return location


# ---------------------------------------------------------------------------
# Scheme master and versions (maker-checker)
# ---------------------------------------------------------------------------

def _apply_rules(version, rules):
    for key, value in rules.items():
        if key not in SchemeVersion.RULE_FIELDS and key not in ('change_note', 'effective_from'):
            raise SchemeError(f'Unknown scheme rule: {key}')
        setattr(version, key, value)
    try:
        version.full_clean(exclude=['scheme', 'tenant', 'company'])
    except ValidationError as exc:
        raise SchemeError('; '.join(f'{k}: {", ".join(v)}' for k, v in exc.message_dict.items()))


@transaction.atomic
def create_scheme(actor, *, code, name, description='', account_prefix='GSP', locations=(), collect_at_any_location=True,
                  redeem_at_any_location=True, active_from=None, active_to=None, product_rules=(), **rules):
    require(actor, 'configure')
    code = (code or '').strip().upper()
    if not code or not (name or '').strip():
        raise SchemeError('Scheme code and name are required.')
    if JewellerySavingsScheme.objects.filter(tenant=actor.tenant, code=code).exists():
        raise SchemeError(f'Scheme {code} already exists.')
    setup = get_setup(actor.tenant)
    scheme = JewellerySavingsScheme.objects.create(
        tenant=actor.tenant, company=setup.company, code=code, name=name.strip(), description=description,
        account_prefix=(account_prefix or 'GSP').strip().upper()[:10], collect_at_any_location=collect_at_any_location,
        redeem_at_any_location=redeem_at_any_location, active_from=active_from or dj_timezone.localdate(), active_to=active_to,
        created_by=actor.user,
    )
    if locations:
        scheme.locations.set([_own(actor, loc) for loc in locations])
    version = SchemeVersion(tenant=actor.tenant, company=setup.company, scheme=scheme, version_no=1, created_by=actor.user)
    _apply_rules(version, rules)
    version.save()
    _set_product_rules(actor, version, product_rules)
    audit(actor, 'create', 'SCHEME', code, new={'name': name, 'version': 1, **{k: rules[k] for k in list(rules)[:12]}})
    return scheme, version


def _draft(version):
    if not version.editable:
        raise SchemeError(f'{version} is {version.get_status_display().lower()} and frozen. Create a new version to change its rules.')


@transaction.atomic
def update_version(actor, version, **rules):
    require(actor, 'configure')
    version = SchemeVersion.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    _draft(version)
    old = {k: getattr(version, k) for k in rules if hasattr(version, k)}
    _apply_rules(version, rules)
    version.status, version.updated_by = 'DRAFT', actor.user
    version.save()
    audit(actor, 'update', 'SCHEME_VERSION', str(version), old=old, new=rules)
    return version


def _set_product_rules(actor, version, rules):
    version.product_rules.all().delete()
    for rule in rules or ():
        rule = dict(rule)
        if rule.get('rule_type', 'INCLUDE') not in ('INCLUDE', 'EXCLUDE') or rule.get('scope', 'ALL') not in dict(SchemeProductRule.SCOPES):
            raise SchemeError('Invalid product rule.')
        if rule.get('scope', 'ALL') != 'ALL' and not (rule.get('value') or '').strip():
            raise SchemeError('Enter a value for every product rule except "All jewellery".')
        SchemeProductRule.objects.create(tenant=actor.tenant, company=version.company, version=version,
                                         rule_type=rule.get('rule_type', 'INCLUDE'), scope=rule.get('scope', 'ALL'),
                                         value=(rule.get('value') or '').strip(), created_by=actor.user)


@transaction.atomic
def set_product_rules(actor, version, rules):
    require(actor, 'configure')
    version = SchemeVersion.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    _draft(version)
    _set_product_rules(actor, version, rules)
    audit(actor, 'product_rules', 'SCHEME_VERSION', str(version), new={'rules': rules})


@transaction.atomic
def new_version(actor, scheme, *, change_note='', effective_from=None):
    require(actor, 'configure')
    scheme = JewellerySavingsScheme.objects.select_for_update().get(pk=scheme.pk, tenant=actor.tenant)
    if scheme.versions.filter(status__in=('DRAFT', 'UNDER_REVIEW')).exists():
        raise SchemeError('A draft version already exists; finish or reject it first.')
    source = scheme.versions.order_by('-version_no').first()
    version = SchemeVersion(tenant=actor.tenant, company=scheme.company, scheme=scheme, version_no=source.version_no + 1,
                            change_note=change_note[:250], effective_from=effective_from or dj_timezone.localdate(), created_by=actor.user)
    for name in SchemeVersion.RULE_FIELDS:
        setattr(version, name, getattr(source, name))
    version.save()
    _set_product_rules(actor, version, [r.snapshot() for r in source.product_rules.all()])
    audit(actor, 'new_version', 'SCHEME_VERSION', str(version), new={'copied_from': str(source), 'note': change_note})
    return version


@transaction.atomic
def transition_version(actor, version, action, *, note=''):
    """submit (maker) -> approve (checker, activates and closes the previous version for new enrolments) / reject."""
    version = SchemeVersion.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    setup = get_setup(actor.tenant)
    old = version.status
    if action == 'submit':
        require(actor, 'configure')
        if version.status != 'DRAFT':
            raise SchemeError('Only a draft version can be submitted.')
        version.status, version.submitted_by = 'UNDER_REVIEW', actor.user
        version.save(update_fields=['status', 'submitted_by', 'updated_at'])
    elif action == 'approve':
        require(actor, 'approve_scheme')
        if version.status != 'UNDER_REVIEW':
            raise SchemeError('Submit the version for review before approving it.')
        maker_checker(actor, version.submitted_by or version.created_by, setup, 'scheme version')
        version.scheme.versions.filter(status='ACTIVE').exclude(pk=version.pk).update(status='CLOSED', updated_at=dj_timezone.now())
        version.status, version.approved_by, version.approved_at = 'ACTIVE', actor.user, dj_timezone.now()
        version.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
        scheme = version.scheme
        if scheme.status in ('DRAFT', 'UNDER_REVIEW', 'APPROVED'):
            scheme.status = 'ACTIVE'
            scheme.save(update_fields=['status', 'updated_at'])
    elif action == 'reject':
        require(actor, 'approve_scheme')
        if version.status != 'UNDER_REVIEW':
            raise SchemeError('Only a version under review can be rejected.')
        version.status = 'DRAFT'
        version.save(update_fields=['status', 'updated_at'])
    else:
        raise SchemeError('Unknown action.')
    audit(actor, action, 'SCHEME_VERSION', str(version), old={'status': old}, new={'status': version.status}, reason=note)
    return version


@transaction.atomic
def set_scheme_status(actor, scheme, status, *, reason=''):
    require(actor, 'configure')
    scheme = JewellerySavingsScheme.objects.select_for_update().get(pk=scheme.pk, tenant=actor.tenant)
    allowed = {'ACTIVE': ('SUSPENDED',), 'SUSPENDED': ('ACTIVE',), 'EXPIRED': ('ACTIVE', 'SUSPENDED'),
               'CLOSED': ('ACTIVE', 'SUSPENDED', 'EXPIRED'), 'ARCHIVED': ('CLOSED', 'DRAFT')}
    if scheme.status not in allowed.get(status, ()):
        raise SchemeError(f'A {scheme.get_status_display().lower()} scheme cannot become {status.lower()}.')
    if status == 'ACTIVE' and not scheme.current_version():
        raise SchemeError('Approve a scheme version first.')
    old = scheme.status
    scheme.status = status
    scheme.save(update_fields=['status', 'updated_at'])
    audit(actor, 'status', 'SCHEME', scheme.code, old={'status': old}, new={'status': status}, reason=reason)
    return scheme


ELEVEN_PLUS_ONE = dict(
    installment_mode='FIXED', installment_amount=Decimal('10000'), min_installment=Decimal('1000'), installment_step=Decimal('500'),
    number_of_installments=11, due_day=5, maturity_months_after_last=1, grace_days=10, partial_payment_allowed=True,
    advance_mode='ALLOCATE_FUTURE', missed_installment_rule='MUST_PAY', benefit_type='ONE_INSTALLMENT', benefit_eligibility='ALL_PAID',
    benefit_application='MONETARY', redemption_window_days=365, partial_redemption_allowed=True, remaining_balance_rule='KEEP',
    cancellation_benefit_rule='FORFEIT', refund_allowed=True,
    tax_treatment='To be confirmed with tax advisor', advance_treatment='Customer advance held as scheme liability',
    redemption_treatment='Settlement of the sales invoice; invoice GST from the normal tax engine',
    benefit_treatment_note='Jeweller promotional benefit - not customer money',
    terms=('1. Pay the monthly installment on or before the due date; a grace period applies as stated.\n'
           '2. After all installments are paid the jeweller adds the stated benefit on maturity.\n'
           '3. The purchase entitlement can be used only for eligible jewellery within the redemption window.\n'
           '4. The jeweller benefit is not refundable in cash; on cancellation only the customer contribution is refunded.\n'
           '5. Normal jewellery pricing, making charges and GST apply at the time of purchase.'),
)


def create_template_scheme(actor, *, code='GSP11', name='Gold Jewellery 11+1 Plan', **overrides):
    """The configurable 11+1 template - a starting point, not a hard-coded product."""
    return create_scheme(actor, code=code, name=name, account_prefix=overrides.pop('account_prefix', 'GSP'),
                         description='Pay 11 monthly installments; the jeweller adds one installment as benefit.',
                         **{**ELEVEN_PLUS_ONE, **overrides})


# ---------------------------------------------------------------------------
# Enrolment, agreement, approval, nominee
# ---------------------------------------------------------------------------

def first_due_date(start, due_day):
    due = start.replace(day=due_day)
    return due if due >= start else add_months(due, 1)


def duplicate_warnings(actor, customer, exclude=None):
    """Existing open scheme accounts of the customer, and other customers sharing the mobile / email / PAN."""
    open_accounts = SchemeEnrollment.objects.filter(tenant=actor.tenant, customer=customer, status__in=SchemeEnrollment.OPEN + ('DRAFT',))
    if exclude is not None:
        open_accounts = open_accounts.exclude(pk=exclude.pk)
    match = Q()
    if customer.phone:
        match |= Q(mobile=customer.phone)
    if customer.email:
        match |= Q(email__iexact=customer.email)
    if customer.pan:
        match |= Q(customer__pan__iexact=customer.pan)
    others = (SchemeEnrollment.objects.filter(tenant=actor.tenant).filter(match).exclude(customer=customer)
              .values_list('customer_name', 'account_no')) if match else []
    warnings = [f'Customer already has scheme {e.account_no} ({e.get_status_display().lower()}).' for e in open_accounts]
    warnings += [f'{name} ({account}) has the same mobile / email / PAN.' for name, account in others]
    return list(open_accounts), warnings


def validate_installment(rules, amount):
    amount = money(amount)
    if amount <= 0:
        raise SchemeError('Enter a monthly installment greater than zero.')
    minimum, maximum, step = d(rules.get('min_installment')), d(rules.get('max_installment')), d(rules.get('installment_step'))
    if minimum and amount < minimum:
        raise SchemeError(f'Minimum monthly installment is ₹{money(minimum)}.')
    if maximum and amount > maximum:
        raise SchemeError(f'Maximum monthly installment is ₹{money(maximum)}.')
    if step and amount % step:
        raise SchemeError(f'Installment must be a multiple of ₹{money(step)}.')
    return amount


@transaction.atomic
def enroll(actor, *, scheme, customer, installment_amount=None, start_date=None, branch=None, sales_staff=None, relationship_manager=None,
           nominee=None, confirm_duplicate=False):
    require(actor, 'enroll')
    scheme = _own(actor, scheme)
    setup = get_setup(actor.tenant)
    today = dj_timezone.localdate()
    if scheme.status != 'ACTIVE':
        raise SchemeError(f'{scheme.code} is {scheme.get_status_display().lower()}; new enrolments are not accepted.')
    if scheme.calculation_model != 'MONETARY':
        raise SchemeError('Gold-weight accumulation schemes are not available yet.')
    if today < scheme.active_from or (scheme.active_to and today > scheme.active_to):
        raise SchemeError(f'{scheme.code} accepts enrolments from {scheme.active_from} to {scheme.active_to or "open"}.')
    version = scheme.current_version()
    if version is None:
        raise SchemeError(f'{scheme.code} has no approved version.')
    if customer is None or not getattr(customer, 'is_active', True):
        raise SchemeError('Select an active customer.')
    rules = version.snapshot()
    amount = validate_installment(rules, installment_amount or version.installment_amount)
    if rules['installment_mode'] == 'FIXED' and version.installment_amount and not installment_amount:
        amount = money(version.installment_amount)
    if rules['kyc_required'] and not (customer.pan or '').strip():
        raise SchemeError('KYC required: add the customer\'s PAN in the customer master before enrolling.')
    if rules['nominee_required'] and not (nominee or {}).get('name'):
        raise SchemeError('This scheme requires a nominee.')
    if branch is not None:
        _location(actor, branch)
        allowed = set(scheme.locations.values_list('pk', flat=True))
        if allowed and branch.pk not in allowed:
            raise SchemeError(f'{branch.code} does not offer {scheme.code}.')

    existing, warnings = duplicate_warnings(actor, customer)
    active = [e for e in existing if e.status != 'DRAFT']
    if active and not setup.allow_multiple_active_schemes:
        raise SchemeError(f'{customer.name} already has an active scheme ({active[0].account_no}); multiple schemes are not allowed.')
    if setup.max_active_schemes_per_customer and len(active) >= setup.max_active_schemes_per_customer:
        raise SchemeError(f'{customer.name} already has {len(active)} active scheme(s) - the limit is {setup.max_active_schemes_per_customer}.')
    monthly = sum((e.installment_amount for e in active if e.status in SchemeEnrollment.CONTRIBUTING), ZERO) + amount
    if setup.max_monthly_contribution and monthly > setup.max_monthly_contribution:
        raise SchemeError(f'Total monthly contribution ₹{monthly} would exceed the limit of ₹{setup.max_monthly_contribution}.')
    if warnings and not confirm_duplicate:
        raise ConfirmationRequired(' '.join(warnings))

    count = int(rules['number_of_installments'])
    planned = money(amount * count)
    expected = bf.expected(rules, amount, count)
    cap = d(rules['max_entitlement'])
    if cap and planned > cap:
        raise SchemeError(f'Planned contribution ₹{planned} exceeds the maximum entitlement ₹{money(cap)}.')
    start = start_date or today
    first = first_due_date(start, int(rules['due_day']))
    last = add_months(first, count - 1)
    maturity = add_months(last, int(rules['maturity_months_after_last']))
    enrollment = SchemeEnrollment.objects.create(
        tenant=actor.tenant, company=setup.company, account_no=next_number(actor.tenant, f'SCHEME_ACCOUNT:{scheme.account_prefix}',
                                                                           scheme.account_prefix),
        scheme=scheme, version=version, rules=rules, customer=customer, customer_name=customer.name[:200], mobile=customer.phone or '',
        email=customer.email or '', address=customer.address or '', kyc_reference=mask(customer.pan), branch=branch,
        sales_staff=sales_staff, sales_staff_name=staff_name(sales_staff), relationship_manager=relationship_manager,
        enrollment_date=today, start_date=start, last_due_date=last, expected_maturity_date=maturity, installment_amount=amount,
        number_of_installments=count, planned_contribution=planned, expected_benefit=expected, expected_entitlement=planned + expected,
        agreement_version=f'{scheme.code}-V{version.version_no}', created_by=actor.user,
    )
    variable = rules['installment_mode'] == 'VARIABLE'
    maximum = d(rules['max_installment'])
    for number in range(1, count + 1):
        SchemeInstallment.objects.create(
            tenant=actor.tenant, company=setup.company, enrollment=enrollment, installment_no=number, due_date=add_months(first, number - 1),
            scheduled_amount=amount, cap_amount=money(maximum) if variable and maximum > amount else amount, created_by=actor.user,
        )
    if nominee and nominee.get('name'):
        _add_nominee(actor, enrollment, nominee)
    audit(actor, 'enroll', 'SCHEME_ENROLLMENT', enrollment.account_no, enrollment=enrollment,
          new={'scheme': str(version), 'installment': amount, 'installments': count, 'maturity': maturity, 'duplicates_confirmed': bool(warnings)})
    return enrollment


@transaction.atomic
def accept_agreement(actor, enrollment, *, method, signature_reference=''):
    require(actor, 'enroll')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if e.status != 'DRAFT':
        raise SchemeError('The agreement has already been accepted.')
    if method not in dict(SchemeEnrollment.ACCEPTANCE_METHODS):
        raise SchemeError('Select how the customer accepted the agreement.')
    if method == 'PHYSICAL' and not (signature_reference or '').strip():
        raise SchemeError('Enter the signed agreement reference.')
    e.acceptance_method, e.signature_reference = method, (signature_reference or '')[:120]
    e.agreement_accepted_at, e.agreement_staff = dj_timezone.now(), actor.user
    e.status = 'PENDING_APPROVAL'
    e.save()
    audit(actor, 'accept_agreement', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e,
          new={'agreement': e.agreement_version, 'method': method, 'reference': signature_reference})
    if not get_setup(actor.tenant).require_enrollment_approval:
        _activate(actor, e)
    return _reload(e)


def _activate(actor, e):
    e.status, e.approved_by, e.approved_at = 'ACTIVE', actor.user, dj_timezone.now()
    e.save()
    engine = SchemePostingEngine(actor, e, posting_date=dj_timezone.localdate(), location=e.branch)
    engine.ledger('ENROLLMENT', 'enrollment', e.account_no,
                  f'Enrolled in {e.scheme.name} ({e.agreement_version}): ₹{e.installment_amount} × {e.number_of_installments}')
    refresh_enrollment(engine.enrollment)
    scheme_label = get_setup(actor.tenant).display_name
    queue_notification(e, 'ENROLLMENT', f'Welcome to {e.scheme.name} ({scheme_label}). Your Scheme Account is {e.account_no}. '
                                        f'Your monthly installment is ₹{e.installment_amount}.', f'enrol:{e.account_no}')


@transaction.atomic
def approve_enrollment(actor, enrollment):
    require(actor, 'approve_enrollment')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if e.status != 'PENDING_APPROVAL':
        raise SchemeError('Only an enrolment with an accepted agreement awaiting approval can be approved.')
    maker_checker(actor, e.created_by, get_setup(actor.tenant), 'enrolment')
    _activate(actor, e)
    audit(actor, 'approve', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e)
    return _reload(e)


def _add_nominee(actor, e, data):
    percent = d(data.get('percentage') or 100)
    if percent <= 0 or percent > 100:
        raise SchemeError('Nominee share must be between 1 and 100%.')
    total = (e.nominees.filter(active=True).aggregate(p=Sum('percentage'))['p'] or ZERO) + percent
    if total > 100:
        raise SchemeError(f'Nominee shares would total {total}%.')
    return SchemeNominee.objects.create(
        tenant=actor.tenant, company=e.company, enrollment=e, name=data['name'].strip()[:150], relationship=(data.get('relationship') or '')[:60],
        date_of_birth=data.get('date_of_birth') or None, mobile=(data.get('mobile') or '')[:15], address=data.get('address') or '',
        id_reference=mask(data.get('id_reference')), percentage=percent, effective_from=data.get('effective_from') or dj_timezone.localdate(),
        created_by=actor.user,
    )


@transaction.atomic
def save_nominee(actor, enrollment, data, *, replace=None):
    """Add a nominee, or replace one: the old row is deactivated (kept for audit) and a new one created."""
    require(actor, 'enroll')
    e = _own(actor, enrollment)
    if not (data.get('name') or '').strip():
        raise SchemeError('Nominee name is required.')
    old = {}
    if replace is not None:
        replace = e.nominees.get(pk=getattr(replace, 'pk', replace))
        old = {'name': replace.name, 'relationship': replace.relationship, 'percentage': replace.percentage}
        replace.active = False
        replace.save(update_fields=['active', 'updated_at'])
    nominee = _add_nominee(actor, e, data)
    audit(actor, 'nominee', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e, old=old,
          new={'name': nominee.name, 'relationship': nominee.relationship, 'percentage': nominee.percentage})
    return nominee


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

@transaction.atomic
def collect(actor, enrollment, *, amount, payment_method=None, method_type='', bank_account=None, reference_no='', location=None,
            payment_date=None, idempotency_key='', remarks='', confirm_duplicate=False):
    require(actor, 'collect')
    e = _own(actor, enrollment)
    _location(actor, location)
    engine = SchemePostingEngine(actor, e, posting_date=payment_date, location=location)
    engine.apply_advance()
    return engine.collect(amount=amount, payment_method=payment_method, method_type=method_type, bank_account=bank_account,
                          reference_no=reference_no.strip(), idempotency_key=idempotency_key, remarks=remarks,
                          confirm_duplicate=confirm_duplicate)


@transaction.atomic
def reverse_payment(actor, payment, *, reason):
    require(actor, 'reverse_payment')
    payment = _own(actor, payment)
    return SchemePostingEngine(actor, payment.enrollment).reverse_payment(payment, reason=reason)


# ---------------------------------------------------------------------------
# Maturity and benefit
# ---------------------------------------------------------------------------

def calculate_benefit(enrollment, as_of=None):
    """Preview only - nothing is stored."""
    return bf.calculate(enrollment, list(enrollment.installments.all()), as_of or dj_timezone.localdate())


def _store_calculation(actor, e, as_of):
    result = calculate_benefit(e, as_of)
    e.benefit_calculations.filter(status='CALCULATED').update(status='SUPERSEDED', updated_at=dj_timezone.now())
    calc = BenefitCalculation.objects.create(
        tenant=e.tenant, company=e.company, calculation_no=next_number(e.tenant, 'BENEFIT_CALC'), enrollment=e, as_of=as_of,
        scheduled_contribution=money(result.scheduled_contribution), paid_contribution=result.paid_contribution,
        eligible_contribution=result.eligible_contribution, outstanding_contribution=money(result.outstanding_contribution),
        installments_paid=result.installments_paid, installments_missed=result.installments_missed,
        installments_late=result.installments_late, installments_partial=result.installments_partial, gross_benefit=result.gross_benefit,
        calculated_benefit=result.benefit, eligibility=result.eligibility, explanation=result.explanation, created_by=actor.user,
    )
    e.benefit_eligibility = result.eligibility
    return calc


def _mature(actor, enrollment, as_of):
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if e.status not in SchemeEnrollment.CONTRIBUTING:
        raise SchemeError(f'{e.account_no} is {e.get_status_display().lower()}; only a contributing scheme can mature.')
    engine = SchemePostingEngine(actor, e, posting_date=as_of)
    engine.apply_advance()
    installments = refresh_enrollment(engine.enrollment, as_of)
    e = _reload(e)
    rules = e.rules
    unsettled = [i for i in installments if i.status not in SchemeInstallment.SETTLED]
    if as_of < e.expected_maturity_date and not (rules.get('allow_early_maturity') and not unsettled):
        raise SchemeError(f'{e.account_no} matures on {e.expected_maturity_date}.')
    rule = rules.get('missed_installment_rule')
    if unsettled and rule == 'MUST_PAY':
        due = money(sum((i.outstanding for i in unsettled), ZERO))
        raise SchemeError(f'{len(unsettled)} installment(s) with ₹{due} outstanding must be paid (or waived) before maturity.')
    if unsettled and rule == 'EXTEND_MATURITY':
        base = add_months(e.last_due_date, int(rules.get('maturity_months_after_last') or 0))
        extended = add_months(base, len(unsettled))
        if extended > as_of:
            if extended != e.expected_maturity_date:
                old = e.expected_maturity_date
                e.expected_maturity_date = extended
                e.save(update_fields=['expected_maturity_date', 'updated_at'])
                audit(actor, 'extend_maturity', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e, old={'maturity': old},
                      new={'maturity': extended, 'unpaid': len(unsettled)})
            raise SchemeError(f'{len(unsettled)} installment(s) unpaid: maturity extended to {extended}.')
    calc = _store_calculation(actor, e, as_of)
    e.status, e.matured_on = 'MATURED', as_of
    e.save()
    audit(actor, 'mature', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e,
          new={'calculation': calc.calculation_no, 'benefit': calc.calculated_benefit, 'eligibility': calc.eligibility})
    queue_notification(e, 'MATURITY', f'Your Jewellery Scheme {e.account_no} has matured. Benefit of ₹{calc.calculated_benefit} is '
                                      f'awaiting approval; your contribution is ₹{e.contribution_balance}.', f'maturity:{e.account_no}')
    return calc


@transaction.atomic
def mature(actor, enrollment, *, as_of=None):
    require(actor, 'mature')
    return _mature(actor, _own(actor, enrollment), as_of or dj_timezone.localdate())


@transaction.atomic
def recalculate_benefit(actor, enrollment, *, as_of=None):
    require(actor, 'mature')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if e.status != 'MATURED':
        raise SchemeError('Only a matured scheme awaiting benefit approval can be recalculated.')
    calc = _store_calculation(actor, e, as_of or dj_timezone.localdate())
    e.save(update_fields=['benefit_eligibility', 'updated_at'])
    audit(actor, 'recalculate', 'BENEFIT_CALC', calc.calculation_no, enrollment=e, new={'benefit': calc.calculated_benefit})
    return calc


@transaction.atomic
def approve_benefit(actor, calculation, *, approved_amount=None, reason=''):
    require(actor, 'approve_benefit')
    calc = BenefitCalculation.objects.select_for_update().get(pk=calculation.pk, tenant=actor.tenant)
    e = SchemeEnrollment.objects.select_for_update().get(pk=calc.enrollment_id)
    setup = get_setup(actor.tenant)
    if calc.status != 'CALCULATED' or e.status != 'MATURED':
        raise SchemeError(f'{calc.calculation_no} is not awaiting approval.')
    maker_checker(actor, calc.created_by, setup, 'benefit calculation')
    amount = calc.calculated_benefit if approved_amount in (None, '') else money(approved_amount)
    overridden = amount != calc.calculated_benefit
    if amount < 0:
        raise SchemeError('The benefit cannot be negative.')
    if overridden:
        require(actor, 'override_benefit')
        if not e.rules.get('benefit_override_allowed'):
            raise SchemeError('This scheme does not allow the calculated benefit to be overridden.')
        if not (reason or '').strip():
            raise SchemeError('A reason is required to override the calculated benefit.')
        limit = d(e.rules.get('max_override_percent'))
        if limit:
            base = calc.calculated_benefit
            if not base or abs(amount - base) * 100 / base > limit:
                raise SchemeError(f'Override may change the calculated benefit by at most {limit.normalize()}%.')
        maximum = d(e.rules.get('max_benefit'))
        if maximum and amount > maximum:
            raise SchemeError(f'Maximum benefit is ₹{money(maximum)}.')
    engine = SchemePostingEngine(actor, e, posting_date=dj_timezone.localdate())
    if amount:
        engine.accrue_benefit(calc, amount, description=f'Jeweller benefit {calc.calculation_no}'
                              + (f' (calculated ₹{calc.calculated_benefit}, override: {reason})' if overridden else ''))
    calc.status, calc.approved_benefit, calc.override_reason = 'APPROVED', amount, (reason or '')[:250] if overridden else ''
    calc.approved_by, calc.approved_at = actor.user, dj_timezone.now()
    calc.save()
    e = engine.enrollment
    e.status = 'BENEFIT_APPROVED'
    window = int(e.rules.get('redemption_window_days') or 0)
    e.redemption_valid_until = (e.matured_on or dj_timezone.localdate()) + timedelta(days=window) if window else None
    e.save()
    audit(actor, 'override_benefit' if overridden else 'approve_benefit', 'BENEFIT_CALC', calc.calculation_no, enrollment=e,
          old={'calculated': calc.calculated_benefit}, new={'approved': amount}, reason=reason)
    queue_notification(e, 'BENEFIT', f'Your Jewellery Scheme {e.account_no} benefit of ₹{amount} is confirmed. '
                                     f'Available purchase entitlement: ₹{e.available_entitlement}.', f'benefit:{calc.calculation_no}')
    return calc


@transaction.atomic
def reverse_benefit(actor, enrollment, *, reason):
    """Wrong benefit: post a reversal and return the scheme to 'matured' for a fresh calculation. Never overwrite."""
    require(actor, 'override_benefit')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if not (reason or '').strip():
        raise SchemeError('A reason is required to reverse a benefit.')
    if e.status != 'BENEFIT_APPROVED' or e.redemption_count:
        raise SchemeError('Only an approved benefit with no redemptions can be reversed.')
    calc = e.benefit_calculations.filter(status='APPROVED').first()
    if calc is None:
        raise SchemeError('No approved benefit found.')
    engine = SchemePostingEngine(actor, e, posting_date=dj_timezone.localdate())
    if calc.approved_benefit:
        engine.reverse_benefit(calc, reason=reason)
    calc.status = 'REVERSED'
    calc.save(update_fields=['status', 'updated_at'])
    e = engine.enrollment
    e.status, e.redemption_valid_until = 'MATURED', None
    e.save()
    audit(actor, 'reverse_benefit', 'BENEFIT_CALC', calc.calculation_no, enrollment=e, old={'approved': calc.approved_benefit}, reason=reason)
    return _store_calculation(actor, e, dj_timezone.localdate())


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------

def _settle_remaining(actor, engine, redemption):
    """Lower-value purchase: apply the scheme's remaining-balance rule (keep / refund / forfeit / credit)."""
    e, rule = engine.enrollment, engine.enrollment.rules.get('remaining_balance_rule', 'KEEP')
    if rule == 'KEEP' or e.available_entitlement <= 0:
        return None
    contribution, benefit = e.contribution_balance, e.benefit_balance
    no = redemption.redemption_no
    if rule == 'FORFEIT':
        engine.forfeit(redemption, no, contribution=contribution, benefit=benefit, description=f'Remaining balance forfeited after {no}')
    elif rule == 'CREDIT':
        engine.credit_transfer(redemption, no, description=f'Remaining contribution to customer credit after {no}; benefit balance lapses')
    elif rule == 'REFUND':
        engine.forfeit(redemption, no, benefit=benefit, description=f'Unused benefit lapses after {no} (benefit is never refunded)')
        refund = SchemeRefund.objects.create(
            tenant=actor.tenant, company=e.company, refund_no=next_number(actor.tenant, 'SCHEME_REFUND'), enrollment=e,
            contribution_refund=contribution, refund_amount=contribution, reason=f'Remaining contribution after {no}',
            requested_by=actor.user, location=engine.location, created_by=actor.user,
        )
        audit(actor, 'refund_request', 'SCHEME_REFUND', refund.refund_no, enrollment=e, new={'amount': contribution})
        return refund
    e = engine.enrollment
    if e.available_entitlement <= 0:
        e.status, e.closed_at = 'REDEEMED', dj_timezone.now()
        e.save()
    audit(actor, f'remaining_{rule.lower()}', 'SCHEME_REDEMPTION', no, enrollment=e, new={'contribution': contribution, 'benefit': benefit})
    return None


@transaction.atomic
def redeem(actor, enrollment, *, lines, invoice_value=None, sales_invoice=None, invoice_reference='', amount=None, location=None,
           idempotency_key='', reason=''):
    require(actor, 'redeem')
    e = _own(actor, enrollment)
    _location(actor, location)
    engine = SchemePostingEngine(actor, e, location=location)
    existing = idempotency_key and SchemeRedemption.objects.filter(tenant=actor.tenant, idempotency_key=idempotency_key).first()
    redemption = engine.redeem(lines=lines, invoice_value=invoice_value, sales_invoice=sales_invoice, invoice_reference=invoice_reference,
                               amount=amount, idempotency_key=idempotency_key, reason=reason)
    if not existing and engine.enrollment.status == 'PARTIALLY_REDEEMED':
        _settle_remaining(actor, engine, redemption)
    return redemption


@transaction.atomic
def reverse_redemption(actor, redemption, *, reason):
    require(actor, 'reverse_redemption')
    redemption = _own(actor, redemption)
    maker_checker(actor, redemption.created_by, get_setup(actor.tenant), 'redemption')
    e = redemption.enrollment
    if e.contribution_forfeited or e.contribution_credited or e.refunds.exclude(status='REJECTED').exists():
        raise SchemeError('The remaining balance of this scheme has already been settled; the redemption can no longer be reversed.')
    return SchemePostingEngine(actor, e).reverse_redemption(redemption, reason=reason)


# ---------------------------------------------------------------------------
# Cancellation, refund, closure
# ---------------------------------------------------------------------------

def cancellation_terms(e):
    """(contribution balance, benefit balance, charge, benefit forfeited, refundable)."""
    rules = e.rules
    contribution, benefit = e.contribution_balance, e.benefit_balance
    kind, value = rules.get('cancellation_charge_type'), d(rules.get('cancellation_charge_value'))
    charge = money(value) if kind == 'FIXED' else money(contribution * value / 100) if kind == 'PERCENT' else ZERO
    charge = min(charge, contribution)
    return contribution, benefit, charge, benefit, money(contribution - charge)


@transaction.atomic
def request_cancellation(actor, enrollment, *, cancellation_type='CUSTOMER', reason):
    require(actor, 'cancel')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if not (reason or '').strip():
        raise SchemeError('A reason is required to cancel a scheme.')
    if cancellation_type not in dict(SchemeCancellation.TYPES):
        raise SchemeError('Select the cancellation type.')
    if e.status not in SchemeEnrollment.OPEN + ('DRAFT',):
        raise SchemeError(f'{e.account_no} is {e.get_status_display().lower()} and cannot be cancelled.')
    if cancellation_type == 'CUSTOMER' and not e.rules.get('cancellation_allowed'):
        raise SchemeError('This scheme does not allow customer cancellation.')
    if e.benefit_approved and e.rules.get('cancellation_benefit_rule') == 'KEEP_APPROVED' and cancellation_type == 'CUSTOMER':
        raise SchemeError('The benefit is approved: under this scheme the entitlement must be redeemed, not cancelled.')
    if e.cancellations.filter(status='REQUESTED').exists():
        raise SchemeError('A cancellation request is already pending.')
    contribution, benefit, charge, forfeited, refundable = cancellation_terms(e)
    c = SchemeCancellation.objects.create(
        tenant=actor.tenant, company=e.company, cancellation_no=next_number(actor.tenant, 'SCHEME_CANCELLATION'), enrollment=e,
        cancellation_type=cancellation_type, reason=reason[:250], contribution_balance=contribution, benefit_balance=benefit,
        cancellation_charge=charge, benefit_forfeited=forfeited, refundable_amount=refundable, requested_by=actor.user, created_by=actor.user,
    )
    audit(actor, 'cancel_request', 'SCHEME_CANCELLATION', c.cancellation_no, enrollment=e,
          new={'type': cancellation_type, 'refundable': refundable, 'charge': charge, 'benefit_forfeited': forfeited}, reason=reason)
    return c


@transaction.atomic
def decide_cancellation(actor, cancellation, *, approve, note=''):
    require(actor, 'approve_cancellation')
    c = SchemeCancellation.objects.select_for_update().get(pk=cancellation.pk, tenant=actor.tenant)
    if c.status != 'REQUESTED':
        raise SchemeError(f'{c.cancellation_no} has already been decided.')
    maker_checker(actor, c.requested_by, get_setup(actor.tenant), 'cancellation')
    c.approved_by, c.approved_at = actor.user, dj_timezone.now()
    if not approve:
        c.status = 'REJECTED'
        c.save()
        audit(actor, 'cancel_reject', 'SCHEME_CANCELLATION', c.cancellation_no, enrollment=c.enrollment, reason=note)
        return c, None
    engine = SchemePostingEngine(actor, c.enrollment, posting_date=dj_timezone.localdate())
    e = engine.enrollment
    contribution, benefit, charge, forfeited, refundable = cancellation_terms(e)   # balances as of approval
    c.contribution_balance, c.benefit_balance, c.cancellation_charge = contribution, benefit, charge
    c.benefit_forfeited, c.refundable_amount, c.status = forfeited, refundable, 'APPROVED'
    c.save()
    engine.forfeit(c, c.cancellation_no, benefit=forfeited, description=f'Benefit forfeited on cancellation {c.cancellation_no}')
    e.installments.exclude(status__in=SchemeInstallment.SETTLED).update(status='CANCELLED', updated_at=dj_timezone.now())
    e = engine.enrollment
    refund = None
    e.status = 'CANCELLED'
    if contribution <= 0:
        e.status, e.closed_at, e.close_reason = 'CLOSED', dj_timezone.now(), f'Cancelled ({c.cancellation_no}); nothing to refund'
    elif e.rules.get('refund_allowed'):
        refund = SchemeRefund.objects.create(
            tenant=actor.tenant, company=e.company, refund_no=next_number(actor.tenant, 'SCHEME_REFUND'), enrollment=e, cancellation=c,
            contribution_refund=refundable, deduction=charge, refund_amount=refundable, reason=f'Cancellation {c.cancellation_no}: {c.reason}'[:250],
            requested_by=actor.user, created_by=actor.user,
        )
    else:
        engine.forfeit(c, c.cancellation_no, contribution=charge, description=f'Cancellation charge {c.cancellation_no}')
        engine.credit_transfer(c, c.cancellation_no, description=f'Contribution to customer credit - scheme is not refundable ({c.cancellation_no})')
        e = engine.enrollment
        e.status, e.closed_at, e.close_reason = 'CLOSED', dj_timezone.now(), f'Cancelled ({c.cancellation_no}); converted to customer credit'
    e.save()
    audit(actor, 'cancel_approve', 'SCHEME_CANCELLATION', c.cancellation_no, enrollment=e,
          new={'refund': refund.refund_no if refund else '', 'refundable': refundable, 'benefit_forfeited': forfeited}, reason=note)
    return c, refund


@transaction.atomic
def request_refund(actor, enrollment, *, contribution_refund, deduction=0, reason, location=None):
    require(actor, 'refund_request')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if not e.rules.get('refund_allowed'):
        raise SchemeError('This scheme does not allow refunds.')
    if e.status not in ('CANCELLED', 'PARTIALLY_REDEEMED'):
        raise SchemeError('Cancel the scheme first; refunds are paid only on cancellation or for a remaining balance.')
    if e.refunds.filter(status__in=('REQUESTED', 'APPROVED')).exists():
        raise SchemeError('A refund is already pending for this scheme.')
    amount, deduction = money(contribution_refund), money(deduction)
    if amount <= 0 or amount + deduction > e.contribution_balance:
        raise SchemeError(f'Refund must be between ₹0.01 and the contribution balance of ₹{e.contribution_balance}.')
    if not (reason or '').strip():
        raise SchemeError('A reason is required.')
    refund = SchemeRefund.objects.create(
        tenant=actor.tenant, company=e.company, refund_no=next_number(actor.tenant, 'SCHEME_REFUND'), enrollment=e,
        contribution_refund=amount, deduction=deduction, refund_amount=amount, reason=reason[:250], requested_by=actor.user,
        location=location, created_by=actor.user,
    )
    audit(actor, 'refund_request', 'SCHEME_REFUND', refund.refund_no, enrollment=e, new={'amount': amount, 'deduction': deduction}, reason=reason)
    return refund


@transaction.atomic
def decide_refund(actor, refund, *, approve, note=''):
    require(actor, 'approve_refund')
    r = SchemeRefund.objects.select_for_update().get(pk=refund.pk, tenant=actor.tenant)
    if r.status != 'REQUESTED':
        raise SchemeError(f'{r.refund_no} has already been decided.')
    maker_checker(actor, r.requested_by, get_setup(actor.tenant), 'refund')
    r.status = 'APPROVED' if approve else 'REJECTED'
    r.approved_by, r.approved_at = actor.user, dj_timezone.now()
    r.save()
    audit(actor, 'refund_approve' if approve else 'refund_reject', 'SCHEME_REFUND', r.refund_no, enrollment=r.enrollment,
          new={'amount': r.refund_amount}, reason=note)
    return r


@transaction.atomic
def pay_refund(actor, refund, *, payment_method=None, method_type='', bank_account=None, bank_details_reference='', location=None,
               refund_date=None):
    require(actor, 'pay_refund')
    r = SchemeRefund.objects.select_for_update().get(pk=refund.pk, tenant=actor.tenant)
    if r.status != 'APPROVED':
        raise SchemeError(f'{r.refund_no} must be approved before it is paid.')
    _location(actor, location)
    r.payment_method, r.bank_account = payment_method, bank_account
    r.method_type = method_type or (payment_method.method_type if payment_method else '')
    if not r.method_type:
        raise SchemeError('Select the refund payment method.')
    r.bank_details_reference, r.location = (bank_details_reference or '')[:120], location
    engine = SchemePostingEngine(actor, r.enrollment, posting_date=refund_date, location=location)
    r.finance_voucher = engine.pay_refund(r)
    r.status, r.paid_by, r.refund_date = 'PAID', actor.user, engine.posting_date
    r.save()
    e = engine.enrollment
    if e.available_entitlement <= 0:
        e.status = 'REFUNDED' if e.status == 'CANCELLED' else 'REDEEMED'
        e.closed_at = dj_timezone.now()
        e.save()
    audit(actor, 'refund_pay', 'SCHEME_REFUND', r.refund_no, enrollment=e, new={'amount': r.refund_amount, 'method': r.method_type})
    queue_notification(e, 'REFUND', f'A refund of ₹{r.refund_amount} for your Jewellery Scheme {e.account_no} has been paid ({r.refund_no}).',
                       f'refund:{r.refund_no}')
    return r


@transaction.atomic
def close_enrollment(actor, enrollment, *, reason=''):
    require(actor, 'approve_cancellation')
    e = SchemeEnrollment.objects.select_for_update().get(pk=enrollment.pk, tenant=actor.tenant)
    if e.status not in ('REFUNDED', 'REDEEMED', 'CANCELLED'):
        raise SchemeError('Only a redeemed, refunded or cancelled scheme can be closed.')
    if e.available_entitlement > 0:
        raise SchemeError(f'₹{e.available_entitlement} is still available on {e.account_no}.')
    e.status, e.closed_at, e.close_reason = 'CLOSED', dj_timezone.now(), (reason or 'Closed')[:250]
    e.save()
    audit(actor, 'close', 'SCHEME_ENROLLMENT', e.account_no, enrollment=e, reason=reason)
    return e


# ---------------------------------------------------------------------------
# Adjustment journal (maker-checker)
# ---------------------------------------------------------------------------

ADJUSTMENT_ALERT_COUNT = 5


@transaction.atomic
def create_adjustment(actor, enrollment, *, adjustment_type, amount=0, reason, reference, installment=None):
    require(actor, 'adjust')
    e = _own(actor, enrollment)
    if adjustment_type not in dict(SchemeAdjustment.TYPES):
        raise SchemeError('Select the adjustment type.')
    if not (reason or '').strip() or not (reference or '').strip():
        raise SchemeError('Reason and reference are required for a scheme adjustment.')
    amount = money(amount)
    if adjustment_type == 'WAIVER':
        if installment is None or installment.enrollment_id != e.pk:
            raise SchemeError('Select the installment to waive.')
        if installment.status in SchemeInstallment.SETTLED or installment.paid_amount:
            raise SchemeError('Only an unpaid installment can be waived.')
    elif not amount:
        raise SchemeError('Enter the adjustment amount.')
    adj = SchemeAdjustment.objects.create(
        tenant=actor.tenant, company=e.company, adjustment_no=next_number(actor.tenant, 'SCHEME_ADJUSTMENT'), enrollment=e,
        adjustment_type=adjustment_type, amount=amount, installment=installment, reason=reason[:250], reference=reference[:80],
        requested_by=actor.user, created_by=actor.user,
    )
    audit(actor, 'adjust_request', 'SCHEME_ADJUSTMENT', adj.adjustment_no, enrollment=e, new={'type': adjustment_type, 'amount': amount}, reason=reason)
    recent = SchemeAdjustment.objects.filter(tenant=actor.tenant, requested_by=actor.user,
                                             created_at__gte=dj_timezone.now() - timedelta(days=30)).count()
    if recent > ADJUSTMENT_ALERT_COUNT:
        audit(actor, 'alert_excessive_adjustments', 'SCHEME_ADJUSTMENT', adj.adjustment_no, enrollment=e,
              reason=f'{recent} manual adjustments in 30 days')
    return adj


@transaction.atomic
def decide_adjustment(actor, adjustment, *, approve, note=''):
    require(actor, 'approve_adjustment')
    adj = SchemeAdjustment.objects.select_for_update().get(pk=adjustment.pk, tenant=actor.tenant)
    if adj.status != 'PENDING_APPROVAL':
        raise SchemeError(f'{adj.adjustment_no} has already been decided.')
    maker_checker(actor, adj.requested_by, get_setup(actor.tenant), 'adjustment')
    if approve:
        SchemePostingEngine(actor, adj.enrollment).post_adjustment(adj)
    adj.status = 'POSTED' if approve else 'REJECTED'
    adj.approved_by, adj.approved_at = actor.user, dj_timezone.now()
    adj.save()
    audit(actor, 'adjust_post' if approve else 'adjust_reject', 'SCHEME_ADJUSTMENT', adj.adjustment_no, enrollment=adj.enrollment,
          new={'type': adj.adjustment_type, 'amount': adj.amount}, reason=note)
    return adj


# ---------------------------------------------------------------------------
# Lookups and customer-facing summary
# ---------------------------------------------------------------------------

def find_enrollments(actor, q):
    q = (q or '').strip()
    qs = SchemeEnrollment.objects.filter(tenant=actor.tenant).select_related('scheme', 'customer')
    if not q:
        return qs.none()
    match = Q(account_no__iexact=q) | Q(mobile__icontains=q) | Q(customer_name__icontains=q) | Q(customer__customer_no__iexact=q)
    try:
        import uuid
        match |= Q(qr_token=uuid.UUID(q.rsplit('/', 1)[-1] if '/' in q else q))
    except ValueError:
        pass
    return qs.filter(match)


def summary(e, as_of=None):
    """Every balance with its own label - never one ambiguous 'balance'."""
    as_of = as_of or dj_timezone.localdate()
    installments = list(e.installments.all())
    paid = sum(1 for i in installments if i.status == 'PAID')
    open_insts = [i for i in installments if i.status not in SchemeInstallment.SETTLED]
    next_due = open_insts[0] if open_insts else None
    if e.status in SchemeEnrollment.REDEEMABLE:
        available = e.available_entitlement
    elif e.status in SchemeEnrollment.CONTRIBUTING and e.rules.get('redeem_before_maturity'):
        available = e.contribution_balance      # contribution only - the benefit is never available before maturity
    else:
        available = ZERO
    ended = e.status in ('CANCELLED', 'REFUNDED', 'CLOSED')
    return {
        'installments_total': e.number_of_installments, 'installments_paid': paid, 'installments_remaining': len(open_insts),
        'contribution_paid': e.contribution_paid, 'unallocated_advance': e.unallocated_advance,
        'outstanding_contribution': money(sum((i.outstanding for i in open_insts), ZERO)),
        'overdue_amount': money(sum((i.outstanding for i in open_insts if i.status == 'OVERDUE'), ZERO)),
        'expected_benefit': e.expected_benefit, 'benefit_approved': e.benefit_approved,
        'expected_entitlement': e.total_entitlement if e.benefit_approved or ended else money(e.contribution_paid + e.expected_benefit),
        'total_entitlement': e.total_entitlement, 'redeemed': e.total_redeemed, 'contribution_redeemed': e.contribution_redeemed,
        'benefit_redeemed': e.benefit_redeemed, 'refunded': e.contribution_refunded,
        'forfeited': e.contribution_forfeited + e.benefit_forfeited, 'credited': e.contribution_credited,
        'contribution_balance': e.contribution_balance, 'benefit_balance': e.benefit_balance,
        'available_for_purchase': available,
        'next_due': next_due, 'maturity_date': e.expected_maturity_date, 'redemption_valid_until': e.redemption_valid_until,
        'matured': e.status in ('MATURED',) + SchemeEnrollment.REDEEMABLE + ('REDEEMED',),
        'redemption_expired': bool(e.redemption_valid_until and as_of > e.redemption_valid_until),
    }


# ---------------------------------------------------------------------------
# Scheduled jobs (idempotent)
# ---------------------------------------------------------------------------

def run_daily_jobs(tenant, as_of=None, *, auto_mature=True):
    """Apply advances, refresh installment / scheme status, queue due / overdue reminders and mature fully paid schemes
    whose maturity date has arrived. Safe to run any number of times for the same day."""
    as_of = as_of or dj_timezone.localdate()
    actor = Actor(tenant=tenant, user=None)
    setup = get_setup(tenant)
    stats = {'refreshed': 0, 'advance_applied': ZERO, 'due_reminders': 0, 'overdue_reminders': 0, 'matured': 0, 'maturity_queue': 0}
    ids = SchemeEnrollment.objects.filter(tenant=tenant, status__in=SchemeEnrollment.CONTRIBUTING).values_list('pk', flat=True)
    for pk in ids:
        with transaction.atomic():
            e = SchemeEnrollment.objects.get(pk=pk)
            engine = SchemePostingEngine(actor, e, posting_date=as_of)
            stats['advance_applied'] += engine.apply_advance()
            installments = refresh_enrollment(engine.enrollment, as_of, setup)
            e = _reload(e)
            stats['refreshed'] += 1
            for inst in installments:
                if inst.status == 'DUE':
                    stats['due_reminders'] += _notify_once(
                        e, 'DUE', f'Your monthly jewellery scheme installment of ₹{inst.outstanding} is due on '
                                  f'{inst.due_date:%d-%b-%Y} ({e.account_no}).', f'due:{e.account_no}:{inst.installment_no}')
                elif inst.status == 'OVERDUE':
                    stats['overdue_reminders'] += _notify_once(
                        e, 'OVERDUE', f'Your installment of ₹{inst.outstanding} ({e.account_no}) is overdue. Please make the payment to '
                                      f'maintain scheme eligibility.', f'overdue:{e.account_no}:{inst.installment_no}')
            if as_of >= e.expected_maturity_date:
                stats['maturity_queue'] += 1
                if auto_mature and e.status == 'COMPLETED':
                    try:
                        with transaction.atomic():
                            _mature(actor, e, as_of)
                        stats['matured'] += 1
                    except SchemeError:
                        pass
    return stats


def _notify_once(e, event, message, key):
    from .models import SchemeNotification
    if SchemeNotification.objects.filter(tenant=e.tenant, dedupe_key=key).exists():
        return 0
    queue_notification(e, event, message, key)
    return 1


def payment_receipt(payment):
    return SchemePayment.objects.select_related('enrollment', 'enrollment__scheme', 'payment_method', 'location').get(pk=payment.pk)


# ---------------------------------------------------------------------------
# New member (customer) and setup
# ---------------------------------------------------------------------------

@transaction.atomic
def create_customer(actor, *, name, phone='', email='', address='', pan=''):
    """A new scheme member goes into the shared Goldio customer master (same duplicate rules as the customer screen)."""
    require(actor, 'enroll')
    from erp.models import Customer
    from erp.services import get_next_customer_number
    name, phone, email, pan = (name or '').strip(), (phone or '').strip(), (email or '').strip(), (pan or '').strip().upper()
    if not name:
        raise SchemeError('Customer name is required.')
    if not phone:
        raise SchemeError('Mobile number is required for scheme reminders.')
    if Customer.objects.filter(phone=phone, is_active=True).exists():
        raise SchemeError(f'An active customer already uses mobile {phone}; search for and select that customer instead.')
    if email and Customer.objects.filter(email__iexact=email, is_active=True).exists():
        raise SchemeError(f'An active customer already uses {email}; search for and select that customer instead.')
    customer = Customer.objects.create(name=name[:200], phone=phone[:15], email=email, address=address or '', pan=pan[:10],
                                       customer_no=get_next_customer_number())
    audit(actor, 'create_customer', 'CUSTOMER', customer.customer_no, new={'name': name, 'mobile': phone})
    return customer


SETUP_FIELDS = ('display_name', 'reminder_days', 'require_enrollment_approval', 'allow_self_approval', 'allow_multiple_active_schemes',
                'max_active_schemes_per_customer', 'max_monthly_contribution')


@transaction.atomic
def update_setup(actor, **values):
    require(actor, 'configure')
    setup = get_setup(actor.tenant)
    old = {k: getattr(setup, k) for k in values}
    for key, value in values.items():
        if key not in SETUP_FIELDS:
            raise SchemeError(f'Unknown setting: {key}')
        setattr(setup, key, value)
    setup.updated_by = actor.user
    setup.save()
    audit(actor, 'setup', 'SAVINGS_SETUP', '', old=old, new=values)
    return setup
