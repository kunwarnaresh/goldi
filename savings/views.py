from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from erp.models import Customer
from inventory.engine import InventoryError
from inventory.models import Location
from inventory.tenancy import allowed_locations

from . import reports
from . import services as svc
from .engine import refresh_enrollment
from .models import (
    BENEFIT_ELIGIBILITY, BENEFIT_TYPES, INSTALLMENT_MODES, MISSED_RULES, JewellerySavingsScheme, SchemeEnrollment, SchemePayment,
    SchemeVersion,
)
from .security import ConfirmationRequired, actor_from_request, get_setup, permission_map

MENU = [
    ('Overview', [('savings_dashboard', 'Scheme dashboard')]),
    ('Members', [('savings_member_new', 'Enrol new member'), ('savings_members', 'Scheme members')]),
    ('Reports', [('savings_report_enrollment', 'Enrolment report'), ('savings_report_pending', 'Pending installments'),
                 ('savings_report_collection', 'Collection report')]),
    ('Setup', [('savings_schemes', 'Scheme master'), ('savings_setup', 'Savings setup')]),
]

PAYMENT_METHODS = [('cash', 'Cash'), ('upi', 'UPI'), ('card', 'Card'), ('bank', 'Bank transfer'), ('cheque', 'Cheque'),
                   ('wallet', 'Wallet'), ('other', 'Other')]


def menu_links():
    return [(group, [(reverse(name), label) for name, label in entries]) for group, entries in MENU]


def savings_view(view):
    """Login + tenant resolution. Rejected actions become messages and bounce back to the page."""
    @login_required(login_url='login')
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        actor = actor_from_request(request)
        try:
            return view(request, actor, *args, **kwargs)
        except ConfirmationRequired as exc:
            messages.warning(request, f'{exc} Tick the confirmation box and submit again.')
            return redirect(request.get_full_path())
        except (InventoryError, PermissionDenied) as exc:
            if request.method != 'POST':
                raise PermissionDenied(str(exc)) if isinstance(exc, PermissionDenied) else Http404(str(exc))
            messages.error(request, str(exc) or 'You do not have permission to do that.')
            return redirect(request.get_full_path())
    return wrapper


def page(request, actor, template, **context):
    context.setdefault('savings_menu', menu_links())
    context.setdefault('perms_sav', permission_map(actor))
    context.setdefault('setup', get_setup(actor.tenant))
    return render(request, f'savings/{template}', context)


def get_obj(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=pk).first() if str(pk or '').isdigit() else None
    if obj is None:
        raise Http404
    return obj


def decimal_of(value, default=None):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, AttributeError):
        return default


def date_range(request, default_days=30):
    today = timezone.localdate()
    end = parse_date(request.GET.get('end') or '') or today
    start = parse_date(request.GET.get('start') or '') or end - timedelta(days=default_days)
    return start, end


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@savings_view
def dashboard(request, actor):
    if request.method == 'POST' and request.POST.get('action') == 'create_template':
        scheme, _ = svc.create_template_scheme(actor)
        messages.success(request, f'{scheme.name} created as a draft - review its rules, then submit and approve it.')
        return redirect('savings_scheme_detail', pk=scheme.pk)
    recent = SchemeEnrollment.objects.filter(tenant=actor.tenant).select_related('scheme')[:10]
    return page(request, actor, 'dashboard.html', kpis=reports.dashboard(actor.tenant), schemes=reports.scheme_performance(actor.tenant),
                recent=recent)


# ---------------------------------------------------------------------------
# Members: enrol, list, account (schedule + customer ledger + collection), customer
# ---------------------------------------------------------------------------

