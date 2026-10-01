"""Store / location / staff / POS terminal / tender business rules.

Every entry point (web forms, API, Excel import, POS login, POS sale posting) goes through these functions, so the
hierarchy Location -> Store -> {Staff, POS terminal, Store tender} is enforced in one place, on top of the database
constraints on the models."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from erp.models import (
    AuditLog, POSPayment, POSSession, POSShift, POSStaff, POSStaffAssignment, POSTerminal, SalesInvoice, SalesReturn,
    Store, StoreTender,
)

from .permissions import staff_has_pos_access, staff_permissions

MAX_FAILED_LOGINS = getattr(settings, 'POS_MAX_FAILED_LOGINS', 5)
PASSWORD_MIN_LENGTH = getattr(settings, 'POS_PASSWORD_MIN_LENGTH', 6)
SESSION_IDLE_MINUTES = getattr(settings, 'POS_SESSION_IDLE_MINUTES', 720)
PENDING_INVOICE_STATUSES = ('draft', 'pending_approval')


# --- Audit ---------------------------------------------------------------------------------------------------------

def _client(request):
    if request is None:
        return None, ''
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')
    return ip or None, request.META.get('HTTP_USER_AGENT', '')[:100]


def audit(request, obj, action, description='', old=None, new=None, actor=None):
    """Write an audit row. Callers must never pass passwords or hashes in old/new."""
    ip, device = _client(request)
    user = actor if actor is not None else (getattr(request, 'user', None) if request is not None else None)
    company = getattr(obj, 'company', None) or getattr(getattr(obj, 'store', None), 'company', None)
    AuditLog.objects.create(
        company=company if company is not None and getattr(company, 'pk', None) else None,
        actor=user if user is not None and getattr(user, 'is_authenticated', False) else None,
        table_name=obj._meta.db_table, action=action, record_id=str(obj.pk or ''), description=description,
        old_value=old or {}, new_value=new or {}, ip_address=ip, device=device,
    )


def snapshot(obj, fields):
    out = {}
    for field in fields:
        value = getattr(obj, f'{field}_id', None) if hasattr(obj, f'{field}_id') else getattr(obj, field, None)
        out[field] = value if isinstance(value, (int, bool, type(None))) else str(value)
    return out


def changes(before, after):
    """Only the fields that changed, as (old, new) dicts."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


# --- Passwords & account security ------------------------------------------------------------------------------------

def validate_password_value(raw, confirm=None):
    errors = []
    if not raw or len(raw) < PASSWORD_MIN_LENGTH:
        errors.append(f'Password must be at least {PASSWORD_MIN_LENGTH} characters.')
    if confirm is not None and raw != confirm:
        errors.append('Password and confirm password do not match.')
    if errors:
        raise ValidationError(errors)


def set_password(staff, raw, request=None, reset=False, require_change=False):
    staff.pin_hash = make_password(raw)
    staff.password_changed_at = timezone.now()
    staff.password_change_required = require_change
    staff.failed_login_attempts = 0
    staff.save(update_fields=['pin_hash', 'password_changed_at', 'password_change_required', 'failed_login_attempts'])
    if reset:
        audit(request, staff, 'password_reset', f'Password reset for {staff.employee_code}.')


def lock_account(staff, request=None, reason='Locked by administrator.'):
    staff.is_blocked = True
    staff.locked_at = timezone.now()
    staff.save(update_fields=['is_blocked', 'locked_at'])
    audit(request, staff, 'lock', reason)


def unlock_account(staff, request=None):
    staff.is_blocked = False
    staff.locked_at = None
    staff.failed_login_attempts = 0
    staff.save(update_fields=['is_blocked', 'locked_at', 'failed_login_attempts'])
    audit(request, staff, 'unlock', f'Account unlocked for {staff.employee_code}.')


def set_staff_active(staff, active, request=None):
    """Deactivating a staff member takes effect immediately: any open POS session is closed."""
    staff.is_active = active
    staff.save(update_fields=['is_active'])
    if not active:
        for session in POSSession.objects.filter(staff=staff, status='active'):
            close_session(session, status='force_closed')
    audit(request, staff, 'activate' if active else 'deactivate', f'Staff {staff.employee_code} {"activated" if active else "deactivated"}.')


