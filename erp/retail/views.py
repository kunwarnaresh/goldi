"""Store & POS administration pages: Location, Store (the central hub), Staff, POS terminal, Tender, Store tender,
staff POS assignments, POS roles, sessions, shifts, Excel import/export and the audit trail."""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from erp.models import (
    AuditLog, Location, POSPayment, POSRole, POSSession, POSShift, POSStaff, POSStaffAssignment, POSTerminal,
    RetailImportBatch, SalesInvoice, Store, StoreTender, Tender,
)
from inventory.templatetags.inventory_tags import badge

from . import forms as f
from . import imports, services
from .permissions import LABELS, staff_permissions

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
RETAIL_TABLES = [m._meta.db_table for m in (Location, Store, POSStaff, POSTerminal, Tender, StoreTender, POSStaffAssignment,
                                            POSRole, RetailImportBatch)]

NAV = [
    ('retail_home', 'Overview'), ('retail_stores', 'Stores'), ('retail_locations', 'Locations'), ('retail_staff', 'Staff'),
    ('retail_terminals', 'POS terminals'), ('retail_tenders', 'Tenders'), ('retail_store_tenders', 'Store tenders'),
    ('retail_assignments', 'Staff POS access'), ('retail_roles', 'POS roles'), ('retail_sessions', 'POS sessions'),
    ('retail_shifts', 'POS shifts'), ('retail_imports', 'Import / export'), ('retail_audit', 'Audit trail'),
]


