"""REST API for the store / POS masters and POS login. Mounted at /api/retail/.

Writes go through the same forms and services as the web pages, so every rule is identical. Password hashes are never
serialized."""
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from erp.models import Location, POSSession, POSStaff, POSTerminal, Store, Tender

from . import forms as f
from . import services
from .permissions import staff_permissions


def _code(obj, attr='code'):
    return getattr(obj, attr) if obj is not None else None


def location_data(loc):
    store = Store.objects.filter(location=loc).first()
    return {'id': loc.pk, 'code': loc.location_code, 'name': loc.location_name, 'company': loc.company.company_code,
            'city': loc.city, 'status': loc.status, 'store_id': store.pk if store else None, 'store_code': _code(store)}


def store_data(store, detail=False):
    data = {'id': store.pk, 'code': store.code, 'name': store.name, 'store_type': store.store_type, 'status': store.status,
            'company': store.company.company_code, 'location_id': store.location_id,
            'location_code': _code(store.location, 'location_code'), 'location_name': _code(store.location, 'location_name'),
            'city': store.city, 'state': store.state, 'gstin': store.gstin, 'phone': store.phone, 'email': store.email}
    if detail:
        data.update({'address': store.address, 'pin_code': store.pin_code, 'country': store.country,
                     'manager_id': store.manager_id, 'opening_date': store.opening_date, 'closing_date': store.closing_date,
                     'kpis': {k: str(v) if not isinstance(v, int) else v for k, v in services.store_kpis(store).items()}})
    return data


def staff_data(staff):
    # Deliberately no pin_hash / password fields.
    return {'id': staff.pk, 'employee_code': staff.employee_code, 'name': staff.name, 'mobile': staff.mobile,
            'email': staff.email, 'designation': staff.designation,
            'role': staff.staff_role.name if staff.staff_role_id else staff.get_role_display(),
            'role_code': _code(staff.staff_role), 'store_id': staff.store_id, 'store_code': staff.store.code,
            'location_id': staff.store.location_id, 'location_code': _code(staff.store.location, 'location_code'),
            'login_id': staff.login_id, 'pos_login_enabled': staff.pos_access, 'active': staff.is_active,
            'locked': staff.is_blocked, 'last_login': staff.last_login, 'default_terminal_id': staff.default_terminal_id}


def terminal_data(t):
    return {'id': t.pk, 'code': t.code, 'name': t.name, 'store_id': t.store_id, 'store_code': t.store.code,
            'location_id': t.store.location_id, 'location_code': _code(t.store.location, 'location_code'),
            'terminal_type': t.terminal_type, 'device_id': t.device_id, 'serial_number': t.serial_number,
            'ip_address': t.ip_address, 'status': t.status, 'default_tender': _code(t.default_tender)}


def tender_data(t):
    return {'id': t.pk, 'code': t.code, 'name': t.name, 'tender_type': t.tender_type, 'status': t.status,
            'requires_reference': t.requires_reference, 'requires_approval': t.requires_approval,
            'allow_refund': t.allow_refund, 'allow_change': t.allow_change, 'allow_split_payment': t.allow_split_payment,
            'allow_partial_payment': t.allow_partial_payment, 'minimum_amount': t.minimum_amount,
            'maximum_amount': t.maximum_amount, 'is_cash': t.is_cash, 'is_card': t.is_card, 'is_digital': t.is_digital,
            'is_online': t.is_online}


def store_tender_data(st):
    return {'id': st.pk, 'store_id': st.store_id, 'tender_id': st.tender_id, 'tender_code': st.tender.code,
            'tender_name': st.tender.name, 'tender_type': st.tender.tender_type, 'active': st.active,
            'effective': st.is_effective(), 'default': st.is_default, 'sequence': st.sequence,
            'requires_reference': st.needs_reference, 'allow_refund': st.can_refund, 'allow_change': st.can_give_change,
            'allow_split_payment': st.can_split, 'minimum_amount': st.effective_min, 'maximum_amount': st.effective_max}


def _require_admin(request):
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied('Only administrators can change store and POS setup.')


def _invalid(form):
    return Response({'errors': form.errors.get_json_data()}, status=status.HTTP_400_BAD_REQUEST)


def _data(request):
    data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
    # Booleans arrive as JSON true/false; unchecked checkboxes must simply be absent for Django forms.
    return {k: v for k, v in data.items() if v is not False}


@api_view(['GET'])
def locations(request):
    return Response([location_data(loc) for loc in Location.objects.select_related('company').order_by('location_code')])


@api_view(['GET'])
def stores(request):
    return Response([store_data(s) for s in Store.objects.select_related('company', 'location').order_by('code')])


@api_view(['GET'])
def store_detail(request, pk):
    return Response(store_data(get_object_or_404(Store.objects.select_related('company', 'location'), pk=pk), detail=True))


