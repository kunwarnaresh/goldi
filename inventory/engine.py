"""The one Inventory Posting Engine, Availability Service and costing for Goldio.

Every inventory-changing transaction - POS sale, purchase, return, transfer, adjustment, count,
repair, QC or reclassification - calls ``InventoryPostingEngine``. It validates, locks the affected
balance rows (and jewellery units), writes immutable ``InventoryLedgerEntry`` rows and updates
``InventoryBalance`` in one database transaction. Nothing else writes those tables.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone as dj_timezone

from .models import (
    InventoryBalance, InventoryLedgerEntry, InventoryPeriod, InventoryReservation, JewelleryUnit,
    TransferOrderLine,
)
from .tenancy import is_tenant_admin

ZERO = Decimal('0')
QTY_PLACES = Decimal('0.001')
MONEY_PLACES = Decimal('0.01')

# Inventory state -> InventoryBalance column. RESERVED..PICKED are subsets of ON_HAND.
STATE_FIELDS = {
    'ON_HAND': 'on_hand_qty', 'IN_TRANSIT': 'in_transit_qty', 'RESERVED': 'reserved_qty', 'BLOCKED': 'blocked_qty',
    'QC': 'qc_qty', 'REPAIR': 'repair_qty', 'DAMAGED': 'damaged_qty', 'PICKED': 'picked_qty',
}
SUB_STATES = ('RESERVED', 'BLOCKED', 'QC', 'REPAIR', 'DAMAGED', 'PICKED')
# JewelleryUnit.status <-> balance sub-state (AVAILABLE = on hand, in no sub-state)
UNIT_STATUS_STATE = {'AVAILABLE': None, 'RESERVED': 'RESERVED', 'BLOCKED': 'BLOCKED', 'QC': 'QC',
                     'REPAIR': 'REPAIR', 'DAMAGED': 'DAMAGED', 'PICKED': 'PICKED'}
STATE_UNIT_STATUS = {state: status for status, state in UNIT_STATUS_STATE.items()}
OPEN_TRANSFER_STATUSES = ('APPROVED', 'RELEASED', 'PARTIALLY_SHIPPED', 'SHIPPED', 'PARTIALLY_RECEIVED')


class InventoryError(Exception):
    """A posting was rejected. The message is safe to show to the user."""


class InsufficientStock(InventoryError):
    pass


def D(value):
    return Decimal(str(value or 0))


def money(value):
    return D(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def compute_available(balance):
    """THE availability formula. Every screen, report, API and validation uses this."""
    return (balance.on_hand_qty - balance.reserved_qty - balance.blocked_qty - balance.qc_qty
            - balance.repair_qty - balance.damaged_qty - balance.picked_qty)


def physical_qty(balance):
    return balance.in_transit_qty if balance.location.is_transit else balance.on_hand_qty


class InventoryPostingEngine:
    def __init__(self, tenant, user, *, document_type, document_no, posting_date=None, allow_closed_period=False):
        self.tenant = tenant
        self.user = user
        self.document_type = document_type
        self.document_no = document_no
        self.posting_date = posting_date or dj_timezone.localdate()
        self.entries = []
        self._validate_posting_date(allow_closed_period)

    # ------------------------------------------------------------------ validation

    def _validate_posting_date(self, allow_closed_period):
        today = dj_timezone.localdate()
        if self.posting_date > today:
            raise InventoryError('Inventory cannot be posted with a future posting date.')
        admin = is_tenant_admin(self.tenant, self.user) if self.user else False
        closed = InventoryPeriod.objects.filter(tenant=self.tenant, closed=True, period_start__lte=self.posting_date,
                                                period_end__gte=self.posting_date).exists()
        if closed and not (allow_closed_period and admin):
            raise InventoryError(f'The inventory period containing {self.posting_date} is closed.')
        if self.posting_date < today and not admin:
            raise InventoryError('Backdated inventory postings require an administrator.')

    def _check_tenant(self, *objects):
        for obj in objects:
            if obj is not None and obj.tenant_id != self.tenant.id:
                raise InventoryError('Cross-tenant reference rejected.')

    def _validate(self, *, item, location, variant=None, sku=None, bin=None, unit=None, inbound):
        self._check_tenant(item, location, variant, sku, bin, unit)
        if not item.active or item.blocked:
            raise InventoryError(f'Item {item.item_no} is blocked or inactive.')
        if variant is not None and (variant.item_id != item.id or variant.blocked):
            raise InventoryError('Variant does not belong to the item or is blocked.')
        if not location.usable:
            raise InventoryError(f'Location {location.code} is blocked or inactive.')
        if sku is not None:
            if sku.item_id != item.id or sku.location_id != location.id or sku.variant_id != (variant.id if variant else None):
                raise InventoryError(f'SKU {sku.code} does not match the item/variant/location.')
            if sku.blocked or not sku.active:
                raise InventoryError(f'SKU {sku.code} is blocked.')
        if bin is not None:
            if bin.location_id != location.id:
                raise InventoryError(f'Bin {bin.code} is not in location {location.code}.')
            if bin.blocked or not bin.active:
                raise InventoryError(f'Bin {bin.code} is blocked.')
        elif inbound and location.bin_mandatory and not location.is_transit:
            raise InventoryError(f'Location {location.code} requires a bin.')
        if item.serial_tracking and unit is None:
            raise InventoryError(f'Item {item.item_no} is serialized: scan the jewellery unit.')
        if unit is not None and unit.item_id != item.id:
            raise InventoryError('Jewellery unit does not belong to the item.')

    # ------------------------------------------------------------------ primitives

    def _lock_balance(self, *, item, variant, location, bin, unit, lot_no, sku):
        key = InventoryBalance.make_key(item.id, variant.id if variant else None, location.id,
                                        bin.id if bin else None, unit.id if unit else None, lot_no)
        balance = InventoryBalance.objects.select_for_update().select_related('location').filter(tenant=self.tenant, bucket_key=key).first()
        if balance is None:
            try:
                with transaction.atomic():
                    InventoryBalance.objects.create(
                        tenant=self.tenant, bucket_key=key, item=item, variant=variant, sku=sku, location=location,
                        bin=bin, jewellery_unit=unit, lot_no=lot_no or '', uom=item.base_uom, created_by=self.user,
                    )
            except IntegrityError:
                pass  # created concurrently - lock the winner's row below
            balance = InventoryBalance.objects.select_for_update().select_related('location').get(tenant=self.tenant, bucket_key=key)
        if sku is not None and balance.sku_id is None:
            balance.sku = sku
        return balance

    def _lock_unit(self, unit):
        locked = JewelleryUnit.objects.select_for_update().get(pk=unit.pk, tenant=self.tenant)
        if locked.version != unit.version:
            raise InventoryError(f'Jewellery unit {unit.barcode} was changed by another transaction. Reload and retry.')
        return locked

    def _negative_allowed(self, location):
        return location.allow_negative_inventory or self.tenant.allow_negative_inventory

    def _save_balance(self, balance):
        balance.available_qty = compute_available(balance)
        balance.updated_by = self.user
        balance.save()

    def _ledger(self, balance, **fields):
        entry = InventoryLedgerEntry(
            tenant=self.tenant, posting_date=self.posting_date, document_type=self.document_type,
            document_no=self.document_no, item=balance.item, variant=balance.variant, sku=balance.sku,
            location=balance.location, bin=balance.bin, jewellery_unit=balance.jewellery_unit, lot_no=balance.lot_no,
            uom=balance.uom, user=self.user, created_by=self.user, company=balance.location.company,
            dimensions={'location': balance.location.code, 'cost_centre': balance.location.cost_centre,
                        'profit_centre': balance.location.profit_centre, 'posting_group': balance.location.inventory_posting_group},
            **fields,
        )
        unit = balance.jewellery_unit
        if unit is not None:
            entry.serial_no, entry.barcode, entry.huid = unit.serial_no, unit.barcode, unit.huid or ''
        entry.base_quantity = entry.quantity
        entry.save()
        self.entries.append(entry)
        return entry

    # ------------------------------------------------------------------ costing

    def _outbound_cost(self, balance, qty):
        """Historical cost leaving `balance` for `qty` units, per the item's costing method."""
        item = balance.item
        method = item.effective_costing_method()
        on_hand = physical_qty(balance)
        if method == 'SPECIFIC' or balance.location.is_transit:
            # A unit bucket (or a transit bucket) carries exactly the cost that entered it.
            return money(balance.cost_value * qty / on_hand) if on_hand > 0 else ZERO
        if method == 'STANDARD':
            standard = balance.sku.standard_cost if balance.sku_id and balance.sku.standard_cost else item.standard_cost
            return money(standard * qty)
        if method == 'FIFO':
            return self._consume_fifo(balance, qty)
        totals = InventoryBalance.objects.filter(tenant=self.tenant, item=item, variant=balance.variant, location=balance.location) \
            .aggregate(qty=Sum('on_hand_qty'), value=Sum('cost_value'))
        if totals['qty'] and totals['qty'] > 0:
            return money(totals['value'] * qty / totals['qty'])
        return ZERO

    def _consume_fifo(self, balance, qty):
        layers = InventoryLedgerEntry.objects.select_for_update().filter(
            tenant=self.tenant, item=balance.item, variant=balance.variant, location=balance.location,
            quantity__gt=0, remaining_quantity__gt=0,
        ).order_by('posting_date', 'id')
        remaining, cost, last_unit_cost = qty, ZERO, ZERO
        for layer in layers:
            if remaining <= 0:
                break
            take = min(layer.remaining_quantity, remaining)
            cost += take * layer.unit_cost
            last_unit_cost = layer.unit_cost
            InventoryLedgerEntry.objects.filter(pk=layer.pk).update(remaining_quantity=F('remaining_quantity') - take)
            remaining -= take
        if remaining > 0:  # negative inventory allowed - value the shortfall at the last known cost
            cost += remaining * last_unit_cost
        return money(cost)

    # ------------------------------------------------------------------ core movement

    def _post_out(self, balance, qty, *, transaction_type, consume_state=None, cost_amount=None, gross=None, net=None, **ledger):
        """Remove `qty` of physical stock from `balance`. Returns the ledger entry."""
        state = 'IN_TRANSIT' if balance.location.is_transit else 'ON_HAND'
        before = physical_qty(balance)
        if consume_state:
            field = STATE_FIELDS[consume_state]
            if getattr(balance, field) < qty:
                raise InsufficientStock(f'Only {getattr(balance, field)} {consume_state.lower()} at {balance.location.code}.')
            setattr(balance, field, getattr(balance, field) - qty)
        elif state == 'ON_HAND' and compute_available(balance) < qty and not self._negative_allowed(balance.location):
            raise InsufficientStock(f'Only {compute_available(balance)} available of {balance.item.item_no} at {balance.location.code}.')
        if state == 'IN_TRANSIT' and before < qty:
            raise InsufficientStock(f'Only {before} in transit.')
        if cost_amount is None:
            cost_amount = self._outbound_cost(balance, qty)
        if gross is None:
            gross = (balance.gross_weight * qty / before).quantize(QTY_PLACES) if before > 0 else ZERO
            net = (balance.net_weight * qty / before).quantize(QTY_PLACES) if before > 0 else ZERO
        setattr(balance, STATE_FIELDS[state], before - qty)
        balance.gross_weight -= gross
        balance.net_weight -= net
        balance.cost_value -= cost_amount
        if physical_qty(balance) == 0:  # never leave stray weight/value on an empty bucket
            balance.gross_weight = balance.net_weight = balance.cost_value = ZERO
        self._save_balance(balance)
        return self._ledger(balance, transaction_type=transaction_type, stock_state=state, quantity=-qty,
                            gross_weight=-gross, net_weight=-(net or ZERO), cost_amount=-cost_amount,
                            unit_cost=(cost_amount / qty) if qty else ZERO, status_from=consume_state or '', **ledger)

    def _post_in(self, balance, qty, *, transaction_type, cost_amount, gross, net, into_state=None, **ledger):
        state = 'IN_TRANSIT' if balance.location.is_transit else 'ON_HAND'
        setattr(balance, STATE_FIELDS[state], getattr(balance, STATE_FIELDS[state]) + qty)
        if into_state:
            field = STATE_FIELDS[into_state]
            setattr(balance, field, getattr(balance, field) + qty)
        balance.gross_weight += gross
        balance.net_weight += net
        balance.cost_value += cost_amount
        self._save_balance(balance)
        return self._ledger(balance, transaction_type=transaction_type, stock_state=state, quantity=qty,
                            gross_weight=gross, net_weight=net, cost_amount=cost_amount, remaining_quantity=qty,
                            unit_cost=(cost_amount / qty) if qty else ZERO, status_to=into_state or '', **ledger)

    def _unit_weights(self, unit):
        return unit.gross_weight, unit.net_metal_weight

    def _set_unit(self, unit, *, location, bin, status, sku=False):
        unit.current_location = location
        unit.current_bin = bin
        unit.status = status
        if sku is not False:  # a unit's SKU is location-specific, so it follows the piece
            unit.sku = sku
        unit.version += 1
        unit.updated_by = self.user
        unit.save(update_fields=['current_location', 'current_bin', 'status', 'sku', 'version', 'updated_by', 'updated_at'])

    # ------------------------------------------------------------------ public API

    @transaction.atomic
    def receive(self, *, transaction_type, item, location, quantity=1, unit_cost=None, variant=None, sku=None, bin=None,
                unit=None, lot_no='', gross_weight=None, net_weight=None, into_state=None, reason_code='', line_no=0,
                reversal_of=None):
        """Inbound stock: purchase, opening, positive adjustment, customer return, count surplus, production output."""
        qty = D(quantity)
        if qty <= 0:
            raise InventoryError('Quantity must be greater than zero.')
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=True)
        if location.is_transit:
            raise InventoryError('Stock cannot be received directly into a transit location.')
        if unit is not None:
            unit = self._lock_unit(unit)
            if qty != 1:
                raise InventoryError('A jewellery unit is always quantity 1.')
            if unit.in_stock or unit.status == 'IN_TRANSIT':
                raise InventoryError(f'Jewellery unit {unit.barcode} is already in stock at {unit.current_location}.')
            gross_weight, net_weight = self._unit_weights(unit)
            unit_cost = unit.total_cost if unit_cost is None else unit_cost
        if gross_weight is None and sku is not None:
            gross_weight, net_weight = sku.gross_weight * qty, sku.net_metal_weight * qty
        cost = money(D(unit_cost if unit_cost is not None else (sku.unit_cost if sku else item.standard_cost)) * qty)
        balance = self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no=lot_no, sku=sku)
        entry = self._post_in(balance, qty, transaction_type=transaction_type, cost_amount=cost, gross=D(gross_weight),
                              net=D(net_weight), into_state=into_state, reason_code=reason_code, document_line_no=line_no,
                              to_location=location, to_bin=bin, reversal_of=reversal_of)
        if unit is not None:
            self._set_unit(unit, location=location, bin=bin, status=STATE_UNIT_STATUS.get(into_state, 'AVAILABLE'), sku=sku)
        if sku is not None and transaction_type in ('PURCHASE', 'OPENING'):
            self._refresh_sku_cost(sku, last_unit_cost=cost / qty)
        return entry

    @transaction.atomic
    def issue(self, *, transaction_type, item, location, quantity=1, variant=None, sku=None, bin=None, unit=None, lot_no='',
              consume_state=None, reservation=None, final_unit_status='SOLD', reason_code='', line_no=0,
              gross_weight=None, net_weight=None, cost_amount=None, reversal_of=None):
        """Outbound stock: sale, negative adjustment, count shortage, scrap, return to vendor, production consumption.

        `cost_amount` overrides the costing method - only for reversals, which must leave at the original cost."""
        qty = D(quantity)
        if qty <= 0:
            raise InventoryError('Quantity must be greater than zero.')
        if unit is not None:
            unit = self._lock_unit(unit)
            location, bin = self._unit_position(unit, location, bin)
            consume_state = self._unit_consume_state(unit, consume_state, reservation)
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=False)
        if location.is_transit:
            raise InventoryError('Stock in transit can only leave through a transfer receipt.')
        if transaction_type == 'SALE' and not location.allow_sales:
            raise InventoryError(f'Sales are not allowed from {location.code}.')
        if reservation is not None:
            bin = self._reservation_bucket(reservation, item, location, unit, bin)
            consume_state = 'RESERVED'
            self._consume_reservation(reservation, qty)
        if transaction_type == 'SALE' and consume_state not in (None, 'RESERVED', 'PICKED'):
            raise InventoryError(f'Stock in {consume_state.lower()} cannot be sold.')
        balances = [(self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no=lot_no, sku=sku), qty)] \
            if (unit is not None or bin is not None or reservation is not None or consume_state) \
            else self._allocate(item=item, variant=variant, location=location, qty=qty, sku=sku)
        entries = []
        for balance, part in balances:
            if unit is not None:
                gross_weight, net_weight = self._unit_weights(unit)
            entries.append(self._post_out(balance, part, transaction_type=transaction_type, consume_state=consume_state,
                                          gross=D(gross_weight) if gross_weight is not None else None,
                                          net=D(net_weight) if net_weight is not None else None,
                                          cost_amount=money(D(cost_amount) * part / qty) if cost_amount is not None else None,
                                          reason_code=reason_code, document_line_no=line_no, from_location=location,
                                          from_bin=balance.bin, reversal_of=reversal_of))
        if unit is not None:
            self._set_unit(unit, location=location, bin=bin, status=final_unit_status)
        return entries[0] if len(entries) == 1 else entries

    @transaction.atomic
    def move(self, *, transaction_type, item, from_location, to_location, quantity=1, variant=None, from_sku=None, to_sku=None,
             from_bin=None, to_bin=None, unit=None, lot_no='', consume_state=None, into_state=None, reservation=None,
             reason_code='', line_no=0, allow_from_transit=False, in_transaction_type=None):
        """Move stock between two buckets with its cost and weight intact (transfer ship/receive, bin move, reclass).

        Returns (out_entries, in_entries). No revenue, customer or tax is ever created by a move.
        """
        qty = D(quantity)
        if qty <= 0:
            raise InventoryError('Quantity must be greater than zero.')
        if unit is not None:
            unit = self._lock_unit(unit)
            from_location, from_bin = self._unit_position(unit, from_location, from_bin, allow_transit=allow_from_transit)
            if not from_location.is_transit:
                consume_state = self._unit_consume_state(unit, consume_state, reservation)
        if from_location.is_transit and not allow_from_transit:
            raise InventoryError('Stock in transit can only leave through a transfer receipt.')
        self._validate(item=item, location=from_location, variant=variant, sku=from_sku, bin=from_bin, unit=unit, inbound=False)
        self._validate(item=item, location=to_location, variant=variant, sku=to_sku, bin=to_bin, unit=unit, inbound=True)
        if from_location.id == to_location.id and (from_bin.id if from_bin else None) == (to_bin.id if to_bin else None) \
                and (from_sku.id if from_sku else None) == (to_sku.id if to_sku else None) and not into_state and not consume_state:
            raise InventoryError('Source and destination are identical.')
        if reservation is not None:
            from_bin = self._reservation_bucket(reservation, item, from_location, unit, from_bin)
            consume_state = 'RESERVED'
            self._consume_reservation(reservation, qty)
        if unit is not None or from_bin is not None or consume_state or from_location.is_transit:
            sources = [(self._lock_balance(item=item, variant=variant, location=from_location, bin=from_bin, unit=unit,
                                           lot_no=lot_no, sku=from_sku), qty)]
        else:
            sources = self._allocate(item=item, variant=variant, location=from_location, qty=qty, sku=from_sku)
        out_entries, in_entries = [], []
        for source, part in sources:
            gross = net = None
            if unit is not None:
                gross, net = self._unit_weights(unit)
            out_entry = self._post_out(source, part, transaction_type=transaction_type, consume_state=consume_state,
                                       gross=gross, net=net, reason_code=reason_code, document_line_no=line_no,
                                       from_location=from_location, to_location=to_location, from_bin=source.bin, to_bin=to_bin)
            target = self._lock_balance(item=item, variant=variant, location=to_location, bin=to_bin, unit=unit, lot_no=lot_no, sku=to_sku)
            in_entries.append(self._post_in(target, part, transaction_type=in_transaction_type or transaction_type,
                                            cost_amount=-out_entry.cost_amount,
                                            gross=-out_entry.gross_weight, net=-out_entry.net_weight, into_state=into_state,
                                            reason_code=reason_code, document_line_no=line_no, from_location=from_location,
                                            to_location=to_location, from_bin=source.bin, to_bin=to_bin))
            out_entries.append(out_entry)
        if unit is not None:
            status = 'IN_TRANSIT' if to_location.is_transit else STATE_UNIT_STATUS.get(into_state, 'AVAILABLE')
            self._set_unit(unit, location=to_location, bin=to_bin, status=status,
                           sku=to_sku if to_location.id != from_location.id or to_sku is not None else False)
        return out_entries, in_entries

    @transaction.atomic
    def write_off_transit(self, *, item, transit_location, quantity, variant=None, unit=None, reason_code='',
                          line_no=0, from_location=None, to_location=None):
        """Short-close: stock shipped but never received leaves the transit location as a loss."""
        qty = D(quantity)
        if not transit_location.is_transit:
            raise InventoryError('Only transit stock can be short-closed.')
        if unit is not None:
            unit = self._lock_unit(unit)
            if unit.status != 'IN_TRANSIT' or unit.current_location_id != transit_location.id:
                raise InventoryError(f'Jewellery unit {unit.barcode} is not in transit.')
        self._check_tenant(item, transit_location, unit)
        balance = self._lock_balance(item=item, variant=variant, location=transit_location, bin=None, unit=unit, lot_no='', sku=None)
        entry = self._post_out(balance, qty, transaction_type='TRANSFER_LOSS', reason_code=reason_code, document_line_no=line_no,
                               from_location=from_location, to_location=to_location)
        if unit is not None:
            self._set_unit(unit, location=transit_location, bin=None, status='MISSING')
        return entry

    @transaction.atomic
    def change_state(self, *, item, location, to_state, quantity=1, from_state=None, variant=None, sku=None, bin=None,
                     unit=None, lot_no='', transaction_type='STATUS', reason_code='', line_no=0):
        """Move stock between sub-states inside on-hand (e.g. AVAILABLE -> DAMAGED, QC -> AVAILABLE).

        `from_state`/`to_state` of None mean 'available'.
        """
        qty = D(quantity)
        if unit is not None:
            unit = self._lock_unit(unit)
            location, bin = self._unit_position(unit, location, bin)
            from_state = UNIT_STATUS_STATE.get(unit.status, 'invalid')
            if from_state == 'invalid':
                raise InventoryError(f'Jewellery unit {unit.barcode} is {unit.get_status_display().lower()}.')
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=False)
        if from_state == to_state:
            raise InventoryError('The stock is already in that state.')
        balance = self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no=lot_no, sku=sku)
        if from_state:
            field = STATE_FIELDS[from_state]
            if getattr(balance, field) < qty:
                raise InsufficientStock(f'Only {getattr(balance, field)} in {from_state.lower()}.')
            setattr(balance, field, getattr(balance, field) - qty)
        elif compute_available(balance) < qty:
            raise InsufficientStock(f'Only {compute_available(balance)} available.')
        if to_state:
            setattr(balance, STATE_FIELDS[to_state], getattr(balance, STATE_FIELDS[to_state]) + qty)
        self._save_balance(balance)
        entry = self._ledger(balance, transaction_type=transaction_type, stock_state='ON_HAND', quantity=ZERO,
                             status_from=from_state or 'AVAILABLE', status_to=to_state or 'AVAILABLE',
                             status_quantity=qty, reason_code=reason_code, document_line_no=line_no)
        if unit is not None:
            self._set_unit(unit, location=location, bin=bin, status=STATE_UNIT_STATUS[to_state])
        return entry

    @transaction.atomic
    def adjust_weight(self, *, item, location, gross_delta, net_delta, variant=None, sku=None, bin=None, unit=None,
                      reason_code='WEIGHT', line_no=0):
        """Weight-only correction (quantity unchanged) - e.g. a re-weighed piece."""
        if unit is not None:
            unit = self._lock_unit(unit)
            location, bin = self._unit_position(unit, location, bin)
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=True)
        balance = self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no='', sku=sku)
        balance.gross_weight += D(gross_delta)
        balance.net_weight += D(net_delta)
        if balance.gross_weight < 0:
            raise InventoryError('Weight cannot become negative.')
        self._save_balance(balance)
        if unit is not None:
            unit.gross_weight += D(gross_delta)
            unit.stone_weight = max(unit.gross_weight - unit.other_weight - (unit.net_metal_weight + D(net_delta)), ZERO)
            unit.version += 1
            unit.save()
        return self._ledger(balance, transaction_type='ADJUSTMENT_POSITIVE' if D(gross_delta) >= 0 else 'ADJUSTMENT_NEGATIVE',
                            stock_state='ON_HAND', quantity=ZERO, gross_weight=D(gross_delta), net_weight=D(net_delta),
                            reason_code=reason_code, document_line_no=line_no)

    @transaction.atomic
    def revalue(self, *, item, location, cost_delta, variant=None, sku=None, bin=None, unit=None, lot_no='', reason_code='',
                line_no=0):
        """Value-only adjustment (quantity and weight unchanged) of stock still on hand - e.g. settling a production
        order's actual cost onto its output. The bucket must hold stock; a unit's historical cost follows the delta."""
        delta = money(cost_delta)
        if unit is not None:
            unit = self._lock_unit(unit)
            location, bin = self._unit_position(unit, location, bin)
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=True)
        balance = self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no=lot_no, sku=sku)
        if physical_qty(balance) <= 0:
            raise InventoryError(f'Nothing on hand to revalue for {item.item_no} at {location.code}.')
        if balance.cost_value + delta < 0:
            raise InventoryError('Revaluation would make the stock value negative.')
        balance.cost_value += delta
        self._save_balance(balance)
        if unit is not None:
            unit.other_cost += delta
            unit.purchase_cost = unit.total_cost + delta if unit.purchase_cost else unit.purchase_cost
            unit.version += 1
            unit.updated_by = self.user
            unit.save(update_fields=['other_cost', 'purchase_cost', 'version', 'updated_by', 'updated_at'])
        return self._ledger(balance, transaction_type='REVALUATION', stock_state='ON_HAND', quantity=ZERO, cost_amount=delta,
                            reason_code=reason_code, document_line_no=line_no)

    # ------------------------------------------------------------------ reservations

    @transaction.atomic
    def reserve(self, *, item, location, quantity, source_type, source_no, source_line_no=0, variant=None, sku=None,
                bin=None, unit=None, expires_at=None):
        """Reserve stock: AVAILABLE goes down, ON_HAND does not. Returns the reservation rows created."""
        qty = D(quantity)
        if not location.allow_reservation:
            raise InventoryError(f'Reservations are disabled at {location.code}.')
        if unit is not None:
            unit = self._lock_unit(unit)
            location, bin = self._unit_position(unit, location, bin)
            if unit.status != 'AVAILABLE':
                raise InventoryError(f'Jewellery unit {unit.barcode} is {unit.get_status_display().lower()}, not available.')
        self._validate(item=item, location=location, variant=variant, sku=sku, bin=bin, unit=unit, inbound=False)
        if unit is not None or bin is not None:
            parts = [(self._lock_balance(item=item, variant=variant, location=location, bin=bin, unit=unit, lot_no='', sku=sku), qty)]
        else:
            parts = self._allocate(item=item, variant=variant, location=location, qty=qty, sku=sku, strict=True)
        reservations = []
        for balance, part in parts:
            if compute_available(balance) < part:
                raise InsufficientStock(f'Only {compute_available(balance)} available to reserve at {location.code}.')
            balance.reserved_qty += part
            self._save_balance(balance)
            reservations.append(InventoryReservation.objects.create(
                tenant=self.tenant, item=item, variant=variant, sku=sku or balance.sku, location=location, bin=balance.bin,
                jewellery_unit=unit, balance=balance, quantity=part, open_quantity=part, source_type=source_type,
                source_no=source_no, source_line_no=source_line_no, expires_at=expires_at, created_by=self.user,
            ))
        if unit is not None:
            self._set_unit(unit, location=location, bin=bin, status='RESERVED')
        return reservations

    @transaction.atomic
    def release(self, reservation, quantity=None):
        reservation = InventoryReservation.objects.select_for_update().get(pk=reservation.pk, tenant=self.tenant)
        qty = D(quantity) if quantity is not None else reservation.open_quantity
        if reservation.status != 'ACTIVE' or qty > reservation.open_quantity:
            raise InventoryError('Reservation is not open for that quantity.')
        balance = InventoryBalance.objects.select_for_update().select_related('location').get(pk=reservation.balance_id)
        balance.reserved_qty -= qty
        self._save_balance(balance)
        reservation.open_quantity -= qty
        if reservation.open_quantity == 0:
            reservation.status = 'RELEASED'
        reservation.save(update_fields=['open_quantity', 'status', 'updated_at'])
        if reservation.jewellery_unit_id:
            unit = self._lock_unit(reservation.jewellery_unit)
            if unit.status == 'RESERVED':
                self._set_unit(unit, location=unit.current_location, bin=unit.current_bin, status='AVAILABLE')

    def _reservation_bucket(self, reservation, item, location, unit, bin):
        """Validate a reservation against the posting and return the bin it holds stock in."""
        if reservation.tenant_id != self.tenant.id or reservation.item_id != item.id or reservation.location_id != location.id:
            raise InventoryError('Reservation does not match this item/location.')
        if (reservation.jewellery_unit_id or None) != (unit.id if unit else None):
            raise InventoryError('Reservation is for a different jewellery unit.')
        return reservation.bin if bin is None else bin

    def _consume_reservation(self, reservation, qty):
        reservation = InventoryReservation.objects.select_for_update().get(pk=reservation.pk, tenant=self.tenant)
        if reservation.status != 'ACTIVE' or reservation.open_quantity < qty:
            raise InventoryError('Reservation does not cover this quantity.')
        reservation.open_quantity -= qty
        if reservation.open_quantity == 0:
            reservation.status = 'CONSUMED'
        reservation.save(update_fields=['open_quantity', 'status', 'updated_at'])

    # ------------------------------------------------------------------ helpers

    def _unit_position(self, unit, location, bin, allow_transit=False):
        if unit.status in ('SOLD', 'MISSING', 'SCRAPPED', 'RETURNED_TO_VENDOR', 'NOT_IN_STOCK', 'CONSUMED'):
            raise InventoryError(f'Jewellery unit {unit.barcode} is {unit.get_status_display().lower()}.')
        if unit.status == 'IN_TRANSIT' and not allow_transit:
            raise InventoryError(f'Jewellery unit {unit.barcode} is in transit and cannot be used here.')
        if location is not None and unit.current_location_id != location.id:
            raise InventoryError(f'Jewellery unit {unit.barcode} is at {unit.current_location.code}, not {location.code}.')
        return unit.current_location, unit.current_bin

    def _unit_consume_state(self, unit, consume_state, reservation):
        state = UNIT_STATUS_STATE.get(unit.status)
        if reservation is None and state == 'RESERVED':
            raise InventoryError(f'Jewellery unit {unit.barcode} is reserved for another document.')
        if state in ('BLOCKED',) and consume_state != 'BLOCKED':
            raise InventoryError(f'Jewellery unit {unit.barcode} is blocked.')
        return consume_state or state

    def _allocate(self, *, item, variant, location, qty, sku=None, strict=False):
        """Split `qty` across the location's non-serialized buckets by pick sequence."""
        balances = list(InventoryBalance.objects.select_for_update().select_related('location', 'bin').filter(
            tenant=self.tenant, item=item, variant=variant, location=location, jewellery_unit__isnull=True,
        ).order_by(F('bin__pick_sequence').asc(nulls_first=True), 'id'))
        parts, remaining = [], qty
        for balance in balances:
            take = min(compute_available(balance), remaining)
            if take > 0:
                parts.append((balance, take))
                remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            if strict or not self._negative_allowed(location):
                available = sum((compute_available(b) for b in balances), ZERO)
                raise InsufficientStock(f'Only {available} available of {item.item_no} at {location.code}.')
            default_bin = sku.default_bin if sku else None
            parts.append((self._lock_balance(item=item, variant=variant, location=location, bin=default_bin, unit=None, lot_no='', sku=sku), remaining))
        return parts

    def _refresh_sku_cost(self, sku, last_unit_cost):
        totals = InventoryBalance.objects.filter(tenant=self.tenant, item=sku.item, variant=sku.variant, location=sku.location) \
            .aggregate(qty=Sum('on_hand_qty'), value=Sum('cost_value'))
        sku.last_cost = money(last_unit_cost)
        if totals['qty']:
            sku.average_cost = money(totals['value'] / totals['qty'])
        sku.save(update_fields=['last_cost', 'average_cost', 'updated_at'])