def set_terminal_status(terminal, status, request=None):
    terminal.status = status
    terminal.save(update_fields=['status', 'is_active'])
    if status != 'active':
        for session in POSSession.objects.filter(terminal=terminal, status='active'):
            close_session(session, status='force_closed')
    audit(request, terminal, 'activate' if status == 'active' else 'deactivate', f'Terminal {terminal.code} set to {status}.')


# --- Hierarchy validation ------------------------------------------------------------------------------------------

def resolve_store_for_location(location):
    return Store.objects.filter(location=location).first()


def validate_staff_placement(staff, new_store, location=None):
    """Checks for creating a staff member in `new_store`, or moving an existing one there (acceptance test 16).
    Returns a list of warnings for things that will be adjusted automatically."""
    if new_store is None:
        raise ValidationError({'store': 'Store is required.'})
    if new_store.location_id is None:
        raise ValidationError({'store': f'Store {new_store.code} has no Location assigned. Assign a location to the store first.'})
    if location is not None and new_store.location_id != location.pk:
        raise ValidationError({'location': f'Location {location.location_code} is not the location of store {new_store.code}.'})
    warnings = []
    if staff.pk and staff.store_id and staff.store_id != new_store.pk:
        if POSSession.objects.filter(staff=staff, status='active').exists():
            raise ValidationError({'store': 'This staff member has an open POS session. Close it before changing store.'})
        pending = SalesInvoice.objects.filter(cashier_staff=staff, status__in=PENDING_INVOICE_STATUSES).count()
        if pending:
            raise ValidationError({'store': f'This staff member has {pending} pending POS bill(s) awaiting approval. '
                                            'Complete or cancel them before changing store.'})
        if Store.objects.filter(pk=staff.store_id, manager=staff).exists():
            raise ValidationError({'store': 'This staff member is the manager of their current store. Assign a new manager first.'})
        assignments = POSStaffAssignment.objects.filter(staff=staff, active=True).count()
        if assignments:
            warnings.append(f'{assignments} POS terminal assignment(s) at the old store will be ended.')
        if staff.default_terminal_id:
            warnings.append('The default POS terminal will be cleared.')
    return warnings


@transaction.atomic
def save_staff(staff, request=None, location=None, password=None):
    """Persist a staff member, applying the store-change side effects and audit trail."""
    before = {}
    if staff.pk:
        previous = POSStaff.objects.get(pk=staff.pk)
        before = snapshot(previous, STAFF_AUDIT_FIELDS)
        moved = previous.store_id != staff.store_id
    else:
        moved = False
    validate_staff_placement(POSStaff.objects.get(pk=staff.pk) if staff.pk else staff, staff.store, location)
    if moved:
        POSStaffAssignment.objects.filter(staff=staff, active=True).update(active=False, effective_to=timezone.localdate())
        staff.default_terminal = None
    created = staff.pk is None
    if created and not staff.pin_hash:
        staff.pin_hash = make_password(None)  # unusable until a password is set
    try:
        staff.save()
    except IntegrityError as exc:
        raise ValidationError({'login_id': 'This login ID is already used by another staff member.'}) from exc
    if password:
        set_password(staff, password, request=request, reset=not created)
    after = snapshot(staff, STAFF_AUDIT_FIELDS)
    if created:
        audit(request, staff, 'create', f'Staff {staff.employee_code} created in store {staff.store.code}.', new=after)
    else:
        old, new = changes(before, after)
        if old:
            action = 'assign' if 'store' in new else 'update'
            audit(request, staff, action, f'Staff {staff.employee_code} updated.', old=old, new=new)
    return staff


STAFF_AUDIT_FIELDS = ['employee_code', 'name', 'store', 'staff_role', 'login_id', 'pos_access', 'is_active',
                      'default_terminal', 'designation']
STORE_AUDIT_FIELDS = ['code', 'name', 'location', 'status', 'manager', 'gstin']
TERMINAL_AUDIT_FIELDS = ['code', 'name', 'store', 'status', 'device_id', 'default_tender']
TENDER_AUDIT_FIELDS = ['code', 'name', 'tender_type', 'status', 'requires_reference', 'allow_refund', 'allow_change',
                       'allow_split_payment']
