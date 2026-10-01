"""Inventory document workflows. Each one validates permissions, then posts through InventoryPostingEngine."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone as dj_timezone

from .engine import D, InventoryError, InventoryPostingEngine, ZERO, available_qty
from .models import (
    ApprovalRule, Bin, InventoryAdjustment, InventoryAdjustmentLine, InventoryAuditLog, InventoryBalance,
    InventoryReservation, JewelleryUnit, Location, NumberSeries, PhysicalCount, PhysicalCountLine, Reclassification,
    ReclassificationLine, ReplenishmentLine, SKU, TransferOrder, TransferOrderLine, TransferReceipt, TransferReceiptLine,
    TransferRequest, TransferRequestLine, TransferRoute, TransferShipment, TransferShipmentLine,
)
from .tenancy import approval_level, get_current_tenant, is_tenant_admin, require_location

PREFIXES = {
    'TRANSFER_ORDER': 'TO', 'TRANSFER_REQUEST': 'TRQ', 'TRANSFER_SHIPMENT': 'TSH', 'TRANSFER_RECEIPT': 'TRC',
    'ADJUSTMENT': 'ADJ', 'RECLASS': 'RCL', 'COUNT': 'PHY', 'UNIT': 'JWL', 'OPENING': 'OPN', 'POS_SALE': 'PSL',
    'RESERVATION': 'RSV',
}


@dataclass
class Actor:
    """Who is doing what, from where. Tenant always comes from the server-side session."""
    tenant: object
    user: object
    ip: str = None
    device: str = ''


def actor_from_request(request):
    return Actor(tenant=get_current_tenant(request), user=request.user,
                 ip=request.META.get('REMOTE_ADDR'), device=request.META.get('HTTP_USER_AGENT', '')[:200])


def next_number(tenant, document_type):
    with transaction.atomic():
        series = NumberSeries.objects.select_for_update().filter(tenant=tenant, document_type=document_type).first()
        if series is None:
            try:
                with transaction.atomic():
                    NumberSeries.objects.create(tenant=tenant, document_type=document_type, prefix=PREFIXES.get(document_type, 'DOC'))
            except IntegrityError:
                pass
            series = NumberSeries.objects.select_for_update().get(tenant=tenant, document_type=document_type)
        number = f'{series.prefix}-{series.next_number:0{series.padding}d}'
        series.next_number += 1
        series.save(update_fields=['next_number', 'updated_at'])
        return number


def audit(actor, action, document_type, document_no='', *, location=None, old=None, new=None, reason=''):
    InventoryAuditLog.objects.create(
        tenant=actor.tenant, user=actor.user, ip_address=actor.ip, device=actor.device, location=location,
        action=action, document_type=document_type, document_no=document_no, old_value=old or {}, new_value=new or {},
        reason=reason[:250],
    )


def _get(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=getattr(pk, 'pk', pk)).first()
    if obj is None:
        raise InventoryError(f'{model._meta.verbose_name.title()} not found.')
    return obj


def find_unit(tenant, code):
    """Resolve a scanned barcode / serial / HUID / unit number to a jewellery unit in this tenant."""
    code = (code or '').strip()
    if not code:
        return None
    units = JewelleryUnit.objects.filter(tenant=tenant).select_related('item', 'sku', 'current_location', 'current_bin')
    return (units.filter(barcode=code).first() or units.filter(serial_no=code).first()
            or units.filter(huid=code.upper()).first() or units.filter(unit_no=code).first())


def default_transit(tenant):
    return Location.objects.filter(tenant=tenant, location_type='TRANSIT', active=True, blocked=False).order_by('id').first()


def estimated_unit_cost(tenant, item, variant, location, sku=None):
    totals = InventoryBalance.objects.filter(tenant=tenant, item=item, variant=variant, location=location) \
        .aggregate(q=Sum('on_hand_qty'), v=Sum('cost_value'))
    if totals['q']:
        return (totals['v'] / totals['q']).quantize(Decimal('0.0001'))
    return (sku.unit_cost if sku and sku.unit_cost else item.standard_cost)


def sku_for(tenant, item, variant, location):
    return SKU.objects.filter(tenant=tenant, item=item, variant=variant, location=location).first()


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

def register_unit(actor, *, sku, barcode, serial_no, huid=None, **attributes):
    """Create a jewellery unit record (not yet in stock - post it with a purchase/opening receipt)."""
    tenant = actor.tenant
    if sku.tenant_id != tenant.id:
        raise InventoryError('SKU not found.')
    if not sku.item.serial_tracking:
        raise InventoryError(f'Item {sku.item.item_no} is not serialized.')
    huid = (huid or '').strip().upper() or None
    if JewelleryUnit.objects.filter(tenant=tenant, barcode=barcode).exists():
        raise InventoryError(f'Barcode {barcode} already exists.')
    if JewelleryUnit.objects.filter(tenant=tenant, serial_no=serial_no).exists():
        raise InventoryError(f'Serial {serial_no} already exists.')
    if huid and JewelleryUnit.objects.filter(tenant=tenant, huid=huid).exists():
        raise InventoryError(f'HUID {huid} already exists.')
    defaults = {'metal': sku.metal, 'purity': sku.purity, 'gross_weight': sku.gross_weight, 'stone_weight': sku.stone_weight,
                'other_weight': sku.other_weight, 'making_charge': sku.making_charge, 'wastage_percent': sku.wastage_percent,
                'retail_price': sku.retail_price, 'hallmark': sku.hallmark}
    defaults.update({k: v for k, v in attributes.items() if v is not None})
    unit = JewelleryUnit(tenant=tenant, unit_no=next_number(tenant, 'UNIT'), item=sku.item, sku=sku, variant=sku.variant,
                         barcode=barcode, serial_no=serial_no, huid=huid, created_by=actor.user, **defaults)
    try:
        unit.full_clean(exclude=['current_location', 'current_bin'])
    except Exception as exc:  # surface model validation (weights, HUID format) as an inventory error
        raise InventoryError('; '.join(getattr(exc, 'messages', [str(exc)])))
    try:
        unit.save()
    except IntegrityError:
        raise InventoryError('Duplicate barcode, serial or HUID.')
    audit(actor, 'create', 'JEWELLERY_UNIT', unit.unit_no, new={'barcode': barcode, 'huid': huid})
    return unit


@transaction.atomic
def post_receipt(actor, *, location, sku, quantity=1, unit_cost=None, bin=None, unit=None, transaction_type='OPENING',
                 document_no=None, into_state=None, gross_weight=None, net_weight=None):
    """Put stock into a location (opening balance, purchase receipt, customer return)."""
    require_location(actor.tenant, actor.user, location, 'adjust' if transaction_type in ('OPENING', 'ADJUSTMENT_POSITIVE') else 'receive')
    if transaction_type == 'PURCHASE' and not location.allow_purchase:
        raise InventoryError(f'Purchases are not allowed at {location.code}.')
    document_no = document_no or next_number(actor.tenant, 'OPENING')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type=transaction_type, document_no=document_no)
    entry = engine.receive(transaction_type=transaction_type, item=sku.item, variant=sku.variant, sku=sku, location=location,
                           quantity=quantity, unit_cost=unit_cost, bin=bin or sku.default_bin, unit=unit, into_state=into_state,
                           gross_weight=gross_weight, net_weight=net_weight)
    audit(actor, 'post', transaction_type, document_no, location=location, new={'sku': sku.code, 'qty': str(quantity)})
    return entry


@transaction.atomic
def post_sale(actor, *, location, barcode=None, sku=None, quantity=1, document_no, reservation=None):
    """Sell from a location. Serialized jewellery is identified by its scanned barcode/HUID."""
    require_location(actor.tenant, actor.user, location, 'sell')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='SALE', document_no=document_no)
    if barcode:
        unit = find_unit(actor.tenant, barcode)
        if unit is None:
            raise InventoryError(f'No jewellery unit with barcode/HUID {barcode}.')
        entry = engine.issue(transaction_type='SALE', item=unit.item, variant=unit.variant, location=location, unit=unit,
                             sku=sku_for(actor.tenant, unit.item, unit.variant, location), reservation=reservation)
    else:
        if sku is None or sku.location_id != location.id:
            raise InventoryError('Select a SKU of this location.')
        entry = engine.issue(transaction_type='SALE', item=sku.item, variant=sku.variant, sku=sku, location=location,
                             quantity=quantity, reservation=reservation)
    audit(actor, 'post', 'SALE', document_no, location=location, new={'barcode': barcode or '', 'qty': str(quantity)})
    return entry


def sell_from_pos(actor, *, terminal, barcode=None, sku=None, quantity=1, document_no):
    """POS sale: the location comes from terminal -> store -> location, never from the cashier."""
    location = Location.objects.filter(tenant=actor.tenant, store=terminal.store).first()
    if location is None:
        raise InventoryError(f'Store {terminal.store} is not linked to an inventory location.')
    if not location.allow_pos:
        raise InventoryError(f'POS is not enabled at {location.code}.')
    return post_sale(actor, location=location, barcode=barcode, sku=sku, quantity=quantity, document_no=document_no)


# ---------------------------------------------------------------------------
# Transfer requests
# ---------------------------------------------------------------------------

@transaction.atomic
def create_transfer_request(actor, *, to_location, lines, from_location=None, priority='NORMAL', reason='', remarks=''):
    require_location(actor.tenant, actor.user, to_location, 'create_transfer')
    if not lines:
        raise InventoryError('Add at least one line.')
    request = TransferRequest.objects.create(
        tenant=actor.tenant, request_no=next_number(actor.tenant, 'TRANSFER_REQUEST'), from_location=from_location,
        to_location=to_location, requested_by=actor.user, priority=priority, reason=reason, remarks=remarks, created_by=actor.user,
    )
    for number, line in enumerate(lines, 1):
        item = line['item']
        if item.tenant_id != actor.tenant.id or D(line['quantity']) <= 0:
            raise InventoryError(f'Line {number}: invalid item or quantity.')
        TransferRequestLine.objects.create(tenant=actor.tenant, request=request, line_no=number * 10000, item=item,
                                           variant=line.get('variant'), requested_qty=D(line['quantity']), created_by=actor.user)
    audit(actor, 'create', 'TRANSFER_REQUEST', request.request_no, location=to_location)
    return request


@transaction.atomic
def approve_transfer_request(actor, request, *, approved_quantities=None, from_location=None, convert=True):
    request = TransferRequest.objects.select_for_update().get(pk=request.pk, tenant=actor.tenant)
    if request.status != 'REQUESTED':
        raise InventoryError(f'Request is {request.get_status_display().lower()}.')
    source = from_location or request.from_location
    if source is None:
        raise InventoryError('Choose the location that will supply this request.')
    require_location(actor.tenant, actor.user, source, 'approve_transfer')
    approved_quantities = {int(k): D(v) for k, v in (approved_quantities or {}).items()}
    for line in request.lines.all():
        qty = approved_quantities.get(line.pk, line.requested_qty)
        if qty < 0 or qty > line.requested_qty:
            raise InventoryError(f'Approved quantity for line {line.line_no} must be between 0 and {line.requested_qty}.')
        line.approved_qty = qty
        line.save(update_fields=['approved_qty', 'updated_at'])
    request.from_location, request.status, request.approved_by = source, 'APPROVED', actor.user
    request.save()
    audit(actor, 'approve', 'TRANSFER_REQUEST', request.request_no, location=source)
    if convert:
        convert_request_to_transfer(actor, request)
    return request


@transaction.atomic
def reject_transfer_request(actor, request, reason=''):
    request = TransferRequest.objects.select_for_update().get(pk=request.pk, tenant=actor.tenant)
    if request.status not in ('REQUESTED', 'APPROVED'):
        raise InventoryError('Only open requests can be rejected.')
    if request.from_location:
        require_location(actor.tenant, actor.user, request.from_location, 'approve_transfer')
    elif not is_tenant_admin(actor.tenant, actor.user):
        raise PermissionDenied('Only a supplying-location approver can reject this request.')
    request.status = 'REJECTED'
    request.remarks = (request.remarks + f'\nRejected: {reason}').strip()
    request.save()
    audit(actor, 'reject', 'TRANSFER_REQUEST', request.request_no, reason=reason)
    return request


@transaction.atomic
def convert_request_to_transfer(actor, request):
    if request.status != 'APPROVED':
        raise InventoryError('Approve the request before converting it.')
    lines = [{'item': line.item, 'variant': line.variant, 'quantity': line.approved_qty}
             for line in request.lines.all() if line.approved_qty > 0]
    if not lines:
        raise InventoryError('Nothing was approved on this request.')
    order = create_transfer_order(actor, from_location=request.from_location, to_location=request.to_location, lines=lines,
                                  source_type='REQUEST', priority=request.priority, reason=request.reason,
                                  remarks=f'From request {request.request_no}', permission='approve_transfer')
    request.status, request.transfer_order = 'CONVERTED', order
    request.save(update_fields=['status', 'transfer_order', 'updated_at'])
    return order


# ---------------------------------------------------------------------------
# Transfer orders
# ---------------------------------------------------------------------------

def required_approval_level(order):
    values, weights = {}, {}
    for line in order.lines.select_related('item'):
        values[line.item.metal] = values.get(line.item.metal, ZERO) + line.line_value
        weights[line.item.metal] = weights.get(line.item.metal, ZERO) + line.gross_weight
    total_value, total_weight = sum(values.values(), ZERO), sum(weights.values(), ZERO)
    level = 0
    for rule in ApprovalRule.objects.filter(tenant=order.tenant, document_type='TRANSFER', active=True):
        value = values.get(rule.metal, ZERO) if rule.metal else total_value
        weight = weights.get(rule.metal, ZERO) if rule.metal else total_weight
        if (rule.min_value > 0 and value >= rule.min_value) or (rule.min_weight > 0 and weight >= rule.min_weight):
            level = max(level, rule.approver_level)
    return level


@transaction.atomic
def create_transfer_order(actor, *, from_location, to_location, lines, source_type='MANUAL', direct=False, priority='NORMAL',
                          reason='', remarks='', shipment_date=None, return_reason='', permission='create_transfer'):
    """Create a DRAFT transfer. `lines`: dicts with item/sku/unit, quantity, variant, from_bin, to_bin."""
    tenant = actor.tenant
    require_location(tenant, actor.user, from_location, permission)
    if to_location.tenant_id != tenant.id:
        raise PermissionDenied('Location not found.')
    if not from_location.allow_transfer_out or not from_location.usable:
        raise InventoryError(f'{from_location.code} does not allow outbound transfers.')
    if not to_location.allow_transfer_in or not to_location.usable:
        raise InventoryError(f'{to_location.code} does not allow inbound transfers.')
    route = TransferRoute.objects.filter(tenant=tenant, from_location=from_location, to_location=to_location, active=True).first()
    if route is not None and not route.allow_transfer:
        raise InventoryError(f'Transfers {from_location.code} -> {to_location.code} are not allowed by the transfer route.')
    transit = None
    if direct:
        require_location(tenant, actor.user, from_location, 'direct_transfer')
        if not from_location.allow_direct_transfer:
            raise InventoryError(f'Direct transfers are not enabled at {from_location.code}.')
    else:
        transit = (route.transit_location if route else None) or default_transit(tenant)
        if transit is None:
            raise InventoryError('Create a TRANSIT location (or a transfer route) before transferring stock.')
    if not lines:
        raise InventoryError('Add at least one line.')
    shipment_date = shipment_date or dj_timezone.localdate()
    order = TransferOrder(
        tenant=tenant, transfer_no=next_number(tenant, 'TRANSFER_ORDER'), from_location=from_location, to_location=to_location,
        transit_location=transit, route=route, direct_transfer=direct, requested_by=actor.user, shipment_date=shipment_date,
        expected_receipt_date=shipment_date + timedelta(days=route.transfer_days if route else (0 if direct else 1)),
        priority=priority, source_type=source_type, reason=reason, remarks=remarks, return_reason=return_reason,
        shipping_agent=route.shipping_agent if route else '', created_by=actor.user,
    )
    try:
        order.clean()
    except Exception as exc:
        raise InventoryError('; '.join(getattr(exc, 'messages', [str(exc)])))
    order.save()
    seen_units = set()
    for number, spec in enumerate(lines, 1):
        unit, sku = spec.get('unit'), spec.get('sku')
        if unit is not None:
            unit = JewelleryUnit.objects.filter(pk=unit.pk, tenant=tenant).first()  # never trust a caller's stale copy
            if unit is None or unit.pk in seen_units:
                raise InventoryError(f'Line {number}: invalid or duplicate jewellery unit.')
            if unit.current_location_id != from_location.id or unit.status != 'AVAILABLE':
                raise InventoryError(f'Jewellery unit {unit.barcode} is not available at {from_location.code}.')
            seen_units.add(unit.pk)
            item, variant, quantity = unit.item, unit.variant, Decimal('1')
            sku = unit.sku if unit.sku and unit.sku.location_id == from_location.id else sku_for(tenant, item, variant, from_location)
            gross, net, unit_cost = unit.gross_weight, unit.net_metal_weight, unit.total_cost
        else:
            item = sku.item if sku else spec['item']
            variant = sku.variant if sku else spec.get('variant')
            quantity = D(spec['quantity'])
            if item.tenant_id != tenant.id or quantity <= 0:
                raise InventoryError(f'Line {number}: invalid item or quantity.')
            if item.serial_tracking:
                raise InventoryError(f'Line {number}: {item.item_no} is serialized - add each jewellery unit by barcode.')
            sku = sku if sku and sku.location_id == from_location.id else sku_for(tenant, item, variant, from_location)
            unit_cost = estimated_unit_cost(tenant, item, variant, from_location, sku)
            gross = (sku.gross_weight * quantity) if sku else ZERO
            net = (sku.net_metal_weight * quantity) if sku else ZERO
        for field in ('from_bin', 'to_bin'):
            bin_ = spec.get(field)
            expected = from_location if field == 'from_bin' else to_location
            if bin_ is not None and bin_.location_id != expected.id:
                raise InventoryError(f'Line {number}: {bin_.code} is not a bin of {expected.code}.')
        to_sku = sku_for(tenant, item, variant, to_location)
        TransferOrderLine.objects.create(
            tenant=tenant, transfer=order, line_no=number * 10000, item=item, variant=variant, sku=sku,
            to_sku=to_sku, jewellery_unit=unit, description=item.description,
            from_bin=spec.get('from_bin'), to_bin=spec.get('to_bin') or (to_sku.default_bin if to_sku else None),
            requested_qty=quantity, quantity=quantity, uom=item.base_uom, gross_weight=gross, net_weight=net,
            unit_cost=unit_cost, created_by=actor.user,
        )
    order.required_approval_level = required_approval_level(order)
    order.save(update_fields=['required_approval_level'])
    audit(actor, 'create', 'TRANSFER_ORDER', order.transfer_no, location=from_location,
          new={'from': from_location.code, 'to': to_location.code, 'lines': len(lines)})
    return order


def _lock_order(actor, order):
    return TransferOrder.objects.select_for_update().select_related('from_location', 'to_location', 'transit_location') \
        .get(pk=getattr(order, 'pk', order), tenant=actor.tenant)


@transaction.atomic
def submit_transfer(actor, order):
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.from_location, 'create_transfer')
    if order.status != 'DRAFT':
        raise InventoryError('Only draft transfers can be submitted.')
    order.status = 'PENDING_APPROVAL'
    order.required_approval_level = required_approval_level(order)
    order.save()
    audit(actor, 'submit', 'TRANSFER_ORDER', order.transfer_no, location=order.from_location)
    return order


@transaction.atomic
def approve_transfer(actor, order, approved_quantities=None):
    """Approve and reserve the stock at the source: source AVAILABLE goes down, RESERVED goes up."""
    order = _lock_order(actor, order)
    if order.status not in ('DRAFT', 'PENDING_APPROVAL'):
        raise InventoryError(f'Transfer is {order.get_status_display().lower()}.')
    require_location(actor.tenant, actor.user, order.from_location, 'approve_transfer')
    approved_quantities = {int(k): D(v) for k, v in (approved_quantities or {}).items()}
    for line in order.lines.all():
        if line.pk in approved_quantities:
            qty = approved_quantities[line.pk]
            if qty < 0 or qty > line.requested_qty or (line.jewellery_unit_id and qty not in (0, 1)):
                raise InventoryError(f'Approved quantity for line {line.line_no} must be between 0 and {line.requested_qty}.')
            line.quantity = qty
            line.status = 'CANCELLED' if qty == 0 else 'OPEN'
            line.save(update_fields=['quantity', 'status', 'updated_at'])
    level = required_approval_level(order)
    if approval_level(actor.tenant, actor.user, order.from_location) < level:
        raise PermissionDenied(f'This transfer needs approval level {level} (value/weight threshold).')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='TRANSFER_ORDER', document_no=order.transfer_no)
    for line in order.lines.filter(quantity__gt=0).select_related('item', 'variant', 'sku', 'jewellery_unit', 'from_bin'):
        engine.reserve(item=line.item, variant=line.variant, sku=line.sku, location=order.from_location, quantity=line.quantity,
                       source_type='TRANSFER_ORDER', source_no=order.transfer_no, source_line_no=line.line_no,
                       bin=line.from_bin, unit=line.jewellery_unit)
    if not order.lines.filter(quantity__gt=0).exists():
        raise InventoryError('Nothing left to transfer - cancel the transfer instead.')
    order.status, order.approved_by, order.required_approval_level = 'APPROVED', actor.user, level
    order.save()
    audit(actor, 'approve', 'TRANSFER_ORDER', order.transfer_no, location=order.from_location, new={'level': level})
    return order


@transaction.atomic
def release_transfer(actor, order):
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.from_location, 'ship')
    if order.status != 'APPROVED':
        raise InventoryError('Only approved transfers can be released.')
    order.status = 'RELEASED'
    order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'release', 'TRANSFER_ORDER', order.transfer_no, location=order.from_location)
    return order


def _line_reservations(order, line):
    return InventoryReservation.objects.filter(tenant=order.tenant, source_type='TRANSFER_ORDER', source_no=order.transfer_no,
                                               source_line_no=line.line_no, status='ACTIVE').select_related('bin').order_by('id')


def _refresh_order_status(order):
    lines = list(order.lines.all())
    for line in lines:
        target = line.quantity - line.qty_cancelled
        if line.quantity == 0 or target <= 0:
            line.status = 'CANCELLED'
        elif line.qty_received + line.qty_short_closed >= target:
            line.status = 'CLOSED' if line.qty_short_closed or not line.qty_damaged else 'RECEIVED'
        elif line.qty_received > 0:
            line.status = 'PARTIALLY_RECEIVED'
        elif line.qty_shipped >= target:
            line.status = 'SHIPPED'
        elif line.qty_shipped > 0:
            line.status = 'PARTIALLY_SHIPPED'
        line.save(update_fields=['status', 'updated_at'])
    total = sum((l.quantity - l.qty_cancelled for l in lines), ZERO)
    shipped = sum((l.qty_shipped for l in lines), ZERO)
    done = sum((l.qty_received + l.qty_short_closed for l in lines), ZERO)
    received = sum((l.qty_received for l in lines), ZERO)
    if total <= 0:
        order.status = 'CANCELLED'
    elif done >= total:
        # Damaged receipts stay RECEIVED until someone resolves the exception and closes the transfer.
        order.status = 'RECEIVED' if any(l.qty_damaged for l in lines) and order.status != 'CLOSED' else 'CLOSED'
        order.actual_receipt_date = order.actual_receipt_date or dj_timezone.localdate()
    elif received > 0:
        order.status = 'PARTIALLY_RECEIVED'
    elif shipped >= total:
        order.status = 'SHIPPED'
    elif shipped > 0:
        order.status = 'PARTIALLY_SHIPPED'
    order.save()


def _scanned_lines(actor, order, barcodes, qty_attr):
    """Map scanned barcodes to transfer lines. Serialized: one unit per line. Non-serialized: SKU barcode = 1 each."""
    counts = {}
    lines = list(order.lines.select_related('jewellery_unit', 'sku'))
    for code in barcodes:
        code = code.strip()
        if not code:
            continue
        unit = find_unit(actor.tenant, code)
        match = None
        if unit is not None:
            match = next((l for l in lines if l.jewellery_unit_id == unit.pk), None)
            if match is None:
                raise InventoryError(f'{code} ({unit.unit_no}) is not on transfer {order.transfer_no}.')
            if match.pk in counts:
                raise InventoryError(f'{code} was scanned twice.')
        else:
            match = next((l for l in lines if l.jewellery_unit_id is None and l.sku and l.sku.barcode == code), None)
            if match is None:
                raise InventoryError(f'Barcode {code} is not on transfer {order.transfer_no}.')
        counts[match.pk] = counts.get(match.pk, ZERO) + 1
        if counts[match.pk] > getattr(match, qty_attr):
            raise InventoryError(f'Scanned more of {code} than is outstanding.')
    return counts


@transaction.atomic
def ship_transfer(actor, order, *, quantities=None, barcodes=None, posting_date=None):
    """Post a shipment: source ON_HAND down, transit IN_TRANSIT up (or straight to destination for direct transfers)."""
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.from_location, 'ship')
    if order.status not in ('APPROVED', 'RELEASED', 'PARTIALLY_SHIPPED', 'PARTIALLY_RECEIVED'):
        raise InventoryError(f'A {order.get_status_display().lower()} transfer cannot be shipped.')
    lines = {l.pk: l for l in order.lines.select_related('item', 'variant', 'sku', 'to_sku', 'jewellery_unit', 'from_bin', 'to_bin')}
    if barcodes:
        plan = _scanned_lines(actor, order, barcodes, 'qty_to_ship')
    elif quantities:
        plan = {int(k): D(v) for k, v in quantities.items() if D(v) > 0}
    else:
        plan = {pk: line.qty_to_ship for pk, line in lines.items() if line.qty_to_ship > 0}
    if not plan:
        raise InventoryError('Nothing to ship.')
    shipment = TransferShipment.objects.create(tenant=actor.tenant, shipment_no=next_number(actor.tenant, 'TRANSFER_SHIPMENT'),
                                               transfer=order, posting_date=posting_date or dj_timezone.localdate(),
                                               shipped_by=actor.user, created_by=actor.user)
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='TRANSFER_SHIPMENT', document_no=shipment.shipment_no,
                                    posting_date=shipment.posting_date)
    destination = order.to_location if order.direct_transfer else order.transit_location
    for pk, qty in plan.items():
        line = lines.get(pk)
        if line is None:
            raise InventoryError('Line not on this transfer.')
        if qty > line.qty_to_ship:
            raise InventoryError(f'Line {line.line_no}: only {line.qty_to_ship} left to ship.')
        common = dict(item=line.item, variant=line.variant, from_location=order.from_location, to_location=destination,
                      from_sku=line.sku, to_sku=line.to_sku if order.direct_transfer else None, unit=line.jewellery_unit,
                      to_bin=line.to_bin if order.direct_transfer else None, line_no=line.line_no,
                      transaction_type='TRANSFER_SHIPMENT',
                      in_transaction_type='TRANSFER_RECEIPT' if order.direct_transfer else 'TRANSFER_SHIPMENT')
        outs, remaining = [], qty
        for reservation in _line_reservations(order, line):
            take = min(reservation.open_quantity, remaining)
            if take <= 0:
                break
            outs += engine.move(quantity=take, reservation=reservation, **common)[0]
            remaining -= take
        if remaining > 0:
            outs += engine.move(quantity=remaining, from_bin=line.from_bin, **common)[0]
        cost = -sum((e.cost_amount for e in outs), ZERO)
        TransferShipmentLine.objects.create(tenant=actor.tenant, shipment=shipment, transfer_line=line, jewellery_unit=line.jewellery_unit,
                                            quantity=qty, gross_weight=-sum((e.gross_weight for e in outs), ZERO),
                                            net_weight=-sum((e.net_weight for e in outs), ZERO), cost_amount=cost, created_by=actor.user)
        if not line.qty_shipped:
            line.unit_cost = (cost / qty).quantize(Decimal('0.0001'))
        line.qty_shipped += qty
        if order.direct_transfer:
            line.qty_received += qty
        line.save()
    order.actual_shipment_date = order.actual_shipment_date or shipment.posting_date
    _refresh_order_status(order)
    audit(actor, 'ship', 'TRANSFER_ORDER', order.transfer_no, location=order.from_location,
          new={'shipment': shipment.shipment_no, 'lines': {str(k): str(v) for k, v in plan.items()}})
    return shipment


def _damage_bin(location):
    return Bin.objects.filter(location=location, bin_type='DAMAGED', active=True, blocked=False).order_by('id').first()


@transaction.atomic
def receive_transfer(actor, order, *, lines=None, barcodes=None, damaged_barcodes=None, posting_date=None):
    """Post a receipt from transit into the destination.

    `lines`: [{'line': id, 'quantity': total received, 'damaged_qty': of which damaged, 'excess_qty': extra beyond
    what was shipped, 'reason_code': ..., 'to_bin': Bin}]. Damaged stock lands in DAMAGED state (and the damage bin),
    never directly available for sale.
    """
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.to_location, 'receive')
    if order.direct_transfer or order.status not in ('SHIPPED', 'PARTIALLY_SHIPPED', 'PARTIALLY_RECEIVED'):
        raise InventoryError(f'A {order.get_status_display().lower()} transfer has nothing to receive.')
    order_lines = {l.pk: l for l in order.lines.select_related('item', 'variant', 'to_sku', 'jewellery_unit', 'to_bin')}
    plan = {}
    if barcodes or damaged_barcodes:
        for pk, qty in _scanned_lines(actor, order, list(barcodes or []), 'qty_in_transit').items():
            plan[pk] = {'quantity': qty, 'damaged_qty': ZERO}
        for pk, qty in _scanned_lines(actor, order, list(damaged_barcodes or []), 'qty_in_transit').items():
            entry = plan.setdefault(pk, {'quantity': ZERO, 'damaged_qty': ZERO})
            entry['quantity'] += qty
            entry['damaged_qty'] += qty
            entry['reason_code'] = 'DAMAGED'
    elif lines:
        for spec in lines:
            pk = int(getattr(spec['line'], 'pk', spec['line']))
            plan[pk] = {'quantity': D(spec.get('quantity')), 'damaged_qty': D(spec.get('damaged_qty')),
                        'excess_qty': D(spec.get('excess_qty')), 'reason_code': spec.get('reason_code', ''), 'to_bin': spec.get('to_bin')}
    else:
        plan = {pk: {'quantity': l.qty_in_transit, 'damaged_qty': ZERO} for pk, l in order_lines.items() if l.qty_in_transit > 0}
    if not any(p['quantity'] > 0 or p.get('excess_qty') for p in plan.values()):
        raise InventoryError('Nothing to receive.')
    receipt = TransferReceipt.objects.create(tenant=actor.tenant, receipt_no=next_number(actor.tenant, 'TRANSFER_RECEIPT'),
                                             transfer=order, posting_date=posting_date or dj_timezone.localdate(),
                                             received_by=actor.user, created_by=actor.user)
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='TRANSFER_RECEIPT', document_no=receipt.receipt_no,
                                    posting_date=receipt.posting_date)
    for pk, spec in plan.items():
        line = order_lines.get(pk)
        if line is None:
            raise InventoryError('Line not on this transfer.')
        qty, damaged, excess = spec['quantity'], spec['damaged_qty'], spec.get('excess_qty') or ZERO
        reason = spec.get('reason_code') or ''
        to_bin = spec.get('to_bin') or line.to_bin
        if to_bin is not None and to_bin.location_id != order.to_location_id:
            raise InventoryError(f'{to_bin.code} is not a bin of {order.to_location.code}.')
        if qty < 0 or damaged < 0 or damaged > qty or excess < 0:
            raise InventoryError(f'Line {line.line_no}: invalid quantities.')
        if qty > line.qty_in_transit:
            raise InventoryError(f'Line {line.line_no}: only {line.qty_in_transit} in transit. Record the rest as excess.')
        if qty < line.qty_in_transit and reason and reason not in dict(TransferReceiptLine.REASONS):
            raise InventoryError('Unknown short-receipt reason.')
        common = dict(item=line.item, variant=line.variant, from_location=order.transit_location, to_location=order.to_location,
                      to_sku=line.to_sku, unit=line.jewellery_unit, line_no=line.line_no, transaction_type='TRANSFER_RECEIPT',
                      allow_from_transit=True, reason_code=reason)
        cost = ZERO
        if qty - damaged > 0:
            cost -= sum((e.cost_amount for e in engine.move(quantity=qty - damaged, to_bin=to_bin, **common)[0]), ZERO)
        if damaged > 0:
            common['reason_code'] = reason or 'DAMAGED'
            cost -= sum((e.cost_amount for e in engine.move(quantity=damaged, to_bin=_damage_bin(order.to_location) or to_bin,
                                                             into_state='DAMAGED', **common)[0]), ZERO)
        if excess > 0:
            if line.jewellery_unit_id:
                raise InventoryError('Serialized units that were not shipped cannot be received - raise an exception adjustment.')
            if not reason:
                raise InventoryError('Excess receipts need a reason.')
            require_location(actor.tenant, actor.user, order.to_location, 'approve_transfer')
            entry = engine.receive(transaction_type='ADJUSTMENT_POSITIVE', item=line.item, variant=line.variant, sku=line.to_sku,
                                   location=order.to_location, quantity=excess, unit_cost=line.unit_cost, bin=to_bin,
                                   reason_code=f'TRANSFER_EXCESS:{reason}', line_no=line.line_no)
            cost += entry.cost_amount
        TransferReceiptLine.objects.create(tenant=actor.tenant, receipt=receipt, transfer_line=line, jewellery_unit=line.jewellery_unit,
                                           to_bin=to_bin, quantity=qty, damaged_qty=damaged, excess_qty=excess, reason_code=reason,
                                           cost_amount=cost, created_by=actor.user)
        line.qty_received += qty
        line.qty_damaged += damaged
        line.save()
    _refresh_order_status(order)
    audit(actor, 'receive', 'TRANSFER_ORDER', order.transfer_no, location=order.to_location,
          new={'receipt': receipt.receipt_no, 'lines': {str(k): {kk: str(vv) for kk, vv in v.items() if kk != 'to_bin'} for k, v in plan.items()}})
    return receipt


@transaction.atomic
def cancel_remaining(actor, order, reason=''):
    """Cancel what has not shipped yet and release its reservations."""
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.from_location, 'approve_transfer')
    if order.status in ('CLOSED', 'CANCELLED'):
        raise InventoryError('Transfer is already closed.')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='TRANSFER_ORDER', document_no=order.transfer_no)
    for line in order.lines.all():
        for reservation in _line_reservations(order, line):
            engine.release(reservation)
        if line.qty_to_ship > 0:
            line.qty_cancelled += line.qty_to_ship
            line.save()
    _refresh_order_status(order)
    audit(actor, 'cancel', 'TRANSFER_ORDER', order.transfer_no, location=order.from_location, reason=reason)
    return order


@transaction.atomic
def short_close_transfer(actor, order, reason_code, remarks=''):
    """Close a transfer whose remaining in-transit stock will never arrive (written off from transit with a reason)."""
    if not reason_code:
        raise InventoryError('A short-close needs a reason.')
    cancel_remaining(actor, order, reason=f'short close: {reason_code}')
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.to_location, 'approve_transfer')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='TRANSFER_ORDER', document_no=order.transfer_no)
    for line in order.lines.select_related('item', 'variant', 'jewellery_unit'):
        missing = line.qty_in_transit
        if missing > 0:
            engine.write_off_transit(item=line.item, variant=line.variant, transit_location=order.transit_location, quantity=missing,
                                     unit=line.jewellery_unit, reason_code=reason_code, line_no=line.line_no,
                                     from_location=order.from_location, to_location=order.to_location)
            line.qty_short_closed += missing
            line.save()
    order.status = 'CLOSED'
    order.remarks = (order.remarks + f'\nShort-closed ({reason_code}) {remarks}').strip()
    order.save()
    _refresh_order_status(order)
    audit(actor, 'short_close', 'TRANSFER_ORDER', order.transfer_no, location=order.to_location, reason=f'{reason_code} {remarks}')
    return order


@transaction.atomic
def close_transfer(actor, order):
    """Close a fully received transfer once its receipt exceptions (e.g. damaged pieces) are resolved."""
    order = _lock_order(actor, order)
    require_location(actor.tenant, actor.user, order.to_location, 'receive')
    if order.status != 'RECEIVED':
        raise InventoryError('Only fully received transfers can be closed.')
    order.status = 'CLOSED'
    order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'close', 'TRANSFER_ORDER', order.transfer_no, location=order.to_location)
    return order


@transaction.atomic
def cancel_transfer(actor, order, reason=''):
    order = _lock_order(actor, order)
    if order.lines.filter(qty_shipped__gt=0).exists():
        raise InventoryError('Stock has already shipped - use Cancel remaining or Short close.')
    return cancel_remaining(actor, order, reason=reason)


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------

@transaction.atomic
def create_adjustment(actor, *, location, reason_code, lines, reference='', remarks='', posting_date=None, source_count=None,
                      permission='adjust'):
    require_location(actor.tenant, actor.user, location, permission)
    if not location.allow_adjustment:
        raise InventoryError(f'Adjustments are not allowed at {location.code}.')
    if reason_code not in dict(InventoryAdjustment.REASONS):
        raise InventoryError('Choose a valid reason code.')
    if not lines:
        raise InventoryError('Add at least one line.')
    adjustment = InventoryAdjustment.objects.create(
        tenant=actor.tenant, adjustment_no=next_number(actor.tenant, 'ADJUSTMENT'), location=location, reason_code=reason_code,
        reference=reference, remarks=remarks, posting_date=posting_date or dj_timezone.localdate(), source_count=source_count,
        created_by=actor.user,
    )
    for number, spec in enumerate(lines, 1):
        unit, sku = spec.get('unit'), spec.get('sku')
        item = unit.item if unit else (sku.item if sku else spec['item'])
        kind = spec['adjustment_type']
        if kind not in dict(InventoryAdjustmentLine.TYPES):
            raise InventoryError(f'Line {number}: unknown adjustment type.')
        if item.tenant_id != actor.tenant.id:
            raise InventoryError(f'Line {number}: item not found.')
        InventoryAdjustmentLine.objects.create(
            tenant=actor.tenant, adjustment=adjustment, line_no=number * 10000, adjustment_type=kind, item=item,
            variant=unit.variant if unit else (sku.variant if sku else spec.get('variant')),
            sku=sku or (unit.sku if unit else None), bin=spec.get('bin'), jewellery_unit=unit,
            quantity=D(spec.get('quantity', 1 if unit else 0)), gross_weight=D(spec.get('gross_weight')),
            net_weight=D(spec.get('net_weight')), unit_cost=D(spec.get('unit_cost')),
            from_status=spec.get('from_status', ''), to_status=spec.get('to_status', ''), created_by=actor.user,
        )
    audit(actor, 'create', 'ADJUSTMENT', adjustment.adjustment_no, location=location, reason=reason_code)
    return adjustment


def _lock_adjustment(actor, adjustment):
    return InventoryAdjustment.objects.select_for_update().select_related('location').get(pk=getattr(adjustment, 'pk', adjustment), tenant=actor.tenant)


@transaction.atomic
def submit_adjustment(actor, adjustment, permission='adjust'):
    adjustment = _lock_adjustment(actor, adjustment)
    require_location(actor.tenant, actor.user, adjustment.location, permission)
    if adjustment.status != 'DRAFT':
        raise InventoryError('Only draft adjustments can be submitted.')
    adjustment.status = 'SUBMITTED'
    adjustment.save(update_fields=['status', 'updated_at'])
    audit(actor, 'submit', 'ADJUSTMENT', adjustment.adjustment_no, location=adjustment.location)
    return adjustment


@transaction.atomic
def approve_adjustment(actor, adjustment):
    adjustment = _lock_adjustment(actor, adjustment)
    require_location(actor.tenant, actor.user, adjustment.location, 'approve_adjustment')
    if adjustment.status != 'SUBMITTED':
        raise InventoryError('Submit the adjustment before approving it.')
    if adjustment.created_by_id == actor.user.pk and not is_tenant_admin(actor.tenant, actor.user):
        raise PermissionDenied('An adjustment must be approved by someone other than its creator.')
    adjustment.status, adjustment.approved_by = 'APPROVED', actor.user
    adjustment.save(update_fields=['status', 'approved_by', 'updated_at'])
    audit(actor, 'approve', 'ADJUSTMENT', adjustment.adjustment_no, location=adjustment.location)
    return adjustment


@transaction.atomic
def post_adjustment(actor, adjustment):
    adjustment = _lock_adjustment(actor, adjustment)
    try:
        require_location(actor.tenant, actor.user, adjustment.location, 'adjust')
    except PermissionDenied:
        require_location(actor.tenant, actor.user, adjustment.location, 'approve_adjustment')
    if adjustment.status != 'APPROVED':
        raise InventoryError('Only approved adjustments can be posted.')
    location = adjustment.location
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='ADJUSTMENT', document_no=adjustment.adjustment_no,
                                    posting_date=adjustment.posting_date)
    counted = adjustment.source_count_id is not None
    for line in adjustment.lines.select_related('item', 'variant', 'sku', 'bin', 'jewellery_unit'):
        common = dict(item=line.item, variant=line.variant, sku=line.sku, location=location, bin=line.bin,
                      unit=line.jewellery_unit, reason_code=adjustment.reason_code, line_no=line.line_no)
        if line.adjustment_type == 'POSITIVE':
            engine.receive(transaction_type='COUNT' if counted else 'ADJUSTMENT_POSITIVE', quantity=line.quantity,
                           unit_cost=line.unit_cost or None, gross_weight=line.gross_weight or None,
                           net_weight=line.net_weight or None, **common)
        elif line.adjustment_type == 'NEGATIVE':
            final = 'SCRAPPED' if adjustment.reason_code in ('SCRAP', 'DAMAGE') else 'MISSING'
            state = line.from_status or None
            engine.issue(transaction_type='COUNT' if counted else 'ADJUSTMENT_NEGATIVE', quantity=line.quantity,
                         consume_state=state, final_unit_status=final, **common)
        elif line.adjustment_type == 'STATUS':
            if line.to_status == 'DAMAGED':
                transaction_type = 'DAMAGE'
            elif 'QC' in (line.to_status, line.from_status):
                transaction_type = 'QC'
            elif 'REPAIR' in (line.to_status, line.from_status):
                transaction_type = 'REPAIR'
            else:
                transaction_type = 'STATUS'
            engine.change_state(to_state=line.to_status or None, from_state=line.from_status or None, quantity=line.quantity,
                                transaction_type=transaction_type, **common)
        else:
            engine.adjust_weight(gross_delta=line.gross_weight, net_delta=line.net_weight, **common)
    adjustment.status, adjustment.posted_by = 'POSTED', actor.user
    adjustment.save(update_fields=['status', 'posted_by', 'updated_at'])
    if counted:
        PhysicalCount.objects.filter(pk=adjustment.source_count_id).update(status='POSTED')
    audit(actor, 'post', 'ADJUSTMENT', adjustment.adjustment_no, location=location, reason=adjustment.reason_code)
    return adjustment


# ---------------------------------------------------------------------------
# Reclassification (bin / location / status moves) and direct transfers
# ---------------------------------------------------------------------------

@transaction.atomic
def post_reclassification(actor, *, lines, reason='', posting_date=None):
    """Create and post a reclassification. Lines: unit or item(+variant, quantity) with from/to location, bin and status."""
    reclass = Reclassification.objects.create(tenant=actor.tenant, reclass_no=next_number(actor.tenant, 'RECLASS'), reason=reason,
                                              posting_date=posting_date or dj_timezone.localdate(), created_by=actor.user)
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='RECLASSIFICATION', document_no=reclass.reclass_no,
                                    posting_date=reclass.posting_date)
    for number, spec in enumerate(lines, 1):
        unit = spec.get('unit')
        item = unit.item if unit else spec['item']
        variant = unit.variant if unit else spec.get('variant')
        from_location = unit.current_location if unit else spec['from_location']
        to_location = spec.get('to_location') or from_location
        from_bin = unit.current_bin if unit else spec.get('from_bin')
        to_bin = spec.get('to_bin', from_bin if to_location == from_location else None)
        to_status = spec.get('to_status')  # None = leave status; 'AVAILABLE' = release to available
        quantity = Decimal('1') if unit else D(spec['quantity'])
        if from_location is None:
            raise InventoryError(f'Line {number}: {unit.barcode if unit else item.item_no} is not in stock.')
        if to_location.pk != from_location.pk:
            require_location(actor.tenant, actor.user, from_location, 'direct_transfer')
            require_location(actor.tenant, actor.user, to_location, 'receive')
            if not from_location.allow_direct_transfer:
                raise InventoryError(f'Direct transfers are not enabled at {from_location.code}.')
        else:
            require_location(actor.tenant, actor.user, from_location, 'reclassify')
        ReclassificationLine.objects.create(tenant=actor.tenant, reclass=reclass, line_no=number * 10000, item=item, variant=variant,
                                            jewellery_unit=unit, from_location=from_location, to_location=to_location,
                                            from_bin=from_bin, to_bin=to_bin, to_status=to_status or '', quantity=quantity,
                                            from_status=unit.status if unit else '', created_by=actor.user)
        moved = to_location.pk != from_location.pk or (from_bin.pk if from_bin else None) != (to_bin.pk if to_bin else None)
        if moved:
            engine.move(transaction_type='RECLASSIFICATION', item=item, variant=variant, from_location=from_location,
                        to_location=to_location, from_bin=from_bin, to_bin=to_bin, unit=unit, quantity=quantity,
                        from_sku=sku_for(actor.tenant, item, variant, from_location), to_sku=sku_for(actor.tenant, item, variant, to_location),
                        line_no=number * 10000, reason_code=reason[:40])
            if unit is not None:
                unit.refresh_from_db()
        if to_status:
            target = None if to_status == 'AVAILABLE' else to_status
            engine.change_state(item=item, variant=variant, location=to_location, bin=to_bin, unit=unit, quantity=quantity,
                                to_state=target, from_state=spec.get('from_status'), line_no=number * 10000,
                                transaction_type='RECLASSIFICATION', reason_code=reason[:40])
        if not moved and not to_status:
            raise InventoryError(f'Line {number}: nothing changes.')
    reclass.status, reclass.posted_by = 'POSTED', actor.user
    reclass.save(update_fields=['status', 'posted_by', 'updated_at'])
    audit(actor, 'reclassify', 'RECLASSIFICATION', reclass.reclass_no, reason=reason)
    return reclass


# ---------------------------------------------------------------------------
# Physical inventory
# ---------------------------------------------------------------------------

@transaction.atomic
def create_count(actor, *, location, blind=False, remarks=''):
    require_location(actor.tenant, actor.user, location, 'count')
    if not location.allow_physical_count:
        raise InventoryError(f'Physical counts are disabled at {location.code}.')
    count = PhysicalCount.objects.create(tenant=actor.tenant, count_no=next_number(actor.tenant, 'COUNT'), location=location,
                                         blind_count=blind, remarks=remarks, created_by=actor.user)
    audit(actor, 'create', 'PHYSICAL_COUNT', count.count_no, location=location)
    return count


@transaction.atomic
def snapshot_count(actor, count):
    """Freeze the expected quantities: one line per non-serialized bucket and one per jewellery unit."""
    count = PhysicalCount.objects.select_for_update().select_related('location').get(pk=count.pk, tenant=actor.tenant)
    require_location(actor.tenant, actor.user, count.location, 'count')
    if count.status != 'DRAFT':
        raise InventoryError('The snapshot has already been taken.')
    balances = InventoryBalance.objects.filter(tenant=actor.tenant, location=count.location).exclude(on_hand_qty=0) \
        .select_related('item', 'variant', 'sku', 'bin', 'jewellery_unit')
    for balance in balances:
        PhysicalCountLine.objects.create(
            tenant=actor.tenant, count=count, item=balance.item, variant=balance.variant, sku=balance.sku, bin=balance.bin,
            jewellery_unit=balance.jewellery_unit, system_qty=balance.on_hand_qty, system_gross_weight=balance.gross_weight,
            unit_cost=(balance.cost_value / balance.on_hand_qty) if balance.on_hand_qty else ZERO, created_by=actor.user,
        )
    count.status, count.snapshot_at = 'COUNTING', dj_timezone.now()
    count.save(update_fields=['status', 'snapshot_at', 'updated_at'])
    return count


def _open_count(actor, count):
    count = PhysicalCount.objects.select_for_update().select_related('location').get(pk=getattr(count, 'pk', count), tenant=actor.tenant)
    require_location(actor.tenant, actor.user, count.location, 'count')
    if count.status != 'COUNTING':
        raise InventoryError('This count is not open for counting.')
    return count


@transaction.atomic
def record_count(actor, count, *, line, counted_qty, counted_gross_weight=None):
    count = _open_count(actor, count)
    line = PhysicalCountLine.objects.get(pk=getattr(line, 'pk', line), count=count)
    if D(counted_qty) < 0:
        raise InventoryError('Counted quantity cannot be negative.')
    line.counted_qty = D(counted_qty)
    line.counted_gross_weight = D(counted_gross_weight) if counted_gross_weight not in (None, '') else None
    line.save(update_fields=['counted_qty', 'counted_gross_weight', 'updated_at'])
    return line


@transaction.atomic
def scan_count(actor, count, code, gross_weight=None):
    """Scan a barcode/HUID during a count. Units not in the snapshot are recorded as unexpected."""
    count = _open_count(actor, count)
    unit = find_unit(actor.tenant, code)
    if unit is not None:
        line = count.lines.filter(jewellery_unit=unit).first()
        if line is None:
            line = PhysicalCountLine.objects.create(tenant=actor.tenant, count=count, item=unit.item, variant=unit.variant,
                                                    sku=unit.sku, jewellery_unit=unit, unexpected=True, created_by=actor.user)
        if line.scanned:
            raise InventoryError(f'{code} was already scanned.')
        line.counted_qty, line.scanned = Decimal('1'), True
        line.counted_gross_weight = D(gross_weight) if gross_weight not in (None, '') else unit.gross_weight
        line.save()
        return line
    sku = SKU.objects.filter(tenant=actor.tenant, barcode=code, location=count.location).first()
    line = count.lines.filter(sku=sku, jewellery_unit__isnull=True).order_by('id').first() if sku else None
    if line is None:
        raise InventoryError(f'{code} is not a known barcode at {count.location.code}.')
    line.counted_qty = (line.counted_qty or ZERO) + 1
    line.scanned = True
    line.save()
    return line


@transaction.atomic
def submit_count(actor, count):
    """Close counting and generate the variance adjustment (status SUBMITTED - it still needs approval to post)."""
    count = _open_count(actor, count)
    lines = list(count.lines.select_related('item', 'jewellery_unit', 'sku', 'variant', 'bin'))
    uncounted = [l for l in lines if l.counted_qty is None and l.jewellery_unit_id is None]
    if uncounted:
        raise InventoryError(f'{len(uncounted)} non-serialized line(s) have not been counted.')
    adjustment_lines = []
    for line in lines:
        if line.jewellery_unit_id and line.counted_qty is None:
            line.counted_qty = ZERO  # an unscanned piece is missing
            line.save(update_fields=['counted_qty', 'updated_at'])
        variance = line.variance_qty
        if line.unexpected:
            continue  # a piece belonging elsewhere needs a reclassification, not a count adjustment
        if variance > 0:
            adjustment_lines.append({'adjustment_type': 'POSITIVE', 'item': line.item, 'variant': line.variant, 'sku': line.sku,
                                     'bin': line.bin, 'quantity': variance, 'unit_cost': line.unit_cost,
                                     'gross_weight': (line.counted_gross_weight or ZERO) - line.system_gross_weight if line.counted_gross_weight is not None else None})
        elif variance < 0:
            adjustment_lines.append({'adjustment_type': 'NEGATIVE', 'item': line.item, 'variant': line.variant, 'sku': line.sku,
                                     'bin': line.bin, 'unit': line.jewellery_unit, 'quantity': -variance})
        elif line.counted_gross_weight is not None and line.variance_weight and line.jewellery_unit_id is None:
            adjustment_lines.append({'adjustment_type': 'WEIGHT', 'item': line.item, 'variant': line.variant, 'sku': line.sku,
                                     'bin': line.bin, 'gross_weight': line.variance_weight, 'net_weight': ZERO})
    count.status = 'SUBMITTED'
    count.save(update_fields=['status', 'updated_at'])
    adjustment = None
    if adjustment_lines:
        adjustment = create_adjustment(actor, location=count.location, reason_code='COUNT', lines=adjustment_lines,
                                       reference=count.count_no, source_count=count, permission='count')
        adjustment = submit_adjustment(actor, adjustment, permission='count')
    else:
        count.status = 'POSTED'
        count.save(update_fields=['status', 'updated_at'])
    audit(actor, 'submit', 'PHYSICAL_COUNT', count.count_no, location=count.location, new={'variance_lines': len(adjustment_lines)})
    return adjustment


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_replenishment(actor, locations=None):
    """Suggest transfers for every SKU whose available + incoming stock is below its reorder point."""
    tenant = actor.tenant
    ReplenishmentLine.objects.filter(tenant=tenant, status='SUGGESTED').delete()
    skus = SKU.objects.filter(tenant=tenant, active=True, blocked=False, replenishment_system='TRANSFER') \
        .exclude(reorder_point=0, minimum_stock=0).select_related('item', 'variant', 'location', 'replenishment_source')
    if locations is not None:
        skus = skus.filter(location__in=locations)
    fallback = Location.objects.filter(tenant=tenant, location_type__in=('WAREHOUSE', 'DISTRIBUTION_CENTER'), active=True, blocked=False).order_by('id').first()
    created = []
    for sku in skus:
        incoming = sum((l.qty_outstanding for l in TransferOrderLine.objects.filter(
            tenant=tenant, item=sku.item, variant=sku.variant, transfer__to_location=sku.location,
            transfer__status__in=('APPROVED', 'RELEASED', 'PARTIALLY_SHIPPED', 'SHIPPED', 'PARTIALLY_RECEIVED'))), ZERO)
        current = available_qty(tenant, item=sku.item, location=sku.location, variant=sku.variant) + incoming
        trigger = sku.reorder_point or sku.minimum_stock
        if current >= trigger:
            continue
        suggested = (sku.maximum_stock - current) if sku.maximum_stock > 0 else sku.reorder_quantity
        if suggested <= 0:
            continue
        source = sku.replenishment_source or fallback
        created.append(ReplenishmentLine.objects.create(
            tenant=tenant, sku=sku, location=sku.location, source_location=source if source and source.pk != sku.location_id else None,
            current_qty=current, minimum_qty=sku.minimum_stock, maximum_qty=sku.maximum_stock, reorder_point=sku.reorder_point,
            shortage_qty=max(trigger - current, ZERO), suggested_qty=suggested, created_by=actor.user,
        ))
    return created


@transaction.atomic
def create_transfers_from_worksheet(actor, lines):
    groups = {}
    for line in lines:
        if line.tenant_id != actor.tenant.id or line.status not in ('SUGGESTED', 'APPROVED') or line.suggested_qty <= 0:
            continue
        if line.source_location is None:
            raise InventoryError(f'{line.sku.code}: choose a source location.')
        groups.setdefault((line.source_location, line.location), []).append(line)
    orders = []
    for (source, destination), group in groups.items():
        order = create_transfer_order(actor, from_location=source, to_location=destination, source_type='REPLENISHMENT',
                                      lines=[{'item': l.sku.item, 'variant': l.sku.variant, 'quantity': l.suggested_qty} for l in group],
                                      reason='Replenishment worksheet')
        for line in group:
            line.status, line.transfer_order = 'CONVERTED', order
            line.save(update_fields=['status', 'transfer_order', 'updated_at'])
        orders.append(order)
    return orders