@savings_view
def member_new(request, actor):
    schemes = [s for s in JewellerySavingsScheme.objects.filter(tenant=actor.tenant, status='ACTIVE') if s.current_version()]
    branches = allowed_locations(actor.tenant, actor.user, 'view')
    customer_q = request.GET.get('customer_q', '').strip()
    matches = []
    if customer_q:
        matches = Customer.objects.filter(is_active=True).filter(
            Q(phone__icontains=customer_q) | Q(name__icontains=customer_q) | Q(customer_no__iexact=customer_q)
            | Q(email__iexact=customer_q) | Q(pan__iexact=customer_q)).order_by('name')[:20]
    selected = Customer.objects.filter(pk=request.GET.get('customer'), is_active=True).first() if str(request.GET.get('customer', '')).isdigit() else None

    context = dict(schemes=schemes, branches=branches, customer_q=customer_q, matches=matches, selected=selected,
                   acceptance_methods=SchemeEnrollment.ACCEPTANCE_METHODS, today=timezone.localdate(), posted={})
    if request.method == 'POST':
        try:
            with transaction.atomic():      # a new customer is only kept if the enrolment succeeds
                return _enrol(request, actor)
        except (InventoryError, PermissionDenied) as exc:     # re-show the form with what was typed
            if isinstance(exc, ConfirmationRequired):
                context['confirm_message'] = str(exc)
            else:
                messages.error(request, str(exc) or 'You do not have permission to do that.')
            if request.POST.get('customer_mode') != 'new' and str(request.POST.get('customer', '')).isdigit():
                context['selected'] = Customer.objects.filter(pk=request.POST['customer'], is_active=True).first()
            context['posted'] = request.POST
            return page(request, actor, 'member_new.html', **context)
    return page(request, actor, 'member_new.html', **context)


def _enrol(request, actor):
    p = request.POST
    scheme = JewellerySavingsScheme.objects.filter(tenant=actor.tenant, pk=p.get('scheme') or 0, status='ACTIVE').first()
    if scheme is None:
        raise svc.SchemeError('Select the scheme to enrol in.')
    if p.get('customer_mode') == 'new':
        customer = svc.create_customer(actor, name=p.get('new_name', ''), phone=p.get('new_phone', ''), email=p.get('new_email', ''),
                                       address=p.get('new_address', ''), pan=p.get('new_pan', ''))
    else:
        customer = Customer.objects.filter(pk=p.get('customer'), is_active=True).first() if str(p.get('customer', '')).isdigit() else None
        if customer is None:
            raise svc.SchemeError('Search for and select an existing customer, or choose "New customer".')
    branch = get_obj(Location, actor, p['branch']) if p.get('branch') else None
    nominee = {'name': p.get('nominee_name', ''), 'relationship': p.get('nominee_relationship', ''), 'mobile': p.get('nominee_mobile', '')}
    enrollment = svc.enroll(actor, scheme=scheme, customer=customer, installment_amount=decimal_of(p.get('installment_amount')),
                            start_date=parse_date(p.get('start_date') or ''), branch=branch, sales_staff=actor.user,
                            nominee=nominee if nominee['name'] else None, confirm_duplicate=p.get('confirm') == 'on')
    enrollment = svc.accept_agreement(actor, enrollment, method=p.get('acceptance_method') or 'PHYSICAL',
                                      signature_reference=p.get('signature_reference', ''))
    messages.success(request, f'{customer.name} enrolled - scheme account {enrollment.account_no} '
                              f'({enrollment.get_status_display().lower()}).')
    return redirect('savings_member_detail', pk=enrollment.pk)


@savings_view
def member_list(request, actor):
    members = SchemeEnrollment.objects.filter(tenant=actor.tenant).select_related('scheme', 'branch')
    status, scheme_id, q = request.GET.get('status', ''), request.GET.get('scheme', ''), request.GET.get('q', '').strip()
    if status == 'open':
        members = members.filter(status__in=SchemeEnrollment.OPEN)
    elif status:
        members = members.filter(status=status)
    if scheme_id.isdigit():
        members = members.filter(scheme_id=scheme_id)
    if q:
        members = members.filter(Q(account_no__icontains=q) | Q(customer_name__icontains=q) | Q(mobile__icontains=q))
    return page(request, actor, 'member_list.html', members=members[:500], status=status, scheme_id=scheme_id, q=q,
                statuses=SchemeEnrollment.STATUSES, schemes=JewellerySavingsScheme.objects.filter(tenant=actor.tenant))