def can_manage(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def retail_view(view):
    """Any signed-in user can read; changing data (POST) needs an ERP staff/admin account."""
    @wraps(view)
    @login_required(login_url='login')
    def wrapper(request, *args, **kwargs):
        if request.method == 'POST' and not can_manage(request.user):
            raise PermissionDenied('Only administrators can change store and POS setup.')
        return view(request, *args, **kwargs)
    return wrapper


def page(request, template, **context):
    context.setdefault('can_manage', can_manage(request.user))
    nav = [(reverse(name), label) for name, label in NAV]
    current = max((url for url, _ in nav if request.path.startswith(url)), key=len, default=None)
    context['retail_nav'] = [(url, label, url == current) for url, label in nav]
    return render(request, f'erp/retail/{template}', context)


def status_badge(active, yes='ACTIVE', no='INACTIVE'):
    return badge(yes if active else no)


def link(url, text):
    return format_html('<a class="text-sky-700 hover:underline" href="{}">{}</a>', url, text)


def _apply_errors(form, exc):
    for field, errors in getattr(exc, 'error_dict', {None: exc.messages}).items():
        form.add_error(field if field in form.fields else None, errors)


def xlsx_response(content, name):
    response = HttpResponse(content, content_type=XLSX)
    response['Content-Disposition'] = f'attachment; filename="{name}.xlsx"'
    return response


def _export(request, kind, queryset=None):
    services.audit(request, RetailImportBatch(), 'export', f'Exported {kind} to Excel.')
    return xlsx_response(imports.export_workbook(kind, queryset), f'{kind}-{timezone.localdate():%Y%m%d}')


def list_page(request, *, title, subtitle, headers, rows, new_url=None, new_label='New', export_kind=None, filters=None,
              extra_actions=None):
    controls = []
    for name, spec in filters or []:
        value = request.GET.get(name, '')
        if isinstance(spec, str):
            controls.append({'name': name, 'placeholder': spec, 'value': value})
        else:
            controls.append({'name': name, 'value': value, 'options': [(str(k), label) for k, label in spec]})
    params = request.GET.copy()
    params['export'] = '1'
    return page(request, 'list.html', title=title, subtitle=subtitle, headers=headers, rows=rows, new_url=new_url,
                new_label=new_label, export_kind=export_kind, filters=controls, extra_actions=extra_actions or [],
                export_query=params.urlencode())


def form_page(request, form, *, title, eyebrow, subtitle='', back_url, submit='Save', panels=None, actions=None, template='form.html', **extra):
    return page(request, template, form=form, sections=_sections(form), title=title, eyebrow=eyebrow, subtitle=subtitle,
                back_url=back_url, submit=submit, panels=panels or [], actions=actions or [], **extra)


def _sections(form):
    names = set(form.fields)
    sections = [(title, [form[n] for n in fields if n in names]) for title, fields in getattr(form, 'SECTIONS', [])]
    listed = {n for _, fields in getattr(form, 'SECTIONS', []) for n in fields}
    rest = [form[n] for n in form.fields if n not in listed and not form[n].is_hidden]
    if rest:
        sections.append(('Other', rest))
    return sections


# --- Overview --------------------------------------------------------------------------------------------------------

@retail_view
def home(request):
    stores = Store.objects.select_related('location').annotate(
        staff_count=Count('pos_staff', distinct=True), terminal_count=Count('pos_terminals', distinct=True),
        tender_count=Count('store_tenders', distinct=True)).order_by('code')
    issues = []
    for store in stores:
        if store.location_id is None:
            issues.append((store, 'No location assigned: staff cannot log in to its POS terminals.'))
        elif not store.tender_count:
            issues.append((store, 'No tenders assigned: POS cannot take payment.'))
    return page(request, 'home.html', stores=stores, issues=issues, counts={
        'locations': Location.objects.count(), 'stores': stores.count(), 'staff': POSStaff.objects.filter(is_active=True).count(),
        'terminals': POSTerminal.objects.filter(status='active').count(), 'tenders': Tender.objects.filter(status='active').count(),
        'sessions': POSSession.objects.filter(status='active').count(),
    })


# --- Locations -------------------------------------------------------------------------------------------------------

@retail_view
def location_list(request):
    if request.GET.get('export'):
        return _export(request, 'locations')
    qs = Location.objects.select_related('company', 'store').order_by('location_code')
    if request.GET.get('q'):
        q = request.GET['q']
        qs = qs.filter(Q(location_code__icontains=q) | Q(location_name__icontains=q) | Q(city__icontains=q))
    rows = []
    for loc in qs:
        store = getattr(loc, 'store', None)
        rows.append({'cells': [link(reverse('retail_location_card', args=[loc.pk]), loc.location_code), loc.location_name,
                               loc.company.company_code, loc.city,
                               link(reverse('retail_store_card', args=[store.pk]), f'{store.code} · {store.name}') if store else badge('UNASSIGNED', 'No store'),
                               badge(loc.status.upper())]})
    return list_page(request, title='Locations', subtitle='Each location backs exactly one store.',
                     headers=['Code', 'Name', 'Company', 'City', 'Store', 'Status'], rows=rows,
                     new_url=reverse('retail_location_new'), export_kind='locations', filters=[('q', 'Search code, name or city')])


@retail_view
def location_card(request, pk=None):
    location = get_object_or_404(Location.objects.select_related('company'), pk=pk) if pk else None
    form = f.LocationForm(request.POST or None, instance=location)
    if request.method == 'POST' and form.is_valid():
        location = services.save_with_audit(form.save(commit=False), ['location_code', 'location_name', 'status', 'company'], request, 'Location')
        messages.success(request, f'Location {location.location_code} saved.')
        return redirect('retail_location_card', location.pk)
    panels, actions = [], []
    if location:
        store = Store.objects.filter(location=location).first()
        if store:
            actions = [(reverse('retail_store_card', args=[store.pk]), 'Open store', 'fa-store')]
            panels = [
                {'title': 'Store', 'rows': [[link(reverse('retail_store_card', args=[store.pk]), store.code), store.name, badge(store.status.upper())]]},
                {'title': 'Staff', 'rows': [[link(reverse('retail_staff_card', args=[s.pk]), s.employee_code), s.name, status_badge(s.is_active)]
                                            for s in store.pos_staff.all()[:20]],
                 'more': (reverse('retail_staff') + f'?store={store.pk}', 'All staff')},
                {'title': 'POS terminals', 'rows': [[link(reverse('retail_terminal_card', args=[t.pk]), t.code), t.name, badge(t.status.upper())]
                                                    for t in store.pos_terminals.all()],
                 'more': (reverse('retail_terminals') + f'?store={store.pk}', 'All terminals')},
            ]
        else:
            panels = [{'title': 'Store', 'rows': [], 'empty': 'No store uses this location yet.',
                       'more': (reverse('retail_store_new') + f'?location={location.pk}', 'Create store for this location')}]
    return form_page(request, form, title=f'{location.location_code} · {location.location_name}' if location else 'New location',
                     eyebrow='Location card', back_url=reverse('retail_locations'), panels=panels, actions=actions)


# --- Stores ----------------------------------------------------------------------------------------------------------

@retail_view
def store_list(request):
    if request.GET.get('export'):
        return _export(request, 'stores')
    qs = Store.objects.select_related('location', 'company').annotate(
        staff_count=Count('pos_staff', distinct=True), terminal_count=Count('pos_terminals', distinct=True),
        tender_count=Count('store_tenders', distinct=True)).order_by('code')
    if request.GET.get('q'):
        q = request.GET['q']
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(city__icontains=q))
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    rows = [{'cells': [link(reverse('retail_store_card', args=[s.pk]), s.code), s.name, s.get_store_type_display(),
                       s.location.location_code if s.location else badge('MISSING', 'No location'), s.city,
                       s.staff_count, s.terminal_count, s.tender_count, badge(s.status.upper())]} for s in qs]
    return list_page(request, title='Stores', subtitle='The store is the control point for its location, staff, POS terminals and tenders.',
                     headers=['Code', 'Name', 'Type', 'Location', 'City', 'Staff', 'Terminals', 'Tenders', 'Status'], rows=rows,
                     new_url=reverse('retail_store_new'), export_kind='stores',
                     filters=[('q', 'Search code, name or city'), ('status', Store.STATUS_CHOICES)])


