import csv
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from inventory.models import InventoryBalance
from inventory.tenancy import allowed_locations, get_current_tenant

from .models import SavedAnalysisView
from .semantic import DATASETS, dataset_catalog, execute_query


def _date_filters(request):
    params = request.GET if request.method == 'GET' else request.POST
    today = timezone.localdate()
    period = params.get('period', '')
    start, end = params.get('date_from'), params.get('date_to')
    if period:
        start, end = _period_range(period, today)
    start = start or today.replace(day=1).isoformat()
    end = end or today.isoformat()
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError:
        raise ValidationError('Enter dates in YYYY-MM-DD format.')
    if start > end:
        raise ValidationError('The start date must be on or before the end date.')
    return {'date_from': start, 'date_to': end}


def _period_range(period, today):
    month_start = today.replace(day=1)
    if period == 'today':
        return today.isoformat(), today.isoformat()
    if period == 'yesterday':
        yesterday = today - timedelta(days=1)
        return yesterday.isoformat(), yesterday.isoformat()
    if period == 'mtd':
        return month_start.isoformat(), today.isoformat()
    if period == 'previous_month':
        end = month_start - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if period == 'rolling_7':
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == 'rolling_30':
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if period == 'rolling_90':
        return (today - timedelta(days=89)).isoformat(), today.isoformat()
    if period == 'ytd':
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if period in ('fytd', 'previous_fy'):
        fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        if period == 'fytd':
            return fy_start.isoformat(), today.isoformat()
        previous_end = fy_start - timedelta(days=1)
        previous_start = date(previous_end.year if previous_end.month >= 4 else previous_end.year - 1, 4, 1)
        return previous_start.isoformat(), previous_end.isoformat()
    raise ValidationError('Choose a supported date range.')


def _configuration(request, dataset):
    params = request.GET if request.method == 'GET' else request.POST
    dimensions = params.getlist('dimension') or params.get('dimensions', '').split(',')
    measures = params.getlist('measure') or params.get('measures', '').split(',')
    return {
        'dataset': dataset,
        'dimensions': [value for value in dimensions if value],
        'measures': [value for value in measures if value],
        'filters': {**_date_filters(request), **{key: params.get(key, '') for key in (
            'location', 'store', 'customer', 'status', 'category', 'metal', 'purity', 'type', 'document', 'method', 'scheme', 'item'
        )}},
    }


def _run(configuration, tenant, user):
    return execute_query(tenant=tenant, user=user, dataset=configuration['dataset'],
                        dimensions=configuration.get('dimensions', []), measures=configuration.get('measures', []),
                        filters=configuration.get('filters', {}))


def _period_query(request, key, dimensions, measures):
    tenant = get_current_tenant(request)
    conf = {'dataset': key, 'dimensions': dimensions, 'measures': measures,
            'filters': {'date_from': request.GET.get('date_from', ''), 'date_to': request.GET.get('date_to', '')}}
    return _run(conf, tenant, request.user)


@login_required(login_url='login')
def dashboard(request):
    tenant = get_current_tenant(request)
    locations = allowed_locations(tenant, request.user, 'view')
    today = timezone.localdate()
    try:
        dates = _date_filters(request)
    except ValidationError as exc:
        dates = {'date_from': today.replace(day=1).isoformat(), 'date_to': today.isoformat()}
        messages.error(request, exc.messages[0])
    sales = _run({'dataset': 'sales', 'dimensions': [], 'measures': ['sales', 'discount', 'invoices'],
                  'filters': dates}, tenant, request.user)
    inventory = InventoryBalance.objects.filter(tenant=tenant, location__in=locations).aggregate(
        quantity=Sum('on_hand_qty'), value=Sum('cost_value'))
    inventory = {key: value or Decimal('0') for key, value in inventory.items()}
    from manufacturing.models import ProductionOrder
    production = ProductionOrder.objects.filter(tenant=tenant, location__in=locations).exclude(
        status__in=('FINISHED', 'CANCELLED', 'CLOSED')).count()
    saved = SavedAnalysisView.objects.filter(tenant=tenant).filter(models_q(request.user)).order_by('-updated_at')[:8]
    return render(request, 'analytics/dashboard.html', {
        'tenant': tenant, 'date_from': dates['date_from'], 'date_to': dates['date_to'], 'sales': sales['totals'],
        'inventory': inventory, 'production_orders': production, 'locations_count': locations.count(),
        'datasets': dataset_catalog(), 'saved_views': saved,
    })


def models_q(user):
    return Q(owner=user) | Q(shared=True)