@savings_view
def member_detail(request, actor, pk):
    enrollment = get_obj(SchemeEnrollment, actor, pk)
    if request.method == 'POST':
        p, action = request.POST, request.POST.get('action')
        if action == 'approve':
            svc.approve_enrollment(actor, enrollment)
            messages.success(request, f'{enrollment.account_no} approved and active.')
        elif action == 'collect':
            amount = decimal_of(p.get('amount'))
            if not amount or amount <= 0:
                raise svc.SchemeError('Enter the amount received.')
            location = get_obj(Location, actor, p['location']) if p.get('location') else None
            payment = svc.collect(actor, enrollment, amount=amount, method_type=p.get('method_type', ''), reference_no=p.get('reference_no', ''),
                                  location=location, remarks=p.get('remarks', ''), confirm_duplicate=p.get('confirm') == 'on')
            messages.success(request, f'Receipt {payment.receipt_no}: ₹{payment.amount} collected.')
        elif action == 'reverse_payment':
            payment = get_obj(SchemePayment, actor, p.get('payment'))
            reversal = svc.reverse_payment(actor, payment, reason=p.get('reason', ''))
            messages.success(request, f'{payment.receipt_no} reversed by {reversal.receipt_no}.')
        else:
            raise Http404
        return redirect('savings_member_detail', pk=enrollment.pk)
    if enrollment.status in SchemeEnrollment.CONTRIBUTING:
        refresh_enrollment(enrollment)
        enrollment.refresh_from_db()
    return page(request, actor, 'member_detail.html', e=enrollment, s=svc.summary(enrollment),
                installments=enrollment.installments.all(), ledger=enrollment.ledger.select_related('location'),
                payments=enrollment.payments.select_related('location'), nominees=enrollment.nominees.filter(active=True),
                calculations=enrollment.benefit_calculations.all()[:5], methods=PAYMENT_METHODS,
                locations=allowed_locations(actor.tenant, actor.user, 'view'))


@savings_view
def customer_detail(request, actor, pk):
    """One customer's scheme accounts and their combined scheme ledger."""
    accounts = list(SchemeEnrollment.objects.filter(tenant=actor.tenant, customer_id=pk).select_related('scheme'))
    if not accounts:
        raise Http404
    customer = accounts[0].customer
    from .models import SchemeLedgerEntry
    ledger = SchemeLedgerEntry.objects.filter(tenant=actor.tenant, enrollment__customer_id=pk).select_related('enrollment').order_by('entry_date', 'id')
    totals = {k: sum((getattr(a, k) for a in accounts), Decimal('0')) for k in
              ('contribution_paid', 'benefit_approved', 'contribution_redeemed', 'benefit_redeemed', 'contribution_refunded')}
    totals['available'] = sum((a.available_entitlement for a in accounts), Decimal('0'))
    pending = sum((svc.summary(a)['outstanding_contribution'] for a in accounts if a.status in SchemeEnrollment.CONTRIBUTING), Decimal('0'))
    return page(request, actor, 'customer_detail.html', customer=customer, accounts=accounts, ledger=ledger, totals=totals, pending=pending)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@savings_view
def report_enrollment(request, actor):
    start = parse_date(request.GET.get('start') or '')
    end = parse_date(request.GET.get('end') or '')
    scheme = get_obj(JewellerySavingsScheme, actor, request.GET['scheme']) if request.GET.get('scheme') else None
    data = reports.enrollment_summary(actor.tenant, start=start, end=end, scheme=scheme)
    return page(request, actor, 'report_enrollment.html', data=data, start=start, end=end, scheme=scheme,
                schemes=JewellerySavingsScheme.objects.filter(tenant=actor.tenant))


@savings_view
def report_pending(request, actor):
    as_of = parse_date(request.GET.get('as_of') or '') or timezone.localdate()
    scheme = get_obj(JewellerySavingsScheme, actor, request.GET['scheme']) if request.GET.get('scheme') else None
    location = get_obj(Location, actor, request.GET['location']) if request.GET.get('location') else None
    overdue_only = request.GET.get('overdue') == '1'
    rows, totals = reports.pending_by_member(actor.tenant, as_of=as_of, scheme=scheme, location=location, overdue_only=overdue_only)
    return page(request, actor, 'report_pending.html', rows=rows, totals=totals, as_of=as_of, scheme=scheme, location=location,
                overdue_only=overdue_only, schemes=JewellerySavingsScheme.objects.filter(tenant=actor.tenant),
                locations=allowed_locations(actor.tenant, actor.user, 'view'))


@savings_view
def report_collection(request, actor):
    start, end = date_range(request)
    group_by = request.GET.get('group_by', 'day')
    if group_by not in ('day', 'staff', 'location', 'method', 'scheme'):
        group_by = 'day'
    rows, total = reports.collections(actor.tenant, start=start, end=end, group_by=group_by)
    return page(request, actor, 'report_collection.html', rows=rows, total=total, start=start, end=end, group_by=group_by)


