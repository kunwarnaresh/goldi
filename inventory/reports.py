"""Read-side inventory services shared by pages, APIs and exports. Nothing here writes."""
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone as dj_timezone

from .engine import OPEN_TRANSFER_STATUSES, ZERO, money
from .models import InventoryBalance, InventoryLedgerEntry, JewelleryUnit, TransferOrder, TransferOrderLine

INBOUND_TYPES = ('OPENING', 'PURCHASE', 'RETURN', 'ADJUSTMENT_POSITIVE', 'PRODUCTION', 'ASSEMBLY')


class RateBook:
    """Current metal rate per gram for (metal, purity, location): central rate + location premium.

    Uses the existing metal-rate engine; historical cost is never touched.
    """

    def __init__(self):
        self._cache = {}

    def rate(self, metal, purity, location=None):
        if not metal or metal in ('NONE', 'DIAMOND') or not purity:
            return None
        key = (metal, purity)
        if key not in self._cache:
            from erp.services import resolve_current_metal_rate
            resolved = resolve_current_metal_rate(metal_type=metal.lower(), purity=purity)
            self._cache[key] = resolved['rate_per_gram'] if resolved['resolved'] else None
        central = self._cache[key]
        if central is None:
            return None
        return central + (location.metal_rate_premium_per_gram if location is not None else ZERO)


def valuation_by_location(tenant, locations):
    """Quantity, weight, historical cost, current metal value and retail value per location."""
    rates = RateBook()
    rows = {}
    balances = InventoryBalance.objects.filter(tenant=tenant, location__in=locations) \
        .filter(Q(on_hand_qty__gt=0) | Q(in_transit_qty__gt=0)) \
        .select_related('location', 'item', 'sku', 'jewellery_unit')
    for balance in balances:
        row = rows.setdefault(balance.location_id, {
            'location': balance.location, 'qty': ZERO, 'gross_weight': ZERO, 'net_weight': ZERO, 'cost_value': ZERO,
            'metal_value': ZERO, 'current_value': ZERO, 'retail_value': ZERO, 'unrated_weight': ZERO,
        })
        qty = balance.in_transit_qty if balance.location.is_transit else balance.on_hand_qty
        unit, item = balance.jewellery_unit, balance.item
        metal, purity = (unit.metal, unit.purity) if unit else (item.metal, item.purity)
        rate = rates.rate(metal, purity, balance.location)
        metal_value = money(balance.net_weight * rate) if rate is not None else ZERO
        if rate is None:
            row['unrated_weight'] += balance.net_weight
        extras = (unit.stone_value + unit.making_charge) if unit else ZERO
        retail = unit.retail_price if unit else (balance.sku.retail_price * qty if balance.sku_id else ZERO)
        row['qty'] += qty
        row['gross_weight'] += balance.gross_weight
        row['net_weight'] += balance.net_weight
        row['cost_value'] += balance.cost_value
        row['metal_value'] += metal_value
        row['current_value'] += metal_value + extras
        row['retail_value'] += retail
    return sorted(rows.values(), key=lambda r: r['location'].code)


def weights_by_metal(tenant, locations):
    """Gold / silver weight and diamond carat currently held (on hand + in transit)."""
    result = {'GOLD': ZERO, 'SILVER': ZERO, 'PLATINUM': ZERO, 'DIAMOND_CT': ZERO, 'pieces': ZERO}
    for row in InventoryBalance.objects.filter(tenant=tenant, location__in=locations).values('item__metal') \
            .annotate(net=Sum('net_weight'), qty=Sum('on_hand_qty'), transit=Sum('in_transit_qty')):
        if row['item__metal'] in result:
            result[row['item__metal']] += row['net'] or ZERO
        result['pieces'] += (row['qty'] or ZERO) + (row['transit'] or ZERO)
    carat = JewelleryUnit.objects.filter(tenant=tenant, current_location__in=locations,
                                         status__in=JewelleryUnit.IN_STOCK_STATUSES + ('IN_TRANSIT',)).aggregate(c=Sum('diamond_carat'))['c']
    result['DIAMOND_CT'] = carat or ZERO
    return result


