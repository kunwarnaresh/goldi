"""Allow-listed semantic datasets for tenant-safe analytics queries.

Reports are composed from these field and measure definitions. Request data never becomes
an ORM path or SQL fragment, and each dataset applies tenant and location security first.
"""
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Sum

from inventory.models import InventoryBalance, InventoryLedgerEntry, Location
from inventory.tenancy import allowed_locations


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    description: str
    dimensions: dict
    measures: dict
    date_field: str | None = None


DATASETS = {
    'sales': Dataset('sales', 'Sales lines', 'Posted sales invoice lines with store, customer, product and jewellery attributes.', {
        'invoice': ('invoice__invoice_no', 'Invoice'), 'date': ('invoice__sales_date__date', 'Sales date'),
        'store': ('invoice__store_name_snapshot', 'Store'), 'customer': ('invoice__customer__name', 'Customer'),
        'category': ('product__item_category__name', 'Category'), 'product': ('product__name', 'Product'),
        'sku': ('product__sku', 'SKU'), 'cashier': ('invoice__cashier_name_snapshot', 'Cashier'),
        'salesperson': ('invoice__sales_staff_name_snapshot', 'Salesperson'), 'metal': ('jewellery_unit__metal_type', 'Metal'),
        'purity': ('jewellery_unit__purity', 'Purity'), 'barcode': ('barcode', 'Barcode'),
    }, {
        'sales': ('sum', 'line_total', 'Sales'), 'quantity': ('sum', 'quantity', 'Quantity'),
        'discount': ('sum', 'discount_amount', 'Discount'), 'making': ('sum', 'making_charge', 'Making charges'),
        'metal_weight': ('sum', 'net_metal_weight', 'Net metal weight'),
        'invoices': ('count_distinct', 'invoice_id', 'Invoices'),
    }, 'invoice__sales_date__date'),
    'inventory': Dataset('inventory', 'Inventory position', 'Current stock balances, including available quantity, weight and historical cost.', {
        'location': ('location__name', 'Location'), 'location_code': ('location__code', 'Location code'),
        'item': ('item__description', 'Item'), 'item_no': ('item__item_no', 'Item number'),
        'category': ('item__category', 'Category'), 'metal': ('item__metal', 'Metal'), 'purity': ('item__purity', 'Purity'),
        'sku': ('sku__code', 'SKU'), 'lot': ('lot_no', 'Lot'),
    }, {
        'quantity': ('sum', 'on_hand_qty', 'On hand'), 'available': ('sum', 'available_qty', 'Available'),
        'reserved': ('sum', 'reserved_qty', 'Reserved'), 'gross_weight': ('sum', 'gross_weight', 'Gross weight'),
        'net_weight': ('sum', 'net_weight', 'Net weight'), 'cost_value': ('sum', 'cost_value', 'Cost value'),
        'buckets': ('count', 'id', 'Stock buckets'),
    }),
    'movements': Dataset('movements', 'Inventory movements', 'Immutable posted inventory ledger activity.', {
        'date': ('posting_date', 'Posting date'), 'location': ('location__name', 'Location'), 'item': ('item__description', 'Item'),
        'item_no': ('item__item_no', 'Item number'), 'category': ('item__category', 'Category'),
        'type': ('transaction_type', 'Transaction type'), 'document': ('document_no', 'Document'),
        'metal': ('item__metal', 'Metal'), 'purity': ('item__purity', 'Purity'),
    }, {
        'quantity': ('sum', 'quantity', 'Quantity change'), 'gross_weight': ('sum', 'gross_weight', 'Gross weight change'),
        'net_weight': ('sum', 'net_weight', 'Net weight change'), 'cost': ('sum', 'cost_amount', 'Cost movement'),
        'entries': ('count', 'id', 'Entries'),
    }, 'posting_date'),
    'production': Dataset('production', 'Production orders', 'Tenant production orders, planned cost and production quantities.', {
        'date': ('order_date', 'Order date'), 'order': ('order_no', 'Order'), 'status': ('status', 'Status'),
        'location': ('location__name', 'Location'), 'item': ('item__description', 'Item'),
        'item_no': ('item__item_no', 'Item number'), 'category': ('item__category', 'Category'),
        'bom': ('bom__bom_no', 'BOM'), 'routing': ('routing__routing_no', 'Routing'),
    }, {
        'planned_qty': ('sum', 'planned_qty', 'Planned quantity'), 'produced_qty': ('sum', 'produced_qty', 'Produced quantity'),
        'rejected_qty': ('sum', 'rejected_qty', 'Rejected quantity'), 'scrap_qty': ('sum', 'scrap_qty', 'Scrap quantity'),
        'planned_cost': ('sum', 'planned_total_cost', 'Planned cost'), 'orders': ('count', 'id', 'Orders'),
    }, 'order_date'),
    'schemes': Dataset('schemes', 'Scheme collections', 'Member contribution and collection activity from the scheme payment ledger.', {
        'date': ('payment_date', 'Payment date'), 'scheme': ('enrollment__scheme__name', 'Scheme'),
        'customer': ('enrollment__customer__name', 'Customer'), 'location': ('location__name', 'Location'),
        'method': ('method_type', 'Payment method'), 'status': ('status', 'Status'),
    }, {
        'amount': ('sum', 'amount', 'Collection amount'), 'contribution': ('sum', 'contribution_amount', 'Contribution'),
        'penalty': ('sum', 'penalty_amount', 'Penalty'), 'payments': ('count', 'id', 'Payments'),
    }, 'payment_date'),
}