# ---------------------------------------------------------------------------
# Scheme master and setup
# ---------------------------------------------------------------------------

@savings_view
def scheme_list(request, actor):
    return page(request, actor, 'scheme_list.html', schemes=JewellerySavingsScheme.objects.filter(tenant=actor.tenant).prefetch_related('versions'))


VERSION_DECIMALS = ('installment_amount', 'min_installment', 'max_installment', 'installment_step', 'benefit_value', 'max_benefit',
                    'late_payment_value')
VERSION_INTS = ('number_of_installments', 'due_day', 'grace_days', 'maturity_months_after_last', 'redemption_window_days')
VERSION_CHOICES = ('installment_mode', 'benefit_type', 'benefit_eligibility', 'missed_installment_rule')
VERSION_BOOLS = ('partial_payment_allowed', 'partial_redemption_allowed', 'refund_allowed', 'nominee_required', 'kyc_required')


@savings_view
def scheme_detail(request, actor, pk):
    scheme = get_obj(JewellerySavingsScheme, actor, pk)
    versions = scheme.versions.all()
    if request.method == 'POST':
        p, action = request.POST, request.POST.get('action')
        version = versions.filter(pk=p.get('version')).first()
        if action == 'save' and version:
            values = {k: decimal_of(p.get(k), Decimal('0')) for k in VERSION_DECIMALS if k in p}
            values.update({k: int(p[k]) for k in VERSION_INTS if p.get(k, '').isdigit()})
            values.update({k: p[k] for k in VERSION_CHOICES if p.get(k)})
            values.update({k: p.get(k) == 'on' for k in VERSION_BOOLS})
            if 'terms' in p:
                values['terms'] = p['terms']
            svc.update_version(actor, version, **values)
            messages.success(request, f'{version} saved.')
        elif action in ('submit', 'approve', 'reject') and version:
            svc.transition_version(actor, version, action, note=p.get('note', ''))
            messages.success(request, f'{version} {"submitted for approval" if action == "submit" else action + "d"}.')
        elif action == 'new_version':
            version = svc.new_version(actor, scheme, change_note=p.get('note', ''))
            messages.success(request, f'{version} created as a draft.')
        elif action in ('SUSPENDED', 'ACTIVE', 'CLOSED'):
            svc.set_scheme_status(actor, scheme, action, reason=p.get('note', ''))
            messages.success(request, f'{scheme.code} is now {action.lower()}.')
        else:
            raise Http404
        return redirect(f'{request.path}?version={version.pk}' if version else request.path)
    selected = versions.filter(pk=request.GET.get('version')).first() if str(request.GET.get('version', '')).isdigit() else None
    selected = selected or versions.filter(status__in=('DRAFT', 'UNDER_REVIEW')).first() or scheme.current_version() or versions.first()
    return page(request, actor, 'scheme_detail.html', scheme=scheme, versions=versions, v=selected,
                choices={'installment_mode': INSTALLMENT_MODES, 'benefit_type': BENEFIT_TYPES, 'benefit_eligibility': BENEFIT_ELIGIBILITY,
                         'missed_installment_rule': MISSED_RULES},
                enrolled=scheme.enrollments.count())


@savings_view
def setup_view(request, actor):
    if request.method == 'POST':
        p = request.POST
        svc.update_setup(
            actor, display_name=(p.get('display_name') or 'Jewellery Savings Plan')[:80],
            reminder_days=int(p['reminder_days']) if p.get('reminder_days', '').isdigit() else 5,
            require_enrollment_approval=p.get('require_enrollment_approval') == 'on', allow_self_approval=p.get('allow_self_approval') == 'on',
            allow_multiple_active_schemes=p.get('allow_multiple_active_schemes') == 'on',
            max_active_schemes_per_customer=int(p['max_active_schemes_per_customer']) if p.get('max_active_schemes_per_customer', '').isdigit() else 0,
            max_monthly_contribution=decimal_of(p.get('max_monthly_contribution'), Decimal('0')),
        )
        messages.success(request, 'Savings setup saved.')
        return redirect('savings_setup')
    return page(request, actor, 'setup.html')