STORE_TENDER_AUDIT_FIELDS = ['store', 'tender', 'active', 'is_default', 'allow_refund', 'allow_change', 'allow_split_payment']


def save_with_audit(obj, fields, request=None, label=''):
    before = snapshot(type(obj).objects.get(pk=obj.pk), fields) if obj.pk else None
    obj.save()
    after = snapshot(obj, fields)
    if before is None:
        audit(request, obj, 'create', f'{label or obj._meta.verbose_name} {obj} created.', new=after)
    else:
        old, new = changes(before, after)
        if old:
            audit(request, obj, 'update', f'{label or obj._meta.verbose_name} {obj} updated.', old=old, new=new)
    return obj


def validate_store_location(store, location):
    if location is None:
        raise ValidationError({'location': 'Location is required.'})
    other = Store.objects.filter(location=location).exclude(pk=store.pk).first()
    if other:
        raise ValidationError({'location': f'Location {location.location_code} is already assigned to store {other.code}.'})
    if store.company_id and location.company_id != store.company_id:
        raise ValidationError({'location': 'Location belongs to a different company.'})


@transaction.atomic
def assign_store_tender(store_tender, request=None):
    if not store_tender.pk and StoreTender.objects.filter(store=store_tender.store, tender=store_tender.tender).exists():
        raise ValidationError({'tender': f'{store_tender.tender.name} is already assigned to {store_tender.store.name}.'})
    if store_tender.tender.status != 'active' and not store_tender.pk:
        raise ValidationError({'tender': 'Only active tenders can be assigned.'})
    if store_tender.is_default:
        StoreTender.objects.filter(store=store_tender.store, is_default=True).exclude(pk=store_tender.pk).update(is_default=False)
    if store_tender.pk:
        return save_with_audit(store_tender, STORE_TENDER_AUDIT_FIELDS, request, 'Store tender')
    store_tender.save()
    audit(request, store_tender, 'assign', f'Tender {store_tender.tender.name} assigned to store {store_tender.store.code}.',
          new=snapshot(store_tender, STORE_TENDER_AUDIT_FIELDS))
    return store_tender


def validate_assignment(staff, terminal):
    if staff.store_id != terminal.store_id:
        raise ValidationError('Staff can only be assigned to POS terminals of their own store.')


# --- POS login -----------------------------------------------------------------------------------------------------

class POSLoginError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def terminal_problem(terminal, on=None):
    on = on or timezone.localdate()
    if terminal is None:
        return 'terminal_missing', 'This POS terminal is not configured.'
    if terminal.status != 'active':
        return 'terminal_inactive', f'POS terminal {terminal.code} is {terminal.get_status_display().lower()}.'
    if (terminal.active_from and terminal.active_from > on) or (terminal.active_to and terminal.active_to < on):
        return 'terminal_inactive', f'POS terminal {terminal.code} is outside its active dates.'
    store = terminal.store
    if store.status != 'active':
        return 'store_inactive', f'Store {store.code} is not active.'
    if store.location_id is None:
        return 'location_missing', f'Store {store.code} has no Location assigned.'
    if store.location.status != 'active':
        return 'location_inactive', f'Location {store.location.location_code} is not active.'
    return None


def staff_problem(staff, terminal, on=None):
    """Everything after the password check, in the order of the specified login flow."""
    on = on or timezone.localdate()
    if not staff.is_active or (staff.leaving_date and staff.leaving_date < on):
        return 'staff_inactive', 'Your staff account is inactive.'
    if not staff.pos_access:
        return 'pos_disabled', 'POS login is not enabled for your account.'
    if not staff_has_pos_access(staff):
        return 'role_denied', 'Your role does not allow POS access.'
    if staff.store_id is None:
        return 'store_missing', 'You are not assigned to a store.'
    if staff.store_id != terminal.store_id:
        return 'wrong_store', 'Staff is not authorized for this Store.'
    if staff.store.location_id is None or staff.store.location_id != terminal.store.location_id:
        return 'wrong_location', 'Staff is not authorized for this Location.'
    assignments = [a for a in POSStaffAssignment.objects.filter(staff=staff, active=True) if a.is_effective(on)]
    if assignments and not any(a.terminal_id == terminal.pk for a in assignments):
        return 'terminal_denied', f'You are not assigned to POS terminal {terminal.code}.'
    if 'pos.sale.create' not in staff_permissions(staff) and 'pos.report.view' not in staff_permissions(staff):
        return 'role_denied', 'Your role has no POS permissions.'
    return None


