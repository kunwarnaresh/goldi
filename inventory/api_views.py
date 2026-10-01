"""Tenant-aware inventory REST API. Tenant = the authenticated user's workspace; ids from other tenants 404."""
from functools import wraps

from django.http import Http404
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import reports, services
from .engine import D, InventoryError, availability_by_location, availability_totals
from .models import (
    Bin, InventoryAdjustment, InventoryBalance, Item, ItemVariant, Location, PhysicalCount, SKU,
    TransferOrder, TransferRequest,
)
from .tenancy import allowed_locations, require_location


def inventory_api(view):
    """Resolve the actor from the session and turn posting errors into 400s."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        actor = services.actor_from_request(request)
        try:
            return view(request, actor, *args, **kwargs)
        except InventoryError as exc:
            return Response({'error': str(exc)}, status=400)
    return wrapper


def obj(model, actor, pk, **filters):
    if pk in (None, ''):
        return None
    try:
        return model.objects.get(tenant=actor.tenant, pk=int(pk), **filters)
    except (model.DoesNotExist, ValueError, TypeError):
        raise Http404(f'{model.__name__} not found.')


def visible_location(actor, pk, action='view'):
    location = obj(Location, actor, pk)
    require_location(actor.tenant, actor.user, location, action)
    return location


def num(value):
    return str(value) if value is not None else None


def location_json(location):
    return {'id': location.pk, 'code': location.code, 'name': location.name, 'type': location.location_type,
            'city': location.city, 'gstin': location.gstin, 'active': location.active, 'blocked': location.blocked,
            'is_warehouse': location.is_warehouse, 'allow_pos': location.allow_pos, 'bin_mandatory': location.bin_mandatory}


def sku_json(sku):
    return {'id': sku.pk, 'code': sku.code, 'item': sku.item_id, 'item_no': sku.item.item_no, 'variant': sku.variant_id,
            'location': sku.location_id, 'location_code': sku.location.code, 'metal': sku.metal, 'purity': sku.purity,
            'reorder_point': num(sku.reorder_point), 'maximum_stock': num(sku.maximum_stock), 'safety_stock': num(sku.safety_stock),
            'unit_cost': num(sku.unit_cost), 'average_cost': num(sku.average_cost), 'blocked': sku.blocked}


def availability_json(rows):
    keys = ('on_hand', 'reserved', 'available', 'blocked', 'qc', 'repair', 'damaged', 'picked', 'in_transit', 'gross_weight',
            'net_weight', 'cost_value', 'on_purchase', 'transfer_in', 'transfer_out', 'sales_demand', 'projected')
    return [{'location': row['location'].pk, 'location_code': row['location'].code, **{k: num(row[k]) for k in keys}} for row in rows]


def transfer_json(order):
    return {
        'id': order.pk, 'transfer_no': order.transfer_no, 'status': order.status, 'from_location': order.from_location.code,
        'to_location': order.to_location.code, 'transit_location': order.transit_location.code if order.transit_location else None,
        'direct_transfer': order.direct_transfer, 'transfer_date': order.transfer_date, 'expected_receipt_date': order.expected_receipt_date,
        'required_approval_level': order.required_approval_level,
        'lines': [{'id': l.pk, 'line_no': l.line_no, 'item_no': l.item.item_no, 'sku': l.sku.code if l.sku else None,
                   'barcode': l.jewellery_unit.barcode if l.jewellery_unit else None, 'quantity': num(l.quantity),
                   'qty_shipped': num(l.qty_shipped), 'qty_received': num(l.qty_received), 'qty_in_transit': num(l.qty_in_transit),
                   'qty_outstanding': num(l.qty_outstanding), 'qty_damaged': num(l.qty_damaged), 'status': l.status}
                  for l in order.lines.select_related('item', 'sku', 'jewellery_unit')],
    }


def visible_transfer(actor, pk):
    order = obj(TransferOrder, actor, pk)
    permitted = allowed_locations(actor.tenant, actor.user)
    if not permitted.filter(pk__in=[order.from_location_id, order.to_location_id]).exists():
        raise Http404('Transfer not found.')
    return order


# ---------------------------------------------------------------- masters

@api_view(['GET'])
@inventory_api
def location_list(request, actor):
    return Response({'results': [location_json(l) for l in allowed_locations(actor.tenant, actor.user).order_by('code')]})


@api_view(['GET'])
@inventory_api
def location_detail(request, actor, pk):
    location = obj(Location, actor, pk)
    require_location(actor.tenant, actor.user, location, 'view')
    return Response(location_json(location))


@api_view(['GET'])
@inventory_api
def sku_list(request, actor):
    skus = SKU.objects.filter(tenant=actor.tenant, location__in=allowed_locations(actor.tenant, actor.user)).select_related('item', 'location')
    if request.GET.get('location'):
        skus = skus.filter(location=visible_location(actor, request.GET['location']))
    if request.GET.get('q'):
        skus = skus.filter(code__icontains=request.GET['q'])
    return Response({'results': [sku_json(s) for s in skus.order_by('code')[:500]]})


@api_view(['GET'])
@inventory_api
def sku_detail(request, actor, pk):
    sku = obj(SKU, actor, pk)
    require_location(actor.tenant, actor.user, sku.location, 'view')
    return Response({**sku_json(sku), 'availability': availability_json(availability_by_location(actor.tenant, sku=sku))})


@api_view(['GET'])
@inventory_api
def unit_lookup(request, actor):
    unit = services.find_unit(actor.tenant, request.GET.get('code'))
    if unit is None or (unit.current_location and not allowed_locations(actor.tenant, actor.user).filter(pk=unit.current_location_id).exists()):
        raise Http404('Jewellery unit not found.')
    return Response({'id': unit.pk, 'unit_no': unit.unit_no, 'barcode': unit.barcode, 'serial_no': unit.serial_no, 'huid': unit.huid,
                     'item_no': unit.item.item_no, 'sku': unit.sku.code if unit.sku else None, 'status': unit.status,
                     'location': unit.current_location.code if unit.current_location else None,
                     'bin': unit.current_bin.code if unit.current_bin else None, 'metal': unit.metal, 'purity': unit.purity,
                     'gross_weight': num(unit.gross_weight), 'net_metal_weight': num(unit.net_metal_weight)})


# ---------------------------------------------------------------- inventory

@api_view(['GET'])
@inventory_api
def inventory_balances(request, actor):
    permitted = allowed_locations(actor.tenant, actor.user)
    balances = InventoryBalance.objects.filter(tenant=actor.tenant, location__in=permitted) \
        .exclude(on_hand_qty=0, in_transit_qty=0).select_related('item', 'location', 'bin', 'sku', 'jewellery_unit')
    if request.GET.get('location'):
        balances = balances.filter(location=visible_location(actor, request.GET['location']))
    if request.GET.get('item'):
        balances = balances.filter(item_id=request.GET['item'])
    return Response({'results': [{
        'item_no': b.item.item_no, 'sku': b.sku.code if b.sku else None, 'location_code': b.location.code,
        'bin': b.bin.code if b.bin else None, 'barcode': b.jewellery_unit.barcode if b.jewellery_unit else None,
        'on_hand': num(b.on_hand_qty), 'reserved': num(b.reserved_qty), 'available': num(b.available_qty),
        'in_transit': num(b.in_transit_qty), 'qc': num(b.qc_qty), 'damaged': num(b.damaged_qty),
        'gross_weight': num(b.gross_weight), 'net_weight': num(b.net_weight), 'cost_value': num(b.cost_value),
    } for b in balances[:1000]]})


@api_view(['GET'])
@inventory_api
def inventory_availability(request, actor):
    item = obj(Item, actor, request.GET.get('item'))
    sku = obj(SKU, actor, request.GET.get('sku'))
    locations = allowed_locations(actor.tenant, actor.user)
    if request.GET.get('location'):
        locations = [visible_location(actor, request.GET['location'])]
    if sku is not None:
        require_location(actor.tenant, actor.user, sku.location, 'view')
    rows = availability_by_location(actor.tenant, item=item, sku=sku, locations=locations)
    totals = availability_totals(rows)
    return Response({'results': availability_json(rows), 'totals': {k: num(v) for k, v in totals.items()}})


@api_view(['GET'])
@inventory_api
def item_availability(request, actor, pk):
    item = obj(Item, actor, pk)
    variant = obj(ItemVariant, actor, request.GET.get('variant'), item=item)
    rows = availability_by_location(actor.tenant, item=item, variant=variant, locations=allowed_locations(actor.tenant, actor.user))
    return Response({'item': item.item_no, 'results': availability_json(rows),
                     'totals': {k: num(v) for k, v in availability_totals(rows).items()}})


@api_view(['GET'])
@inventory_api
def inventory_transit(request, actor):
    rows = reports.transit_rows(actor.tenant, allowed_locations(actor.tenant, actor.user))
    return Response({'results': [{
        'transfer_no': r['order'].transfer_no, 'from': r['order'].from_location.code, 'to': r['order'].to_location.code,
        'item_no': r['line'].item.item_no, 'qty': num(r['qty']), 'weight': num(r['weight']), 'value': num(r['value']),
        'shipment_date': r['shipment_date'], 'expected_receipt': r['order'].expected_receipt_date, 'days_in_transit': r['days'],
        'overdue': r['overdue'],
    } for r in rows]})


# ---------------------------------------------------------------- transfers

def _transfer_lines(actor, specs):
    lines = []
    for spec in specs or []:
        if spec.get('barcode'):
            unit = services.find_unit(actor.tenant, spec['barcode'])
            if unit is None:
                raise InventoryError(f'Unknown barcode {spec["barcode"]}.')
            lines.append({'unit': unit})
        else:
            lines.append({'sku': obj(SKU, actor, spec.get('sku')), 'item': obj(Item, actor, spec.get('item')),
                          'variant': obj(ItemVariant, actor, spec.get('variant')), 'quantity': spec.get('quantity'),
                          'from_bin': obj(Bin, actor, spec.get('from_bin')), 'to_bin': obj(Bin, actor, spec.get('to_bin'))})
    return lines


@api_view(['GET', 'POST'])
@inventory_api
def transfer_request_list(request, actor):
    if request.method == 'POST':
        data = request.data
        req = services.create_transfer_request(
            actor, to_location=visible_location(actor, data.get('to_location'), 'create_transfer'),
            from_location=obj(Location, actor, data.get('from_location')), priority=data.get('priority', 'NORMAL'),
            reason=data.get('reason', ''), lines=[{'item': obj(Item, actor, l.get('item')), 'variant': obj(ItemVariant, actor, l.get('variant')),
                                                   'quantity': l.get('quantity')} for l in data.get('lines', [])])
        return Response({'id': req.pk, 'request_no': req.request_no, 'status': req.status}, status=201)
    permitted = allowed_locations(actor.tenant, actor.user)
    requests = TransferRequest.objects.filter(tenant=actor.tenant, to_location__in=permitted).select_related('to_location', 'from_location')
    return Response({'results': [{'id': r.pk, 'request_no': r.request_no, 'to': r.to_location.code,
                                  'from': r.from_location.code if r.from_location else None, 'status': r.status,
                                  'priority': r.priority, 'date': r.request_date} for r in requests[:500]]})


@api_view(['GET', 'POST'])
@inventory_api
def transfer_order_list(request, actor):
    if request.method == 'POST':
        data = request.data
        order = services.create_transfer_order(
            actor, from_location=visible_location(actor, data.get('from_location'), 'create_transfer'),
            to_location=obj(Location, actor, data.get('to_location')), lines=_transfer_lines(actor, data.get('lines')),
            direct=bool(data.get('direct')), priority=data.get('priority', 'NORMAL'), reason=data.get('reason', ''))
        return Response(transfer_json(order), status=201)
    permitted = allowed_locations(actor.tenant, actor.user)
    orders = TransferOrder.objects.filter(tenant=actor.tenant, from_location__in=permitted) | \
        TransferOrder.objects.filter(tenant=actor.tenant, to_location__in=permitted)
    if request.GET.get('status'):
        orders = orders.filter(status=request.GET['status'])
    return Response({'results': [{'id': o.pk, 'transfer_no': o.transfer_no, 'status': o.status, 'from': o.from_location.code,
                                  'to': o.to_location.code, 'date': o.transfer_date}
                                 for o in orders.distinct().select_related('from_location', 'to_location')[:500]]})


@api_view(['GET'])
@inventory_api
def transfer_order_detail(request, actor, pk):
    return Response(transfer_json(visible_transfer(actor, pk)))


@api_view(['POST'])
@inventory_api
def transfer_order_approve(request, actor, pk):
    order = services.approve_transfer(actor, visible_transfer(actor, pk), request.data.get('quantities'))
    return Response(transfer_json(order))


@api_view(['POST'])
@inventory_api
def transfer_order_ship(request, actor, pk):
    order = visible_transfer(actor, pk)
    shipment = services.ship_transfer(actor, order, quantities=request.data.get('quantities'), barcodes=request.data.get('barcodes'))
    return Response({'shipment_no': shipment.shipment_no, **transfer_json(TransferOrder.objects.get(pk=order.pk))})


@api_view(['POST'])
@inventory_api
def transfer_order_receive(request, actor, pk):
    order = visible_transfer(actor, pk)
    lines = [{**line, 'to_bin': obj(Bin, actor, line.get('to_bin'))} for line in request.data.get('lines') or []]
    receipt = services.receive_transfer(actor, order, lines=lines or None, barcodes=request.data.get('barcodes'),
                                        damaged_barcodes=request.data.get('damaged_barcodes'))
    return Response({'receipt_no': receipt.receipt_no, **transfer_json(TransferOrder.objects.get(pk=order.pk))})


# ---------------------------------------------------------------- journals

@api_view(['POST'])
@inventory_api
def adjustment_create(request, actor):
    data = request.data
    lines = []
    for spec in data.get('lines', []):
        unit = services.find_unit(actor.tenant, spec['barcode']) if spec.get('barcode') else None
        lines.append({'adjustment_type': spec.get('adjustment_type'), 'unit': unit, 'sku': obj(SKU, actor, spec.get('sku')),
                      'item': obj(Item, actor, spec.get('item')), 'bin': obj(Bin, actor, spec.get('bin')),
                      'quantity': spec.get('quantity', 1 if unit else 0), 'unit_cost': spec.get('unit_cost'),
                      'gross_weight': spec.get('gross_weight'), 'net_weight': spec.get('net_weight'),
                      'from_status': spec.get('from_status', ''), 'to_status': spec.get('to_status', '')})
    adjustment = services.create_adjustment(actor, location=visible_location(actor, data.get('location'), 'adjust'),
                                            reason_code=data.get('reason_code'), lines=lines, reference=data.get('reference', ''),
                                            remarks=data.get('remarks', ''))
    if data.get('submit'):
        adjustment = services.submit_adjustment(actor, adjustment)
    return Response({'id': adjustment.pk, 'adjustment_no': adjustment.adjustment_no, 'status': adjustment.status}, status=201)


@api_view(['POST'])
@inventory_api
def adjustment_action(request, actor, pk, action):
    adjustment = obj(InventoryAdjustment, actor, pk)
    handler = {'submit': services.submit_adjustment, 'approve': services.approve_adjustment, 'post': services.post_adjustment}.get(action)
    if handler is None:
        raise Http404
    adjustment = handler(actor, adjustment)
    return Response({'id': adjustment.pk, 'adjustment_no': adjustment.adjustment_no, 'status': adjustment.status})


@api_view(['POST'])
@inventory_api
def count_create(request, actor):
    count = services.create_count(actor, location=visible_location(actor, request.data.get('location'), 'count'),
                                  blind=bool(request.data.get('blind')))
    count = services.snapshot_count(actor, count)
    return Response({'id': count.pk, 'count_no': count.count_no, 'status': count.status, 'lines': count.lines.count()}, status=201)


@api_view(['POST'])
@inventory_api
def count_action(request, actor, pk, action):
    count = obj(PhysicalCount, actor, pk)
    if action == 'scan':
        line = services.scan_count(actor, count, request.data.get('code'), request.data.get('gross_weight'))
        return Response({'line': line.pk, 'counted_qty': num(line.counted_qty), 'unexpected': line.unexpected})
    if action == 'submit':
        adjustment = services.submit_count(actor, count)
        return Response({'count_no': count.count_no, 'adjustment_no': adjustment.adjustment_no if adjustment else None})
    raise Http404


@api_view(['POST'])
@inventory_api
def reclassification_create(request, actor):
    lines = []
    for spec in request.data.get('lines', []):
        unit = services.find_unit(actor.tenant, spec['barcode']) if spec.get('barcode') else None
        line = {'unit': unit, 'item': obj(Item, actor, spec.get('item')), 'quantity': spec.get('quantity'),
                'from_location': obj(Location, actor, spec.get('from_location')), 'to_status': spec.get('to_status') or None,
                'from_bin': obj(Bin, actor, spec.get('from_bin'))}
        if spec.get('to_location'):
            line['to_location'] = obj(Location, actor, spec['to_location'])
        if 'to_bin' in spec:
            line['to_bin'] = obj(Bin, actor, spec.get('to_bin'))
        lines.append(line)
    reclass = services.post_reclassification(actor, lines=lines, reason=request.data.get('reason', ''))
    return Response({'reclass_no': reclass.reclass_no, 'status': reclass.status}, status=201)


@api_view(['POST'])
@inventory_api
def pos_sale(request, actor):
    """Sell from a POS terminal: location comes from terminal -> store -> location."""
    from erp.models import POSTerminal
    terminal = POSTerminal.objects.filter(pk=request.data.get('terminal'), is_active=True).select_related('store').first()
    if terminal is None:
        raise Http404('Terminal not found.')
    entry = services.sell_from_pos(actor, terminal=terminal, barcode=request.data.get('barcode'),
                                   sku=obj(SKU, actor, request.data.get('sku')), quantity=D(request.data.get('quantity') or 1),
                                   document_no=request.data.get('document_no') or services.next_number(actor.tenant, 'POS_SALE'))
    return Response({'entry': entry.pk, 'document_no': entry.document_no}, status=201)