@api_view(['GET', 'POST'])
def store_staff(request, pk):
    store = get_object_or_404(Store.objects.select_related('location'), pk=pk)
    if request.method == 'GET':
        return Response([staff_data(s) for s in store.pos_staff.select_related('store__location', 'staff_role')])
    _require_admin(request)
    form = f.StaffForm(_data(request), fixed_store=store)
    if not form.is_valid():
        return _invalid(form)
    staff = form.save(commit=False)
    staff.store = store
    try:
        services.save_staff(staff, request, location=store.location, password=form.cleaned_data.get('password') or None)
    except ValidationError as exc:
        return Response({'errors': exc.message_dict if hasattr(exc, 'error_dict') else exc.messages}, status=status.HTTP_400_BAD_REQUEST)
    return Response(staff_data(staff), status=status.HTTP_201_CREATED)


@api_view(['GET', 'POST'])
def store_terminals(request, pk):
    store = get_object_or_404(Store.objects.select_related('location'), pk=pk)
    if request.method == 'GET':
        return Response([terminal_data(t) for t in store.pos_terminals.select_related('store__location', 'default_tender')])
    _require_admin(request)
    form = f.TerminalForm(_data(request), fixed_store=store)
    if not form.is_valid():
        return _invalid(form)
    terminal = services.save_with_audit(form.save(commit=False), services.TERMINAL_AUDIT_FIELDS, request, 'POS terminal')
    return Response(terminal_data(terminal), status=status.HTTP_201_CREATED)


@api_view(['GET', 'POST'])
def store_tenders(request, pk):
    store = get_object_or_404(Store, pk=pk)
    if request.method == 'GET':
        return Response([store_tender_data(st) for st in store.store_tenders.select_related('tender')])
    _require_admin(request)
    form = f.StoreTenderForm(_data(request), store=store)
    if not form.is_valid():
        return _invalid(form)
    try:
        st = services.assign_store_tender(form.save(commit=False), request)
    except ValidationError as exc:
        return Response({'errors': exc.message_dict}, status=status.HTTP_400_BAD_REQUEST)
    return Response(store_tender_data(st), status=status.HTTP_201_CREATED)


@api_view(['GET'])
def staff_list(request):
    qs = POSStaff.objects.select_related('store__location', 'staff_role').order_by('employee_code')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    return Response([staff_data(s) for s in qs])


@api_view(['GET'])
def staff_detail(request, pk):
    return Response(staff_data(get_object_or_404(POSStaff.objects.select_related('store__location', 'staff_role'), pk=pk)))


@api_view(['GET'])
def terminals(request):
    qs = POSTerminal.objects.select_related('store__location', 'default_tender').order_by('store__code', 'code')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    return Response([terminal_data(t) for t in qs])


@api_view(['GET'])
def terminal_detail(request, pk):
    return Response(terminal_data(get_object_or_404(POSTerminal.objects.select_related('store__location', 'default_tender'), pk=pk)))


@api_view(['GET'])
def tenders(request):
    return Response([tender_data(t) for t in Tender.objects.order_by('name')])


def session_payload(session):
    staff = session.staff
    return {
        'success': True, 'session_id': session.pk, 'session_no': session.session_no,
        'staff_id': staff.pk, 'staff_code': session.staff_code_snapshot, 'staff_name': session.staff_name_snapshot,
        'store_id': session.terminal.store_id, 'store_code': session.store_code_snapshot, 'store_name': session.store_name_snapshot,
        'location_id': session.terminal.store.location_id, 'location_code': session.location_code_snapshot,
        'location_name': session.location_name_snapshot,
        'terminal_id': session.terminal_id, 'terminal_code': session.terminal_code_snapshot,
        'terminal_name': session.terminal_name_snapshot, 'shift': session.shift.shift_code,
        'role': session.role_snapshot, 'permissions': sorted(staff_permissions(staff)),
        'login_time': session.login_time,
    }


@api_view(['POST'])
def staff_login(request):
    """Body: {login_id, password, terminal_id}. On success the POS session is also bound to the caller's web session."""
    terminal = POSTerminal.objects.select_related('store__location').filter(pk=request.data.get('terminal_id')).first()
    try:
        if not request.session.session_key:
            request.session.save()
        session = services.authenticate(request.data.get('login_id'), request.data.get('password'), terminal,
                                        request=request, django_session_key=request.session.session_key)
    except services.POSLoginError as exc:
        return Response({'success': False, 'code': exc.code, 'detail': exc.message}, status=status.HTTP_403_FORBIDDEN)
    request.session['pos_session_id'] = session.pk
    return Response(session_payload(session))


@api_view(['POST'])
def staff_logout(request):
    session = get_object_or_404(POSSession, pk=request.data.get('session_id') or request.session.get('pos_session_id'), status='active')
    if session.session_key != request.session.session_key and not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied('This POS session belongs to another login.')
    services.close_session(session, request=request)
    request.session.pop('pos_session_id', None)
    return Response({'success': True, 'session_id': session.pk, 'sales_count': session.sales_count,
                     'sales_amount': session.sales_amount, 'cash_collected': session.cash_collected})


@api_view(['GET'])
def store_tender_options(request, pk):
    """Tenders currently usable at a store (active + effective), for POS clients."""
    store = get_object_or_404(Store, pk=pk)
    return Response([store_tender_data(st) for st in services.active_store_tenders(store)])