def find_staff(identifier):
    identifier = (identifier or '').strip()
    if not identifier:
        return None
    return (POSStaff.objects.select_related('store__location', 'staff_role').filter(login_id=identifier.lower()).first()
            or POSStaff.objects.select_related('store__location', 'staff_role').filter(employee_code__iexact=identifier).first())


def expire_idle_sessions(terminal=None):
    cutoff = timezone.now() - timedelta(minutes=SESSION_IDLE_MINUTES)
    stale = POSSession.objects.filter(status='active', last_activity__lt=cutoff)
    if terminal is not None:
        stale = stale.filter(terminal=terminal)
    for session in stale:
        close_session(session, status='expired')


def authenticate(identifier, password, terminal, request=None, django_session_key=''):
    """Full POS login flow. Returns an open POSSession or raises POSLoginError."""
    problem = terminal_problem(terminal)
    if problem:
        raise POSLoginError(*problem)
    staff = find_staff(identifier)
    if staff is None:
        raise POSLoginError('invalid_credentials', 'Invalid login ID or password.')
    if staff.is_blocked:
        raise POSLoginError('locked', 'Account locked. Please contact Store Manager/Admin.')
    if not check_password(password or '', staff.pin_hash):
        staff.failed_login_attempts += 1
        fields = ['failed_login_attempts']
        if staff.failed_login_attempts >= MAX_FAILED_LOGINS:
            staff.is_blocked = True
            staff.locked_at = timezone.now()
            fields += ['is_blocked', 'locked_at']
        staff.save(update_fields=fields)
        audit(request, staff, 'login_failed', f'Failed POS login on {terminal.code} (attempt {staff.failed_login_attempts}).')
        if staff.is_blocked:
            audit(request, staff, 'lock', f'Locked after {staff.failed_login_attempts} failed login attempts.')
            raise POSLoginError('locked', 'Account locked. Please contact Store Manager/Admin.')
        raise POSLoginError('invalid_credentials', 'Invalid login ID or password.')
    problem = staff_problem(staff, terminal)
    if problem:
        audit(request, staff, 'login_failed', f'POS login rejected on {terminal.code}: {problem[1]}')
        raise POSLoginError(*problem)

    expire_idle_sessions(terminal)
    with transaction.atomic():
        busy = POSSession.objects.select_for_update().filter(terminal=terminal, status='active').first()
        if busy and busy.staff_id != staff.pk:
            raise POSLoginError('terminal_busy', f'POS terminal {terminal.code} is in use by {busy.staff_name_snapshot or busy.staff.name}. '
                                                 'They must log out, or a manager can close the session.')
        if busy:
            close_session(busy, status='logged_out')
        session = open_session(staff, terminal, request, django_session_key)
    staff.failed_login_attempts = 0
    staff.last_login = timezone.now()
    staff.save(update_fields=['failed_login_attempts', 'last_login'])
    terminal.last_login = timezone.now()
    terminal.save(update_fields=['last_login'])
    audit(request, staff, 'login', f'POS login on {terminal.code} ({session.session_no}).')
    return session