# ---------------------------------------------------------------------------
# Availability service
# ---------------------------------------------------------------------------

def _balance_filter(tenant, *, item=None, variant=None, sku=None, locations=None):
    qs = InventoryBalance.objects.filter(tenant=tenant)
    if sku is not None:
        qs = qs.filter(item=sku.item, variant=sku.variant, location=sku.location)
    if item is not None:
        qs = qs.filter(item=item)
    if variant is not None:
        qs = qs.filter(variant=variant)
    if locations is not None:
        qs = qs.filter(location__in=locations)
    return qs


def availability_by_location(tenant, *, item=None, variant=None, sku=None, locations=None):
    """Per-location availability, including projected availability from open transfers.

    PROJECTED = ON HAND + EXPECTED PURCHASE + EXPECTED TRANSFER IN
                - RESERVED (other than for transfers) - EXPECTED SALES - EXPECTED TRANSFER OUT
    """
    sums = {f: Sum(f) for f in ('on_hand_qty', 'reserved_qty', 'blocked_qty', 'qc_qty', 'repair_qty', 'damaged_qty',
                                'in_transit_qty', 'picked_qty', 'available_qty', 'gross_weight', 'net_weight', 'cost_value')}
    rows = {r['location']: r for r in _balance_filter(tenant, item=item, variant=variant, sku=sku, locations=locations)
            .values('location').annotate(**sums)}
    lines = TransferOrderLine.objects.filter(tenant=tenant, transfer__status__in=OPEN_TRANSFER_STATUSES)
    reservations = InventoryReservation.objects.filter(tenant=tenant, status='ACTIVE', source_type='TRANSFER_ORDER')
    if sku is not None:
        item, variant = sku.item, sku.variant
    if item is not None:
        lines = lines.filter(item=item)
        reservations = reservations.filter(item=item)
    if variant is not None:
        lines = lines.filter(variant=variant)
        reservations = reservations.filter(variant=variant)
    transfer_in, transfer_out, transfer_reserved = {}, {}, {}
    for line in lines.select_related('transfer'):
        transfer_in[line.transfer.to_location_id] = transfer_in.get(line.transfer.to_location_id, ZERO) + line.qty_outstanding
        transfer_out[line.transfer.from_location_id] = transfer_out.get(line.transfer.from_location_id, ZERO) + line.qty_to_ship
    for res in reservations.values('location').annotate(q=Sum('open_quantity')):
        transfer_reserved[res['location']] = res['q']
    from .models import Location
    location_qs = Location.objects.for_tenant(tenant)
    if locations is not None:
        location_qs = location_qs.filter(pk__in=[getattr(loc, 'pk', loc) for loc in locations])
    if sku is not None:
        location_qs = location_qs.filter(pk=sku.location_id)
    result = []
    for location in location_qs.order_by('location_type', 'code'):
        r = rows.get(location.id, {})
        if not r and location.id not in transfer_in and location.id not in transfer_out:
            continue
        on_hand = r.get('on_hand_qty') or ZERO
        reserved = r.get('reserved_qty') or ZERO
        t_in, t_out = transfer_in.get(location.id, ZERO), transfer_out.get(location.id, ZERO)
        other_reserved = reserved - transfer_reserved.get(location.id, ZERO)
        expected_purchase = expected_sales = ZERO  # hooks: no location-aware purchase/sales orders feed this module yet
        result.append({
            'location': location, 'on_hand': on_hand, 'reserved': reserved, 'available': r.get('available_qty') or ZERO,
            'blocked': r.get('blocked_qty') or ZERO, 'qc': r.get('qc_qty') or ZERO, 'repair': r.get('repair_qty') or ZERO,
            'damaged': r.get('damaged_qty') or ZERO, 'picked': r.get('picked_qty') or ZERO,
            'in_transit': r.get('in_transit_qty') or ZERO, 'gross_weight': r.get('gross_weight') or ZERO,
            'net_weight': r.get('net_weight') or ZERO, 'cost_value': r.get('cost_value') or ZERO,
            'on_purchase': expected_purchase, 'transfer_in': t_in, 'transfer_out': t_out, 'sales_demand': expected_sales,
            'projected': on_hand + expected_purchase + t_in - other_reserved - expected_sales - t_out,
        })
    return result


def availability_totals(rows):
    keys = ('on_hand', 'reserved', 'available', 'in_transit', 'gross_weight', 'net_weight', 'cost_value', 'projected')
    return {key: sum((row[key] for row in rows), ZERO) for key in keys}


def available_qty(tenant, *, item, location, variant=None):
    total = InventoryBalance.objects.filter(tenant=tenant, item=item, variant=variant, location=location).aggregate(q=Sum('available_qty'))['q']
    return total or ZERO


def channel_available(tenant, *, sku, channel):
    """Stock a sales channel may sell at the SKU's location, after safety stock and channel allocation."""
    from .models import ChannelAllocation
    available = available_qty(tenant, item=sku.item, location=sku.location, variant=sku.variant)
    allocation = ChannelAllocation.objects.filter(tenant=tenant, location=sku.location, channel=channel) \
        .filter(Q(sku=sku) | Q(sku__isnull=True)).order_by(F('sku').desc(nulls_last=True)).first()
    safety = sku.safety_stock
    if allocation is None:
        return max(available - safety, ZERO)
    safety = allocation.safety_stock or safety
    share = allocation.allocation_qty or (available * allocation.allocation_percent / 100).quantize(Decimal('1'))
    return max(min(share, available - safety), ZERO)