@retail_view
def store_edit(request, pk=None):
    store = get_object_or_404(Store, pk=pk) if pk else None
    initial = {'location': request.GET.get('location')} if not store and request.GET.get('location') else None
    form = f.StoreForm(request.POST or None, instance=store, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            store = services.save_with_audit(form.save(commit=False), services.STORE_AUDIT_FIELDS, request, 'Store')
        except ValidationError as exc:
            _apply_errors(form, exc)
        else:
            messages.success(request, f'Store {store.code} saved.')
            return redirect('retail_store_card', store.pk)
    return form_page(request, form, title=f'Edit {store.code}' if store else 'New store', eyebrow='Store master',
                     back_url=reverse('retail_store_card', args=[store.pk]) if store else reverse('retail_stores'))


@retail_view
def store_card(request, pk):
    store = get_object_or_404(Store.objects.select_related('location', 'company', 'manager', 'branch'), pk=pk)
    staff = store.pos_staff.select_related('staff_role').order_by('name')
    terminals = store.pos_terminals.select_related('default_tender').order_by('code')
    tenders = store.store_tenders.select_related('tender')
    sessions = POSSession.objects.filter(terminal__store=store).order_by('-login_time')[:10]
    return page(request, 'store_card.html', store=store, kpis=services.store_kpis(store), staff=staff[:12],
                terminals=terminals, tenders=tenders, sessions=sessions, staff_total=staff.count())


@retail_view
def store_add_staff(request, pk):
    store = get_object_or_404(Store.objects.select_related('location'), pk=pk)
    if store.location_id is None:
        messages.error(request, 'Assign a location to this store before adding staff.')
        return redirect('retail_store_card', store.pk)
    return _staff_form(request, None, fixed_store=store)


@retail_view
def store_add_terminal(request, pk):
    store = get_object_or_404(Store.objects.select_related('location'), pk=pk)
    if store.location_id is None:
        messages.error(request, 'Assign a location to this store before adding POS terminals.')
        return redirect('retail_store_card', store.pk)
    return _terminal_form(request, None, fixed_store=store)


@retail_view
def store_add_tender(request, pk):
    store = get_object_or_404(Store, pk=pk)
    return _store_tender_form(request, None, store)


# --- Staff -----------------------------------------------------------------------------------------------------------

@retail_view
def staff_list(request):
    qs = POSStaff.objects.select_related('store__location', 'staff_role').order_by('employee_code')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    if request.GET.get('q'):
        q = request.GET['q']
        qs = qs.filter(Q(employee_code__icontains=q) | Q(name__icontains=q) | Q(login_id__icontains=q) | Q(mobile__icontains=q))
    status = request.GET.get('status')
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'locked':
        qs = qs.filter(is_blocked=True)
    if request.GET.get('export'):
        return _export(request, 'staff', qs)
    rows = []
    for s in qs:
        state = badge('BLOCKED', 'Locked') if s.is_blocked else status_badge(s.is_active)
        rows.append({'cells': [link(reverse('retail_staff_card', args=[s.pk]), s.employee_code), s.employee_code, s.name,
                               s.staff_role.name if s.staff_role else s.get_role_display(), s.login_id or '—',
                               f'{s.store.code} · {s.store.name}', s.store.location.location_code if s.store.location else '—',
                               status_badge(s.pos_access, 'ENABLED', 'DISABLED'), state,
                               timezone.localtime(s.last_login).strftime('%d %b %Y %H:%M') if s.last_login else '—']})
    return list_page(request, title='Staff', subtitle='One staff member belongs to one store at a time.',
                     headers=['Staff ID', 'Employee code', 'Staff name', 'Role', 'Login ID', 'Store', 'Location', 'POS login', 'Status', 'Last login'],
                     rows=rows, new_url=reverse('retail_staff_new'), export_kind='staff',
                     filters=[('q', 'Search code, name, login or mobile'), ('store', [(s.pk, s.code) for s in Store.objects.order_by('code')]),
                              ('status', [('active', 'Active'), ('inactive', 'Inactive'), ('locked', 'Locked')])])


def _staff_form(request, staff, fixed_store=None):
    form = f.StaffForm(request.POST or None, instance=staff, fixed_store=fixed_store)
    if request.method == 'POST' and form.is_valid():
        instance = form.save(commit=False)
        instance.store = form.cleaned_data['store']
        try:
            services.save_staff(instance, request, location=form.cleaned_data.get('location'),
                                password=form.cleaned_data.get('password') or None)
        except ValidationError as exc:
            _apply_errors(form, exc)
        else:
            for warning in getattr(form, 'warnings', []):
                messages.warning(request, warning)
            messages.success(request, f'Staff {instance.employee_code} saved.')
            return redirect('retail_staff_card', instance.pk)
    back = reverse('retail_store_card', args=[fixed_store.pk]) if fixed_store else (
        reverse('retail_staff_card', args=[staff.pk]) if staff else reverse('retail_staff'))
    title = f'Add staff to {fixed_store.name}' if fixed_store else (f'{staff.employee_code} · {staff.name}' if staff else 'New staff')
    return form_page(request, form, title=title, eyebrow='Staff card', back_url=back, template='staff_form.html',
                     store_locations=form.store_locations)


@retail_view
def staff_new(request):
    return _staff_form(request, None)


@retail_view
def staff_edit(request, pk):
    return _staff_form(request, get_object_or_404(POSStaff, pk=pk))


@retail_view
def staff_card(request, pk):
    staff = get_object_or_404(POSStaff.objects.select_related('store__location', 'staff_role', 'default_terminal'), pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'activate':
            services.set_staff_active(staff, True, request)
        elif action == 'deactivate':
            services.set_staff_active(staff, False, request)
        elif action == 'lock':
            services.lock_account(staff, request)
        elif action == 'unlock':
            services.unlock_account(staff, request)
        elif action in ('enable_pos', 'disable_pos'):
            staff.pos_access = action == 'enable_pos'
            staff.save(update_fields=['pos_access'])
            if not staff.pos_access:
                for session in POSSession.objects.filter(staff=staff, status='active'):
                    services.close_session(session, status='force_closed')
            services.audit(request, staff, 'update', f'POS login {"enabled" if staff.pos_access else "disabled"}.', new={'pos_access': staff.pos_access})
        else:
            raise Http404
        messages.success(request, 'Staff updated.')
        return redirect('retail_staff_card', staff.pk)
    assignments = staff.assignments.select_related('terminal').order_by('-active', 'terminal__code')
    sessions = staff.pos_sessions.order_by('-login_time')[:15]
    sales = SalesInvoice.objects.filter(cashier_staff=staff).order_by('-sales_date')[:15]
    allowed = services.staff_can_use_terminal_q(staff).filter(status='active')
    permissions = sorted(LABELS.get(p, p) for p in staff_permissions(staff))
    audit_rows = AuditLog.objects.filter(table_name=POSStaff._meta.db_table, record_id=str(staff.pk)).select_related('actor').order_by('-created_at')[:15]
    return page(request, 'staff_card.html', staff=staff, assignments=assignments, sessions=sessions, sales=sales,
                allowed=allowed, permissions=permissions, audit_rows=audit_rows, max_attempts=services.MAX_FAILED_LOGINS)


@retail_view
def staff_reset_password(request, pk):
    staff = get_object_or_404(POSStaff, pk=pk)
    form = f.PasswordResetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        services.set_password(staff, form.cleaned_data['password'], request, reset=True,
                              require_change=form.cleaned_data['require_change'])
        if staff.is_blocked:
            services.unlock_account(staff, request)
        messages.success(request, f'Password reset for {staff.employee_code}.')
        return redirect('retail_staff_card', staff.pk)
    return form_page(request, form, title=f'Reset password · {staff.name}', eyebrow='Staff security',
                     back_url=reverse('retail_staff_card', args=[staff.pk]), submit='Reset password')


@retail_view
def assignment_edit(request, staff_pk=None, pk=None):
    assignment = get_object_or_404(POSStaffAssignment.objects.select_related('staff'), pk=pk) if pk else None
    staff = assignment.staff if assignment else get_object_or_404(POSStaff, pk=staff_pk)
    form = f.AssignmentForm(request.POST or None, instance=assignment, staff=staff)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = form.save()
        except ValidationError as exc:
            _apply_errors(form, exc)
        else:
            if saved.primary_terminal and saved.active:
                staff.default_terminal = saved.terminal
                staff.save(update_fields=['default_terminal'])
                staff.assignments.exclude(pk=saved.pk).update(primary_terminal=False)
            services.audit(request, staff, 'assign' if saved.active else 'unassign',
                           f'{staff.employee_code} {"assigned to" if saved.active else "removed from"} terminal {saved.terminal.code}.',
                           new={'terminal': saved.terminal.code, 'role': saved.role, 'active': saved.active})
            messages.success(request, 'POS terminal access saved.')
            return redirect('retail_staff_card', staff.pk)
    return form_page(request, form, title=f'POS terminal access · {staff.name}', eyebrow=f'Store {staff.store.code}',
                     subtitle='Leave a staff member without assignments to allow every terminal of their store.',
                     back_url=reverse('retail_staff_card', args=[staff.pk]))


@retail_view
def assignment_list(request):
    qs = POSStaffAssignment.objects.select_related('staff', 'terminal__store').order_by('terminal__store__code', 'staff__employee_code')
    if request.GET.get('store'):
        qs = qs.filter(terminal__store_id=request.GET['store'])
    if request.GET.get('export'):
        return _export(request, 'staff-terminals', qs)
    rows = [{'cells': [link(reverse('retail_staff_card', args=[a.staff_id]), a.staff.employee_code), a.staff.name,
                       a.terminal.store.code, link(reverse('retail_terminal_card', args=[a.terminal_id]), a.terminal.code),
                       a.get_role_display(), 'Yes' if a.primary_terminal else '', a.effective_from, a.effective_to or '',
                       status_badge(a.is_effective()),
                       link(reverse('retail_assignment_edit', args=[a.pk]), 'Edit')]} for a in qs]
    return list_page(request, title='Staff POS terminal access', subtitle='Optional allow-list of terminals per staff member (same store only).',
                     headers=['Staff', 'Name', 'Store', 'Terminal', 'Role', 'Default', 'From', 'To', 'Status', ''], rows=rows,
                     export_kind='staff-terminals', filters=[('store', [(s.pk, s.code) for s in Store.objects.order_by('code')])])


# --- POS terminals ---------------------------------------------------------------------------------------------------

@retail_view
def terminal_list(request):
    qs = POSTerminal.objects.select_related('store__location', 'default_tender').order_by('store__code', 'code')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    if request.GET.get('export'):
        return _export(request, 'terminals', qs)
    rows = [{'cells': [link(reverse('retail_terminal_card', args=[t.pk]), t.code), t.name, f'{t.store.code} · {t.store.name}',
                       t.store.location.location_code if t.store.location else '—', t.get_terminal_type_display(),
                       t.device_id or '—', t.default_tender.name if t.default_tender else '—', badge(t.status.upper()),
                       timezone.localtime(t.last_login).strftime('%d %b %H:%M') if t.last_login else '—']} for t in qs]
    return list_page(request, title='POS terminals', subtitle='Every terminal belongs to exactly one store.',
                     headers=['Code', 'Name', 'Store', 'Location', 'Type', 'Device', 'Default tender', 'Status', 'Last login'],
                     rows=rows, new_url=reverse('retail_terminal_new'), export_kind='terminals',
                     filters=[('store', [(s.pk, s.code) for s in Store.objects.order_by('code')]), ('status', POSTerminal.STATUS_CHOICES)])


def _terminal_form(request, terminal, fixed_store=None):
    form = f.TerminalForm(request.POST or None, instance=terminal, fixed_store=fixed_store)
    if request.method == 'POST' and form.is_valid():
        try:
            terminal = services.save_with_audit(form.save(commit=False), services.TERMINAL_AUDIT_FIELDS, request, 'POS terminal')
        except ValidationError as exc:
            _apply_errors(form, exc)
        else:
            messages.success(request, f'POS terminal {terminal.code} saved.')
            return redirect('retail_terminal_card', terminal.pk)
    back = reverse('retail_store_card', args=[fixed_store.pk]) if fixed_store else (
        reverse('retail_terminal_card', args=[terminal.pk]) if terminal else reverse('retail_terminals'))
    title = f'Add POS terminal to {fixed_store.name}' if fixed_store else (f'{terminal.code} · {terminal.name}' if terminal else 'New POS terminal')
    return form_page(request, form, title=title, eyebrow='POS terminal card', back_url=back, template='terminal_form.html',
                     store_locations=form.store_locations, fixed_store=fixed_store)


@retail_view
def terminal_new(request):
    return _terminal_form(request, None)


@retail_view
def terminal_edit(request, pk):
    return _terminal_form(request, get_object_or_404(POSTerminal, pk=pk))


@retail_view
def terminal_card(request, pk):
    terminal = get_object_or_404(POSTerminal.objects.select_related('store__location', 'default_tender'), pk=pk)
    if request.method == 'POST':
        status = {'activate': 'active', 'deactivate': 'inactive', 'block': 'blocked'}.get(request.POST.get('action'))
        if status is None:
            raise Http404
        services.set_terminal_status(terminal, status, request)
        messages.success(request, f'Terminal {terminal.code} is now {terminal.get_status_display().lower()}.')
        return redirect('retail_terminal_card', terminal.pk)
    assigned = terminal.staff_assignments.select_related('staff').order_by('-active', 'staff__name')
    unrestricted = terminal.store.pos_staff.filter(is_active=True, pos_access=True).exclude(
        pk__in=POSStaffAssignment.objects.filter(active=True).values('staff_id'))
    sessions = terminal.pos_sessions.order_by('-login_time')[:15]
    shifts = terminal.shifts.order_by('-opening_time')[:10]
    return page(request, 'terminal_card.html', terminal=terminal, assigned=assigned, unrestricted=unrestricted,
                sessions=sessions, shifts=shifts)


# --- Tenders ---------------------------------------------------------------------------------------------------------

@retail_view
def tender_list(request):
    if request.GET.get('export'):
        return _export(request, 'tenders')
    qs = Tender.objects.annotate(store_count=Count('store_tenders')).order_by('name')
    yes = lambda v: 'Yes' if v else ''
    rows = [{'cells': [link(reverse('retail_tender_card', args=[t.pk]), t.code), t.name, t.get_tender_type_display(),
                       yes(t.requires_reference), yes(t.allow_refund), yes(t.allow_change), yes(t.allow_split_payment),
                       t.store_count, badge(t.status.upper())]} for t in qs]
    return list_page(request, title='Tender master', subtitle='Payment tenders are maintained here and opted into per store.',
                     headers=['Code', 'Name', 'Type', 'Reference', 'Refund', 'Change', 'Split', 'Stores', 'Status'], rows=rows,
                     new_url=reverse('retail_tender_new'), export_kind='tenders')


@retail_view
def tender_card(request, pk=None):
    tender = get_object_or_404(Tender, pk=pk) if pk else None
    form = f.TenderForm(request.POST or None, instance=tender)
    if request.method == 'POST' and form.is_valid():
        was_active = tender.status == 'active' if tender else None
        tender = services.save_with_audit(form.save(commit=False), services.TENDER_AUDIT_FIELDS, request, 'Tender')
        if was_active is not None and was_active != (tender.status == 'active'):
            services.audit(request, tender, 'activate' if tender.status == 'active' else 'deactivate', f'Tender {tender.code} {tender.status}.')
        messages.success(request, f'Tender {tender.code} saved.')
        return redirect('retail_tender_card', tender.pk)
    panels = []
    if tender:
        panels = [{'title': 'Stores using this tender', 'rows': [
            [link(reverse('retail_store_card', args=[st.store_id]), st.store.code), st.store.name,
             'Default' if st.is_default else '', status_badge(st.is_effective())]
            for st in tender.store_tenders.select_related('store')], 'empty': 'Not assigned to any store yet.'}]
    return form_page(request, form, title=f'{tender.code} · {tender.name}' if tender else 'New tender', eyebrow='Tender card',
                     back_url=reverse('retail_tenders'), panels=panels)


@retail_view
def store_tender_list(request):
    qs = StoreTender.objects.select_related('store', 'tender').order_by('store__code', 'sequence')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    if request.GET.get('export'):
        return _export(request, 'store-tenders', qs)
    rows = [{'cells': [link(reverse('retail_store_card', args=[st.store_id]), st.store.code), st.tender.name,
                       st.tender.get_tender_type_display(), 'Yes' if st.is_default else '', st.sequence,
                       status_badge(st.is_effective()), link(reverse('retail_store_tender_edit', args=[st.pk]), 'Edit')]} for st in qs]
    return list_page(request, title='Store tenders', subtitle='Which tenders each store accepts.',
                     headers=['Store', 'Tender', 'Type', 'Default', 'Sequence', 'Status', ''], rows=rows, export_kind='store-tenders',
                     filters=[('store', [(s.pk, s.code) for s in Store.objects.order_by('code')])])


def _store_tender_form(request, store_tender, store):
    form = f.StoreTenderForm(request.POST or None, instance=store_tender, store=store)
    if request.method == 'POST' and form.is_valid():
        try:
            services.assign_store_tender(form.save(commit=False), request)
        except ValidationError as exc:
            _apply_errors(form, exc)
        else:
            messages.success(request, 'Store tender saved.')
            return redirect('retail_store_card', store.pk)
    title = f'{store_tender.tender.name} at {store.name}' if store_tender else f'Add tender to {store.name}'
    return form_page(request, form, title=title, eyebrow=f'Store {store.code}', back_url=reverse('retail_store_card', args=[store.pk]),
                     template='store_tender_form.html', tender_rules=form.tender_rules)


@retail_view
def store_tender_edit(request, pk):
    store_tender = get_object_or_404(StoreTender.objects.select_related('store', 'tender'), pk=pk)
    return _store_tender_form(request, store_tender, store_tender.store)


# --- Roles -----------------------------------------------------------------------------------------------------------

@retail_view
def role_list(request):
    qs = POSRole.objects.annotate(staff_count=Count('staff'))
    rows = [{'cells': [link(reverse('retail_role_card', args=[r.pk]), r.code), r.name, r.get_base_role_display(),
                       'Yes' if r.pos_access else 'No', len(r.permissions or []), r.staff_count, status_badge(r.is_active)]} for r in qs]
    return list_page(request, title='POS roles', subtitle='Configurable permissions for POS staff.',
                     headers=['Code', 'Name', 'Category', 'POS login', 'Permissions', 'Staff', 'Status'], rows=rows,
                     new_url=reverse('retail_role_new'))


@retail_view
def role_card(request, pk=None):
    role = get_object_or_404(POSRole, pk=pk) if pk else None
    form = f.POSRoleForm(request.POST or None, instance=role, initial={'permissions': role.permissions} if role else None)
    if request.method == 'POST' and form.is_valid():
        role = services.save_with_audit(form.save(commit=False), ['code', 'name', 'pos_access', 'is_active', 'permissions'], request, 'POS role')
        if role.staff.exists():
            POSStaff.objects.filter(staff_role=role).update(role=role.base_role)
        messages.success(request, f'Role {role.name} saved.')
        return redirect('retail_role_card', role.pk)
    return form_page(request, form, title=role.name if role else 'New POS role', eyebrow='POS role', back_url=reverse('retail_roles'))


# --- Sessions & shifts -----------------------------------------------------------------------------------------------

@retail_view
def session_list(request):
    qs = POSSession.objects.select_related('staff', 'terminal__store').order_by('-login_time')
    for key, field in (('store', 'terminal__store_id'), ('staff', 'staff_id'), ('terminal', 'terminal_id'), ('status', 'status')):
        if request.GET.get(key):
            qs = qs.filter(**{field: request.GET[key]})
    if request.method == 'POST':
        session = get_object_or_404(POSSession, pk=request.POST.get('session'), status='active')
        services.close_session(session, status='force_closed', request=request)
        messages.success(request, f'Session {session} closed.')
        return redirect(request.get_full_path())
    rows = []
    for s in qs[:300]:
        close = ''
        if s.status == 'active' and can_manage(request.user):
            close = format_html('<form method="post">{}<input type="hidden" name="session" value="{}"><button class="text-rose-600 text-xs">Close</button></form>',
                                format_html('<input type="hidden" name="csrfmiddlewaretoken" value="{}">', get_token(request)), s.pk)
        rows.append({'cells': [s.session_no or s.pk, s.staff_code_snapshot or s.staff.employee_code, s.staff_name_snapshot or s.staff.name,
                               s.store_code_snapshot or s.terminal.store.code, s.location_code_snapshot or '—',
                               s.terminal_code_snapshot or s.terminal.code,
                               timezone.localtime(s.login_time).strftime('%d %b %H:%M'),
                               timezone.localtime(s.logout_time).strftime('%d %b %H:%M') if s.logout_time else '—',
                               str(s.duration).split('.')[0], s.sales_count, s.sales_amount, s.cash_collected,
                               badge('OPEN' if s.status == 'active' else s.status.upper(), s.get_status_display()), close]})
    return list_page(request, title='POS sessions', subtitle='Login-to-logout history with snapshots of staff, store, location and terminal.',
                     headers=['Session', 'Staff', 'Name', 'Store', 'Location', 'Terminal', 'Login', 'Logout', 'Duration', 'Bills',
                              'Sales', 'Cash', 'Status', ''], rows=rows,
                     filters=[('store', [(s.pk, s.code) for s in Store.objects.order_by('code')]),
                              ('status', POSSession.STATUS_CHOICES)])


@retail_view
def shift_list(request):
    qs = POSShift.objects.select_related('store', 'terminal', 'opening_staff', 'closing_staff').order_by('-opening_time')
    if request.GET.get('store'):
        qs = qs.filter(store_id=request.GET['store'])
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    rows = [{'cells': [link(reverse('retail_shift_detail', args=[s.pk]), s.shift_code), s.store.code, s.terminal.code,
                       s.opening_staff.name, timezone.localtime(s.opening_time).strftime('%d %b %H:%M'),
                       timezone.localtime(s.closing_time).strftime('%d %b %H:%M') if s.closing_time else '—',
                       s.opening_cash, s.expected_cash if s.status == 'closed' else '—',
                       s.actual_cash if s.status == 'closed' else '—', s.cash_difference if s.status == 'closed' else '—',
                       badge(s.status.upper())]} for s in qs[:300]]
    return list_page(request, title='POS shifts', subtitle='Opening float, expected cash from cash tenders, counted cash and difference.',
                     headers=['Shift', 'Store', 'Terminal', 'Opened by', 'Opened', 'Closed', 'Opening cash', 'Expected', 'Counted',
                              'Difference', 'Status'], rows=rows,
                     filters=[('store', [(s.pk, s.code) for s in Store.objects.order_by('code')]), ('status', POSShift.STATUS_CHOICES)])


@retail_view
def shift_detail(request, pk):
    shift = get_object_or_404(POSShift.objects.select_related('store', 'terminal', 'opening_staff', 'closing_staff'), pk=pk)
    form = f.ShiftCloseForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        closer = shift.sessions.filter(status='active').select_related('staff').first()
        closer = closer.staff if closer else shift.opening_staff
        try:
            services.close_shift(shift, closer, form.cleaned_data['actual_cash'], form.cleaned_data['notes'], request)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f'Shift {shift.shift_code} closed.')
            return redirect('retail_shift_detail', shift.pk)
    tender_totals = POSPayment.objects.filter(session__shift=shift).values('tender_name_snapshot', 'tender_type_snapshot').annotate(
        total=Sum('amount'), change=Sum('change_amount'), count=Count('id')).order_by('tender_name_snapshot')
    return page(request, 'shift_detail.html', shift=shift, form=form, sessions=shift.sessions.order_by('login_time'),
                tender_totals=tender_totals, expected=services.shift_expected_cash(shift))


# --- Import / export / audit -----------------------------------------------------------------------------------------

@retail_view
def import_center(request):
    form = f.ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            batch = imports.upload(form.cleaned_data['file'], form.cleaned_data['import_type'], request)
        except Exception as exc:  # unreadable workbook
            form.add_error('file', f'Could not read the workbook: {exc}')
        else:
            return redirect('retail_import_detail', batch.pk)
    return page(request, 'imports.html', form=form, kinds=RetailImportBatch.TYPES,
                batches=RetailImportBatch.objects.select_related('created_by')[:30])


@retail_view
def import_detail(request, pk):
    batch = get_object_or_404(RetailImportBatch, pk=pk)
    if request.method == 'POST':
        if request.POST.get('action') == 'import':
            try:
                imports.run_import(batch, request)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                if batch.status == 'imported':
                    messages.success(request, f'Imported: {batch.created_count} created, {batch.updated_count} updated.')
                else:
                    messages.error(request, 'Import stopped: data changed since validation. Review the errors.')
        else:
            imports.validate(batch, request)
            messages.info(request, 'Batch re-validated.')
        return redirect('retail_import_detail', batch.pk)
    if request.GET.get('errors'):
        return xlsx_response(imports.error_workbook(batch), f'{batch.import_type}-errors-{batch.pk}')
    columns = [c for c, _ in imports.COLUMNS[batch.import_type]]
    by_row = {}
    for error in batch.errors:
        by_row.setdefault(error['row'], []).append(error['message'])
    preview = [(n, [('••••••' if c == 'password' and row.get(c) else row.get(c, '')) for c in columns], by_row.get(n, []))
               for n, row in enumerate(batch.rows[:500], 2)]
    return page(request, 'import_detail.html', batch=batch, columns=columns, preview=preview,
                file_errors=by_row.get(1, []))


@retail_view
def import_template(request, kind):
    if kind not in imports.COLUMNS:
        raise Http404
    return xlsx_response(imports.template_workbook(kind), f'{kind}-template')


@retail_view
def export(request, kind):
    if kind not in imports.COLUMNS:
        raise Http404
    return _export(request, kind)


@retail_view
def audit_list(request):
    qs = AuditLog.objects.filter(table_name__in=RETAIL_TABLES).select_related('actor').order_by('-created_at')
    if request.GET.get('action'):
        qs = qs.filter(action=request.GET['action'])
    rows = [{'cells': [timezone.localtime(a.created_at).strftime('%d %b %Y %H:%M'), a.actor.username if a.actor else 'system',
                       badge(a.action.upper(), a.get_action_display()), a.table_name.replace('erp_', ''), a.record_id, a.description,
                       ', '.join(f'{k}: {a.old_value.get(k)} → {a.new_value.get(k)}' for k in a.new_value if k in a.old_value)[:200],
                       a.ip_address or '', a.device[:40]]} for a in qs[:500]]
    return list_page(request, title='Audit trail', subtitle='Store, location, staff, terminal and tender changes. Passwords are never logged.',
                     headers=['When', 'User', 'Action', 'Table', 'Record', 'Description', 'Changes', 'IP', 'Device'], rows=rows,
                     filters=[('action', AuditLog.ACTION_CHOICES)])