def dataset_for(key):
    try:
        return DATASETS[key]
    except KeyError as exc:
        raise ValidationError('Choose a supported analytics dataset.') from exc


def _base_queryset(key, tenant, user):
    ds = dataset_for(key)
    locations = allowed_locations(tenant, user, 'view')
    if key in ('inventory', 'movements'):
        model = InventoryBalance if key == 'inventory' else InventoryLedgerEntry
        return model.objects.filter(tenant=tenant, location__in=locations)
    if key == 'production':
        from manufacturing.models import ProductionOrder
        return ProductionOrder.objects.filter(tenant=tenant, location__in=locations)
    if key == 'schemes':
        from savings.models import SchemePayment
        return SchemePayment.objects.filter(tenant=tenant, location__in=locations)
    if key == 'sales':
        from erp.models import SalesInvoiceItem
        # Legacy ERP invoices are not tenant rows. Only include invoices whose POS store is
        # explicitly connected to a location this user can view in this tenant.
        store_ids = locations.exclude(store__isnull=True).values_list('store_id', flat=True)
        return SalesInvoiceItem.objects.filter(invoice__pos_terminal__store_id__in=store_ids,
                                               invoice__status__in=('completed', 'posted'))
    raise ValidationError('Choose a supported analytics dataset.')


def execute_query(*, dataset, tenant, user, dimensions=(), measures=(), filters=None, limit=500):
    """Return grouped, bounded aggregates for an allow-listed dataset."""
    spec = dataset_for(dataset)
    dimensions = list(dict.fromkeys(dimensions or []))
    measures = list(dict.fromkeys(measures or []))
    if len(dimensions) > 3 or len(measures) > 8:
        raise ValidationError('Select up to three dimensions and eight measures.')
    if not measures:
        measures = list(spec.measures)[:1]
    invalid = (set(dimensions) - set(spec.dimensions)) | (set(measures) - set(spec.measures))
    if invalid:
        raise ValidationError('One or more selected fields are not available for this dataset.')
    qs = _base_queryset(dataset, tenant, user)
    filters = filters or {}

    # Global filters use a fixed map; no client-supplied ORM paths are accepted.
    filter_map = {
        'sales': {'date_from': 'invoice__sales_date__date__gte', 'date_to': 'invoice__sales_date__date__lte',
                  'store': 'invoice__store_code_snapshot__iexact', 'customer': 'invoice__customer_id',
                  'status': 'invoice__status', 'category': 'product__item_category__name__iexact',
                  'metal': 'jewellery_unit__metal_type__iexact', 'purity': 'jewellery_unit__purity__iexact'},
        'inventory': {'location': 'location_id', 'category': 'item__category__iexact', 'metal': 'item__metal__iexact',
                      'purity': 'item__purity__iexact', 'item': 'item_id'},
        'movements': {'date_from': 'posting_date__gte', 'date_to': 'posting_date__lte', 'location': 'location_id',
                      'type': 'transaction_type', 'category': 'item__category__iexact', 'metal': 'item__metal__iexact',
                      'purity': 'item__purity__iexact', 'document': 'document_no__icontains'},
        'production': {'date_from': 'order_date__gte', 'date_to': 'order_date__lte', 'location': 'location_id',
                       'status': 'status', 'item': 'item_id'},
        'schemes': {'date_from': 'payment_date__gte', 'date_to': 'payment_date__lte', 'location': 'location_id',
                    'status': 'status', 'method': 'method_type', 'scheme': 'enrollment__scheme_id'},
    }[dataset]
    for name, value in filters.items():
        if value in ('', None) or name not in filter_map:
            continue
        qs = qs.filter(**{filter_map[name]: value})
    annotations = {}
    aggregations = {}
    for name, (path, _label) in spec.dimensions.items():
        if name in dimensions:
            annotations[f'dim_{name}'] = F(path)
    for name, (kind, path, _label) in spec.measures.items():
        if name not in measures:
            continue
        annotations[f'measure_{name}'] = Count(path, distinct=True) if kind == 'count_distinct' else Count(path) if kind == 'count' else Sum(path)
        aggregations[f'measure_{name}'] = annotations[f'measure_{name}']
    qs = qs.annotate(**annotations)
    group_fields = [f'dim_{name}' for name in dimensions]
    values = qs.values(*group_fields) if group_fields else qs.values()
    rows = list(values.annotate(**{f'measure_{name}': annotations[f'measure_{name}'] for name in measures})
                .order_by(*group_fields)[:max(1, min(int(limit), 1000))])
    labels = {**{name: spec.dimensions[name][1] for name in dimensions},
              **{name: spec.measures[name][2] for name in measures}}
    normalized = []
    for row in rows:
        normalized.append({**{name: row.get(f'dim_{name}') or '—' for name in dimensions},
                           **{name: row.get(f'measure_{name}') or 0 for name in measures}})
    # Grand totals are evaluated over the filtered source rows. Summing grouped distinct counts
    # would double count documents that contain lines in more than one group.
    raw_totals = qs.aggregate(**aggregations)
    totals = {name: raw_totals.get(f'measure_{name}') or 0 for name in measures}
    return {'dataset': spec, 'dimensions': dimensions, 'measures': measures, 'labels': labels,
            'rows': normalized, 'totals': totals, 'truncated': len(normalized) >= min(int(limit), 1000)}


def dataset_catalog():
    return [{'key': ds.key, 'label': ds.label, 'description': ds.description,
             'dimensions': [{'key': k, 'label': v[1]} for k, v in ds.dimensions.items()],
             'measures': [{'key': k, 'label': v[2]} for k, v in ds.measures.items()]}
            for ds in DATASETS.values()]