@login_required(login_url='login')
def explorer(request):
    tenant = get_current_tenant(request)
    config = None
    saved_id = request.GET.get('view_id')
    if saved_id:
        saved_view = get_object_or_404(SavedAnalysisView.objects.filter(tenant=tenant).filter(models_q(request.user)), pk=saved_id)
        config = saved_view.configuration
    dataset = (config or {}).get('dataset', request.GET.get('dataset', 'sales'))
    spec = DATASETS.get(dataset)
    if spec is None:
        dataset = 'sales'
        spec = DATASETS[dataset]
        messages.error(request, 'That dataset is not available.')
    result = None
    try:
        config = config or _configuration(request, dataset)
        result = _run(config, tenant, request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        config = {'dataset': dataset, 'dimensions': [], 'measures': [], 'filters': _date_filters_fallback()}
        result = _run(config, tenant, request.user)
    views = SavedAnalysisView.objects.filter(tenant=tenant).filter(models_q(request.user)).order_by('name')
    return render(request, 'analytics/explorer.html', {
        'datasets': dataset_catalog(), 'selected_dataset': dataset, 'spec': spec, 'result': result,
        'config': config, 'saved_views': views,
    })


def _date_filters_fallback():
    today = timezone.localdate()
    return {'date_from': today.replace(day=1).isoformat(), 'date_to': today.isoformat()}


@login_required(login_url='login')
def export_report(request, format):
    if format not in ('csv', 'json'):
        raise ValidationError('Choose CSV or JSON export.')
    tenant = get_current_tenant(request)
    dataset = request.GET.get('dataset', 'sales')
    config = _configuration(request, dataset)
    result = _run(config, tenant, request.user)
    columns = config['dimensions'] + config['measures']
    if format == 'json':
        response = JsonResponse({'dataset': dataset, 'filters': config['filters'], 'columns': columns,
                                 'rows': result['rows'], 'totals': result['totals']})
        response['Content-Disposition'] = f'attachment; filename="goldio-{dataset}-analysis.json"'
        return response
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="goldio-{dataset}-analysis.csv"'
    writer = csv.writer(response)
    writer.writerow([result['labels'][key] for key in columns])
    for row in result['rows']:
        writer.writerow([row.get(key) for key in columns])
    if result['rows']:
        writer.writerow(['Totals', *([''] * len(config['dimensions'])), *[result['totals'].get(key, '') for key in result['measures']]])
    return response


@login_required(login_url='login')
@require_POST
def save_view(request):
    tenant = get_current_tenant(request)
    try:
        config = _configuration(request, request.POST.get('dataset', 'sales'))
        # Validate the dataset, selected dimensions/measures and filter values before persisting.
        _run(config, tenant, request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(request.POST.get('next', 'analytics:explorer'))
    name = request.POST.get('name', '').strip()[:120]
    if not name:
        messages.error(request, 'Give this analysis view a name.')
        return redirect('analytics:explorer')
    shared = request.POST.get('shared') == 'on'
    if shared and not request.user.tenant_memberships.filter(tenant=tenant, active=True, role__in=('owner', 'admin')).exists():
        raise PermissionDenied('Only workspace administrators can share analysis views.')
    view = SavedAnalysisView.objects.filter(tenant=tenant, owner=request.user, name=name).first()
    version = view.version + 1 if view else 1
    view, _created = SavedAnalysisView.objects.update_or_create(
        tenant=tenant, owner=request.user, name=name,
        defaults={'dataset': config['dataset'], 'configuration': config, 'shared': shared, 'version': version},
    )
    messages.success(request, f'Analysis view “{view.name}” saved.')
    return redirect(f'{reverse("analytics:explorer")}?{_query_from_config(config)}')


def _query_from_config(config):
    from urllib.parse import urlencode
    query = [('dataset', config['dataset'])]
    query.extend(('dimension', value) for value in config['dimensions'])
    query.extend(('measure', value) for value in config['measures'])
    query.extend((key, value) for key, value in config.get('filters', {}).items() if value)
    return urlencode(query)


@login_required(login_url='login')
@require_POST
def delete_view(request, pk):
    tenant = get_current_tenant(request)
    view = get_object_or_404(SavedAnalysisView, pk=pk, tenant=tenant, owner=request.user)
    view.delete()
    messages.success(request, 'Analysis view deleted.')
    return redirect('analytics:explorer')


@login_required(login_url='login')
def datasets_api(request):
    get_current_tenant(request)
    return JsonResponse({'datasets': dataset_catalog()})


@login_required(login_url='login')
def query_api(request):
    tenant = get_current_tenant(request)
    try:
        config = _configuration(request, request.GET.get('dataset', 'sales'))
        result = _run(config, tenant, request.user)
    except ValidationError as exc:
        return JsonResponse({'error': exc.messages[0]}, status=400)
    return JsonResponse({'dataset': config['dataset'], 'dimensions': config['dimensions'], 'measures': config['measures'],
                         'labels': result['labels'], 'rows': result['rows'], 'totals': result['totals'],
                         'truncated': result['truncated']})