def open_session(staff, terminal, request=None, django_session_key=''):
    store, location = terminal.store, terminal.store.location
    shift = POSShift.objects.filter(terminal=terminal, status='open').first()
    if not shift:
        shift = POSShift.objects.create(
            shift_code=f'SHIFT-{terminal.code}-{timezone.now():%Y%m%d%H%M%S%f}',
            store=store, terminal=terminal, opening_staff=staff, location_code_snapshot=location.location_code,
        )
    ip, device = _client(request)
    session = POSSession.objects.create(
        session_key=django_session_key, staff=staff, terminal=terminal, shift=shift, ip_address=ip,
        device_id=terminal.device_id or device[:100],
        staff_code_snapshot=staff.employee_code, staff_name_snapshot=staff.name,
        role_snapshot=staff.staff_role.name if staff.staff_role_id else staff.get_role_display(),
        store_code_snapshot=store.code, store_name_snapshot=store.name,
        location_code_snapshot=location.location_code, location_name_snapshot=location.location_name,
        terminal_code_snapshot=terminal.code, terminal_name_snapshot=terminal.name,
    )
    session.session_no = f'POSSES-{session.pk:06d}'
    session.save(update_fields=['session_no'])
    return session


def session_totals(session):
    invoices = SalesInvoice.objects.filter(pos_session=session).exclude(status__in=['cancelled', 'rejected'])
    sales = invoices.aggregate(count=Count('id'), total=Sum('total_amount'))
    refunds = SalesReturn.objects.filter(invoice__pos_session=session).aggregate(count=Count('id'), total=Sum('total_return_amount'))
    cash = sum((p.net_amount for p in POSPayment.objects.filter(session=session, tender_type_snapshot='cash', is_refund=False)), Decimal('0'))
    return {
        'sales_count': sales['count'] or 0, 'sales_amount': sales['total'] or Decimal('0'),
        'refund_count': refunds['count'] or 0, 'refund_amount': refunds['total'] or Decimal('0'), 'cash_collected': cash,
    }


def close_session(session, status='logged_out', request=None):
    for key, value in session_totals(session).items():
        setattr(session, key, value)
    session.status = status
    session.logout_time = timezone.now()
    session.save()
    if request is not None:
        audit(request, session.staff, 'logout', f'POS logout from {session.terminal_code_snapshot} ({session.session_no}).')
    return session


def session_problem(session):
    """Re-checked on every POS request so deactivating staff / terminals / stores takes effect immediately."""
    problem = terminal_problem(session.terminal)
    if problem:
        return problem
    staff = POSStaff.objects.select_related('store__location', 'staff_role').get(pk=session.staff_id)
    if staff.is_blocked:
        return 'locked', 'Account locked. Please contact Store Manager/Admin.'
    return staff_problem(staff, session.terminal)


# --- Shifts ---------------------------------------------------------------------------------------------------------

def shift_expected_cash(shift):
    cash = sum((p.net_amount for p in POSPayment.objects.filter(session__shift=shift, tender_type_snapshot='cash', is_refund=False)), Decimal('0'))
    return shift.opening_cash + cash


@transaction.atomic
def close_shift(shift, closing_staff, actual_cash, notes='', request=None):
    if shift.status != 'open':
        raise ValidationError('Only open shifts can be closed.')
    if shift.sessions.filter(status='active').exclude(staff=closing_staff).exists():
        raise ValidationError('Other staff still have open sessions on this shift. They must log out first.')
    for session in shift.sessions.filter(status='active'):
        close_session(session, request=request)
    shift.expected_cash = shift_expected_cash(shift)
    shift.actual_cash = actual_cash
    shift.cash_difference = actual_cash - shift.expected_cash
    shift.closing_staff = closing_staff
    shift.closing_time = timezone.now()
    shift.closing_notes = notes
    shift.status = 'closed'
    shift.save()
    audit(request, shift.terminal, 'update', f'Shift {shift.shift_code} closed. Expected {shift.expected_cash}, counted {actual_cash}.',
          new={'shift': shift.shift_code, 'expected_cash': str(shift.expected_cash), 'actual_cash': str(actual_cash),
               'difference': str(shift.cash_difference)})
    return shift


# --- Tenders at POS -------------------------------------------------------------------------------------------------

@dataclass
class TenderLine:
    store_tender: StoreTender
    amount: Decimal
    reference: str = ''


def active_store_tenders(store, on=None):
    return [st for st in StoreTender.objects.filter(store=store, active=True).select_related('tender') if st.is_effective(on)]