def inventory_kpis(tenant, locations):
    totals = InventoryBalance.objects.filter(tenant=tenant, location__in=locations).aggregate(
        on_hand=Sum('on_hand_qty'), available=Sum('available_qty'), reserved=Sum('reserved_qty'), transit=Sum('in_transit_qty'),
        qc=Sum('qc_qty'), repair=Sum('repair_qty'), damaged=Sum('damaged_qty'), value=Sum('cost_value'),
    )
    totals = {k: v or ZERO for k, v in totals.items()}
    totals['negative_buckets'] = InventoryBalance.objects.filter(tenant=tenant, location__in=locations, on_hand_qty__lt=0).count()
    totals['low_stock'] = len(low_stock(tenant, locations))
    totals.update(weights_by_metal(tenant, locations))
    return totals


def low_stock(tenant, locations):
    from .models import SKU
    skus = SKU.objects.filter(tenant=tenant, location__in=locations, active=True).filter(Q(reorder_point__gt=0) | Q(minimum_stock__gt=0))
    available = {(r['item'], r['variant'], r['location']): r['q'] for r in InventoryBalance.objects.filter(tenant=tenant, location__in=locations)
                 .values('item', 'variant', 'location').annotate(q=Sum('available_qty'))}
    result = []
    for sku in skus.select_related('location', 'item'):
        current = available.get((sku.item_id, sku.variant_id, sku.location_id), ZERO)
        if current < (sku.reorder_point or sku.minimum_stock):
            result.append({'sku': sku, 'available': current})
    return result


def transit_rows(tenant, locations=None):
    today = dj_timezone.localdate()
    lines = TransferOrderLine.objects.filter(tenant=tenant, transfer__status__in=OPEN_TRANSFER_STATUSES, qty_shipped__gt=0) \
        .select_related('transfer', 'transfer__from_location', 'transfer__to_location', 'transfer__transit_location', 'item', 'jewellery_unit')
    if locations is not None:
        lines = lines.filter(Q(transfer__from_location__in=locations) | Q(transfer__to_location__in=locations))
    rows = []
    for line in lines:
        qty = line.qty_in_transit
        if qty <= 0:
            continue
        order = line.transfer
        shipped_on = order.actual_shipment_date or order.shipment_date or today
        days = (today - shipped_on).days
        ratio = qty / line.qty_shipped if line.qty_shipped else ZERO
        rows.append({
            'order': order, 'line': line, 'qty': qty, 'weight': (line.gross_weight * qty / line.quantity) if line.quantity else ZERO,
            'value': money(line.unit_cost * qty), 'shipment_date': shipped_on, 'days': days,
            'overdue': bool(order.expected_receipt_date and today > order.expected_receipt_date),
            'bucket': '0-1' if days <= 1 else '2-3' if days <= 3 else '4-7' if days <= 7 else '>7', 'ratio': ratio,
        })
    return rows


def transfer_kpis(tenant, locations):
    orders = TransferOrder.objects.filter(tenant=tenant).filter(Q(from_location__in=locations) | Q(to_location__in=locations))
    today = dj_timezone.localdate()
    by_status = dict(orders.values_list('status').annotate(c=Count('id')))
    transit = transit_rows(tenant, locations)
    return {
        'today': orders.filter(transfer_date=today).count(),
        'pending_approval': by_status.get('PENDING_APPROVAL', 0) + by_status.get('DRAFT', 0),
        'ready_to_ship': by_status.get('APPROVED', 0) + by_status.get('RELEASED', 0),
        'in_transit': len({row['order'].pk for row in transit}),
        'partially_received': by_status.get('PARTIALLY_RECEIVED', 0),
        'overdue': len({row['order'].pk for row in transit if row['overdue']}),
        'completed': by_status.get('CLOSED', 0) + by_status.get('RECEIVED', 0),
        'cancelled': by_status.get('CANCELLED', 0),
        'transit_value': sum((row['value'] for row in transit), ZERO),
        'transit_weight': sum((row['weight'] for row in transit), ZERO),
    }


