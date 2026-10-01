from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from inventory.engine import InventoryError
from inventory.models import Item, Location
from inventory.tenancy import allowed_locations

from . import engine as eng
from . import services as svc
from .models import (
    MachineCenter, PlanningRun, PlanningSuggestion, ProductionBOM, ProductionOrder, Routing, SubcontractOrder, WorkCenter,
)
from .security import ConfirmationRequired, actor_from_request, can, get_setup, permission_map

MENU = [
    ('Overview', [('manufacturing_dashboard', 'Manufacturing dashboard'), ('manufacturing_capacity', 'Capacity load')]),
    ('Production', [('manufacturing_orders', 'Production orders'), ('manufacturing_order_new', 'New production order'),
                    ('manufacturing_subcontracts', 'Subcontracting')]),
    ('Masters', [('manufacturing_boms', 'Production BOMs'), ('manufacturing_routings', 'Routings'),
                 ('manufacturing_work_centers', 'Work & machine centres')]),
    ('Planning', [('manufacturing_planning', 'Planning worksheet')]),
]


def menu_links():
    return [(group, [(reverse(name), label) for name, label in entries]) for group, entries in MENU]


def manufacturing_view(view):
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
    context.setdefault('mfg_menu', menu_links())
    context.setdefault('perms_mfg', permission_map(actor))
    return render(request, f'manufacturing/{template}', context)


def get_obj(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=pk).first()
    if obj is None:
        raise Http404
    return obj


def decimal_of(value, default=None):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Dashboard and capacity
# ---------------------------------------------------------------------------

@manufacturing_view
def dashboard(request, actor):
    orders = ProductionOrder.objects.filter(tenant=actor.tenant)
    by_status = dict(orders.values_list('status').annotate(n=Count('id')))
    today = timezone.localdate()
    open_orders = orders.exclude(status__in=ProductionOrder.CLOSED_STATUSES)
    kpis = {
        'planning': sum(by_status.get(s, 0) for s in ProductionOrder.PLANNING_STATUSES),
        'in_progress': sum(by_status.get(s, 0) for s in ProductionOrder.EXECUTION_STATUSES),
        'qc_pending': by_status.get('QC_PENDING', 0),
        'finished': by_status.get('FINISHED', 0) + by_status.get('CLOSED', 0),
        'late': open_orders.filter(due_date__lt=today).count(),
        'planned_qty': open_orders.aggregate(q=Sum('planned_qty'))['q'] or 0,
        'produced_qty': open_orders.aggregate(q=Sum('produced_qty'))['q'] or 0,
        'wip_cost': open_orders.filter(status__in=ProductionOrder.EXECUTION_STATUSES).aggregate(v=Sum('planned_total_cost'))['v'] or 0,
        'boms': ProductionBOM.objects.filter(tenant=actor.tenant, blocked=False).count(),
        'work_centers': WorkCenter.objects.filter(tenant=actor.tenant, active=True).count(),
        'subcontract_open': SubcontractOrder.objects.filter(tenant=actor.tenant, status__in=('SENT', 'PARTIALLY_RECEIVED')).count(),
    }
    recent = orders.select_related('item', 'location')[:10]
    due_soon = open_orders.filter(due_date__isnull=False, due_date__lte=today + timedelta(days=7)).select_related('item').order_by('due_date')[:10]
    return page(request, actor, 'dashboard.html', kpis=kpis, recent=recent, due_soon=due_soon,
                setup=get_setup(actor.tenant))


@manufacturing_view
def capacity(request, actor):
    start = parse_date(request.GET.get('start') or '') or timezone.localdate()
    days = request.GET.get('days', '')
    days = min(max(int(days), 1), 31) if days.isdigit() else 14
    rows = svc.capacity_load(actor.tenant, start=start, days=days)
    day_heads = rows[0]['days'] if rows else []
    return page(request, actor, 'capacity.html', rows=rows, start=start, days=days, day_heads=day_heads)


# ---------------------------------------------------------------------------
# Production orders
# ---------------------------------------------------------------------------