def validate_tender_lines(store, lines, bill_total):
    """Validate tender lines for a bill against the store's tender setup. Returns the change due (cash back)."""
    errors = []
    allowed = {st.pk: st for st in active_store_tenders(store)}
    for line in lines:
        st = line.store_tender
        if st.pk not in allowed:
            errors.append(f'{st.tender.name} is not an active tender for store {store.code}.')
            continue
        if line.amount <= 0:
            errors.append(f'{st.tender.name}: amount must be greater than zero.')
        if st.needs_reference and not line.reference.strip():
            errors.append(f'{st.tender.name}: a reference (transaction / approval no.) is required.')
        if st.effective_min is not None and line.amount < st.effective_min:
            errors.append(f'{st.tender.name}: minimum amount is ₹{st.effective_min}.')
        if st.effective_max is not None and line.amount > st.effective_max:
            errors.append(f'{st.tender.name}: maximum amount is ₹{st.effective_max}.')
    if len(lines) > 1:
        for line in lines:
            if not line.store_tender.can_split:
                errors.append(f'{line.store_tender.tender.name} cannot be used in a split payment.')
    paid = sum((line.amount for line in lines), Decimal('0'))
    change = Decimal('0')
    if lines and paid > bill_total:
        change = paid - bill_total
        cash_lines = [line for line in lines if line.store_tender.can_give_change]
        if not cash_lines:
            errors.append('Amount paid exceeds the bill total and none of the tenders allow change.')
        elif sum((line.amount for line in cash_lines), Decimal('0')) < change:
            errors.append('Change due is more than the amount paid in change-giving tenders.')
    if lines and paid < bill_total:
        for line in lines:
            if not line.store_tender.tender.allow_partial_payment:
                errors.append(f'{line.store_tender.tender.name} does not allow partial payment of a bill.')
    if errors:
        raise ValidationError(sorted(set(errors), key=errors.index))
    return change


def record_tender_lines(invoice, session, lines, change):
    remaining_change = change
    payments = []
    for line in lines:
        st = line.store_tender
        line_change = Decimal('0')
        if remaining_change > 0 and st.can_give_change:
            line_change = min(remaining_change, line.amount)
            remaining_change -= line_change
        payments.append(POSPayment.objects.create(
            invoice=invoice, session=session, store_tender=st, tender=st.tender,
            tender_code_snapshot=st.tender.code, tender_name_snapshot=st.tender.name,
            tender_type_snapshot=st.tender.tender_type, amount=line.amount, change_amount=line_change,
            reference=line.reference.strip(),
        ))
    return payments


def authorize_override(store, identifier, password, permission):
    """Manager approval: credentials of an active staff member of the same store holding `permission`."""
    approver = find_staff(identifier)
    if (approver is None or approver.is_blocked or not approver.is_active or approver.store_id != store.pk
            or not check_password(password or '', approver.pin_hash) or permission not in staff_permissions(approver)):
        return None
    return approver


# --- Store dashboard ------------------------------------------------------------------------------------------------

def store_kpis(store):
    today = timezone.localdate()
    todays = SalesInvoice.objects.filter(pos_terminal__store=store, sales_date__date=today).exclude(status__in=['cancelled', 'rejected'])
    return {
        'active_staff': store.pos_staff.filter(is_active=True).count(),
        'total_staff': store.pos_staff.count(),
        'active_terminals': store.pos_terminals.filter(status='active').count(),
        'total_terminals': store.pos_terminals.count(),
        'active_tenders': len(active_store_tenders(store)),
        'total_tenders': store.store_tenders.count(),
        'open_sessions': POSSession.objects.filter(terminal__store=store, status='active').count(),
        'todays_sales': todays.aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
        'todays_bills': todays.count(),
        'todays_returns': SalesReturn.objects.filter(invoice__pos_terminal__store=store, return_date__date=today).count(),
    }


def staff_can_use_terminal_q(staff):
    """Terminals a staff member may log in to (for pickers)."""
    assigned = POSStaffAssignment.objects.filter(staff=staff, active=True).values_list('terminal_id', flat=True)
    qs = POSTerminal.objects.filter(store_id=staff.store_id)
    return qs.filter(pk__in=list(assigned)) if assigned else qs