def trace(tenant, code):
    """Full life of a jewellery unit: where it is now and every movement it has made."""
    from .services import find_unit
    unit = find_unit(tenant, code)
    if unit is None:
        return None
    entries = list(InventoryLedgerEntry.objects.filter(tenant=tenant, jewellery_unit=unit)
                   .select_related('location', 'bin', 'user').order_by('posting_time', 'id'))
    moves = [e for e in entries if e.quantity > 0 or e.status_to]
    path = []
    for entry in moves:
        if entry.quantity > 0 and (not path or path[-1] != entry.location.name):
            path.append(entry.location.name)
    if unit.status == 'SOLD':
        path.append('Sold')
    last = entries[-1] if entries else None
    previous = next((e.location for e in reversed(entries) if e.quantity > 0 and e.location_id != unit.current_location_id), None)
    first_in = next((e for e in entries if e.transaction_type in INBOUND_TYPES and e.quantity > 0), None)
    return {'unit': unit, 'entries': entries, 'path': path, 'last': last, 'previous_location': previous, 'origin': first_in}


def stock_investigation(tenant, *, item, location, variant=None):
    """'Why is stock different?' - opening + inbound - outbound = current, straight from the ledger."""
    entries = InventoryLedgerEntry.objects.filter(tenant=tenant, item=item, variant=variant, location=location)
    by_type = {r['transaction_type']: r for r in entries.values('transaction_type').annotate(
        qty=Sum('quantity'), gross=Sum('gross_weight'), value=Sum('cost_amount'), n=Count('id'))}
    lines = [{'type': t, 'label': dict(InventoryLedgerEntry.TRANSACTION_TYPES).get(t, t), 'qty': r['qty'] or ZERO,
              'gross': r['gross'] or ZERO, 'value': r['value'] or ZERO, 'entries': r['n']} for t, r in sorted(by_type.items())]
    ledger_total = sum((l['qty'] for l in lines), ZERO)
    field = 'in_transit_qty' if location.is_transit else 'on_hand_qty'
    balance_total = InventoryBalance.objects.filter(tenant=tenant, item=item, variant=variant, location=location).aggregate(q=Sum(field))['q'] or ZERO
    return {'lines': lines, 'ledger_total': ledger_total, 'balance_total': balance_total, 'reconciled': ledger_total == balance_total}


def stock_card(tenant, *, item, location, variant=None, date_from=None, date_to=None):
    entries = InventoryLedgerEntry.objects.filter(tenant=tenant, item=item, variant=variant, location=location).exclude(quantity=0)
    opening = {'qty': ZERO, 'gross': ZERO, 'net': ZERO, 'value': ZERO}
    if date_from:
        before = entries.filter(posting_date__lt=date_from).aggregate(q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), v=Sum('cost_amount'))
        opening = {'qty': before['q'] or ZERO, 'gross': before['g'] or ZERO, 'net': before['n'] or ZERO, 'value': before['v'] or ZERO}
        entries = entries.filter(posting_date__gte=date_from)
    if date_to:
        entries = entries.filter(posting_date__lte=date_to)
    running = dict(opening)
    rows = []
    for entry in entries.order_by('posting_date', 'id'):
        running = {'qty': running['qty'] + entry.quantity, 'gross': running['gross'] + entry.gross_weight,
                   'net': running['net'] + entry.net_weight, 'value': running['value'] + entry.cost_amount}
        rows.append({'entry': entry, 'qty_in': entry.quantity if entry.quantity > 0 else None,
                     'qty_out': -entry.quantity if entry.quantity < 0 else None, **{f'balance_{k}': v for k, v in running.items()}})
    return {'opening': opening, 'rows': rows, 'closing': running}


def movement_entries(tenant, locations, filters):
    """Inventory movement report query (ledger), honouring the report filters."""
    qs = InventoryLedgerEntry.objects.filter(tenant=tenant, location__in=locations) \
        .select_related('item', 'sku', 'location', 'from_location', 'to_location', 'user', 'jewellery_unit')
    mapping = {'date_from': 'posting_date__gte', 'date_to': 'posting_date__lte', 'location': 'location_id',
               'transaction_type': 'transaction_type', 'metal': 'item__metal', 'purity': 'item__purity',
               'category': 'item__category__iexact', 'document': 'document_no__icontains', 'user': 'user__username__icontains',
               'barcode': 'barcode', 'huid': 'huid__iexact', 'sku': 'sku__code__icontains', 'item': 'item_id'}
    for key, lookup in mapping.items():
        value = filters.get(key)
        if value:
            qs = qs.filter(**{lookup: value})
    return qs.order_by('-posting_date', '-id')


def pct(part, whole):
    return (Decimal(part) * 100 / whole).quantize(Decimal('0.1')) if whole else ZERO