@manufacturing_view
def order_list(request, actor):
    orders = ProductionOrder.objects.filter(tenant=actor.tenant).select_related('item', 'location')
    status = request.GET.get('status', '')
    if status == 'open':
        orders = orders.exclude(status__in=ProductionOrder.CLOSED_STATUSES)
    elif status:
        orders = orders.filter(status=status)
    q = request.GET.get('q', '').strip()
    if q:
        orders = orders.filter(Q(order_no__icontains=q) | Q(item__item_no__icontains=q) | Q(item__description__icontains=q)
                               | Q(source_no__icontains=q))
    return page(request, actor, 'order_list.html', orders=orders[:300], status=status, q=q, statuses=ProductionOrder.STATUSES)


@manufacturing_view
def order_new(request, actor):
    locations = allowed_locations(actor.tenant, actor.user, 'view')
    items = Item.objects.filter(tenant=actor.tenant, active=True, blocked=False).order_by('item_no')
    if request.method == 'POST':
        item = get_obj(Item, actor, request.POST.get('item'))
        quantity = decimal_of(request.POST.get('quantity'))
        if not quantity or quantity <= 0:
            messages.error(request, 'Enter a quantity greater than zero.')
            return redirect(request.get_full_path())
        location = get_obj(Location, actor, request.POST['location']) if request.POST.get('location') else None
        order = svc.create_order(actor, item=item, quantity=quantity, location=location,
                                 due_date=parse_date(request.POST.get('due_date') or ''),
                                 priority=request.POST.get('priority') or 'NORMAL', remarks=request.POST.get('remarks', ''))
        messages.success(request, f'Production order {order.order_no} created.')
        return redirect('manufacturing_order_detail', pk=order.pk)
    return page(request, actor, 'order_new.html', items=items, locations=locations,
                priorities=ProductionOrder._meta.get_field('priority').choices)


ORDER_ACTIONS = ('refresh', 'approve', 'release', 'cancel', 'finish', 'close', 'firm', 'plan')


