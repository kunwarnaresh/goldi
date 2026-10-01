"""The one Manufacturing Posting Engine.

Consumption, output, runtime, scrap, material issue/return, QC put-away, subcontracting, finish
(cost settlement), reopen and every reversal run through ``ManufacturingPostingEngine`` inside one
database transaction:

    PostingBatch -> InventoryPostingEngine (inventory ledger + balances)
                 -> production ledgers (consumption / output / runtime / scrap)
                 -> CostEntry (the WIP / value ledger)
                 -> one balanced G/L voucher through the ERP finance engine
                 -> audit

Any failure rolls the whole posting back. Screens, the API, shop-floor actions and automatic
flushing all call these functions; none of them writes stock, cost or ledger rows itself.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

from inventory.engine import InventoryError, InventoryPostingEngine, physical_qty
from inventory.models import InventoryBalance, InventoryReservation, JewelleryUnit, SKU
from inventory.services import register_unit

from .calc import D, ZERO, fine_weight, money, q3, runtime_cost
from .models import (
    ConsumptionEntry, CostEntry, OutputEntry, OutputUnit, PostingBatch, ProductionJournal, ProductionMaterialIssue, ProductionOrder,
    ProductionOrderComponent, ProductionOrderLine, ProductionOrderRoutingLine, ProductionQC, ProductionRework, RuntimeEntry,
    ScrapEntry, SubcontractOrder, VarianceEntry,
)
from .security import ConfirmationRequired, ManufacturingError, audit, can, get_setup, next_number, require

# Every CostEntry has a WIP side and a counter account. amount > 0: Dr WIP / Cr counter; amount < 0: Dr counter / Cr WIP.
COUNTER_ACCOUNT = {
    'MATERIAL': 'raw_material_account', 'LABOUR': 'labour_applied_account', 'MACHINE': 'machine_applied_account',
    'OVERHEAD': 'overhead_applied_account', 'SUBCONTRACT': 'subcontract_applied_account', 'OUTPUT': 'finished_goods_account',
    'BYPRODUCT': 'scrap_inventory_account', 'REVALUATION': 'finished_goods_account', 'VARIANCE': 'variance_account',
    'SCRAP_WRITE_OFF': 'finished_goods_account',
}
ACCOUNT_LABELS = {
    'wip_account': 'Production WIP', 'raw_material_account': 'Raw material inventory', 'labour_applied_account': 'Labour applied',
    'machine_applied_account': 'Machine cost applied', 'overhead_applied_account': 'Overhead applied',
    'subcontract_applied_account': 'Subcontracting applied', 'finished_goods_account': 'Finished goods inventory',
    'scrap_inventory_account': 'Scrap / recovery inventory', 'variance_account': 'Production variance',
}
LINE_ORDER = {'CONSUMPTION': 0, 'RUNTIME': 1, 'OUTPUT': 2, 'SCRAP': 3}
DONE_OPERATION = ('COMPLETED', 'QC_PASSED')


class _PreviewRollback(Exception):
    def __init__(self, summary):
        super().__init__('preview')
        self.summary = summary


def lock_order(actor, order):
    locked = ProductionOrder.objects.select_for_update().filter(tenant=actor.tenant, pk=getattr(order, 'pk', order)).first()
    if locked is None:
        raise ManufacturingError('Production order not found.')
    return locked


def assert_executable(order):
    if order.is_closed:
        raise ManufacturingError(f'{order.order_no} is {order.get_status_display().lower()}; reopen it through the controlled process first.')
    if not order.is_executable:
        raise ManufacturingError(f'{order.order_no} must be released before production transactions can be posted.')


def operations_of(order, include_rework=True):
    ops = order.operations.select_related('work_center', 'machine_center').order_by('sequence', 'operation_no')
    return list(ops if include_rework else ops.filter(is_rework=False))


def final_operation(order):
    ops = operations_of(order, include_rework=False)
    return ops[-1] if ops else None


def previous_operation(operation):
    ops = operations_of(operation.order, include_rework=False)
    ids = [op.pk for op in ops]
    if operation.pk not in ids:
        return None
    index = ids.index(operation.pk)
    return ops[index - 1] if index > 0 else None


def main_line(order):
    line = order.lines.filter(line_type='MAIN').order_by('line_no').first()
    if line is None:
        raise ManufacturingError(f'{order.order_no} has no main production line - refresh the order.')
    return line


def open_reservations(order, component):
    return InventoryReservation.objects.filter(tenant=order.tenant, source_type='PRODUCTION_ORDER', source_no=order.order_no,
                                               source_line_no=component.line_no, status='ACTIVE').order_by('id')


def sync_reserved(component):
    component.reserved_qty = open_reservations(component.order, component).aggregate(q=Sum('open_quantity'))['q'] or ZERO
    component.save(update_fields=['reserved_qty', 'updated_at'])


def ensure_sku(actor, item, variant, location, template=None):
    """The SKU of an output item at the location it is received into (created from the item/template when missing)."""
    sku = SKU.objects.filter(tenant=actor.tenant, item=item, variant=variant, location=location).first()
    if sku is not None:
        return sku
    base = f'{item.item_no}{"-" + variant.code if variant else ""}-{location.code}'[:55]
    code, suffix = base, 1
    while SKU.objects.filter(tenant=actor.tenant, code=code).exists():
        suffix += 1
        code = f'{base}-{suffix}'
    fields = {}
    if template is not None:
        fields = {f: getattr(template, f) for f in ('gross_weight', 'stone_weight', 'other_weight', 'making_charge', 'wastage_percent',
                                                     'design', 'collection', 'gender', 'size', 'color', 'hallmark', 'retail_price',
                                                     'standard_cost', 'purity', 'metal')}
    sku = SKU.objects.create(tenant=actor.tenant, code=code, item=item, variant=variant, location=location,
                             replenishment_system='PRODUCTION', created_by=actor.user, **fields)
    audit(actor, 'create', 'SKU', code, new={'reason': 'production output destination'})
    return sku


class ManufacturingPostingEngine:
    """Collects one atomic manufacturing posting. Use inside ``transaction.atomic`` with the order row locked."""

    def __init__(self, actor, order, *, kind='JOURNAL', journal=None, posting_date=None, idempotency_key='', reason='',
                 reversal_of=None):
        self.actor, self.tenant, self.user = actor, actor.tenant, actor.user
        self.order = order
        self.setup = get_setup(self.tenant)
        self.posting_date = posting_date or dj_timezone.localdate()
        self.progress_before = {'produced': str(order.produced_qty), 'status': order.status,
                                'wip': str(self.order_wip())}
        try:
            with transaction.atomic():
                self.batch = PostingBatch.objects.create(
                    tenant=self.tenant, company=order.company, batch_no=next_number(self.tenant, 'POSTING_BATCH'), kind=kind,
                    order=order, journal=journal, posting_date=self.posting_date, posted_by=self.user,
                    idempotency_key=idempotency_key or '', reason=reason[:250], reversal_of=reversal_of, created_by=self.user,
                )
        except IntegrityError:
            raise ManufacturingError('This request was already posted (duplicate idempotency key).')
        self.inventory = InventoryPostingEngine(self.tenant, self.user, document_type=f'PRODUCTION_{kind}',
                                                document_no=order.order_no, posting_date=self.posting_date)
        self.seq = 0
        self.records = {'consumption': [], 'output': [], 'runtime': [], 'scrap': [], 'cost': [], 'issues': [], 'variance': []}
        self.notes = []
        self.extra = {}

    # ------------------------------------------------------------------ helpers

    def _no(self):
        self.seq += 1
        return f'{self.batch.batch_no}/{self.seq:03d}'

    def _ledger_fields(self):
        return {'tenant': self.tenant, 'company': self.order.company, 'created_by': self.user, 'posting_date': self.posting_date,
                'batch': self.batch}

    def order_wip(self):
        # money(): SQLite sums decimals as floats, which would leave sub-paisa residue in a WIP that is really zero
        return money(CostEntry.objects.filter(tenant=self.tenant, order=self.order).aggregate(v=Sum('amount'))['v'] or ZERO)

    def cost(self, cost_type, amount, *, source=None, description='', is_rework=False, reversal_of=None):
        amount = money(amount)
        if amount == 0:
            return None
        counter = COUNTER_ACCOUNT[cost_type]
        entry = CostEntry.objects.create(
            entry_no=self._no(), order=self.order, cost_type=cost_type, amount=amount, is_rework=is_rework,
            source_type=type(source).__name__ if source is not None else '', source_id=source.pk if source is not None else None,
            description=description[:200], debit_account='wip_account' if amount > 0 else counter,
            credit_account=counter if amount > 0 else 'wip_account', reversal_of=reversal_of, **self._ledger_fields(),
        )
        self.records['cost'].append(entry)
        return entry

    def reverse_costs_of(self, source):
        for entry in CostEntry.objects.filter(tenant=self.tenant, order=self.order, source_type=type(source).__name__,
                                              source_id=source.pk, reversed=False, reversal_of__isnull=True):
            self.cost(entry.cost_type, -entry.amount, source=source, description=f'Reversal of {entry.entry_no}',
                      is_rework=entry.is_rework, reversal_of=entry)
            entry.reversed = True
            entry.save(update_fields=['reversed', 'updated_at'])

    def _mark_reversed(self, entry):
        entry.reversed = True
        entry.updated_by = self.user
        entry.save(update_fields=['reversed', 'updated_at', 'updated_by'])

    # ------------------------------------------------------------------ consumption

    def consume(self, component, quantity, *, unit=None, lot_no='', operation=None, source='MANUAL', is_rework=False,
                location=None, bin=None, from_state=None):
        """Material physically consumed: raw material inventory down, production WIP up."""
        order, setup = self.order, self.setup
        qty = q3(quantity)
        if qty <= 0:
            raise ManufacturingError('Consumption quantity must be greater than zero.')
        if component.order_id != order.id:
            raise ManufacturingError('Component does not belong to this production order.')
        item = component.item
        if not is_rework and not setup.allow_over_consumption and source not in ('SUBCONTRACT_LOSS',):
            limit = component.expected_qty * (1 + setup.over_consumption_tolerance_percent / 100)
            if component.consumed_qty + qty > q3(limit):
                raise ManufacturingError(
                    f'Over-consumption of {item.item_no}: expected {component.expected_qty}, already consumed {component.consumed_qty}. '
                    'A supervisor can allow over-consumption in manufacturing setup or record it on a rework order.')
        direct_allowed = (not setup.require_material_issue) or component.flushing_method in ('FORWARD', 'BACKWARD')
        entries = []
        if item.serial_tracking or unit is not None:
            if unit is None:
                raise ManufacturingError(f'{item.item_no} is serialized: scan the jewellery unit being consumed.')
            unit = JewelleryUnit.objects.get(pk=unit.pk, tenant=self.tenant)
            if unit.item_id != item.id:
                raise ManufacturingError(f'Unit {unit.barcode} is not {item.item_no}.')
            if qty != 1:
                raise ManufacturingError('A jewellery unit is always consumed as quantity 1.')
            reservation = None
            from_issue = unit.status == 'PICKED'
            if unit.status == 'RESERVED':
                reservation = open_reservations(order, component).filter(jewellery_unit=unit).first()
                if reservation is None:
                    raise ManufacturingError(f'Unit {unit.barcode} is reserved for another document.')
            if not from_issue and not direct_allowed:
                raise ManufacturingError(f'Unit {unit.barcode} has not been issued to production. Pick / issue it first.')
            entry = self.inventory.issue(transaction_type='CONSUMPTION', item=item, variant=component.variant, location=unit.current_location,
                                         unit=unit, reservation=reservation, final_unit_status='CONSUMED', reason_code=source,
                                         line_no=component.line_no)
            entries.append((entry, from_issue, unit))
        else:
            remaining = qty
            if location is not None:  # explicit bucket (subcontractor loss at the vendor location)
                entries.append((self.inventory.issue(transaction_type='CONSUMPTION', item=item, variant=component.variant, location=location,
                                                     bin=bin, quantity=qty, consume_state=from_state, lot_no=lot_no, reason_code=source,
                                                     line_no=component.line_no), from_state == 'PICKED', None))
                remaining = ZERO
            issued = min(remaining, component.issued_open_qty)
            if issued > 0:
                entries.append((self.inventory.issue(transaction_type='CONSUMPTION', item=item, variant=component.variant, location=order.location,
                                                     bin=order.production_bin, quantity=issued, consume_state='PICKED', lot_no=lot_no,
                                                     reason_code=source, line_no=component.line_no), True, None))
                remaining -= issued
            if remaining > 0:
                if not direct_allowed:
                    raise ManufacturingError(
                        f'Only {component.issued_open_qty} of {item.item_no} has been issued to production; '
                        f'{remaining} more must be picked/issued before it can be consumed.')
                for reservation in open_reservations(order, component):
                    if remaining <= 0:
                        break
                    take = min(reservation.open_quantity, remaining)
                    entries.append((self.inventory.issue(transaction_type='CONSUMPTION', item=item, variant=component.variant,
                                                         location=reservation.location, quantity=take, reservation=reservation,
                                                         lot_no=lot_no, reason_code=source, line_no=component.line_no), False, None))
                    remaining -= take
                if remaining > 0:
                    result = self.inventory.issue(transaction_type='CONSUMPTION', item=item, variant=component.variant,
                                                  location=component.location, bin=component.bin, quantity=remaining, lot_no=lot_no,
                                                  reason_code=source, line_no=component.line_no)
                    for part in (result if isinstance(result, list) else [result]):
                        entries.append((part, False, None))
        created = []
        for inv_entry, from_issue, unit_obj in entries:
            part_qty = -inv_entry.quantity
            cost_amount = -inv_entry.cost_amount
            gross = -inv_entry.gross_weight
            net = -inv_entry.net_weight
            if component.is_metal and not gross:
                gross = net = part_qty * (1000 if component.uom.code == 'KG' else 1)
            purity = component.purity or item.purity
            record = ConsumptionEntry.objects.create(
                entry_no=self._no(), order=order, component=component, operation=operation, item=item, sku=inv_entry.sku,
                location=inv_entry.location, bin=inv_entry.bin, jewellery_unit=unit_obj, lot_no=inv_entry.lot_no,
                huid=(unit_obj.huid or '') if unit_obj else '', quantity=part_qty, uom=component.uom, gross_weight=gross,
                net_weight=net, fine_weight=fine_weight(net, purity) if (component.metal or item.metal) in ('GOLD', 'SILVER', 'PLATINUM') else ZERO,
                metal=component.metal or item.metal, purity=purity, unit_cost=(cost_amount / part_qty) if part_qty else ZERO,
                cost_amount=cost_amount, from_issue=from_issue, source=source, is_rework=is_rework, inventory_entry=inv_entry,
                user=self.user, **self._ledger_fields(),
            )
            self.cost('MATERIAL', cost_amount, source=record, description=f'{item.item_no} x {part_qty}', is_rework=is_rework)
            if self.setup.overhead_enabled and self.setup.material_overhead_percent:
                self.cost('OVERHEAD', cost_amount * self.setup.material_overhead_percent / 100, source=record,
                          description=f'Material overhead {self.setup.material_overhead_percent}%', is_rework=is_rework)
            component.consumed_qty += part_qty
            component.consumed_weight += net
            component.actual_cost += cost_amount
            if from_issue:
                component.consumed_from_issue_qty += part_qty
            self.records['consumption'].append(record)
            created.append(record)
        component.save(update_fields=['consumed_qty', 'consumed_weight', 'actual_cost', 'consumed_from_issue_qty', 'updated_at'])
        sync_reserved(component)
        return created

    # ------------------------------------------------------------------ runtime

    def runtime(self, operation, *, setup=0, run=0, wait=0, move=0, queue=0, downtime=0, breaks=0, employee=None, resource=None,
                machine=None, start=None, end=None, downtime_reason=None, is_rework=False):
        """Capacity used: capacity ledger up, WIP up by labour + machine + overhead. Planned runtime is never touched."""
        if operation.order_id != self.order.id:
            raise ManufacturingError('Operation does not belong to this production order.')
        minutes = {k: q3(v) for k, v in dict(setup=setup, run=run, wait=wait, move=move, queue=queue, downtime=downtime, breaks=breaks).items()}
        if any(v < 0 for v in minutes.values()):
            raise ManufacturingError('Times cannot be negative.')
        if not any(minutes.values()):
            raise ManufacturingError('Enter setup, run, wait, move, queue or downtime minutes.')
        machine = machine or operation.machine_center
        labour_rate = resource.labour_rate if resource is not None and resource.labour_rate else operation.labour_rate
        machine_rate = machine.hourly_cost if machine is not None and machine.pk != operation.machine_center_id else operation.machine_rate
        cost = runtime_cost(setup_minutes=minutes['setup'], run_minutes=minutes['run'], labour_rate=labour_rate,
                            machine_rate=machine_rate, overhead_rate=operation.overhead_rate, overhead_enabled=self.setup.overhead_enabled)
        total = minutes['setup'] + minutes['run'] + minutes['wait'] + minutes['move'] + minutes['queue'] + minutes['downtime']
        record = RuntimeEntry.objects.create(
            entry_no=self._no(), order=self.order, operation=operation, work_center=operation.work_center, machine_center=machine,
            resource=resource, employee=employee or self.user, start_time=start, end_time=end, setup_minutes=minutes['setup'],
            run_minutes=minutes['run'], wait_minutes=minutes['wait'], move_minutes=minutes['move'], queue_minutes=minutes['queue'],
            downtime_minutes=minutes['downtime'], break_minutes=minutes['breaks'], total_minutes=total, downtime_reason=downtime_reason,
            labour_rate=labour_rate, machine_rate=machine_rate, overhead_rate=operation.overhead_rate if self.setup.overhead_enabled else ZERO,
            labour_cost=cost['labour'], machine_cost=cost['machine'], overhead_cost=cost['overhead'], cost_amount=cost['total'],
            is_rework=is_rework, user=self.user, **self._ledger_fields(),
        )
        label = f'Op {operation.operation_no} {operation.description}'
        self.cost('LABOUR', cost['labour'], source=record, description=label, is_rework=is_rework)
        self.cost('MACHINE', cost['machine'], source=record, description=label, is_rework=is_rework)
        self.cost('OVERHEAD', cost['overhead'], source=record, description=label, is_rework=is_rework)
        self._apply_runtime(operation, record, 1)
        if operation.status in ('NOT_STARTED', 'READY'):
            operation.status = 'STARTED'
            operation.actual_start = operation.actual_start or start or dj_timezone.now()
        operation.save()
        self.records['runtime'].append(record)
        return record

    def _apply_runtime(self, operation, record, sign):
        operation.actual_setup_minutes += sign * record.setup_minutes
        operation.actual_run_minutes += sign * record.run_minutes
        operation.actual_wait_minutes += sign * record.wait_minutes
        operation.actual_move_minutes += sign * record.move_minutes
        operation.actual_queue_minutes += sign * record.queue_minutes
        operation.actual_downtime_minutes += sign * record.downtime_minutes
        operation.actual_break_minutes += sign * record.break_minutes
        operation.actual_cost += sign * record.cost_amount

    # ------------------------------------------------------------------ output

    def output(self, *, operation=None, order_line=None, quantity, scrap_qty=0, gross_weight=None, net_weight=None, stone_weight=None,
               units=None, lot_no='', operator=None, machine=None, finished=False, is_rework=False):
        """Good output of an operation. Output of the final operation (or of an order without routing) increases
        finished inventory - into QC when quality control is required - and moves its value out of WIP."""
        order, setup = self.order, self.setup
        qty, scrap_qty = q3(quantity), q3(scrap_qty)
        if qty < 0 or scrap_qty < 0 or (qty == 0 and scrap_qty == 0):
            raise ManufacturingError('Enter a good output and/or scrap quantity.')
        order_line = order_line or main_line(order)
        if order_line.order_id != order.id:
            raise ManufacturingError('Production line does not belong to this order.')
        if operation is not None and operation.order_id != order.id:
            raise ManufacturingError('Operation does not belong to this production order.')
        last = final_operation(order)
        by_product = order_line.line_type != 'MAIN'
        if by_product:
            operation, is_final = None, True
        elif operation is None:
            if last is not None:
                raise ManufacturingError('Select the operation this output belongs to.')
            is_final = True
        else:
            is_final = (not operation.is_rework) and last is not None and operation.pk == last.pk
        if operation is not None:
            if operation.status in ('QC_FAILED',):
                raise ManufacturingError(f'Operation {operation.operation_no} failed QC; complete its rework first.')
            if operation.subcontracting and SubcontractOrder.objects.filter(operation=operation, status__in=('SENT', 'PARTIALLY_RECEIVED')).exists():
                raise ManufacturingError(f'Operation {operation.operation_no} is still at the subcontractor.')
            previous = previous_operation(operation) if not operation.is_rework else None
            if previous is not None and setup.enforce_operation_sequence and not setup.allow_over_output:
                if operation.output_qty + operation.scrap_qty + qty + scrap_qty > previous.output_qty:
                    raise ManufacturingError(
                        f'Operation {operation.operation_no} cannot report more than operation {previous.operation_no} has output '
                        f'({previous.output_qty}); already reported {operation.output_qty + operation.scrap_qty}.')
        if is_final and not is_rework and not setup.allow_over_output and order_line.output_qty + qty > order_line.quantity:
            raise ManufacturingError(f'Output would exceed the planned quantity {order_line.quantity} '
                                     f'(already produced {order_line.output_qty}). Over-output is not allowed.')
        if is_final and not by_product and not setup.allow_partial_output and qty and order_line.output_qty + qty < order_line.quantity:
            raise ManufacturingError('Partial output is disabled: post the full remaining quantity.')

        record = OutputEntry(
            entry_no=self._no(), order=order, order_line=order_line, operation=operation, item=order_line.item, sku=None,
            variant=order_line.variant, quantity=qty, scrap_qty=scrap_qty, uom=order_line.uom, lot_no=lot_no, is_final=is_final and qty > 0,
            is_rework=is_rework, operator=operator or self.user, machine_center=machine or (operation.machine_center if operation else None),
            user=self.user, **self._ledger_fields(),
        )
        created_units = []
        if is_final and qty > 0 and not is_rework:
            created_units = self._post_output_inventory(record, order_line, qty, gross_weight, net_weight, stone_weight, units or [], lot_no)
        else:
            record.save()
        self.records['output'].append(record)
        # quantities
        if operation is not None:
            operation.output_qty += qty
            operation.scrap_qty += scrap_qty
            if operation.status in ('NOT_STARTED', 'READY', 'PAUSED'):
                operation.status = 'STARTED'
                operation.actual_start = operation.actual_start or dj_timezone.now()
            target = order.planned_qty if not operation.is_rework else (operation.rework.quantity if operation.rework_id else qty)
            if finished or operation.output_qty + operation.scrap_qty >= target:
                self.complete_operation(operation)
            operation.save()
        if is_final and not is_rework:
            order_line.output_qty += qty
            order_line.scrap_qty += scrap_qty
            order_line.save(update_fields=['output_qty', 'scrap_qty', 'updated_at'])
            if order_line.line_type == 'MAIN':
                order.produced_qty += qty
                order.scrap_qty += scrap_qty
                if record.into_qc:
                    order.qc_pending_qty += qty
                elif qty:
                    order.accepted_qty += qty
        if not by_product and (qty + scrap_qty) > 0:
            self._backflush(operation, is_final, qty + scrap_qty)
        return record, created_units

    def _post_output_inventory(self, record, order_line, qty, gross_weight, net_weight, stone_weight, units, lot_no):
        order, setup = self.order, self.setup
        into_qc = order_line.line_type != 'BY_PRODUCT' and setup.require_quality_check
        if order_line.line_type == 'BY_PRODUCT':
            location = order.scrap_location or order.location
            bin = order.scrap_bin
        elif into_qc:
            location = order.qc_location or order.location
            bin = order.qc_bin
        else:
            location = order.finished_goods_location or order.location
            bin = order.finished_goods_bin
        template = order.sku if order_line.line_type == 'MAIN' else order_line.sku
        sku = ensure_sku(self.actor, order_line.item, order_line.variant, location, template)
        unit_cost = order_line.unit_cost
        record.sku, record.location, record.bin, record.into_qc = sku, location, bin, into_qc
        record.unit_cost = unit_cost
        profile = getattr(order_line.item, 'manufacturing', None)
        default_gross = sku.gross_weight or (profile.gross_weight if profile else ZERO)
        default_stone = sku.stone_weight or (profile.stone_weight if profile else ZERO)
        created = []
        if order_line.item.serial_tracking:
            if qty != int(qty):
                raise ManufacturingError('Serialized output must be a whole number of pieces.')
            specs = list(units)[:int(qty)]
            if len(specs) > int(qty):
                raise ManufacturingError('More unit details than pieces produced.')
            existing = OutputUnit.objects.filter(tenant=self.tenant, order=order).count()
            per_gross = D(gross_weight) / qty if gross_weight not in (None, '') else default_gross
            per_stone = D(stone_weight) / qty if stone_weight not in (None, '') else default_stone
            specs += [{} for _ in range(int(qty) - len(specs))]
            record.save()
            total_gross = total_net = total_stone = total_cost = ZERO
            conversion_share = (order.planned_labour_cost + order.planned_machine_cost + order.planned_overhead_cost) / order.planned_qty \
                if order.planned_qty else ZERO
            for index, spec in enumerate(specs, 1):
                barcode = (spec.get('barcode') or f'{order.order_no}-{existing + index:04d}').strip()
                gross = D(spec.get('gross_weight')) if spec.get('gross_weight') not in (None, '') else per_gross
                stone = D(spec.get('stone_weight')) if spec.get('stone_weight') not in (None, '') else per_stone
                cost_each = money(unit_cost)
                unit = register_unit(
                    self.actor, sku=sku, barcode=barcode, serial_no=(spec.get('serial_no') or barcode).strip(), huid=spec.get('huid') or None,
                    gross_weight=q3(gross), stone_weight=q3(stone), other_weight=ZERO, lot_no=lot_no,
                    certificate_no=spec.get('certificate_no') or '', making_cost=money(min(conversion_share, cost_each)),
                    metal_cost=money(max(cost_each - conversion_share, ZERO)), purchase_cost=cost_each,
                )
                inv_entry = self.inventory.receive(transaction_type='OUTPUT', item=order_line.item, variant=order_line.variant, sku=sku,
                                                   location=location, bin=bin, unit=unit, unit_cost=cost_each,
                                                   into_state='QC' if into_qc else None, line_no=order_line.line_no)
                record.inventory_entries.add(inv_entry)
                OutputUnit.objects.create(tenant=self.tenant, company=order.company, output=record, order=order, jewellery_unit=unit,
                                          hallmark_status='PENDING' if (profile and profile.hallmark_required) or sku.hallmark else 'NOT_REQUIRED',
                                          certificate_no=spec.get('certificate_no') or '', qc_status='PENDING' if into_qc else 'NOT_REQUIRED',
                                          created_by=self.user)
                unit.refresh_from_db()
                total_gross += unit.gross_weight
                total_net += unit.net_metal_weight
                total_stone += unit.stone_weight
                total_cost += cost_each
                created.append(unit)
            record.gross_weight, record.net_weight, record.stone_weight, record.cost_amount = total_gross, total_net, total_stone, total_cost
            record.save(update_fields=['gross_weight', 'net_weight', 'stone_weight', 'cost_amount', 'sku', 'location', 'bin', 'into_qc',
                                       'unit_cost'], _engine=True)
        else:
            gross = D(gross_weight) if gross_weight not in (None, '') else (qty if order_line.item.base_uom.code in ('GM',) else default_gross * qty)
            stone = D(stone_weight) if stone_weight not in (None, '') else (ZERO if order_line.item.base_uom.code in ('GM',) else default_stone * qty)
            net = D(net_weight) if net_weight not in (None, '') else max(gross - stone, ZERO)
            record.gross_weight, record.net_weight, record.stone_weight = q3(gross), q3(net), q3(stone)
            inv_entry = self.inventory.receive(transaction_type='OUTPUT', item=order_line.item, variant=order_line.variant, sku=sku,
                                               location=location, bin=bin, quantity=qty, unit_cost=unit_cost, gross_weight=record.gross_weight,
                                               net_weight=record.net_weight, lot_no=lot_no, into_state='QC' if into_qc else None,
                                               line_no=order_line.line_no)
            record.cost_amount = inv_entry.cost_amount
            record.save()
            record.inventory_entries.add(inv_entry)
        cost_type = 'BYPRODUCT' if order_line.line_type == 'BY_PRODUCT' else 'OUTPUT'
        self.cost(cost_type, -record.cost_amount, source=record, description=f'{order_line.item.item_no} x {qty} to {location.code}')
        return created

    def complete_operation(self, operation):
        mark_operation_complete(operation)

    def _backflush(self, operation, is_final, reported_qty):
        order = self.order
        if not self.setup.auto_post_consumption or not order.planned_qty:
            return
        components = order.components.filter(flushing_method__in=('BACKWARD', 'PICK_BACKWARD'), expected_qty__gt=0).select_related('item', 'uom')
        for component in components:
            linked = component.routing_link_code
            if operation is None:
                applies = is_final
            elif linked:
                applies = linked == operation.routing_link_code
            else:
                applies = is_final
            if not applies:
                continue
            qty = min(q3(component.expected_qty * reported_qty / order.planned_qty), component.remaining_qty)
            if qty > 0:
                self.consume(component, qty, operation=operation, source='BACKWARD')

    def forward_flush(self, operation=None):
        """Consume forward-flushed components: linked ones when their operation starts, unlinked ones at release."""
        order = self.order
        components = order.components.filter(flushing_method__in=('FORWARD', 'PICK_FORWARD'), expected_qty__gt=0).select_related('item', 'uom')
        for component in components:
            linked = component.routing_link_code
            if operation is None and linked and order.operations.filter(routing_link_code=linked).exists():
                continue
            if operation is not None and (not linked or linked != operation.routing_link_code):
                continue
            if component.remaining_qty > 0:
                self.consume(component, component.remaining_qty, operation=operation, source='FORWARD')

    # ------------------------------------------------------------------ scrap

    def scrap(self, *, operation=None, quantity=0, weight=0, scrap_type='PROCESS_LOSS', reason=None, recoverable=False,
              operator=None, machine=None, is_rework=False):
        order = self.order
        qty, weight = q3(quantity), q3(weight)
        if qty < 0 or weight < 0 or (qty == 0 and weight == 0):
            raise ManufacturingError('Enter a scrap quantity and/or weight.')
        if reason is not None:
            scrap_type = reason.scrap_type or scrap_type
            recoverable = recoverable or reason.recoverable
        recovery_line = order.lines.filter(line_type='BY_PRODUCT').order_by('line_no').first()
        record = ScrapEntry(
            entry_no=self._no(), order=order, operation=operation, item=order.item, scrap_type=scrap_type, reason=reason, quantity=qty,
            weight=weight, recoverable=recoverable, operator=operator or self.user, is_rework=is_rework,
            machine_center=machine or (operation.machine_center if operation else None),
            approved_by=self.user if can(self.actor, 'approve_scrap') else None, user=self.user, **self._ledger_fields(),
        )
        metal_cost_per_gram = self._metal_cost_per_gram()
        if recoverable and weight > 0:
            if recovery_line is None:
                raise ManufacturingError('Recoverable scrap needs a by-product (metal recovery) line on the order.')
            location = order.scrap_location or order.location
            sku = ensure_sku(self.actor, recovery_line.item, recovery_line.variant, location, recovery_line.sku)
            credit = recovery_line.unit_cost or metal_cost_per_gram
            entry = self.inventory.receive(transaction_type='SCRAP', item=recovery_line.item, variant=recovery_line.variant, sku=sku,
                                           location=location, bin=order.scrap_bin, quantity=weight, unit_cost=credit, gross_weight=weight,
                                           net_weight=weight, reason_code=scrap_type)
            record.recovery_item, record.recovery_entry, record.cost_amount = recovery_line.item, entry, entry.cost_amount
            record.save()
            recovery_line.output_qty += weight
            recovery_line.save(update_fields=['output_qty', 'updated_at'])
            self.cost('BYPRODUCT', -entry.cost_amount, source=record, description=f'Recovered {weight} g ({record.get_scrap_type_display()})')
        else:
            record.cost_amount = money(weight * metal_cost_per_gram)  # value lost; stays in WIP until the finish variance
            record.save()
        if operation is not None:
            operation.scrap_qty += qty
            operation.save(update_fields=['scrap_qty', 'updated_at'])
            last = final_operation(order)
            if last is not None and operation.pk == last.pk:
                order.scrap_qty += qty
        elif qty:
            order.scrap_qty += qty
        self.records['scrap'].append(record)
        return record

    def _metal_cost_per_gram(self):
        totals = ConsumptionEntry.objects.filter(tenant=self.tenant, order=self.order, metal__in=('GOLD', 'SILVER', 'PLATINUM'),
                                                 reversed=False, reversal_of__isnull=True).aggregate(w=Sum('net_weight'), c=Sum('cost_amount'))
        return (totals['c'] / totals['w']).quantize(Decimal('0.0001')) if totals['w'] else ZERO

    # ------------------------------------------------------------------ G/L and finalization

    def _gl_lines(self):
        net = {}
        for entry in self.records['cost']:
            amount = entry.amount
            net[entry.debit_account] = net.get(entry.debit_account, ZERO) + abs(amount)
            net[entry.credit_account] = net.get(entry.credit_account, ZERO) - abs(amount)
        return [(field, amount) for field, amount in net.items() if money(amount) != 0]

    def _post_gl(self):
        lines = self._gl_lines()
        setup = self.setup
        if not lines:
            return 'NOT_REQUIRED', []
        preview = [{'account': ACCOUNT_LABELS[f], 'code': getattr(getattr(setup, f), 'account_code', ''),
                    'debit': str(money(a)) if a > 0 else '', 'credit': str(money(-a)) if a < 0 else ''} for f, a in lines]
        if not setup.gl_posting_enabled:
            return 'DISABLED', preview
        missing = [ACCOUNT_LABELS[f] for f, _ in lines if getattr(setup, f'{f}_id') is None]
        if missing:
            raise ManufacturingError(f'Manufacturing posting setup is missing G/L accounts: {", ".join(missing)}.')
        from erp import services as erp_services
        company = setup.company or self.order.company or erp_services.get_default_company()
        voucher_lines = []
        for number, (field, amount) in enumerate(lines, 1):
            line = {'line_no': number, 'account': getattr(setup, field),
                    'description': f'{self.order.order_no} {self.batch.batch_no} {ACCOUNT_LABELS[field]}'[:250]}
            line['debit_amount' if amount > 0 else 'credit_amount'] = money(abs(amount))
            voucher_lines.append(line)
        try:
            voucher = erp_services._post_document_voucher(
                company=company, voucher_type_code='production_journal', user=self.user, lines=voucher_lines,
                narration=f'Production {self.order.order_no} - {self.batch.get_kind_display()} {self.batch.batch_no}',
                document_no=self.batch.batch_no, voucher_date=self.posting_date, source_doc=self.batch, source_doc_type='production_posting',
            )
        except ValueError as exc:
            raise ManufacturingError(f'G/L posting failed: {exc}')
        self.batch.finance_voucher = voucher
        return 'POSTED', preview

    def finalize(self, *, action=None):
        order = self.order
        derive_status(order, self.setup, actor=self.actor)
        if order.actual_start is None and (self.records['consumption'] or self.records['runtime'] or self.records['output']):
            order.actual_start = dj_timezone.now()
        order.updated_by = self.user
        order.save()
        gl_status, gl_preview = self._post_gl()
        wip_delta = sum((c.amount for c in self.records['cost']), ZERO)
        self.batch.gl_status = gl_status
        self.batch.summary = {
            'inventory': [_inventory_row(e) for e in self.inventory.entries],
            'consumption': [{'entry': r.entry_no, 'item': r.item.item_no, 'qty': str(r.quantity), 'uom': r.uom.code if r.uom_id else '',
                             'net_weight': str(r.net_weight), 'fine_weight': str(r.fine_weight), 'cost': str(r.cost_amount),
                             'source': r.source, 'from_issue': r.from_issue} for r in self.records['consumption']],
            'output': [{'entry': r.entry_no, 'item': r.item.item_no, 'operation': r.operation.operation_no if r.operation_id else '',
                        'qty': str(r.quantity), 'scrap': str(r.scrap_qty), 'final': r.is_final, 'cost': str(r.cost_amount),
                        'location': r.location.code if r.location_id else ''} for r in self.records['output']],
            'capacity': [{'entry': r.entry_no, 'operation': r.operation.operation_no, 'work_center': r.work_center.code,
                          'setup': str(r.setup_minutes), 'run': str(r.run_minutes), 'downtime': str(r.downtime_minutes),
                          'total': str(r.total_minutes), 'cost': str(r.cost_amount)} for r in self.records['runtime']],
            'scrap': [{'entry': r.entry_no, 'type': r.get_scrap_type_display(), 'qty': str(r.quantity), 'weight': str(r.weight),
                       'recoverable': r.recoverable, 'value': str(r.cost_amount)} for r in self.records['scrap']],
            'cost': [{'entry': c.entry_no, 'type': c.get_cost_type_display(), 'amount': str(c.amount), 'description': c.description,
                      'debit': ACCOUNT_LABELS[c.debit_account], 'credit': ACCOUNT_LABELS[c.credit_account]} for c in self.records['cost']],
            'wip_delta': str(money(wip_delta)),
            'gl': gl_preview, 'gl_status': gl_status,
            'progress': {'before': self.progress_before,
                         'after': {'produced': str(order.produced_qty), 'status': order.status, 'wip': str(self.order_wip())},
                         'planned': str(order.planned_qty)},
            'notes': self.notes,
            **self.extra,
        }
        self.batch.save()
        audit(self.actor, action or f'post_{self.batch.kind.lower()}', 'PRODUCTION_ORDER', order.order_no, order=order, batch=self.batch,
              new={'batch': self.batch.batch_no, 'wip_delta': money(wip_delta), 'gl': gl_status}, reason=self.batch.reason)
        return self.batch


def mark_operation_complete(operation):
    operation.status = 'QC_PENDING' if operation.quality_check_required else 'COMPLETED'
    operation.actual_end = operation.actual_end or dj_timezone.now()
    operation.clock_started_at = None
    if operation.machine_center_id and operation.machine_center.status == 'RUNNING':
        operation.machine_center.status = 'AVAILABLE'
        operation.machine_center.save(update_fields=['status', 'updated_at'])


def _inventory_row(entry):
    return {'id': entry.pk, 'type': entry.get_transaction_type_display(), 'item': entry.item.item_no, 'location': entry.location.code,
            'bin': entry.bin.code if entry.bin_id else '', 'unit': entry.barcode, 'qty': str(entry.quantity),
            'state': f'{entry.status_from or ""}→{entry.status_to or ""}'.strip('→'), 'gross': str(entry.gross_weight),
            'cost': str(entry.cost_amount)}


def derive_status(order, setup=None, actor=None):
    """Execution status follows what has actually been posted - never set by hand."""
    if order.status not in ProductionOrder.EXECUTION_STATUSES:
        return order.status
    setup = setup or get_setup(order.tenant)
    before = order.status
    components = list(order.components.all())
    if order.planned_qty and order.produced_qty >= order.planned_qty:
        if order.qc_pending_qty > 0:
            status = 'QC_PENDING'
        else:
            status = 'QC_APPROVED' if setup.require_quality_check else 'COMPLETED'
    elif order.produced_qty > 0:
        status = 'PARTIALLY_COMPLETED'
    elif any(c.consumed_qty for c in components) or order.operations.exclude(status='NOT_STARTED').exclude(status='READY').exists() \
            or RuntimeEntry.objects.filter(order=order).exists():
        status = 'IN_PRODUCTION'
    elif any(c.issued_open_qty for c in components):
        status = 'MATERIAL_ISSUED'
    elif any(c.reserved_qty for c in components):
        status = 'MATERIAL_RESERVED'
    else:
        status = 'RELEASED'
    order.status = status
    if actor is not None and before != status:
        audit(actor, 'status', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': before}, new={'status': status})
    return status


# ---------------------------------------------------------------------------
# Production journal: validate, preview, post
# ---------------------------------------------------------------------------

def validate_journal(order, lines):
    errors = []
    for line in lines:
        prefix = f'Line {line.line_no} ({line.get_entry_type_display()})'
        if line.entry_type == 'CONSUMPTION':
            if line.component is None:
                errors.append(f'{prefix}: select the component.')
            elif line.quantity <= 0:
                errors.append(f'{prefix}: quantity must be greater than zero.')
            elif line.component.item.blocked or not line.component.item.active:
                errors.append(f'{prefix}: item {line.component.item.item_no} is blocked or inactive.')
        elif line.entry_type == 'RUNTIME':
            if line.operation is None:
                errors.append(f'{prefix}: select the operation.')
        elif line.entry_type == 'OUTPUT':
            if line.quantity < 0 or line.scrap_qty < 0 or (line.quantity == 0 and line.scrap_qty == 0):
                errors.append(f'{prefix}: enter a good output and/or scrap quantity.')
        elif line.entry_type == 'SCRAP' and line.quantity <= 0 and line.scrap_weight <= 0:
            errors.append(f'{prefix}: enter a scrap quantity or weight.')
        for field in ('component', 'operation', 'order_line'):
            ref = getattr(line, field)
            if ref is not None and ref.order_id != order.id:
                errors.append(f'{prefix}: {field.replace("_", " ")} belongs to another order.')
    if errors:
        raise ManufacturingError(' '.join(errors))


def _post_lines(engine, journal, lines):
    is_rework = journal.rework_id is not None or journal.journal_type == 'REWORK'
    for line in sorted(lines, key=lambda l: (LINE_ORDER[l.entry_type], l.line_no)):
        if line.entry_type == 'CONSUMPTION':
            engine.consume(line.component, line.quantity, unit=line.jewellery_unit, lot_no=line.lot_no, operation=line.operation,
                           source='REWORK' if is_rework else line.source or 'MANUAL', is_rework=is_rework)
        elif line.entry_type == 'RUNTIME':
            engine.runtime(line.operation, setup=line.setup_minutes, run=line.run_minutes, wait=line.wait_minutes, move=line.move_minutes,
                           queue=line.queue_minutes, downtime=line.downtime_minutes, breaks=line.break_minutes, employee=line.operator,
                           resource=line.resource, machine=line.machine_center, start=line.start_time, end=line.end_time,
                           downtime_reason=line.downtime_reason, is_rework=is_rework)
        elif line.entry_type == 'OUTPUT':
            engine.output(operation=line.operation, order_line=line.order_line, quantity=line.quantity,
                          scrap_qty=line.scrap_qty, gross_weight=line.gross_weight, net_weight=line.net_weight,
                          stone_weight=line.stone_weight, units=line.output_units, lot_no=line.lot_no, operator=line.operator,
                          machine=line.machine_center, finished=line.finished, is_rework=is_rework)
        elif line.entry_type == 'SCRAP':
            engine.scrap(operation=line.operation, quantity=line.quantity, weight=line.scrap_weight, scrap_type=line.scrap_type or 'PROCESS_LOSS',
                         reason=line.scrap_reason, recoverable=line.recoverable, operator=line.operator, machine=line.machine_center,
                         is_rework=is_rework)


def post_journal(actor, journal, *, idempotency_key='', preview=False):
    """Post a production journal atomically. Returns the PostingBatch, or None when it now awaits scrap approval."""
    require(actor, 'execute')
    with transaction.atomic():
        if idempotency_key:
            existing = PostingBatch.objects.filter(tenant=actor.tenant, idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing
        journal = ProductionJournal.objects.select_for_update().filter(tenant=actor.tenant, pk=journal.pk).first()
        if journal is None:
            raise ManufacturingError('Journal not found.')
        if journal.status == 'POSTED':
            raise ManufacturingError(f'Journal {journal.journal_no} is already posted.')
        if journal.status == 'CANCELLED':
            raise ManufacturingError(f'Journal {journal.journal_no} is cancelled.')
        order = lock_order(actor, journal.order_id)
        assert_executable(order)
        setup = get_setup(actor.tenant)
        lines = list(journal.lines.select_related('component__item', 'component__uom', 'operation__work_center', 'order_line__item',
                                                  'jewellery_unit', 'scrap_reason', 'machine_center', 'resource'))
        if not lines:
            raise ManufacturingError('The journal has no lines.')
        validate_journal(order, lines)
        if not preview and any(l.entry_type == 'SCRAP' for l in lines) and setup.scrap_requires_approval and not can(actor, 'approve_scrap'):
            journal.status = 'PENDING_APPROVAL'
            journal.save(update_fields=['status', 'updated_at'])
            audit(actor, 'submit_scrap', 'PRODUCTION_JOURNAL', journal.journal_no, order=order, reason='Scrap awaits supervisor approval')
            return None
        engine = ManufacturingPostingEngine(actor, order, kind='JOURNAL', journal=journal, posting_date=journal.posting_date,
                                            idempotency_key='' if preview else idempotency_key)
        _post_lines(engine, journal, lines)
        batch = engine.finalize(action='post_journal')
        journal.status, journal.posted_batch = 'POSTED', batch
        journal.save(update_fields=['status', 'posted_batch', 'updated_at'])
        if journal.rework_id and journal.rework.status == 'OPEN':
            ProductionRework.objects.filter(pk=journal.rework_id).update(status='IN_PROGRESS')
        if preview:
            raise _PreviewRollback(batch.summary)
        return batch


def preview_journal(actor, journal):
    """Run the real posting and roll it back: the preview shows exactly what Post would write."""
    try:
        post_journal(actor, journal, preview=True)
    except _PreviewRollback as rollback:
        return {'ok': True, **rollback.summary}
    except (InventoryError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    return {'ok': False, 'error': 'Nothing to preview.'}


def quick_post(actor, order, lines, *, journal_type='PRODUCTION', description='', idempotency_key='', rework=None, preview=False,
               posting_date=None):
    """Create a journal from line dicts and post (or preview) it - used by shop floor, API and flushing."""
    from .models import ProductionJournalLine
    from .security import JOURNAL_SERIES
    if idempotency_key:
        existing = PostingBatch.objects.filter(tenant=actor.tenant, idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
    with transaction.atomic():
        journal = ProductionJournal.objects.create(
            tenant=actor.tenant, company=order.company, journal_no=next_number(actor.tenant, JOURNAL_SERIES[journal_type]),
            journal_type=journal_type, order=order, rework=rework, description=description[:200], created_by=actor.user,
            posting_date=posting_date or dj_timezone.localdate(),
        )
        for number, data in enumerate(lines, 1):
            ProductionJournalLine.objects.create(tenant=actor.tenant, company=order.company, journal=journal, line_no=number * 10,
                                                 created_by=actor.user, **data)
        if preview:
            summary = preview_journal(actor, journal)
            transaction.set_rollback(True)
            return summary
        return post_journal(actor, journal, idempotency_key=idempotency_key)


# ---------------------------------------------------------------------------
# Material issue / return (warehouse pick to the production bin)
# ---------------------------------------------------------------------------

def issue_material(actor, order, requests, *, pick_list=None):
    """Move material from stores bins to the production bin (PICKED). `requests`: dicts component, quantity, unit, from_bin, pick_line."""
    require(actor, 'execute')
    with transaction.atomic():
        order = lock_order(actor, order)
        assert_executable(order)
        setup = get_setup(actor.tenant)
        engine = ManufacturingPostingEngine(actor, order, kind='ISSUE')
        for request in requests:
            component = ProductionOrderComponent.objects.select_for_update().select_related('item', 'uom', 'location').get(
                pk=request['component'].pk, order=order)
            qty = q3(request.get('quantity') or 0)
            unit = request.get('unit')
            if unit is not None:
                qty = Decimal('1')
            if qty <= 0:
                continue
            if component.location_id != order.location_id:
                raise ManufacturingError(
                    f'{component.item.item_no} is planned from {component.location.code} but production runs at {order.location.code}. '
                    'Create a transfer order to the production location - material is never teleported between locations.')
            if not setup.allow_over_consumption and qty > component.to_issue_qty:
                raise ManufacturingError(f'Only {component.to_issue_qty} of {component.item.item_no} is still to be issued.')
            moves = []
            if unit is not None:
                unit = JewelleryUnit.objects.get(pk=unit.pk, tenant=actor.tenant)
                if unit.item_id != component.item_id:
                    raise ManufacturingError(f'Scanned unit {unit.barcode} is not {component.item.item_no}.')
                if unit.current_location_id != order.location_id:
                    raise ManufacturingError(f'Unit {unit.barcode} is at {unit.current_location}, not the production location. Transfer it first.')
                reservation = open_reservations(order, component).filter(jewellery_unit=unit).first()
                if unit.status == 'RESERVED' and reservation is None:
                    raise ManufacturingError(f'Unit {unit.barcode} is reserved for another document.')
                moves.append(engine.inventory.move(transaction_type='PRODUCTION', item=component.item, variant=component.variant,
                                                   from_location=unit.current_location, to_location=order.location, unit=unit,
                                                   to_bin=order.production_bin, into_state='PICKED', reservation=reservation,
                                                   line_no=component.line_no))
            else:
                remaining = qty
                for reservation in open_reservations(order, component).select_related('jewellery_unit'):
                    if remaining <= 0:
                        break
                    take = min(reservation.open_quantity, remaining)
                    moves.append(engine.inventory.move(transaction_type='PRODUCTION', item=component.item, variant=component.variant,
                                                       from_location=reservation.location, to_location=order.location,
                                                       to_bin=order.production_bin, quantity=take, into_state='PICKED', reservation=reservation,
                                                       unit=reservation.jewellery_unit, line_no=component.line_no))
                    remaining -= take
                if remaining > 0 and component.item.serial_tracking:
                    raise ManufacturingError(f'{component.item.item_no} is serialized: scan the pieces to issue ({remaining} not reserved).')
                if remaining > 0:
                    moves.append(engine.inventory.move(transaction_type='PRODUCTION', item=component.item, variant=component.variant,
                                                       from_location=component.location, to_location=order.location,
                                                       from_bin=request.get('from_bin') or component.bin, to_bin=order.production_bin,
                                                       quantity=remaining, into_state='PICKED', line_no=component.line_no))
            for out_entries, in_entries in moves:
                for out_entry, in_entry in zip(out_entries, in_entries):
                    issue = ProductionMaterialIssue.objects.create(
                        issue_no=engine._no(), order=order, component=component, pick_line=request.get('pick_line'), direction='ISSUE',
                        item=component.item, sku=out_entry.sku, from_location=out_entry.location, from_bin=out_entry.bin,
                        to_location=in_entry.location, to_bin=in_entry.bin, jewellery_unit=in_entry.jewellery_unit, lot_no=in_entry.lot_no,
                        huid=in_entry.huid, quantity=in_entry.quantity, gross_weight=in_entry.gross_weight, net_weight=in_entry.net_weight,
                        unit_cost=in_entry.unit_cost, total_cost=in_entry.cost_amount, issued_by=actor.user,
                        approved_by=actor.user if can(actor, 'release') else None, **engine._ledger_fields(),
                    )
                    engine.records['issues'].append(issue)
                    component.picked_qty += in_entry.quantity
            component.save(update_fields=['picked_qty', 'updated_at'])
            sync_reserved(component)
        if not engine.records['issues']:
            raise ManufacturingError('Nothing to issue.')
        engine.extra['issues'] = [{'issue': i.issue_no, 'item': i.item.item_no, 'qty': str(i.quantity), 'from': i.from_bin.code if i.from_bin_id else i.from_location.code,
                                   'to': i.to_bin.code if i.to_bin_id else i.to_location.code, 'cost': str(i.total_cost)} for i in engine.records['issues']]
        if pick_list is not None:
            pick_list.status, pick_list.posted_by, pick_list.posted_at = 'POSTED', actor.user, dj_timezone.now()
            pick_list.save(update_fields=['status', 'posted_by', 'posted_at', 'updated_at'])
        return engine.finalize(action='issue_material')


def return_material(actor, order, component, quantity=None, *, unit=None, to_bin=None):
    """Return issued-but-unconsumed material from the production bin to stores (available again)."""
    require(actor, 'execute')
    with transaction.atomic():
        order = lock_order(actor, order)
        assert_executable(order)
        component = ProductionOrderComponent.objects.select_for_update().select_related('item').get(pk=component.pk, order=order)
        qty = Decimal('1') if unit is not None else q3(quantity if quantity is not None else component.issued_open_qty)
        if qty <= 0 or qty > component.issued_open_qty:
            raise ManufacturingError(f'Only {component.issued_open_qty} of {component.item.item_no} is issued and unconsumed.')
        engine = ManufacturingPostingEngine(actor, order, kind='RETURN')
        target_bin = to_bin or component.bin or order.material_bin
        out_entries, in_entries = engine.inventory.move(
            transaction_type='PRODUCTION', item=component.item, variant=component.variant, from_location=order.location,
            to_location=component.location, from_bin=order.production_bin if unit is None else None, to_bin=target_bin, quantity=qty,
            unit=unit, consume_state='PICKED', line_no=component.line_no)
        for out_entry, in_entry in zip(out_entries, in_entries):
            engine.records['issues'].append(ProductionMaterialIssue.objects.create(
                issue_no=engine._no(), order=order, component=component, direction='RETURN', item=component.item, sku=in_entry.sku,
                from_location=out_entry.location, from_bin=out_entry.bin, to_location=in_entry.location, to_bin=in_entry.bin,
                jewellery_unit=in_entry.jewellery_unit, lot_no=in_entry.lot_no, quantity=in_entry.quantity, gross_weight=in_entry.gross_weight,
                net_weight=in_entry.net_weight, unit_cost=in_entry.unit_cost, total_cost=in_entry.cost_amount, issued_by=actor.user,
                **engine._ledger_fields()))
        component.returned_qty += qty
        component.save(update_fields=['returned_qty', 'updated_at'])
        return engine.finalize(action='return_material')


# ---------------------------------------------------------------------------
# Quality control
# ---------------------------------------------------------------------------

def post_qc(actor, qc, *, unit_results=None):
    """Post an inspection. Final QC: passed pieces are put away to finished goods, failed pieces are scrapped
    (their value returns to WIP and becomes scrap variance), rework / hold pieces stay in QC."""
    require(actor, 'qc')
    from .services import create_rework
    with transaction.atomic():
        qc = ProductionQC.objects.select_for_update().get(pk=qc.pk, tenant=actor.tenant)
        if qc.status != 'OPEN':
            raise ManufacturingError(f'{qc.qc_no} is already {qc.get_status_display().lower()}.')
        order = lock_order(actor, qc.order_id)
        if order.is_closed:
            raise ManufacturingError(f'{order.order_no} is closed.')
        setup = get_setup(actor.tenant)
        lines = list(qc.lines.select_related('parameter'))
        mandatory = _mandatory_parameters(actor.tenant, qc.stage)
        missing = [p.name for p in mandatory if not any(l.parameter_id == p.pk for l in lines)]
        if missing:
            raise ManufacturingError(f'Record the mandatory QC parameters: {", ".join(missing)}.')
        failed_params = [l.parameter.name for l in lines if l.result == 'FAIL']
        engine = ManufacturingPostingEngine(actor, order, kind='QC')
        if qc.stage in ('FINAL', 'JEWELLERY'):
            line = main_line(order)
            if unit_results is not None:
                qc.unit_results = [{'unit': u.pk, 'result': r} for u, r in unit_results]
                counts = {r: sum(1 for _, x in unit_results if x == r) for r in ('PASS', 'FAIL', 'REWORK', 'HOLD')}
                qc.passed_qty, qc.failed_qty, qc.rework_qty, qc.hold_qty = (D(counts[r]) for r in ('PASS', 'FAIL', 'REWORK', 'HOLD'))
                qc.inspected_qty = D(len(unit_results))
            if qc.passed_qty + qc.failed_qty + qc.rework_qty + qc.hold_qty != qc.inspected_qty or qc.inspected_qty <= 0:
                raise ManufacturingError('Passed + failed + rework + hold must equal the inspected quantity.')
            if qc.inspected_qty > order.qc_pending_qty:
                raise ManufacturingError(f'Only {order.qc_pending_qty} pieces are awaiting QC.')
            if failed_params and qc.passed_qty == qc.inspected_qty:
                raise ManufacturingError(f'Parameters failed ({", ".join(failed_params)}) - the result cannot be a full pass.')
            fg_location = order.finished_goods_location or order.location
            qc_location = order.qc_location or order.location
            fg_sku = ensure_sku(actor, line.item, line.variant, fg_location, order.sku)
            if line.item.serial_tracking:
                if unit_results is None:
                    raise ManufacturingError('Record a result for each serialized piece.')
                profile = getattr(line.item, 'manufacturing', None)
                for unit, result in unit_results:
                    unit = JewelleryUnit.objects.get(pk=unit.pk, tenant=actor.tenant)
                    output_unit = OutputUnit.objects.filter(order=order, jewellery_unit=unit, reversed=False).first()
                    if output_unit is None or unit.status != 'QC':
                        raise ManufacturingError(f'{unit.barcode} is not awaiting QC on this order.')
                    if result == 'PASS':
                        if (setup.require_huid_on_final_qc or (profile and profile.huid_required)) and not unit.huid:
                            raise ManufacturingError(f'{unit.barcode} has no HUID. Assign the HUID before final QC.')
                        if (setup.require_hallmark_on_final_qc or (profile and profile.hallmark_required)) and output_unit.hallmark_status != 'HALLMARKED':
                            raise ManufacturingError(f'{unit.barcode} is not hallmarked yet.')
                        engine.inventory.move(transaction_type='QC', item=line.item, variant=line.variant, from_location=qc_location,
                                              to_location=fg_location, unit=unit, to_sku=fg_sku, to_bin=order.finished_goods_bin,
                                              consume_state='QC', line_no=line.line_no)
                        output_unit.qc_status = 'PASSED'
                    elif result == 'FAIL':
                        entry = engine.inventory.issue(transaction_type='SCRAP', item=line.item, variant=line.variant, location=qc_location,
                                                       unit=unit, consume_state='QC', final_unit_status='SCRAPPED', reason_code='QC_FAIL')
                        engine.cost('SCRAP_WRITE_OFF', -entry.cost_amount, source=qc, description=f'QC rejected {unit.barcode}')
                        output_unit.qc_status = 'FAILED'
                    else:
                        output_unit.qc_status = result
                    output_unit.save(update_fields=['qc_status', 'updated_at'])
            else:
                if qc.passed_qty:
                    engine.inventory.move(transaction_type='QC', item=line.item, variant=line.variant, from_location=qc_location,
                                          to_location=fg_location, from_bin=order.qc_bin, to_bin=order.finished_goods_bin, to_sku=fg_sku,
                                          quantity=qc.passed_qty, consume_state='QC', line_no=line.line_no)
                if qc.failed_qty:
                    entry = engine.inventory.issue(transaction_type='SCRAP', item=line.item, variant=line.variant, location=qc_location,
                                                   bin=order.qc_bin, quantity=qc.failed_qty, consume_state='QC', reason_code='QC_FAIL')
                    engine.cost('SCRAP_WRITE_OFF', -entry.cost_amount, source=qc, description=f'QC rejected {qc.failed_qty}')
            order.accepted_qty += qc.passed_qty
            order.rejected_qty += qc.failed_qty
            order.qc_pending_qty -= qc.passed_qty + qc.failed_qty
            line.accepted_qty += qc.passed_qty
            line.rejected_qty += qc.failed_qty
            line.save(update_fields=['accepted_qty', 'rejected_qty', 'updated_at'])
            qc.result = 'PASS' if qc.passed_qty == qc.inspected_qty else ('REWORK' if qc.rework_qty else ('FAIL' if qc.failed_qty else 'HOLD'))
        elif qc.stage == 'OPERATION':
            if qc.operation is None:
                raise ManufacturingError('Select the operation inspected.')
            operation = qc.operation
            if failed_params and qc.result == 'PASS':
                raise ManufacturingError(f'Parameters failed ({", ".join(failed_params)}) - the result cannot be a pass.')
            operation.status = 'QC_PASSED' if qc.result == 'PASS' else ('QC_FAILED' if qc.result in ('FAIL', 'REWORK') else operation.status)
            operation.save(update_fields=['status', 'updated_at'])
        qc.status, qc.batch = 'POSTED', engine.batch
        qc.inspector = qc.inspector or actor.user
        qc.save()
        engine.extra['qc'] = {'qc': qc.qc_no, 'stage': qc.stage, 'result': qc.result, 'passed': str(qc.passed_qty),
                              'failed': str(qc.failed_qty), 'rework': str(qc.rework_qty), 'hold': str(qc.hold_qty)}
        batch = engine.finalize(action='post_qc')
        if qc.rework_qty > 0 or (qc.stage == 'OPERATION' and qc.result in ('FAIL', 'REWORK')):
            create_rework(actor, order, qc=qc, quantity=qc.rework_qty or qc.inspected_qty,
                          reason=f'QC {qc.qc_no}: {", ".join(failed_params) or qc.remarks or "rework required"}')
        return batch


def _mandatory_parameters(tenant, stage):
    from .models import QualityParameter
    return list(QualityParameter.objects.filter(tenant=tenant, active=True, mandatory=True).filter(Q(stage='ALL') | Q(stage=stage)))


# ---------------------------------------------------------------------------
# Finish (cost settlement), reopen
# ---------------------------------------------------------------------------

def finish_readiness(order, setup=None):
    """(blocking issues, remaining-work warnings) for finishing an order."""
    setup = setup or get_setup(order.tenant)
    blocking, remaining = [], []
    if not order.is_executable:
        blocking.append(f'Order is {order.get_status_display().lower()}.')
    if order.qc_pending_qty > 0:
        blocking.append(f'{order.qc_pending_qty} pieces are still awaiting QC.')
    running = [op.operation_no for op in order.operations.filter(status__in=('STARTED', 'PAUSED'))]
    if running:
        blocking.append(f'Operation(s) {", ".join(running)} are still running - complete or stop them.')
    open_issue = [c.item.item_no for c in order.components.select_related('item') if c.issued_open_qty > 0]
    if open_issue:
        blocking.append(f'Issued material not consumed or returned: {", ".join(open_issue)}.')
    if order.subcontract_orders.filter(status__in=('SENT', 'PARTIALLY_RECEIVED')).exists():
        blocking.append('Material is still at a subcontractor.')
    if order.journals.filter(status='PENDING_APPROVAL').exists():
        blocking.append('A journal is waiting for scrap approval.')
    if order.reworks.filter(status__in=('OPEN', 'IN_PROGRESS')).exists():
        blocking.append('A rework order is still open.')
    if order.produced_qty < order.planned_qty:
        remaining.append(f'Output {order.produced_qty} is below the planned {order.planned_qty}.')
    open_ops = [op.operation_no for op in order.operations.filter(is_rework=False).exclude(status__in=DONE_OPERATION)]
    if open_ops:
        remaining.append(f'Operation(s) not completed: {", ".join(open_ops)}.')
    short = [f'{c.item.item_no} ({c.consumed_qty}/{c.expected_qty})' for c in order.components.select_related('item')
             if not c.optional and c.consumed_qty < c.expected_qty]
    if short:
        remaining.append(f'Components not fully consumed: {", ".join(short)}.')
    return blocking, remaining


def cost_breakdown(order):
    """Standard (scaled to actual output) vs actual cost by type, from the WIP ledger."""
    entries = CostEntry.objects.filter(tenant=order.tenant, order=order)
    actual = {t: ZERO for t, _ in CostEntry.TYPES}
    rework = ZERO
    for row in entries.values('cost_type', 'is_rework').annotate(v=Sum('amount')):
        if row['is_rework'] and row['cost_type'] in ('MATERIAL', 'LABOUR', 'MACHINE', 'OVERHEAD', 'SUBCONTRACT'):
            rework += money(row['v'])
        else:
            actual[row['cost_type']] += money(row['v'])
    factor = (order.produced_qty / order.planned_qty) if order.planned_qty else ZERO
    standard = {'MATERIAL': order.planned_material_cost, 'LABOUR': order.planned_labour_cost, 'MACHINE': order.planned_machine_cost,
                'OVERHEAD': order.planned_overhead_cost, 'SUBCONTRACT': order.planned_subcontract_cost}
    rows = []
    for key in ('MATERIAL', 'LABOUR', 'MACHINE', 'OVERHEAD', 'SUBCONTRACT'):
        std = money(standard[key] * factor)
        rows.append({'type': key, 'planned': standard[key], 'standard': std, 'actual': money(actual[key]), 'variance': money(actual[key] - std)})
    input_total = sum((r['actual'] for r in rows), ZERO) + money(rework)
    return {
        'rows': rows, 'rework': money(rework), 'input_total': input_total,
        'output': money(-actual['OUTPUT']), 'byproduct': money(-actual['BYPRODUCT']), 'scrap_write_off': money(actual['SCRAP_WRITE_OFF']),
        'settled': money(-(actual['VARIANCE'] + actual['REVALUATION'])), 'variance_settled': money(-actual['VARIANCE']),
        'revaluation': money(-actual['REVALUATION']), 'wip': money(sum(actual.values(), ZERO) + rework),
        'standard_total': money(sum((r['standard'] for r in rows), ZERO)), 'planned_total': order.planned_total_cost,
        'final_cost': money(input_total - money(-actual['BYPRODUCT'])), 'factor': factor,
    }


def finish_order(actor, order, *, force=False, reason=''):
    require(actor, 'finish')
    with transaction.atomic():
        order = lock_order(actor, order)
        setup = get_setup(actor.tenant)
        blocking, remaining = finish_readiness(order, setup)
        if blocking:
            raise ManufacturingError(' '.join(blocking))
        if remaining and not force:
            raise ConfirmationRequired('Finishing will close the remaining work: ' + ' '.join(remaining))
        if remaining and not (setup.allow_finish_with_remaining or can(actor, 'approve_order')):
            raise ManufacturingError('Only a manufacturing manager can finish an order with remaining work.')
        engine = ManufacturingPostingEngine(actor, order, kind='FINISH', reason=reason or ('; '.join(remaining))[:250])
        _settle(engine)
        for component in order.components.all():
            for reservation in open_reservations(order, component):
                engine.inventory.release(reservation)
            sync_reserved(component)
        order.status, order.finished_by, order.finished_at = 'FINISHED', actor.user, dj_timezone.now()
        order.actual_end = order.actual_end or dj_timezone.now()
        for operation in order.operations.exclude(status__in=DONE_OPERATION):
            operation.status = 'COMPLETED'
            operation.save(update_fields=['status', 'updated_at'])
        return engine.finalize(action='finish')


def _settle(engine):
    """Close WIP: variance entries for reporting, then settle the residual (actual costing revalues output still in stock)."""
    order = engine.order
    breakdown = cost_breakdown(order)
    for row in breakdown['rows']:
        engine.records['variance'].append(VarianceEntry.objects.create(
            order=order, variance_type=row['type'], standard_amount=row['standard'], actual_amount=row['actual'],
            variance_amount=row['variance'], **engine._ledger_fields()))
    factor = breakdown['factor']
    quantity_variance = sum((money((c.consumed_qty - c.expected_qty * factor) * c.unit_cost) for c in order.components.all()), ZERO)
    std_minutes = sum((op.planned_total_minutes * factor for op in order.operations.filter(is_rework=False)), ZERO)
    act_minutes = sum((op.actual_capacity_minutes for op in order.operations.all()), ZERO)
    scrap_value = sum((s.cost_amount for s in order.scrap_entries.filter(recoverable=False, reversed=False, reversal_of__isnull=True)), ZERO) \
        + breakdown['scrap_write_off']
    extra = [('QUANTITY', ZERO, quantity_variance, quantity_variance), ('RUNTIME', q3(std_minutes), q3(act_minutes), q3(act_minutes - std_minutes)),
             ('SCRAP', ZERO, scrap_value, scrap_value), ('REWORK', ZERO, breakdown['rework'], breakdown['rework'])]
    for kind, std, act, var in extra:
        engine.records['variance'].append(VarianceEntry.objects.create(order=order, variance_type=kind, standard_amount=std,
                                                                       actual_amount=act, variance_amount=var, **engine._ledger_fields()))
    residual = engine.order_wip()
    wip_before = residual
    revaluations = []
    if order.costing_method == 'ACTUAL' and residual != 0:
        revaluations = _revalue_output(engine, residual)
        revalued = sum((D(r['amount']) for r in revaluations), ZERO)
        if revalued:
            engine.cost('REVALUATION', -revalued, description='Actual cost settled onto output still in stock')
        residual -= revalued
    engine.records['variance'].append(VarianceEntry.objects.create(
        order=order, variance_type='TOTAL', standard_amount=breakdown['standard_total'], actual_amount=breakdown['input_total'],
        variance_amount=money(residual), **engine._ledger_fields()))
    if residual != 0:
        engine.cost('VARIANCE', -residual, description='Production variance settled at finish')
    engine.extra['revaluations'] = revaluations
    engine.extra['settlement'] = {'wip_before': str(money(wip_before)), 'wip_after': str(money(engine.order_wip())),
                                  'variance': str(money(residual)), 'revalued': str(money(sum((D(r['amount']) for r in revaluations), ZERO)))}


def _revalue_output(engine, residual):
    order = engine.order
    outputs = list(order.output_entries.filter(is_final=True, reversed=False, reversal_of__isnull=True).exclude(order_line__line_type='BY_PRODUCT'))
    total_qty = sum((o.quantity for o in outputs), ZERO)
    if not total_qty:
        return []
    per_unit = residual / total_qty
    done = []
    for output in outputs:
        units = list(output.units.filter(reversed=False).select_related('jewellery_unit'))
        if units:
            for output_unit in units:
                unit = output_unit.jewellery_unit
                if not unit.in_stock:
                    continue
                try:
                    engine.inventory.revalue(item=unit.item, location=None, unit=unit, cost_delta=money(per_unit), reason_code='ACTUAL_COST')
                except InventoryError:
                    continue
                done.append({'unit': unit.pk, 'amount': str(money(per_unit))})
        else:
            balance = InventoryBalance.objects.filter(tenant=engine.tenant, item=output.item, variant=output.variant, location=output.location,
                                                      bin=output.bin, jewellery_unit__isnull=True, lot_no=output.lot_no).first()
            on_hand = physical_qty(balance) if balance is not None else ZERO
            share = money(per_unit * min(on_hand, output.quantity))
            if share == 0:
                continue
            try:
                engine.inventory.revalue(item=output.item, variant=output.variant, location=output.location, bin=output.bin,
                                         lot_no=output.lot_no, cost_delta=share, reason_code='ACTUAL_COST')
            except InventoryError:
                continue
            done.append({'item': output.item_id, 'variant': output.variant_id, 'location': output.location_id, 'bin': output.bin_id,
                         'lot': output.lot_no, 'amount': str(share)})
    return done


def reopen_order(actor, order, *, reason):
    """Controlled reopen: the finish settlement is reversed (never deleted) and the order returns to execution."""
    require(actor, 'reopen')
    if not (reason or '').strip():
        raise ManufacturingError('A reason is required to reopen a finished order.')
    with transaction.atomic():
        order = lock_order(actor, order)
        if order.status != 'FINISHED':
            raise ManufacturingError('Only finished orders can be reopened.')
        finish = order.posting_batches.filter(kind='FINISH').exclude(reversals__isnull=False).order_by('-id').first()
        engine = ManufacturingPostingEngine(actor, order, kind='REOPEN', reason=reason, reversal_of=finish)
        if finish is not None:
            fallback = ZERO
            for item in (finish.summary or {}).get('revaluations', []):
                amount = D(item['amount'])
                try:
                    if 'unit' in item:
                        unit = JewelleryUnit.objects.get(pk=item['unit'], tenant=actor.tenant)
                        engine.inventory.revalue(item=unit.item, location=None, unit=unit, cost_delta=-amount, reason_code='REOPEN')
                    else:
                        from inventory.models import Bin, Item, ItemVariant, Location
                        engine.inventory.revalue(item=Item.objects.get(pk=item['item']), location=Location.objects.get(pk=item['location']),
                                                 variant=ItemVariant.objects.filter(pk=item['variant']).first(),
                                                 bin=Bin.objects.filter(pk=item['bin']).first(), lot_no=item.get('lot', ''),
                                                 cost_delta=-amount, reason_code='REOPEN')
                except (InventoryError, JewelleryUnit.DoesNotExist):
                    fallback += amount  # already sold / moved: the reversal lands in variance instead
            for entry in CostEntry.objects.filter(tenant=actor.tenant, order=order, batch=finish, reversed=False):
                if entry.cost_type == 'REVALUATION' and fallback:
                    engine.cost('REVALUATION', -entry.amount - fallback, description=f'Reversal of {entry.entry_no}', reversal_of=entry)
                    engine.cost('VARIANCE', fallback, description='Revaluation no longer reversible (output sold/moved)')
                else:
                    engine.cost(entry.cost_type, -entry.amount, description=f'Reversal of {entry.entry_no}', reversal_of=entry)
                entry.reversed = True
                entry.save(update_fields=['reversed', 'updated_at'])
            for variance in VarianceEntry.objects.filter(tenant=actor.tenant, order=order, batch=finish, reversed=False):
                VarianceEntry.objects.create(order=order, variance_type=variance.variance_type, standard_amount=-variance.standard_amount,
                                             actual_amount=-variance.actual_amount, variance_amount=-variance.variance_amount,
                                             reversal_of=variance, **engine._ledger_fields())
                variance.reversed = True
                variance.save(update_fields=['reversed', 'updated_at'])
        order.status = 'COMPLETED'
        order.finished_by, order.finished_at = None, None
        derive_status(order, engine.setup)
        return engine.finalize(action='reopen')


# ---------------------------------------------------------------------------
# Reversals - opposite entries that reference the original; nothing is deleted
# ---------------------------------------------------------------------------

REVERSIBLE = {'consumption': ConsumptionEntry, 'output': OutputEntry, 'runtime': RuntimeEntry, 'scrap': ScrapEntry}


def reverse_entry(actor, kind, entry, *, reason):
    require(actor, 'reverse')
    setup = get_setup(actor.tenant)
    if setup.require_manager_approval:
        require(actor, 'approve_reversal')
    if not (reason or '').strip():
        raise ManufacturingError('A reason is required for every reversal.')
    model = REVERSIBLE.get(kind)
    if model is None:
        raise ManufacturingError('Unknown entry type.')
    with transaction.atomic():
        entry = model.objects.select_for_update().filter(tenant=actor.tenant, pk=getattr(entry, 'pk', entry)).first()
        if entry is None:
            raise ManufacturingError('Entry not found.')
        if entry.reversed or entry.reversal_of_id:
            raise ManufacturingError(f'{entry.entry_no} is already reversed (or is itself a reversal).')
        order = lock_order(actor, entry.order_id)
        assert_executable(order)
        engine = ManufacturingPostingEngine(actor, order, kind='REVERSAL', reason=reason, reversal_of=entry.batch)
        getattr(engine_reversals, kind)(engine, entry)
        engine.reverse_costs_of(entry)
        engine._mark_reversed(entry)
        return engine.finalize(action=f'reverse_{kind}')


class engine_reversals:
    @staticmethod
    def consumption(engine, entry):
        component = ProductionOrderComponent.objects.select_for_update().get(pk=entry.component_id)
        unit = entry.jewellery_unit
        into_state = 'PICKED' if entry.from_issue else None
        inv = engine.inventory.receive(
            transaction_type='REVERSAL', item=entry.item, variant=component.variant, location=entry.location, bin=entry.bin, unit=unit,
            quantity=entry.quantity, unit_cost=(entry.cost_amount / entry.quantity) if entry.quantity else 0,
            gross_weight=entry.gross_weight if unit is None else None, net_weight=entry.net_weight if unit is None else None,
            lot_no=entry.lot_no, into_state=into_state, reason_code='REVERSAL', line_no=component.line_no, reversal_of=entry.inventory_entry)
        record = ConsumptionEntry.objects.create(
            entry_no=engine._no(), order=entry.order, component=component, operation=entry.operation, item=entry.item, sku=entry.sku,
            location=entry.location, bin=entry.bin, jewellery_unit=unit, lot_no=entry.lot_no, huid=entry.huid, quantity=-entry.quantity,
            uom=entry.uom, gross_weight=-entry.gross_weight, net_weight=-entry.net_weight, fine_weight=-entry.fine_weight, metal=entry.metal,
            purity=entry.purity, unit_cost=entry.unit_cost, cost_amount=-entry.cost_amount, from_issue=entry.from_issue, source=entry.source,
            is_rework=entry.is_rework, inventory_entry=inv, user=engine.user, reversal_of=entry, **engine._ledger_fields())
        component.consumed_qty -= entry.quantity
        component.consumed_weight -= entry.net_weight
        component.actual_cost -= entry.cost_amount
        if entry.from_issue:
            component.consumed_from_issue_qty -= entry.quantity
        component.save(update_fields=['consumed_qty', 'consumed_weight', 'actual_cost', 'consumed_from_issue_qty', 'updated_at'])
        engine.records['consumption'].append(record)

    @staticmethod
    def output(engine, entry):
        order = engine.order
        operation = entry.operation
        line = entry.order_line
        if entry.is_final:
            units = list(entry.units.filter(reversed=False).select_related('jewellery_unit'))
            if entry.into_qc and (units and any(u.qc_status not in ('PENDING', 'HOLD', 'REWORK') for u in units)
                                  or not units and order.qc_pending_qty < entry.quantity):
                raise ManufacturingError('This output has already been through QC - reverse the QC first.')
            first_inv = entry.inventory_entries.order_by('id').first()
            if units:
                for output_unit in units:
                    unit = output_unit.jewellery_unit
                    engine.inventory.issue(transaction_type='REVERSAL', item=unit.item, variant=unit.variant, location=unit.current_location,
                                           unit=unit, final_unit_status='NOT_IN_STOCK', reason_code='REVERSAL', reversal_of=first_inv)
                    output_unit.reversed = True
                    output_unit.save(update_fields=['reversed', 'updated_at'])
            else:
                engine.inventory.issue(transaction_type='REVERSAL', item=entry.item, variant=entry.variant, location=entry.location,
                                       bin=entry.bin, quantity=entry.quantity, lot_no=entry.lot_no, consume_state='QC' if entry.into_qc else None,
                                       cost_amount=entry.cost_amount, reason_code='REVERSAL', reversal_of=first_inv)
            line.output_qty -= entry.quantity
            line.scrap_qty -= entry.scrap_qty
            line.save(update_fields=['output_qty', 'scrap_qty', 'updated_at'])
            if line.line_type == 'MAIN':
                order.produced_qty -= entry.quantity
                order.scrap_qty -= entry.scrap_qty
                if entry.into_qc:
                    order.qc_pending_qty -= entry.quantity
                else:
                    order.accepted_qty -= entry.quantity
        if operation is not None:
            following = [op for op in operations_of(order, include_rework=False) if op.sequence > operation.sequence
                         or (op.sequence == operation.sequence and op.operation_no > operation.operation_no)]
            if following and engine.setup.enforce_operation_sequence:
                nxt = following[0]
                if nxt.output_qty + nxt.scrap_qty > operation.output_qty - entry.quantity:
                    raise ManufacturingError(f'Operation {nxt.operation_no} has already reported output from this quantity - reverse it first.')
            operation.output_qty -= entry.quantity
            operation.scrap_qty -= entry.scrap_qty
            if operation.status in DONE_OPERATION + ('QC_PENDING',):
                operation.status = 'STARTED'
                operation.actual_end = None
            operation.save()
        record = OutputEntry.objects.create(
            entry_no=engine._no(), order=order, order_line=line, operation=operation, item=entry.item, sku=entry.sku, variant=entry.variant,
            quantity=-entry.quantity, scrap_qty=-entry.scrap_qty, uom=entry.uom, gross_weight=-entry.gross_weight, net_weight=-entry.net_weight,
            stone_weight=-entry.stone_weight, location=entry.location, bin=entry.bin, lot_no=entry.lot_no, into_qc=entry.into_qc,
            is_final=entry.is_final, is_rework=entry.is_rework, operator=engine.user, unit_cost=entry.unit_cost, cost_amount=-entry.cost_amount,
            user=engine.user, reversal_of=entry, **engine._ledger_fields())
        engine.records['output'].append(record)

    @staticmethod
    def runtime(engine, entry):
        operation = ProductionOrderRoutingLine.objects.select_for_update().get(pk=entry.operation_id)
        fields = {f: -getattr(entry, f) for f in ('setup_minutes', 'run_minutes', 'wait_minutes', 'move_minutes', 'queue_minutes',
                                                  'downtime_minutes', 'break_minutes', 'total_minutes', 'labour_cost', 'machine_cost',
                                                  'overhead_cost', 'cost_amount')}
        record = RuntimeEntry.objects.create(
            entry_no=engine._no(), order=entry.order, operation=operation, work_center=entry.work_center, machine_center=entry.machine_center,
            resource=entry.resource, employee=entry.employee, start_time=entry.start_time, end_time=entry.end_time,
            downtime_reason=entry.downtime_reason, labour_rate=entry.labour_rate, machine_rate=entry.machine_rate,
            overhead_rate=entry.overhead_rate, is_rework=entry.is_rework, user=engine.user, reversal_of=entry, **fields, **engine._ledger_fields())
        engine._apply_runtime(operation, entry, -1)
        operation.save()
        engine.records['runtime'].append(record)

    @staticmethod
    def scrap(engine, entry):
        order = engine.order
        if entry.recovery_entry_id:
            recovery = entry.recovery_entry
            line = order.lines.filter(line_type='BY_PRODUCT', item=entry.recovery_item).first()
            engine.inventory.issue(transaction_type='REVERSAL', item=entry.recovery_item, variant=recovery.variant, location=recovery.location,
                                   bin=recovery.bin, quantity=entry.weight, cost_amount=entry.cost_amount, reason_code='REVERSAL',
                                   reversal_of=recovery)
            if line is not None:
                line.output_qty -= entry.weight
                line.save(update_fields=['output_qty', 'updated_at'])
        if entry.operation_id:
            operation = entry.operation
            operation.scrap_qty -= entry.quantity
            operation.save(update_fields=['scrap_qty', 'updated_at'])
            last = final_operation(order)
            if last is not None and operation.pk == last.pk:
                order.scrap_qty -= entry.quantity
        else:
            order.scrap_qty -= entry.quantity
        record = ScrapEntry.objects.create(
            entry_no=engine._no(), order=order, operation=entry.operation, item=entry.item, scrap_type=entry.scrap_type, reason=entry.reason,
            quantity=-entry.quantity, weight=-entry.weight, recoverable=entry.recoverable, recovery_item=entry.recovery_item,
            cost_amount=-entry.cost_amount, operator=engine.user, is_rework=entry.is_rework, user=engine.user, reversal_of=entry,
            **engine._ledger_fields())
        engine.records['scrap'].append(record)


def reverse_qc(actor, qc, *, reason):
    """Reverse a posted final QC: put-away pieces go back to QC, scrapped pieces are restored to QC at their cost."""
    require(actor, 'reverse')
    if get_setup(actor.tenant).require_manager_approval:
        require(actor, 'approve_reversal')
    if not (reason or '').strip():
        raise ManufacturingError('A reason is required for every reversal.')
    with transaction.atomic():
        qc = ProductionQC.objects.select_for_update().get(pk=qc.pk, tenant=actor.tenant)
        if qc.status != 'POSTED':
            raise ManufacturingError(f'{qc.qc_no} is not posted.')
        order = lock_order(actor, qc.order_id)
        assert_executable(order)
        if qc.reworks.exclude(status__in=('OPEN', 'CANCELLED')).exists():
            raise ManufacturingError('Rework created from this QC has already started.')
        engine = ManufacturingPostingEngine(actor, order, kind='REVERSAL', reason=reason, reversal_of=qc.batch)
        if qc.stage in ('FINAL', 'JEWELLERY'):
            line = main_line(order)
            fg_location = order.finished_goods_location or order.location
            qc_location = order.qc_location or order.location
            if qc.unit_results:
                for row in qc.unit_results:
                    unit = JewelleryUnit.objects.get(pk=row['unit'], tenant=actor.tenant)
                    output_unit = OutputUnit.objects.get(order=order, jewellery_unit=unit)
                    if row['result'] == 'PASS':
                        if not unit.in_stock or unit.status != 'AVAILABLE':
                            raise ManufacturingError(f'{unit.barcode} is no longer available in finished goods (sold or moved).')
                        engine.inventory.move(transaction_type='REVERSAL', item=unit.item, variant=unit.variant, from_location=unit.current_location,
                                              to_location=qc_location, unit=unit, to_bin=order.qc_bin,
                                              to_sku=ensure_sku(actor, unit.item, unit.variant, qc_location, order.sku), into_state='QC')
                    elif row['result'] == 'FAIL':
                        engine.inventory.receive(transaction_type='REVERSAL', item=unit.item, variant=unit.variant, location=qc_location,
                                                 bin=order.qc_bin, unit=unit, unit_cost=unit.total_cost, into_state='QC', reason_code='REVERSAL')
                    output_unit.qc_status = 'PENDING'
                    output_unit.save(update_fields=['qc_status', 'updated_at'])
            else:
                if qc.passed_qty:
                    engine.inventory.move(transaction_type='REVERSAL', item=line.item, variant=line.variant, from_location=fg_location,
                                          to_location=qc_location, from_bin=order.finished_goods_bin, to_bin=order.qc_bin, quantity=qc.passed_qty,
                                          into_state='QC')
                if qc.failed_qty:
                    write_off = CostEntry.objects.filter(tenant=actor.tenant, order=order, source_type='ProductionQC', source_id=qc.pk,
                                                         cost_type='SCRAP_WRITE_OFF').aggregate(v=Sum('amount'))['v'] or ZERO
                    write_off = money(write_off)
                    engine.inventory.receive(transaction_type='REVERSAL', item=line.item, variant=line.variant, location=qc_location,
                                             bin=order.qc_bin, quantity=qc.failed_qty, unit_cost=(write_off / qc.failed_qty),
                                             into_state='QC', reason_code='REVERSAL')
            engine.reverse_costs_of(qc)
            order.accepted_qty -= qc.passed_qty
            order.rejected_qty -= qc.failed_qty
            order.qc_pending_qty += qc.passed_qty + qc.failed_qty
            line.accepted_qty -= qc.passed_qty
            line.rejected_qty -= qc.failed_qty
            line.save(update_fields=['accepted_qty', 'rejected_qty', 'updated_at'])
        elif qc.stage == 'OPERATION' and qc.operation_id:
            qc.operation.status = 'QC_PENDING'
            qc.operation.save(update_fields=['status', 'updated_at'])
        qc.reworks.filter(status='OPEN').update(status='CANCELLED')
        qc.status = 'REVERSED'
        qc.save(update_fields=['status', 'updated_at'])
        return engine.finalize(action='reverse_qc')
