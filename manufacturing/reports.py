"""Manufacturing traceability: a finished piece back to the materials and receipts it came from, and a material lot forward
to the production orders and pieces it went into."""
from django.db.models import Sum

from inventory.models import InventoryLedgerEntry

from .models import ConsumptionEntry, OutputUnit

# Inventory transactions that bring material into stock - the "source" end of a backward trace.
RECEIPT_TYPES = ('PURCHASE', 'OPENING', 'TRANSFER_RECEIPT', 'ADJUSTMENT_POSITIVE', 'RETURN')


def _receipts(tenant, entry):
    """Inbound stock movements that could have supplied a consumption: same item and location (and lot when tracked),
    posted no later than the consumption."""
    qs = InventoryLedgerEntry.objects.filter(tenant=tenant, item_id=entry.item_id, location_id=entry.location_id,
                                             transaction_type__in=RECEIPT_TYPES, quantity__gt=0)
    if entry.lot_no:
        qs = qs.filter(lot_no=entry.lot_no)
    if entry.inventory_entry_id:
        qs = qs.filter(id__lt=entry.inventory_entry_id)
    return list(qs.order_by('posting_date', 'id'))


def trace_unit(tenant, unit):
    """Backward trace of a serialized piece: jewellery unit -> production order -> consumed materials -> source receipts."""
    produced = OutputUnit.objects.filter(tenant=tenant, jewellery_unit=unit).select_related('order', 'output').first()
    if produced is None:
        return None
    order = produced.order
    consumption = (ConsumptionEntry.objects.filter(tenant=tenant, order=order).select_related('item', 'location', 'operation')
                   .order_by('id'))
    materials = [{
        'item': entry.item.item_no, 'description': entry.item.description, 'lot_no': entry.lot_no, 'huid': entry.huid,
        'quantity': entry.quantity, 'fine_weight': entry.fine_weight, 'cost_amount': entry.cost_amount, 'location': entry.location,
        'operation': entry.operation, 'entry': entry, 'receipt': _receipts(tenant, entry),
    } for entry in consumption]
    return {'unit': unit, 'output_unit': produced, 'output': produced.output, 'order': order, 'materials': materials}


def trace_lot(tenant, item, lot_no=''):
    """Forward trace of a material (optionally one lot): consumption -> production orders -> serialized pieces produced."""
    consumption = ConsumptionEntry.objects.filter(tenant=tenant, item=item)
    if lot_no:
        consumption = consumption.filter(lot_no=lot_no)
    order_ids = consumption.values_list('order_id', flat=True).distinct()
    units = OutputUnit.objects.filter(tenant=tenant, order_id__in=order_ids, reversed=False).select_related('order', 'jewellery_unit')
    totals = consumption.values('order_id').annotate(qty=Sum('quantity'), fine=Sum('fine_weight'), cost=Sum('cost_amount'))
    orders = sorted({u.order for u in units} | {e.order for e in consumption.select_related('order')}, key=lambda o: o.pk)
    return {'item': item, 'lot_no': lot_no, 'orders': orders, 'consumed': {row['order_id']: row for row in totals},
            'units': list(units.order_by('id'))}