@manufacturing_view
def order_detail(request, actor, pk):
    order = get_obj(ProductionOrder, actor, pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        confirmed = request.POST.get('confirm') == 'on'
        if action == 'refresh':
            svc.refresh_order(actor, order, confirm=confirmed)
            messages.success(request, 'Order refreshed from BOM and routing.')
        elif action == 'approve':
            svc.approve_order(actor, order)
            messages.success(request, 'Order approved.')
        elif action == 'release':
            svc.release_order(actor, order, override_shortage=confirmed)
            messages.success(request, 'Order released to the shop floor.')
        elif action == 'cancel':
            svc.cancel_order(actor, order, reason=request.POST.get('reason', ''))
            messages.success(request, 'Order cancelled.')
        elif action == 'finish':
            eng.finish_order(actor, order, force=confirmed, reason=request.POST.get('reason', ''))
            messages.success(request, 'Order finished.')
        elif action == 'close':
            svc.close_order(actor, order)
            messages.success(request, 'Order closed.')
        elif action in ('firm', 'plan'):
            svc.update_planning_status(actor, order, 'FIRM_PLANNED' if action == 'firm' else 'PLANNED')
            messages.success(request, 'Planning status updated.')
        return redirect('manufacturing_order_detail', pk=order.pk)
    context = dict(
        order=order,
        lines=order.lines.select_related('item'),
        availability=svc.material_availability(order),
        operations=order.operations.select_related('work_center', 'machine_center'),
        costs=eng.cost_breakdown(order),
        audit=order.audit_logs.select_related('user')[:25],
    )
    if order.is_executable:
        context['blocking'], context['remaining'] = eng.finish_readiness(order)
    return page(request, actor, 'order_detail.html', **context)


# ---------------------------------------------------------------------------
# Masters: BOMs, routings, work centres
# ---------------------------------------------------------------------------

@manufacturing_view
def bom_list(request, actor):
    boms = ProductionBOM.objects.filter(tenant=actor.tenant).select_related('item').annotate(version_count=Count('versions'))
    q = request.GET.get('q', '').strip()
    if q:
        boms = boms.filter(Q(bom_no__icontains=q) | Q(bom_name__icontains=q) | Q(item__item_no__icontains=q))
    return page(request, actor, 'bom_list.html', boms=boms, q=q)


def _versioned_detail(request, actor, obj, template, key):
    versions = obj.versions.all()
    selected = versions.filter(pk=request.GET.get('version')).first() if request.GET.get('version') else None
    selected = selected or obj.active_version() or versions.first()
    if request.method == 'POST':
        version = versions.filter(pk=request.POST.get('version')).first()
        if version is None:
            raise Http404
        svc.transition_version(actor, version, request.POST.get('action', ''), note=request.POST.get('note', ''))
        messages.success(request, f'{version} updated.')
        return redirect(f'{request.path}?version={version.pk}')
    return page(request, actor, template, **{key: obj, 'versions': versions, 'selected': selected,
                                             'lines': selected.lines.all() if selected else []})


@manufacturing_view
def bom_detail(request, actor, pk):
    bom = get_obj(ProductionBOM, actor, pk)
    return _versioned_detail(request, actor, bom, 'bom_detail.html', 'bom')


@manufacturing_view
def routing_list(request, actor):
    routings = Routing.objects.filter(tenant=actor.tenant).select_related('item').annotate(version_count=Count('versions'))
    q = request.GET.get('q', '').strip()
    if q:
        routings = routings.filter(Q(routing_no__icontains=q) | Q(description__icontains=q) | Q(item__item_no__icontains=q))
    return page(request, actor, 'routing_list.html', routings=routings, q=q)


@manufacturing_view
def routing_detail(request, actor, pk):
    routing = get_obj(Routing, actor, pk)
    return _versioned_detail(request, actor, routing, 'routing_detail.html', 'routing')


@manufacturing_view
def work_center_list(request, actor):
    centers = WorkCenter.objects.filter(tenant=actor.tenant).select_related('group', 'location', 'calendar')
    machines = MachineCenter.objects.filter(tenant=actor.tenant).select_related('work_center')
    return page(request, actor, 'work_center_list.html', centers=centers, machines=machines)


# ---------------------------------------------------------------------------
# Subcontracting and planning
# ---------------------------------------------------------------------------

@manufacturing_view
def subcontract_list(request, actor):
    subcontracts = SubcontractOrder.objects.filter(tenant=actor.tenant).select_related('order', 'operation', 'subcontractor')
    return page(request, actor, 'subcontract_list.html', subcontracts=subcontracts[:300])


@manufacturing_view
def planning(request, actor):
    runs = PlanningRun.objects.filter(tenant=actor.tenant)
    if request.method == 'POST':
        if request.POST.get('action') == 'run':
            days = request.POST.get('horizon_days', '30')
            location = get_obj(Location, actor, request.POST['location']) if request.POST.get('location') else None
            run = svc.run_planning(actor, horizon_days=int(days) if days.isdigit() else 30, location=location)
            messages.success(request, f'Planning run {run.run_no} completed.')
            return redirect(f'{request.path}?run={run.pk}')
        ids = request.POST.getlist('suggestion')
        suggestions = list(PlanningSuggestion.objects.filter(tenant=actor.tenant, pk__in=ids).select_related('run', 'item'))
        if not suggestions:
            messages.error(request, 'Select at least one suggestion.')
            return redirect(request.get_full_path())
        results = svc.carry_out(actor, suggestions, firm=request.POST.get('firm') == 'on')
        messages.success(request, f'Carried out: {", ".join(results) or "nothing to do"}.')
        return redirect(request.get_full_path())
    run = runs.filter(pk=request.GET.get('run')).first() if request.GET.get('run') else runs.first()
    suggestions = run.suggestions.select_related('item', 'location', 'source_location', 'production_order') if run else []
    return page(request, actor, 'planning.html', runs=runs[:20], run=run, suggestions=suggestions,
                locations=allowed_locations(actor.tenant, actor.user, 'view'), can_plan=can(actor, 'plan'))
