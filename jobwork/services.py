"""Job work & subcontracting workflows.

Order -> approval -> reservation -> dispatch (delivery challan, e-way bill) -> delivery at the job worker ->
processing report (consumption, output, scrap, loss) -> return dispatch -> receipt at the principal -> QC ->
production operation output -> vendor invoice -> completion / closure.

Every stock movement goes through ``InventoryPostingEngine`` (production-linked consumption through
``ManufacturingPostingEngine``) and writes the job worker stock ledger in the same database transaction. Posted
documents are never edited or deleted: corrections are reversals.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

from inventory.engine import InsufficientStock, InventoryError, InventoryPostingEngine, compute_available
from inventory.models import InventoryReservation, JewelleryUnit, Location
from inventory.services import default_transit, estimated_unit_cost, find_unit, register_unit
from inventory.tenancy import require_location
from manufacturing.calc import fine_weight, q3

from . import compliance
from .models import (
    DeliveryChallan, DeliveryChallanLine, EWayBill, JobWorkCostEntry, JobWorkDispatch, JobWorkDispatchLine, JobWorker, JobWorkerAgreement,
    JobWorkerLocation, JobWorkerStockEntry, JobWorkException, JobWorkOrder, JobWorkOrderLine, JobWorkProcessLine, JobWorkProcessReport,
    JobWorkQC, JobWorkQCLine, JobWorkReceipt, JobWorkReceiptLine, WeightCapture,
)
from .pricing import charge, resolve_price
from .security import JobWorkError, audit, check_maker_checker, get_setup, has_role, next_number, require, required_role

ZERO = Decimal('0')
PRECIOUS = ('GOLD', 'SILVER', 'PLATINUM')
ABSORBED_LOSS = ('PROCESS', 'NON_RECOVERABLE', 'PRINCIPAL')
RETURNED_SCRAP = ('RETURNED', 'RECYCLED')


def D(value):
    return Decimal(str(value if value not in (None, '') else 0))


def money(value):
    return D(value).quantize(Decimal('0.01'))


def _get(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=getattr(pk, 'pk', pk)).first()
    if obj is None:
        raise JobWorkError(f'{model._meta.verbose_name.title()} not found.')
    return obj


def _lock(model, actor, obj):
    locked = model.objects.select_for_update().filter(tenant=actor.tenant, pk=getattr(obj, 'pk', obj)).first()
    if locked is None:
        raise JobWorkError(f'{model._meta.verbose_name.title()} not found.')
    return locked


def _check_tenant(actor, *objects):
    for obj in objects:
        if obj is not None and getattr(obj, 'tenant_id', actor.tenant.id) != actor.tenant.id:
            raise JobWorkError('Cross-tenant reference rejected.')


def is_metal(line):
    return line.metal in PRECIOUS and line.uom_id is not None and line.uom.code in ('GM', 'KG')


def fine_of(net, purity, metal):
    return fine_weight(net, purity) if metal in PRECIOUS else ZERO


def grams_per(line):
    return Decimal('1000') if line.uom_id and line.uom.code == 'KG' else Decimal('1')


def line_weights(line, qty, *, unit=None, gross=None, net=None, stone=None):
    """(gross, net, stone) for `qty` of an order line: the unit's weights, typed/scale weights, bulk metal grams, or pro-rata."""
    qty = D(qty)
    if unit is not None:
        return unit.gross_weight, unit.net_metal_weight, unit.stone_weight
    if gross not in (None, ''):
        gross = q3(gross)
        stone = q3(stone) if stone not in (None, '') else ZERO
        return gross, q3(net) if net not in (None, '') else max(gross - stone, ZERO), stone
    if is_metal(line):
        grams = q3(qty * grams_per(line))
        return grams, grams, ZERO
    if line.quantity and (line.gross_weight or line.net_weight):
        ratio = qty / line.quantity
        return q3(line.gross_weight * ratio), q3(line.net_weight * ratio), q3(line.stone_weight * ratio)
    return ZERO, ZERO, ZERO


def jw_balance(order=None, *, order_line=None, job_worker=None, unit=None, bulk=False):
    """What the job worker ledger holds. `bulk` restricts to non-serialized stock (unit rows excluded)."""
    qs = JobWorkerStockEntry.objects.all()
    if order is not None:
        qs = qs.filter(order=order)
    if order_line is not None:
        qs = qs.filter(order_line=order_line)
    if job_worker is not None:
        qs = qs.filter(job_worker=job_worker)
    if unit is not None:
        qs = qs.filter(jewellery_unit=unit)
    elif bulk:
        qs = qs.filter(jewellery_unit__isnull=True)
    agg = qs.aggregate(q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), f=Sum('fine_weight'), v=Sum('value'))
    # SQLite sums decimals in floating point: quantize every aggregate back to the column's precision
    return {'qty': q3(agg['q'] or 0), 'gross': q3(agg['g'] or 0), 'net': q3(agg['n'] or 0), 'fine': q3(agg['f'] or 0),
            'value': money(agg['v'] or 0)}


def _owner(line):
    return {'VENDOR': 'VENDOR', 'CUSTOMER': 'CUSTOMER'}.get(line.supply_method, 'PRINCIPAL')


def line_is_memo(line):
    """No company inventory behind the line: vendor/customer goods, and production WIP/output (it lives in production WIP)."""
    return line.is_memo or (line.order.is_production and line.line_type in ('WIP', 'OUTPUT', 'BY_PRODUCT'))


def _stock(actor, order, line, entry_type, *, qty, gross=ZERO, net=ZERO, stone=ZERO, value=ZERO, unit=None, location=None,
           document_type, document_no, dispatch=None, inventory_entry=None, loss_class='', posting_date=None, reversal_of=None):
    item = line.item
    purity = line.purity or (item.purity if item else '')
    metal = line.metal or (item.metal if item else '')
    return JobWorkerStockEntry.objects.create(
        tenant=actor.tenant, company=order.company, posting_date=posting_date or dj_timezone.localdate(),
        entry_no=next_number(actor.tenant, 'STOCK_ENTRY'), job_worker=order.job_worker, location=location or order.job_worker_location,
        order=order, order_line=line, entry_type=entry_type, document_type=document_type, document_no=document_no, item=item,
        variant=line.variant, jewellery_unit=unit, lot_no=line.lot_no or (unit.lot_no if unit else ''), huid=(unit.huid or '') if unit else '',
        metal=metal, purity=purity, quantity=q3(qty), gross_weight=q3(gross), net_weight=q3(net), fine_weight=fine_of(net, purity, metal),
        stone_weight=q3(stone), value=money(value), owner=_owner(line), custodian=order.job_worker.legal_name, memo=line_is_memo(line),
        loss_class=loss_class, dispatch=dispatch, inventory_entry=inventory_entry, user=actor.user, created_by=actor.user,
        reversal_of=reversal_of,
    )


def _cost(actor, order, cost_type, amount, *, line=None, document_type='', document_no='', description='', absorbed=True,
          reversal_of=None):
    amount = money(amount)
    if amount == 0:
        return None
    return JobWorkCostEntry.objects.create(
        tenant=actor.tenant, company=order.company, posting_date=dj_timezone.localdate(), entry_no=next_number(actor.tenant, 'COST_ENTRY'),
        order=order, cost_type=cost_type, amount=amount, order_line=line, document_type=document_type, document_no=document_no,
        description=description[:200], absorbed=absorbed, reversal_of=reversal_of, created_by=actor.user)


def material_wip(order):
    return money(JobWorkCostEntry.objects.filter(order=order, absorbed=True).aggregate(v=Sum('amount'))['v'] or 0)


def raise_exception(actor, *, exception_type, description, order=None, order_line=None, job_worker=None, blocking=True, severity='HIGH',
                    expected=0, actual=0, weight=0, value=0, dedupe_key='', metric=None, metric_value=None, document_type='',
                    document_no='', loss_class='', required=''):
    """Create (or refresh the open duplicate of) a job work exception. Approval role comes from the approval matrix."""
    job_worker = job_worker or (order.job_worker if order else None)
    role = required or (required_role(actor.tenant, metric, metric_value if metric_value is not None else value) if metric else '') \
        or required_role(actor.tenant, 'EXCEPTION', value)
    fields = dict(description=description[:300], expected_value=D(expected), actual_value=D(actual), variance=D(actual) - D(expected),
                  weight=q3(weight), value=money(value), blocking=blocking, severity=severity, required_role=role, loss_class=loss_class)
    if dedupe_key:
        existing = JobWorkException.objects.filter(tenant=actor.tenant, dedupe_key=dedupe_key, status__in=('OPEN', 'UNDER_REVIEW')).first()
        if existing is not None:
            for key, val in fields.items():
                setattr(existing, key, val)
            existing.save()
            return existing
    exc = JobWorkException.objects.create(
        tenant=actor.tenant, company=order.company if order else None, exception_no=next_number(actor.tenant, 'LOSS_APPROVAL'),
        exception_type=exception_type, order=order, order_line=order_line, job_worker=job_worker, document_type=document_type,
        document_no=document_no, dedupe_key=dedupe_key, raised_by=actor.user, created_by=actor.user,
        history=[{'at': dj_timezone.now().isoformat(), 'by': actor.user.pk if actor.user else None, 'action': 'raised'}], **fields)
    audit(actor, 'raise', 'JOB_WORK_EXCEPTION', exc.exception_no, order=order, new={'type': exception_type, 'value': value, 'weight': weight})
    return exc


def blocking_exceptions(order):
    return order.exceptions.filter(blocking=True, status__in=JobWorkException.OPEN_STATUSES)


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

@transaction.atomic
def create_job_worker(actor, *, code, legal_name, location_code=None, address_1='', city='', state='', pin='', supplier=None,
                      subcontractor=None, **fields):
    """A job worker plus its own controlled inventory location (material there stays the principal's)."""
    require(actor, 'masters')
    _check_tenant(actor, subcontractor)
    setup = get_setup(actor.tenant)
    worker = JobWorker(tenant=actor.tenant, company=setup.company, code=code.strip().upper(), legal_name=legal_name, state=state,
                       supplier=supplier, subcontractor=subcontractor, created_by=actor.user, **fields)
    try:
        worker.clean()
    except ValidationError as exc:
        raise JobWorkError('; '.join(exc.messages))
    if JobWorker.objects.filter(tenant=actor.tenant, code=worker.code).exists():
        raise JobWorkError(f'Job worker {worker.code} already exists.')
    worker.save()
    location_code = (location_code or f'JW-{worker.code}')[:30]
    if Location.objects.filter(tenant=actor.tenant, code=location_code).exists():
        raise JobWorkError(f'Location {location_code} already exists.')
    location = Location.objects.create(
        tenant=actor.tenant, company=setup.company, code=location_code, name=f'{worker.trade_name or legal_name} (job worker)'[:200],
        location_type='JOB_WORKER', address_1=address_1, city=city, state=state, pin=pin, gstin=worker.gstin,
        contact_person=worker.contact_person, phone=worker.phone, email=worker.email, allow_sales=False, allow_pos=False,
        allow_adjustment=False, allow_transfer_in=False, allow_transfer_out=False, allow_reservation=False, allow_purchase=True,
        created_by=actor.user)
    JobWorkerLocation.objects.create(tenant=actor.tenant, company=setup.company, job_worker=worker, location=location,
                                     state_code=worker.state_code, created_by=actor.user)
    audit(actor, 'create', 'JOB_WORKER', worker.code, new={'location': location.code, 'gstin': worker.gstin})
    return worker


def _loss_terms(actor, job_worker, on_date):
    agreement = JobWorkerAgreement.objects.filter(tenant=actor.tenant, job_worker=job_worker, status='APPROVED', effective_from__lte=on_date) \
        .filter(Q(expires_on__isnull=True) | Q(expires_on__gte=on_date)).order_by('-effective_from').first()
    if agreement is not None and (agreement.expected_loss_percent or agreement.max_loss_percent):
        return agreement.expected_loss_percent, agreement.max_loss_percent
    return job_worker.default_loss_percent, job_worker.max_loss_percent


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _build_line(actor, order, number, spec):
    item, unit = spec.get('item'), spec.get('jewellery_unit') or spec.get('unit')
    if unit is not None:
        item = unit.item
    _check_tenant(actor, item, unit, spec.get('variant'), spec.get('sku'))
    line_type = spec.get('line_type', 'INPUT')
    if item is None and line_type != 'SERVICE':
        raise JobWorkError(f'Line {number}: an item is required.')
    qty = Decimal('1') if unit is not None else q3(spec.get('quantity') or 0)
    if qty < 0 or (qty == 0 and line_type in JobWorkOrderLine.INBOUND_TYPES + ('OUTPUT',)):
        raise JobWorkError(f'Line {number}: quantity must be greater than zero.')
    line = JobWorkOrderLine(
        tenant=actor.tenant, company=order.company, order=order, line_no=number, line_type=line_type,
        supply_method=spec.get('supply_method', 'PRINCIPAL'), item=item, variant=spec.get('variant'), sku=spec.get('sku'),
        jewellery_unit=unit, production_component=spec.get('production_component'), routing_link_code=spec.get('routing_link_code', ''),
        description=spec.get('description') or (item.description if item else ''), uom=spec.get('uom') or (item.base_uom if item else None),
        hsn_code=spec.get('hsn_code') or (item.hsn_code if item else ''), metal=spec.get('metal') or (unit.metal if unit else (item.metal if item else '')),
        purity=spec.get('purity') or (unit.purity if unit else (item.purity if item else '')), lot_no=spec.get('lot_no', ''),
        certificate_no=spec.get('certificate_no') or (unit.certificate_no if unit else ''), quantity=qty, created_by=actor.user,
    )
    gross, net, stone = line_weights(line, qty, unit=unit, gross=spec.get('gross_weight'), net=spec.get('net_weight'),
                                     stone=spec.get('stone_weight'))
    line.gross_weight, line.net_weight, line.stone_weight = gross, net, stone
    if line.line_type in JobWorkOrderLine.INBOUND_TYPES and not line.is_memo and item is not None:
        cost = unit.total_cost if unit is not None else estimated_unit_cost(actor.tenant, item, line.variant, order.source_location, line.sku)
        line.unit_value = D(spec.get('unit_value')) if spec.get('unit_value') not in (None, '') else D(cost)
    else:
        line.unit_value = D(spec.get('unit_value'))
    if order.expected_loss_percent and is_metal(line) and line.is_inbound:
        line.expected_loss_weight = q3(net * order.expected_loss_percent / 100)
    line.save()
    return line


def _price_order(actor, order, *, grams=ZERO, carats=ZERO, hours=ZERO, rate=None, rate_basis=None, minimum_charge=None):
    output = order.lines.filter(line_type='OUTPUT').first()
    item = output.item if output else None
    price = resolve_price(actor.tenant, order.job_worker, operation_code=order.operation_code, item=item, work_center=order.work_center,
                          on_date=order.order_date, quantity=order.planned_quantity)
    if rate is not None:
        order.rate_basis, order.rate = rate_basis or order.rate_basis, D(rate)
        order.minimum_charge = D(minimum_charge)
    elif price is not None:
        order.price, order.rate_basis, order.rate, order.minimum_charge = price, price.rate_basis, price.rate, price.minimum_amount
    order.expected_charge = charge(order.rate_basis, order.rate, order.minimum_charge, pieces=order.planned_quantity, grams=grams,
                                   carats=carats, hours=hours, base_value=order.material_value) if order.rate else ZERO


@transaction.atomic
def create_order(actor, *, job_worker, lines, order_kind='JOB_WORK', transaction_type='JOB_WORK', source_location=None, source_bin=None,
                 return_location=None, return_bin=None, operation_code='', work_center=None, planned_quantity=None, required_date=None,
                 expected_return_date=None, goods_category='INPUTS', service_sac='', description='', priority='NORMAL',
                 selection_mode='MANUAL', source_type='MANUAL', source_no='', sales_order=None, parent=None, rework_of=None,
                 production_order=None, operation=None, job_worker_location=None, expected_loss_percent=None, max_loss_percent=None,
                 qc_required=None, rate=None, rate_basis=None, minimum_charge=None, hours=0, remarks='', check_permission=True):
    if check_permission:
        require(actor, 'create')
    _check_tenant(actor, job_worker, source_location, return_location, work_center, parent, rework_of, production_order, sales_order)
    setup = get_setup(actor.tenant)
    if not setup.enabled:
        raise JobWorkError('Job work is disabled in job work setup.')
    if not job_worker.usable:
        raise JobWorkError(f'Job worker {job_worker.code} is inactive, blocked or not eligible for job work.')
    if job_worker.compliance_status == 'NON_COMPLIANT':
        raise JobWorkError(f'Job worker {job_worker.code} is marked GST non-compliant.')
    source_location = source_location or setup.default_source_location
    if source_location is None:
        raise JobWorkError('Select the location the material is dispatched from.')
    jw_location = job_worker_location or job_worker.default_location
    if jw_location is None:
        raise JobWorkError(f'Job worker {job_worker.code} has no job worker location.')
    if not any(spec.get('line_type', 'INPUT') in JobWorkOrderLine.INBOUND_TYPES for spec in lines):
        raise JobWorkError('A job work order needs at least one input / WIP / component line.')
    expected, maximum = _loss_terms(actor, job_worker, dj_timezone.localdate())
    today = dj_timezone.localdate()
    order = JobWorkOrder.objects.create(
        tenant=actor.tenant, company=setup.company, order_no=next_number(actor.tenant, 'SUBCONTRACT_ORDER' if order_kind == 'SUBCONTRACT' else 'JOB_WORK_ORDER'),
        order_kind=order_kind, transaction_type=transaction_type, source_type=source_type, source_no=source_no, sales_order=sales_order,
        production_order=production_order, operation=operation, parent=parent, rework_of=rework_of, job_worker=job_worker,
        work_center=work_center, job_worker_location=jw_location, source_location=source_location, source_bin=source_bin,
        return_location=return_location or setup.default_return_location or source_location,
        return_bin=return_bin if return_location else (setup.default_return_bin if setup.default_return_location else None),
        operation_code=operation_code, description=description, goods_category=goods_category,
        service_sac=service_sac or job_worker.default_sac, priority=priority, selection_mode=selection_mode, required_date=required_date,
        expected_return_date=expected_return_date or today + timedelta(days=job_worker.lead_time_days),
        expected_loss_percent=D(expected_loss_percent) if expected_loss_percent is not None else expected,
        max_loss_percent=D(max_loss_percent) if max_loss_percent is not None else maximum,
        qc_required=setup.require_qc_on_receipt if qc_required is None else qc_required, remarks=remarks, created_by=actor.user,
    )
    for number, spec in enumerate(lines, 1):
        _build_line(actor, order, number, spec)
    inbound = order.lines.filter(line_type__in=JobWorkOrderLine.INBOUND_TYPES)
    order.material_value = money(sum((l.quantity * l.unit_value for l in inbound if not l.is_memo), ZERO))
    outputs = order.lines.filter(line_type='OUTPUT')
    order.planned_quantity = q3(planned_quantity) if planned_quantity is not None else (
        sum((l.quantity for l in outputs), ZERO) or sum((l.quantity for l in inbound), ZERO))
    grams = sum((l.net_weight for l in (outputs if outputs.exists() else inbound) if l.metal in PRECIOUS), ZERO)
    carats = sum((l.quantity for l in inbound if l.uom_id and l.uom.code == 'CT'), ZERO)
    _price_order(actor, order, grams=grams, carats=carats, hours=hours, rate=rate, rate_basis=rate_basis, minimum_charge=minimum_charge)
    order.save()
    audit(actor, 'create', 'JOB_WORK_ORDER', order.order_no, order=order,
          new={'job_worker': job_worker.code, 'kind': order_kind, 'lines': len(lines), 'expected_charge': order.expected_charge})
    return order


@transaction.atomic
def create_from_production_operation(actor, operation, *, job_worker=None, quantity=None, expected_return_date=None, selection_mode=None):
    """Business Central 'create subcontracting order' from a released production order routing line.

    Components with the operation's routing link code are the material sent; the parent item in process is WIP."""
    from manufacturing import engine as mfg_engine
    from manufacturing.models import ProductionOrderRoutingLine
    operation = ProductionOrderRoutingLine.objects.select_related('order', 'work_center', 'subcontractor').get(pk=operation.pk, tenant=actor.tenant)
    order = operation.order
    if not operation.subcontracting:
        raise JobWorkError(f'Operation {operation.operation_no} is not a subcontract operation.')
    mfg_engine.assert_executable(order)
    mode = selection_mode or 'MANUAL'
    if job_worker is None:
        job_worker = JobWorker.objects.filter(tenant=actor.tenant, subcontractor=operation.subcontractor, active=True).first() \
            if operation.subcontractor_id else None
        mode = selection_mode or 'FIXED'
        if job_worker is None:
            raise JobWorkError(f'No job worker is linked to operation {operation.operation_no}. Select one.')
    open_existing = JobWorkOrder.objects.filter(tenant=actor.tenant, operation=operation).exclude(status__in=('CANCELLED', 'CLOSED')) \
        .exclude(rework_of__isnull=False)
    if open_existing.exists():
        raise JobWorkError(f'Operation {operation.operation_no} already has subcontract order {open_existing.first().order_no}.')
    qty = q3(quantity or order.planned_qty)
    lines = []
    link = operation.routing_link_code
    if link:
        for component in order.components.filter(routing_link_code=link).select_related('item', 'uom'):
            send = component.issued_open_qty
            lines.append({'line_type': 'COMPONENT', 'item': component.item, 'variant': component.variant, 'quantity': send or component.expected_qty,
                          'uom': component.uom, 'production_component': component, 'routing_link_code': link, 'purity': component.purity,
                          'metal': component.metal, 'supply_method': 'VENDOR' if component.supply_method == 'SUBCONTRACT' else
                          ('CUSTOMER' if component.supply_method == 'CUSTOMER' else 'PRINCIPAL')})
    previous = mfg_engine.previous_operation(operation)
    if previous is not None or not lines:
        weight = order.expected_net_weight * qty if order.expected_net_weight else None
        lines.append({'line_type': 'WIP', 'item': order.item, 'variant': order.variant, 'quantity': qty,
                      'gross_weight': weight, 'net_weight': weight, 'description': f'{order.item.description} in process (op {operation.operation_no})'})
    weight = order.expected_net_weight * qty if order.expected_net_weight else None
    lines.append({'line_type': 'OUTPUT', 'item': order.item, 'variant': order.variant, 'quantity': qty, 'gross_weight': weight,
                  'net_weight': weight, 'description': f'{order.item.description} after {operation.description}'})
    jwo = create_order(
        actor, job_worker=job_worker, lines=lines, order_kind='SUBCONTRACT', transaction_type='SUBCONTRACTING', source_type='PRODUCTION_ORDER',
        source_no=order.order_no, production_order=order, operation=operation, source_location=order.location, source_bin=order.production_bin,
        return_location=order.location, return_bin=order.production_bin, operation_code=link or operation.work_center.code,
        planned_quantity=qty, expected_return_date=expected_return_date, required_date=order.due_date, selection_mode=mode,
        description=f'{order.order_no} op {operation.operation_no} {operation.description}')
    return jwo


@transaction.atomic
def submit_order(actor, order):
    require(actor, 'create')
    order = _lock(JobWorkOrder, actor, order)
    if order.status != 'DRAFT':
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    setup = get_setup(actor.tenant)
    metal = sum((l.net_weight for l in order.lines.filter(line_type__in=JobWorkOrderLine.INBOUND_TYPES) if l.metal in PRECIOUS), ZERO)
    roles = [required_role(actor.tenant, 'ORDER_VALUE', order.expected_charge + order.material_value),
             required_role(actor.tenant, 'MATERIAL_VALUE', order.material_value), required_role(actor.tenant, 'METAL_WEIGHT', metal)]
    from .security import ROLE_RANK
    roles = [r for r in roles if r]
    order.required_approval_role = max(roles, key=lambda r: ROLE_RANK.get(r, 99)) if roles else ''
    order.submitted_by = actor.user
    order.status = 'PENDING_APPROVAL' if setup.require_order_approval else 'APPROVED'
    if order.status == 'APPROVED':
        order.approved_by, order.approved_at = actor.user, dj_timezone.now()
    order.save()
    audit(actor, 'submit', 'JOB_WORK_ORDER', order.order_no, order=order, new={'status': order.status, 'role': order.required_approval_role})
    return order


@transaction.atomic
def approve_order(actor, order):
    require(actor, 'approve')
    order = _lock(JobWorkOrder, actor, order)
    if order.status != 'PENDING_APPROVAL':
        raise JobWorkError(f'{order.order_no} is not waiting for approval.')
    check_maker_checker(actor, order.submitted_by or order.created_by)
    if not has_role(actor, order.required_approval_role):
        raise JobWorkError(f'{order.order_no} needs approval by {order.required_approval_role.replace("_", " ").lower()}.')
    worker = order.job_worker
    if worker.metal_capacity_grams:
        from .pricing import open_load
        _, grams = open_load(worker)
        metal = sum((l.net_weight for l in order.lines.all() if l.is_inbound and l.metal in PRECIOUS), ZERO)
        if grams + metal > worker.metal_capacity_grams:
            raise_exception(actor, exception_type='CAPACITY', order=order, blocking=False, severity='MEDIUM',
                            description=f'{worker.code} would hold {grams + metal} g against a metal capacity of {worker.metal_capacity_grams} g.',
                            expected=worker.metal_capacity_grams, actual=grams + metal, dedupe_key=f'CAPACITY:{order.pk}')
    order.status, order.approved_by, order.approved_at = 'APPROVED', actor.user, dj_timezone.now()
    order.save()
    audit(actor, 'approve', 'JOB_WORK_ORDER', order.order_no, order=order)
    return order


@transaction.atomic
def reject_order(actor, order, *, reason):
    require(actor, 'approve')
    order = _lock(JobWorkOrder, actor, order)
    if order.status != 'PENDING_APPROVAL':
        raise JobWorkError(f'{order.order_no} is not waiting for approval.')
    order.status = 'DRAFT'
    order.save()
    audit(actor, 'reject', 'JOB_WORK_ORDER', order.order_no, order=order, reason=reason)
    return order


def _order_reservations(order, line=None):
    qs = InventoryReservation.objects.filter(tenant=order.tenant, source_type='JOB_WORK_ORDER', source_no=order.order_no, status='ACTIVE')
    return qs.filter(source_line_no=line.line_no) if line is not None else qs


@transaction.atomic
def reserve_material(actor, order):
    """Reserve principal stock for every unsent input so the same material cannot go to two job workers."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    if order.status not in ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH'):
        raise JobWorkError(f'{order.order_no} must be approved before material is reserved.')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='JOB_WORK_RESERVATION', document_no=order.order_no)
    short = []
    for line in order.lines.filter(line_type__in=JobWorkOrderLine.INBOUND_TYPES).select_related('item', 'jewellery_unit', 'uom'):
        if line_is_memo(line) or order.is_production or line.supply_method == 'DIRECT_PURCHASE':
            continue  # production material is already issued to the order; memo goods are not ours
        need = line.quantity - line.dispatched_qty - q3(_order_reservations(order, line).aggregate(q=Sum('open_quantity'))['q'] or 0)
        if need <= 0:
            continue
        if line.jewellery_unit_id:
            engine.reserve(item=line.item, location=order.source_location, quantity=1, unit=line.jewellery_unit, source_type='JOB_WORK_ORDER',
                           source_no=order.order_no, source_line_no=line.line_no)
            continue
        try:
            engine.reserve(item=line.item, variant=line.variant, location=order.source_location, bin=order.source_bin, quantity=need,
                           source_type='JOB_WORK_ORDER', source_no=order.order_no, source_line_no=line.line_no)
        except InsufficientStock:
            free = _free_qty(actor, line, order)
            if free > 0:
                engine.reserve(item=line.item, variant=line.variant, location=order.source_location, bin=order.source_bin, quantity=free,
                               source_type='JOB_WORK_ORDER', source_no=order.order_no, source_line_no=line.line_no)
            short.append(f'{line.item.item_no} short {need - free}')
    for line in order.lines.all():
        reserved = q3(_order_reservations(order, line).aggregate(q=Sum('open_quantity'))['q'] or 0)
        if reserved != line.reserved_qty:
            line.reserved_qty = reserved
            line.save(update_fields=['reserved_qty', 'updated_at'])
    order.status = 'MATERIAL_PENDING' if short else 'READY_TO_DISPATCH'
    order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'reserve', 'JOB_WORK_ORDER', order.order_no, order=order, new={'short': '; '.join(short)})
    return order, short


def _free_qty(actor, line, order):
    from inventory.models import InventoryBalance
    balances = InventoryBalance.objects.filter(tenant=actor.tenant, item=line.item, variant=line.variant, location=order.source_location,
                                               jewellery_unit__isnull=True)
    if order.source_bin_id:
        balances = balances.filter(bin=order.source_bin)
    return sum((max(compute_available(b), ZERO) for b in balances), ZERO)


def release_reservations(actor, order):
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='JOB_WORK_RESERVATION', document_no=order.order_no)
    for reservation in _order_reservations(order):
        engine.release(reservation)
    order.lines.update(reserved_qty=ZERO)


@transaction.atomic
def cancel_order(actor, order, *, reason):
    require(actor, 'approve')
    order = _lock(JobWorkOrder, actor, order)
    if order.status in JobWorkOrder.TERMINAL_STATUSES or order.status == 'COMPLETED':
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    if order.dispatches.exclude(status__in=('DRAFT', 'REVERSED', 'CANCELLED')).exists():
        raise JobWorkError('Material has been dispatched: reverse the dispatch or complete the order instead of cancelling.')
    release_reservations(actor, order)
    order.dispatches.filter(status='DRAFT').update(status='CANCELLED')
    order.status, order.cancelled_reason = 'CANCELLED', reason[:250]
    order.save()
    audit(actor, 'cancel', 'JOB_WORK_ORDER', order.order_no, order=order, reason=reason)
    return order


# ---------------------------------------------------------------------------
# Dispatch (principal -> JW, JW -> JW, JW -> principal), delivery challan, e-way bill
# ---------------------------------------------------------------------------

DISPATCHABLE = ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH', 'DISPATCH_CREATED', 'IN_TRANSIT', 'RECEIVED_BY_JOB_WORKER', 'PROCESSING',
                'PARTIALLY_COMPLETED', 'READY_FOR_RETURN', 'RETURN_IN_TRANSIT', 'PARTIALLY_RETURNED', 'RECEIVED', 'QC_PENDING', 'QC_PASSED',
                'QC_FAILED', 'REWORK')


def _scale_weight(actor, spec, line, setup):
    capture = spec.get('weight_capture')
    if capture is not None:
        capture = WeightCapture.objects.select_for_update().get(pk=getattr(capture, 'pk', capture), tenant=actor.tenant)
        if capture.used:
            raise JobWorkError(f'Scale reading {capture.pk} has already been used.')
        capture.used = True
        capture.save(update_fields=['used', 'updated_at'])
        return capture, capture.weight
    if setup.require_scale_weight and line.item and not line.item.serial_tracking and (is_metal(line) or spec.get('gross_weight') not in (None, '')):
        raise JobWorkError(f'{line.item.item_no}: weights must be captured from the weighing scale.')
    return None, spec.get('gross_weight')


def _dispatch_line(actor, dispatch, number, order_line, qty, *, unit=None, gross=None, net=None, stone=None, source_line=None, capture=None):
    line_for_weights = source_line or order_line
    gross, net, stone = line_weights(line_for_weights, qty, unit=unit, gross=gross, net=net, stone=stone)
    purity = (unit.purity if unit else '') or order_line.purity
    metal = (unit.metal if unit else '') or order_line.metal
    return JobWorkDispatchLine.objects.create(
        tenant=actor.tenant, company=dispatch.company, dispatch=dispatch, line_no=number, order_line=order_line, source_line=source_line,
        item=order_line.item, variant=order_line.variant, jewellery_unit=unit, lot_no=order_line.lot_no or (unit.lot_no if unit else ''),
        huid=(unit.huid or '') if unit else '', barcode=unit.barcode if unit else '', hsn_code=order_line.hsn_code or order_line.item.hsn_code,
        memo=line_is_memo(order_line), owner=_owner(order_line), quantity=q3(qty), uom=order_line.uom, gross_weight=gross, net_weight=net,
        fine_weight=fine_of(net, purity, metal), stone_weight=stone, metal=metal, purity=purity,
        value=money(q3(qty) * order_line.unit_value) if not line_is_memo(order_line) else ZERO, weight_capture=capture, created_by=actor.user)


def _resolve_units(actor, order, barcodes, lines):
    """Scanned barcodes / HUIDs / serials -> (order line, unit) pairs."""
    pairs = []
    for code in barcodes or []:
        unit = find_unit(actor.tenant, code)
        if unit is None:
            raise JobWorkError(f'No jewellery unit matches {code}.')
        line = next((l for l in lines if l.jewellery_unit_id == unit.pk), None) or \
            next((l for l in lines if l.item_id == unit.item_id and l.jewellery_unit_id is None), None)
        if line is None:
            raise JobWorkError(f'Unit {unit.barcode} ({unit.item.item_no}) is not on {order.order_no}.')
        pairs.append((line, unit))
    return pairs


def _new_dispatch(actor, order, movement_type, *, from_location, to_location, from_bin=None, from_job_worker=None, to_job_worker=None,
                  source_order=None, vehicle_no='', transporter='', transporter_id='', transport_mode='ROAD', distance_km=0,
                  expected_return_date=None, remarks=''):
    setup = get_setup(actor.tenant)
    transit = setup.transit_location or default_transit(actor.tenant)
    if transit is None:
        raise JobWorkError('No in-transit location is configured.')
    return JobWorkDispatch.objects.create(
        tenant=actor.tenant, company=order.company, dispatch_no=next_number(actor.tenant, 'WIP_TRANSFER' if movement_type == 'JW_TO_JW' else 'JOB_WORK_DISPATCH'),
        movement_type=movement_type, order=order, source_order=source_order, from_job_worker=from_job_worker, to_job_worker=to_job_worker,
        from_location=from_location, from_bin=from_bin, to_location=to_location, transit_location=transit,
        expected_return_date=expected_return_date or order.expected_return_date, vehicle_no=vehicle_no, transporter=transporter,
        transporter_id=transporter_id, transport_mode=transport_mode, distance_km=distance_km or 0, remarks=remarks, created_by=actor.user)


def _totals(dispatch):
    agg = dispatch.lines.aggregate(q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), f=Sum('fine_weight'), v=Sum('value'))
    dispatch.total_qty, dispatch.total_gross, dispatch.total_net = q3(agg['q'] or 0), q3(agg['g'] or 0), q3(agg['n'] or 0)
    dispatch.total_fine, dispatch.total_value = q3(agg['f'] or 0), money(agg['v'] or 0)


@transaction.atomic
def create_dispatch(actor, order, *, lines=None, barcodes=None, **transport):
    """Draft material dispatch principal -> job worker. `lines`: [{order_line, quantity, gross_weight, net_weight, weight_capture}].
    Defaults to everything still to be sent; `barcodes` adds scanned jewellery units."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    if order.status not in DISPATCHABLE:
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}; approve it before dispatching.')
    setup = get_setup(actor.tenant)
    # vendor-supplied material never leaves the principal; direct purchases arrive at the job worker from the supplier
    inbound = list(order.lines.filter(line_type__in=JobWorkOrderLine.INBOUND_TYPES).exclude(supply_method__in=('DIRECT_PURCHASE', 'VENDOR'))
                   .select_related('item', 'uom', 'jewellery_unit'))
    dispatch = _new_dispatch(actor, order, 'PRINCIPAL_TO_JW', from_location=order.source_location, from_bin=order.source_bin,
                             to_location=order.job_worker_location, to_job_worker=order.job_worker, **transport)
    number = 0
    pairs = _resolve_units(actor, order, barcodes, inbound)
    if lines is None and not pairs:
        lines = [{'order_line': l, 'quantity': l.to_dispatch_qty} for l in inbound if l.to_dispatch_qty > 0]
    for line, unit in pairs:
        if line.dispatched_qty + 1 > line.quantity:
            raise JobWorkError(f'{line.item.item_no}: all {line.quantity} already dispatched.')
        number += 1
        _dispatch_line(actor, dispatch, number, line, 1, unit=unit)
    for spec in lines or []:
        line = spec['order_line']
        if line.order_id != order.id or not line.is_inbound:
            raise JobWorkError('Dispatch lines must be input lines of this order.')
        if line.supply_method in ('DIRECT_PURCHASE', 'VENDOR'):
            raise JobWorkError(f'{line.item.item_no} is {line.get_supply_method_display().lower()} - it is not dispatched by the principal.')
        unit = spec.get('unit') or line.jewellery_unit
        qty = Decimal('1') if unit is not None else q3(spec.get('quantity') or 0)
        if qty <= 0:
            continue
        if qty > line.to_dispatch_qty:
            raise JobWorkError(f'{line.item.item_no}: only {line.to_dispatch_qty} still to dispatch.')
        if line.item.serial_tracking and unit is None and not line_is_memo(line):
            raise JobWorkError(f'{line.item.item_no} is serialized: scan the units to dispatch.')
        capture, gross = _scale_weight(actor, spec, line, setup)
        number += 1
        _dispatch_line(actor, dispatch, number, line, qty, unit=unit, gross=gross, net=spec.get('net_weight'), stone=spec.get('stone_weight'),
                       capture=capture)
    if number == 0:
        raise JobWorkError('Nothing to dispatch.')
    _totals(dispatch)
    dispatch.save()
    if order.status in ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH'):
        order.status = 'DISPATCH_CREATED'
        order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'create', 'JOB_WORK_DISPATCH', dispatch.dispatch_no, order=order, new={'lines': number, 'value': dispatch.total_value})
    return dispatch


@transaction.atomic
def create_return(actor, order, *, lines=None, barcodes=None, **transport):
    """Draft return dispatch job worker -> principal. Defaults to everything the ledger shows at the job worker for the order."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    if order.status in JobWorkOrder.TERMINAL_STATUSES or order.status in ('DRAFT', 'PENDING_APPROVAL', 'COMPLETED'):
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    all_lines = list(order.lines.select_related('item', 'uom'))
    dispatch = _new_dispatch(actor, order, 'JW_TO_PRINCIPAL', from_location=order.job_worker_location, to_location=order.return_location,
                             from_job_worker=order.job_worker, **transport)
    number = 0
    for line, unit in _resolve_units(actor, order, barcodes, all_lines):
        if jw_balance(order, order_line=line, unit=unit)['qty'] < 1:
            raise JobWorkError(f'Unit {unit.barcode} is not at {order.job_worker.code} for {order.order_no}.')
        number += 1
        _dispatch_line(actor, dispatch, number, line, 1, unit=unit)
    if lines is None and number == 0:
        lines = []
        for line in all_lines:
            units = JobWorkerStockEntry.objects.filter(order=order, order_line=line, jewellery_unit__isnull=False) \
                .values('jewellery_unit').annotate(q=Sum('quantity')).filter(q__gt=0)
            for row in units:
                lines.append({'order_line': line, 'unit': JewelleryUnit.objects.get(pk=row['jewellery_unit'])})
            bulk = JobWorkerStockEntry.objects.filter(order=order, order_line=line, jewellery_unit__isnull=True) \
                .aggregate(q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'))
            if q3(bulk['q'] or 0) > 0:
                lines.append({'order_line': line, 'quantity': q3(bulk['q']), 'gross_weight': q3(bulk['g'] or 0), 'net_weight': q3(bulk['n'] or 0)})
    for spec in lines or []:
        line, unit = spec['order_line'], spec.get('unit')
        if line.order_id != order.id:
            raise JobWorkError('Return lines must belong to this order.')
        qty = Decimal('1') if unit is not None else q3(spec.get('quantity') or 0)
        if qty <= 0:
            continue
        held = jw_balance(order, order_line=line, unit=unit, bulk=True)
        if qty > held['qty']:
            raise JobWorkError(f'{line.item.item_no}: only {held["qty"]} is at {order.job_worker.code}.')
        gross, net = spec.get('gross_weight'), spec.get('net_weight')
        if unit is None and gross in (None, '') and held['qty']:
            gross, net = q3(held['gross'] * qty / held['qty']), q3(held['net'] * qty / held['qty'])
        number += 1
        _dispatch_line(actor, dispatch, number, line, qty, unit=unit, gross=gross, net=net)
    if number == 0:
        raise JobWorkError(f'Nothing of {order.order_no} is held at the job worker.')
    for dl in dispatch.lines.select_related('order_line'):
        held_value = jw_balance(order, order_line=dl.order_line, unit=dl.jewellery_unit)
        dl.value = money(held_value['value'] * dl.quantity / held_value['qty']) if held_value['qty'] else ZERO
        dl.save(update_fields=['value', 'updated_at'])
    _totals(dispatch)
    dispatch.save()
    audit(actor, 'create', 'JOB_WORK_RETURN', dispatch.dispatch_no, order=order, new={'lines': number})
    return dispatch


@transaction.atomic
def create_wip_transfer(actor, source_order, target_order, *, lines=None, **transport):
    """Job worker A -> job worker B for the next stage. `lines`: [{source_line, target_line, quantity, unit}].
    Defaults: everything at A for the source order, matched to the target's inbound lines by item."""
    require(actor, 'execute')
    source_order, target_order = _lock(JobWorkOrder, actor, source_order), _lock(JobWorkOrder, actor, target_order)
    if target_order.parent_id != source_order.id:
        raise JobWorkError(f'{target_order.order_no} is not the next stage of {source_order.order_no}.')
    if target_order.status not in DISPATCHABLE:
        raise JobWorkError(f'{target_order.order_no} must be approved before material is transferred to it.')
    if target_order.job_worker_id == source_order.job_worker_id:
        raise JobWorkError('Source and target job workers are the same - no transfer is needed.')
    dispatch = _new_dispatch(actor, target_order, 'JW_TO_JW', from_location=source_order.job_worker_location,
                             to_location=target_order.job_worker_location, from_job_worker=source_order.job_worker,
                             to_job_worker=target_order.job_worker, source_order=source_order, **transport)
    targets = list(target_order.lines.filter(line_type__in=JobWorkOrderLine.INBOUND_TYPES).select_related('item', 'uom'))
    if lines is None:
        lines = []
        for line in source_order.lines.select_related('item'):
            held = jw_balance(source_order, order_line=line)
            if held['qty'] <= 0:
                continue
            target = next((t for t in targets if t.item_id == line.item_id), None)
            if target is None:
                raise JobWorkError(f'{target_order.order_no} has no input line for {line.item.item_no}.')
            units = JobWorkerStockEntry.objects.filter(order=source_order, order_line=line, jewellery_unit__isnull=False) \
                .values('jewellery_unit').annotate(q=Sum('quantity')).filter(q__gt=0)
            for row in units:
                lines.append({'source_line': line, 'target_line': target, 'unit': JewelleryUnit.objects.get(pk=row['jewellery_unit'])})
            bulk = JobWorkerStockEntry.objects.filter(order=source_order, order_line=line, jewellery_unit__isnull=True).aggregate(q=Sum('quantity'))
            if q3(bulk['q'] or 0) > 0:
                lines.append({'source_line': line, 'target_line': target, 'quantity': q3(bulk['q'])})
    number = 0
    for spec in lines:
        source_line, target_line, unit = spec['source_line'], spec['target_line'], spec.get('unit')
        if source_line.order_id != source_order.id or target_line.order_id != target_order.id or not target_line.is_inbound:
            raise JobWorkError('Transfer lines must map a source-order line to an input line of the target order.')
        if source_line.item_id != target_line.item_id:
            raise JobWorkError(f'{source_line.item.item_no} cannot arrive as {target_line.item.item_no}.')
        qty = Decimal('1') if unit is not None else q3(spec.get('quantity') or 0)
        held = jw_balance(source_order, order_line=source_line, unit=unit)
        if qty <= 0 or qty > held['qty']:
            raise JobWorkError(f'{source_line.item.item_no}: only {held["qty"]} is at {source_order.job_worker.code}.')
        gross = net = None
        if unit is None and held['qty']:
            gross, net = q3(held['gross'] * qty / held['qty']), q3(held['net'] * qty / held['qty'])
        number += 1
        dl = _dispatch_line(actor, dispatch, number, target_line, qty, unit=unit, gross=gross, net=net, source_line=source_line)
        dl.value = money(held['value'] * qty / held['qty']) if held['qty'] else ZERO
        dl.save(update_fields=['value', 'updated_at'])
    if number == 0:
        raise JobWorkError('Nothing to transfer.')
    _totals(dispatch)
    dispatch.save()
    audit(actor, 'create', 'WIP_TRANSFER', dispatch.dispatch_no, order=target_order, new={'from': source_order.order_no})
    return dispatch


def _address(location):
    return ', '.join(p for p in (location.address_1, location.address_2, location.area, location.city, location.state, location.pin) if p)


def _issue_challan(actor, dispatch):
    order, tenant = dispatch.order, actor.tenant
    company = compliance.principal_company(tenant)
    principal = {'gstin': compliance.principal_gstin(tenant), 'name': company.company_name if company else tenant.name,
                 'state': compliance.principal_state_code(tenant)}

    def party(job_worker, location):
        if job_worker is None:
            return {'gstin': principal['gstin'], 'name': principal['name'], 'address': _address(location), 'state': principal['state']}
        return {'gstin': job_worker.gstin, 'name': job_worker.legal_name, 'address': _address(location), 'state': job_worker.state_code}

    consignor = party(dispatch.from_job_worker, dispatch.from_location)
    consignee = party(dispatch.to_job_worker, dispatch.to_location)
    reason = {'PRINCIPAL_TO_JW': 'JOB_WORK', 'JW_TO_JW': 'JW_TO_JW', 'JW_TO_PRINCIPAL': 'JOB_WORK_RETURN'}[dispatch.movement_type]
    challan = DeliveryChallan.objects.create(
        tenant=tenant, company=order.company, challan_no=next_number(tenant, 'DELIVERY_CHALLAN'), dispatch=dispatch,
        challan_date=dispatch.dispatch_date, reason=reason, job_work_type=order.transaction_type, principal_gstin=principal['gstin'],
        principal_name=principal['name'], consignor_gstin=consignor['gstin'], consignor_name=consignor['name'],
        consignor_address=consignor['address'], consignor_state_code=consignor['state'], consignee_gstin=consignee['gstin'],
        consignee_name=consignee['name'], consignee_address=consignee['address'], consignee_state_code=consignee['state'],
        order_no=order.order_no, production_order_no=order.production_order.order_no if order.production_order_id else '',
        goods_category=order.goods_category, total_value=dispatch.total_value, return_due_date=dispatch.compliance_due_date,
        vehicle_no=dispatch.vehicle_no, transporter=dispatch.transporter, created_by=actor.user)
    for dl in dispatch.lines.select_related('item', 'uom', 'jewellery_unit'):
        DeliveryChallanLine.objects.create(
            tenant=tenant, company=order.company, challan=challan, dispatch_line=dl, line_no=dl.line_no, item_no=dl.item.item_no,
            description=dl.item.description, hsn_code=dl.hsn_code, quantity=dl.quantity, uom=dl.uom.code if dl.uom_id else '',
            gross_weight=dl.gross_weight, net_weight=dl.net_weight, fine_weight=dl.fine_weight, purity=dl.purity, value=dl.value,
            serial_no=dl.jewellery_unit.serial_no if dl.jewellery_unit_id else '', lot_no=dl.lot_no, huid=dl.huid, created_by=actor.user)
    return challan


def _inventory(actor, document_type, document_no):
    return InventoryPostingEngine(actor.tenant, actor.user, document_type=document_type, document_no=document_no)


@transaction.atomic
def post_dispatch(actor, dispatch):
    """Atomic: stock leaves for transit + delivery challan + e-way bill applicability + due date + ledgers + status + audit."""
    require(actor, 'execute')
    dispatch = _lock(JobWorkDispatch, actor, dispatch)
    if dispatch.status != 'DRAFT':
        raise JobWorkError(f'{dispatch.dispatch_no} is {dispatch.get_status_display().lower()}.')
    order = _lock(JobWorkOrder, actor, dispatch.order)
    worker = dispatch.to_job_worker or dispatch.from_job_worker
    if dispatch.movement_type != 'JW_TO_PRINCIPAL':
        if not order.job_worker.usable or order.job_worker.compliance_status == 'NON_COMPLIANT':
            raise JobWorkError(f'{order.job_worker.code} cannot receive material (inactive, blocked or non-compliant).')
        if order.status not in DISPATCHABLE:
            raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    if dispatch.movement_type == 'PRINCIPAL_TO_JW':
        require_location(actor.tenant, actor.user, dispatch.from_location, 'ship')
    source_order = dispatch.source_order
    engine = _inventory(actor, 'JOB_WORK_DISPATCH', dispatch.dispatch_no)
    dispatch.dispatch_date = dj_timezone.localdate()
    for dl in dispatch.lines.select_related('order_line__item', 'order_line__uom', 'source_line', 'jewellery_unit', 'item'):
        line = JobWorkOrderLine.objects.select_for_update().get(pk=dl.order_line_id)
        if dispatch.movement_type == 'PRINCIPAL_TO_JW' and dl.quantity > line.to_dispatch_qty:
            raise JobWorkError(f'{line.item.item_no}: only {line.to_dispatch_qty} still to dispatch.')
        value = dl.value
        if not dl.memo:
            if dispatch.movement_type == 'PRINCIPAL_TO_JW':
                value = _ship_from_principal(engine, order, line, dl, dispatch)
            else:
                outs, _ = engine.move(transaction_type='TRANSFER_SHIPMENT', item=dl.item, variant=dl.variant, from_location=dispatch.from_location,
                                      to_location=dispatch.transit_location, quantity=dl.quantity, unit=dl.jewellery_unit, consume_state='PICKED',
                                      line_no=dl.line_no, reason_code='JOB_WORK')
                value = -sum((e.cost_amount for e in outs), ZERO)
        if dl.value != money(value):
            dl.value = money(value)
            dl.save(update_fields=['value', 'updated_at'])
        if dispatch.movement_type == 'PRINCIPAL_TO_JW':
            line.dispatched_qty += dl.quantity
            line.dispatched_gross += dl.gross_weight
            line.dispatched_net += dl.net_weight
            line.dispatched_fine += dl.fine_weight
            line.dispatched_value += dl.value
            line.save()
        elif dispatch.movement_type == 'JW_TO_JW':
            source_line = JobWorkOrderLine.objects.select_for_update().get(pk=dl.source_line_id)
            source_line.transferred_qty += dl.quantity
            source_line.save(update_fields=['transferred_qty', 'updated_at'])
            _stock(actor, source_order, source_line, 'TRANSFER_OUT', qty=-dl.quantity, gross=-dl.gross_weight, net=-dl.net_weight,
                   stone=-dl.stone_weight, value=-dl.value, unit=dl.jewellery_unit, location=dispatch.from_location,
                   document_type='WIP_TRANSFER', document_no=dispatch.dispatch_no, dispatch=dispatch)
            line.dispatched_qty += dl.quantity
            line.dispatched_gross += dl.gross_weight
            line.dispatched_net += dl.net_weight
            line.dispatched_fine += dl.fine_weight
            line.dispatched_value += dl.value
            line.save()
        else:  # return to principal
            held = jw_balance(order, order_line=line, unit=dl.jewellery_unit)
            if dl.quantity > held['qty']:
                raise JobWorkError(f'{line.item.item_no}: only {held["qty"]} is at the job worker.')
            line.returned_qty += dl.quantity
            line.save(update_fields=['returned_qty', 'updated_at'])
            _stock(actor, order, line, 'RETURN_DISPATCHED', qty=-dl.quantity, gross=-dl.gross_weight, net=-dl.net_weight, stone=-dl.stone_weight,
                   value=-dl.value, unit=dl.jewellery_unit, document_type='JOB_WORK_RETURN', document_no=dispatch.dispatch_no, dispatch=dispatch)
    _totals(dispatch)
    # Section 143 due date runs from the principal's dispatch of the goods
    if dispatch.movement_type == 'PRINCIPAL_TO_JW':
        due, rule = compliance.return_due_date(actor.tenant, order.goods_category, dispatch.dispatch_date)
        dispatch.compliance_due_date = due
        if due is None:
            raise_exception(actor, exception_type='COMPLIANCE_RULE_MISSING', order=order, blocking=False, severity='HIGH',
                            description=f'No approved return-period rule for {order.get_goods_category_display().lower()} on '
                                        f'{dispatch.dispatch_date}: the statutory due date could not be set.',
                            dedupe_key=f'RULE:RETURN_PERIOD:{order.goods_category}', document_type='JOB_WORK_DISPATCH',
                            document_no=dispatch.dispatch_no)
        if order.first_dispatch_date is None:
            order.first_dispatch_date = dispatch.dispatch_date
            order.compliance_due_date = due
    elif dispatch.movement_type == 'JW_TO_JW':
        dispatch.compliance_due_date = source_order.compliance_due_date
        order.first_dispatch_date = order.first_dispatch_date or source_order.first_dispatch_date
        order.compliance_due_date = order.compliance_due_date or source_order.compliance_due_date
    dispatch.status, dispatch.posted_by, dispatch.posted_at = 'IN_TRANSIT', actor.user, dj_timezone.now()
    dispatch.save()
    _issue_challan(actor, dispatch)
    required, reason, _ = compliance.eway_bill_requirement(actor.tenant, dispatch)
    dispatch.eway_bill_reason = reason[:250]
    if required is None:
        raise_exception(actor, exception_type='COMPLIANCE_RULE_MISSING', order=order, blocking=False,
                        description=f'{dispatch.dispatch_no}: {reason}', dedupe_key='RULE:EWAY_BILL', document_type='JOB_WORK_DISPATCH',
                        document_no=dispatch.dispatch_no)
    dispatch.eway_bill_required = bool(required)
    dispatch.save(update_fields=['eway_bill_required', 'eway_bill_reason', 'updated_at'])
    if required:
        EWayBill.objects.create(tenant=actor.tenant, company=order.company, dispatch=dispatch, document_no=dispatch.challan.challan_no,
                                document_date=dispatch.dispatch_date, vehicle_no=dispatch.vehicle_no, transporter=dispatch.transporter,
                                transporter_id=dispatch.transporter_id, request_payload=compliance.eway_payload(dispatch), created_by=actor.user)
    for touched in filter(None, (order, source_order)):
        refresh_status(touched)
    audit(actor, 'post', 'JOB_WORK_DISPATCH', dispatch.dispatch_no, order=order,
          new={'movement': dispatch.movement_type, 'challan': dispatch.challan.challan_no, 'value': dispatch.total_value,
               'net': dispatch.total_net, 'eway_bill_required': dispatch.eway_bill_required, 'due': dispatch.compliance_due_date})
    return dispatch


def _ship_from_principal(engine, order, line, dl, dispatch):
    """Principal stock -> transit. Production material leaves the production bin in PICKED state (it is issued to the order)."""
    if order.is_production:
        outs, _ = engine.move(transaction_type='TRANSFER_SHIPMENT', item=dl.item, variant=dl.variant, from_location=dispatch.from_location,
                              from_bin=dispatch.from_bin if dl.jewellery_unit is None else None, to_location=dispatch.transit_location,
                              quantity=dl.quantity, unit=dl.jewellery_unit, consume_state='PICKED', line_no=dl.line_no, reason_code='JOB_WORK')
        return -sum((e.cost_amount for e in outs), ZERO)
    remaining, value = dl.quantity, ZERO
    reservations = _order_reservations(order, line)
    if dl.jewellery_unit_id:
        reservations = reservations.filter(jewellery_unit=dl.jewellery_unit)
    for reservation in reservations:
        if remaining <= 0:
            break
        take = min(reservation.open_quantity, remaining)
        outs, _ = engine.move(transaction_type='TRANSFER_SHIPMENT', item=dl.item, variant=dl.variant, from_location=reservation.location,
                              to_location=dispatch.transit_location, quantity=take, unit=reservation.jewellery_unit, reservation=reservation,
                              line_no=dl.line_no, reason_code='JOB_WORK')
        value -= sum((e.cost_amount for e in outs), ZERO)
        remaining -= take
        line.reserved_qty = max(line.reserved_qty - take, ZERO)
    if remaining > 0:
        outs, _ = engine.move(transaction_type='TRANSFER_SHIPMENT', item=dl.item, variant=dl.variant, from_location=dispatch.from_location,
                              from_bin=dispatch.from_bin if dl.jewellery_unit is None else None, to_location=dispatch.transit_location,
                              quantity=remaining, unit=dl.jewellery_unit, line_no=dl.line_no, reason_code='JOB_WORK')
        value -= sum((e.cost_amount for e in outs), ZERO)
    return value


@transaction.atomic
def deliver_dispatch(actor, dispatch):
    """The job worker acknowledges receipt: transit -> job worker location (PICKED - committed to job work, never saleable)."""
    require(actor, 'execute')
    dispatch = _lock(JobWorkDispatch, actor, dispatch)
    if dispatch.movement_type == 'JW_TO_PRINCIPAL':
        raise JobWorkError('A return is received at the principal with a job work receipt.')
    if dispatch.status != 'IN_TRANSIT':
        raise JobWorkError(f'{dispatch.dispatch_no} is {dispatch.get_status_display().lower()}.')
    if dispatch.eway_bill_required and not dispatch.eway_bills.filter(status='GENERATED').exists():
        raise JobWorkError(f'{dispatch.dispatch_no} needs a generated e-way bill before the goods move.')
    order = _lock(JobWorkOrder, actor, dispatch.order)
    engine = _inventory(actor, 'JOB_WORK_DELIVERY', dispatch.dispatch_no)
    entry_type = 'TRANSFER_IN' if dispatch.movement_type == 'JW_TO_JW' else 'RECEIVED_BY_JW'
    for dl in dispatch.lines.select_related('order_line__item', 'jewellery_unit', 'item'):
        inv_entry, value = None, dl.value
        if not dl.memo:
            _, ins = engine.move(transaction_type='TRANSFER_RECEIPT', item=dl.item, variant=dl.variant, from_location=dispatch.transit_location,
                                 to_location=dispatch.to_location, quantity=dl.quantity, unit=dl.jewellery_unit, into_state='PICKED',
                                 allow_from_transit=True, line_no=dl.line_no, reason_code='JOB_WORK')
            inv_entry, value = ins[0], sum((e.cost_amount for e in ins), ZERO)
        _stock(actor, order, dl.order_line, entry_type, qty=dl.quantity, gross=dl.gross_weight, net=dl.net_weight, stone=dl.stone_weight,
               value=value, unit=dl.jewellery_unit, location=dispatch.to_location, document_type='JOB_WORK_DISPATCH',
               document_no=dispatch.dispatch_no, dispatch=dispatch, inventory_entry=inv_entry)
        dl.received_qty, dl.received_gross = dl.quantity, dl.gross_weight
        dl.save(update_fields=['received_qty', 'received_gross', 'updated_at'])
    dispatch.status, dispatch.delivered_by, dispatch.delivered_at = 'DELIVERED', actor.user, dj_timezone.now()
    dispatch.save()
    refresh_status(order)
    audit(actor, 'deliver', 'JOB_WORK_DISPATCH', dispatch.dispatch_no, order=order)
    return dispatch


@transaction.atomic
def reverse_dispatch(actor, dispatch, *, reason):
    """Undo a dispatch still in transit: stock returns to its source, the challan is cancelled, the e-way bill must be cancelled."""
    require(actor, 'reverse')
    dispatch = _lock(JobWorkDispatch, actor, dispatch)
    if dispatch.status != 'IN_TRANSIT':
        raise JobWorkError('Only a dispatch still in transit can be reversed; after delivery post a return instead.')
    if dispatch.eway_bills.filter(status='GENERATED').exists():
        raise JobWorkError('Cancel the e-way bill first.')
    order = _lock(JobWorkOrder, actor, dispatch.order)
    engine = _inventory(actor, 'JOB_WORK_DISPATCH_REVERSAL', dispatch.dispatch_no)
    for dl in dispatch.lines.select_related('order_line', 'jewellery_unit', 'item', 'source_line'):
        line = JobWorkOrderLine.objects.select_for_update().get(pk=dl.order_line_id)
        if not dl.memo:
            back_state = 'PICKED' if (dispatch.movement_type != 'PRINCIPAL_TO_JW' or order.is_production) else None
            engine.move(transaction_type='REVERSAL', item=dl.item, variant=dl.variant, from_location=dispatch.transit_location,
                        to_location=dispatch.from_location, to_bin=dispatch.from_bin if dl.jewellery_unit is None else None, quantity=dl.quantity,
                        unit=dl.jewellery_unit, into_state=back_state, allow_from_transit=True, line_no=dl.line_no, reason_code='DISPATCH_REVERSAL')
        if dispatch.movement_type == 'PRINCIPAL_TO_JW':
            line.dispatched_qty -= dl.quantity
            line.dispatched_gross -= dl.gross_weight
            line.dispatched_net -= dl.net_weight
            line.dispatched_fine -= dl.fine_weight
            line.dispatched_value -= dl.value
        elif dispatch.movement_type == 'JW_TO_JW':
            line.dispatched_qty -= dl.quantity
            line.dispatched_gross -= dl.gross_weight
            line.dispatched_net -= dl.net_weight
            line.dispatched_fine -= dl.fine_weight
            line.dispatched_value -= dl.value
            source_line = JobWorkOrderLine.objects.select_for_update().get(pk=dl.source_line_id)
            source_line.transferred_qty -= dl.quantity
            source_line.save(update_fields=['transferred_qty', 'updated_at'])
            _stock(actor, dispatch.source_order, source_line, 'REVERSAL', qty=dl.quantity, gross=dl.gross_weight, net=dl.net_weight,
                   stone=dl.stone_weight, value=dl.value, unit=dl.jewellery_unit, location=dispatch.from_location,
                   document_type='WIP_TRANSFER_REVERSAL', document_no=dispatch.dispatch_no, dispatch=dispatch)
        else:
            line.returned_qty -= dl.quantity
            _stock(actor, order, line, 'REVERSAL', qty=dl.quantity, gross=dl.gross_weight, net=dl.net_weight, stone=dl.stone_weight,
                   value=dl.value, unit=dl.jewellery_unit, location=dispatch.from_location, document_type='JOB_WORK_RETURN_REVERSAL',
                   document_no=dispatch.dispatch_no, dispatch=dispatch)
        line.save()
    DeliveryChallan.objects.filter(dispatch=dispatch).update(status='CANCELLED', cancellation_reason=reason[:250])
    dispatch.eway_bills.filter(status='PENDING').update(status='CANCELLED', cancel_reason=reason[:250])
    dispatch.status, dispatch.reversal_reason = 'REVERSED', reason[:250]
    dispatch.save()
    for touched in filter(None, (order, dispatch.source_order)):
        refresh_status(touched)
    audit(actor, 'reverse', 'JOB_WORK_DISPATCH', dispatch.dispatch_no, order=order, reason=reason)
    return dispatch


@transaction.atomic
def receive_direct_purchase(actor, order, order_line, *, quantity, unit_cost, gross_weight=None, reference=''):
    """Material bought by the principal and delivered straight to the job worker (principal-owned, at the job worker)."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    line = JobWorkOrderLine.objects.select_for_update().get(pk=order_line.pk, order=order)
    if line.supply_method != 'DIRECT_PURCHASE':
        raise JobWorkError(f'Line {line.line_no} is not a direct-purchase line.')
    if order.status not in DISPATCHABLE:
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    qty = q3(quantity)
    gross, net, stone = line_weights(line, qty, gross=gross_weight)
    engine = _inventory(actor, 'JOB_WORK_DIRECT_PURCHASE', reference or order.order_no)
    entry = engine.receive(transaction_type='PURCHASE', item=line.item, variant=line.variant, location=order.job_worker_location, quantity=qty,
                           unit_cost=unit_cost, gross_weight=gross, net_weight=net, into_state='PICKED', line_no=line.line_no,
                           reason_code='JOB_WORK_DIRECT')
    _stock(actor, order, line, 'DIRECT_PURCHASE', qty=qty, gross=gross, net=net, stone=stone, value=entry.cost_amount,
           document_type='DIRECT_PURCHASE', document_no=reference or order.order_no, inventory_entry=entry)
    line.dispatched_qty += qty
    line.dispatched_gross += gross
    line.dispatched_net += net
    line.dispatched_fine += fine_of(net, line.purity, line.metal)
    line.dispatched_value += entry.cost_amount
    line.save()
    if order.first_dispatch_date is None:
        due, _ = compliance.return_due_date(actor.tenant, order.goods_category, dj_timezone.localdate())
        order.first_dispatch_date, order.compliance_due_date = dj_timezone.localdate(), due
        order.save(update_fields=['first_dispatch_date', 'compliance_due_date', 'updated_at'])
    refresh_status(order)
    audit(actor, 'receive', 'DIRECT_PURCHASE', reference, order=order, new={'item': line.item.item_no, 'qty': qty, 'cost': entry.cost_amount})
    return entry


# ---------------------------------------------------------------------------
# Processing report: consumption, output, scrap, loss at the job worker
# ---------------------------------------------------------------------------

PROCESSABLE = ('RECEIVED_BY_JOB_WORKER', 'PROCESSING', 'PARTIALLY_COMPLETED', 'READY_FOR_RETURN', 'RETURN_IN_TRANSIT', 'PARTIALLY_RETURNED',
               'RECEIVED', 'QC_PENDING', 'QC_PASSED', 'QC_FAILED', 'REWORK', 'IN_TRANSIT')


@transaction.atomic
def create_process_report(actor, order, *, lines, reference='', report_date=None):
    """Draft job worker report. Each line: kind, input_line, input_qty, input_unit, result_line, quantity, weights, identifiers,
    loss_class (LOSS), scrap_disposition (SCRAP), reason. Nothing moves until it is posted."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    if order.status not in PROCESSABLE:
        raise JobWorkError(f'{order.order_no} has no material at the job worker ({order.get_status_display().lower()}).')
    report = JobWorkProcessReport.objects.create(tenant=actor.tenant, company=order.company, report_no=next_number(actor.tenant, 'PROCESS_REPORT'),
                                                 order=order, report_date=report_date or dj_timezone.localdate(), reference=reference,
                                                 created_by=actor.user)
    for number, spec in enumerate(lines, 1):
        kind = spec['kind']
        input_line, result_line = spec.get('input_line'), spec.get('result_line')
        for ref in (input_line, result_line):
            if ref is not None and ref.order_id != order.id:
                raise JobWorkError(f'Line {number}: order line belongs to another order.')
        if input_line is None or not input_line.is_inbound:
            raise JobWorkError(f'Line {number}: select the input consumed.')
        if kind == 'OUTPUT' and (result_line is None or result_line.line_type != 'OUTPUT'):
            raise JobWorkError(f'Line {number}: output lines need an OUTPUT order line.')
        if kind == 'LOSS' and not spec.get('loss_class'):
            raise JobWorkError(f'Line {number}: classify the loss - differences are never assumed to be wastage.')
        if kind == 'SCRAP':
            if not spec.get('scrap_disposition'):
                raise JobWorkError(f'Line {number}: state what happens to the scrap.')
            if spec['scrap_disposition'] in RETURNED_SCRAP and not order.is_production and (result_line is None or result_line.line_type != 'SCRAP'):
                raise JobWorkError(f'Line {number}: returned scrap needs a SCRAP order line (the scrap / recovery item).')
        unit = spec.get('input_unit')
        _check_tenant(actor, unit)
        input_qty = Decimal('1') if unit is not None else q3(spec.get('input_qty') or 0)
        if kind == 'OUTPUT' and input_qty == 0 and is_metal(input_line) and spec.get('net_weight') not in (None, ''):
            input_qty = q3(D(spec['net_weight']) / grams_per(input_line))
        if input_qty <= 0:
            raise JobWorkError(f'Line {number}: input quantity must be greater than zero.')
        JobWorkProcessLine.objects.create(
            tenant=actor.tenant, company=order.company, report=report, line_no=number, kind=kind, input_line=input_line, input_qty=input_qty,
            input_unit=unit, result_line=result_line, quantity=q3(spec.get('quantity') or (1 if kind == 'OUTPUT' else 0)),
            gross_weight=q3(spec.get('gross_weight') or 0), net_weight=q3(spec.get('net_weight') or spec.get('gross_weight') or 0),
            stone_weight=q3(spec.get('stone_weight') or 0), purity=spec.get('purity') or (result_line.purity if result_line else ''),
            barcode=spec.get('barcode', ''), serial_no=spec.get('serial_no', ''), huid=(spec.get('huid') or '').upper(),
            loss_class=spec.get('loss_class', ''), scrap_disposition=spec.get('scrap_disposition', ''),
            recoverable_value=money(spec.get('recoverable_value') or 0), reason=spec.get('reason', '')[:200], created_by=actor.user)
    audit(actor, 'create', 'PROCESS_REPORT', report.report_no, order=order, new={'lines': len(lines)})
    return report


def _consume_input(actor, ctx, order, pl, entry_type, *, loss_class='', source='MANUAL'):
    """Remove the input of a process line from the job worker. Returns the value consumed."""
    line = pl.input_line
    unit, qty = pl.input_unit, pl.input_qty
    held = jw_balance(order, order_line=line, unit=unit, bulk=unit is None)
    if line.supply_method == 'VENDOR' and qty > held['qty']:
        # vendor-supplied material is the job worker's own stock: it enters the ledger (memo, vendor-owned) as it is used
        shortfall = qty - held['qty']
        gross, net, stone = line_weights(line, shortfall)
        _stock(actor, order, line, 'RECEIVED_BY_JW', qty=shortfall, gross=gross, net=net, stone=stone, value=ZERO,
               document_type='VENDOR_MATERIAL', document_no=ctx['report'].report_no)
        line.dispatched_qty += shortfall
        line.dispatched_gross += gross
        line.dispatched_net += net
        held = jw_balance(order, order_line=line, bulk=True)
    if qty > held['qty']:
        raise JobWorkError(f'{line.item.item_no}: the job worker holds only {held["qty"]} for {order.order_no}.')
    gross, net, stone = line_weights(line, qty, unit=unit)
    if unit is None and held['qty'] and not is_metal(line):
        gross, net, stone = q3(held['gross'] * qty / held['qty']), q3(held['net'] * qty / held['qty']), ZERO
    inv_entry, value = None, ZERO
    if line_is_memo(line):
        value = money(held['value'] * qty / held['qty']) if held['qty'] else ZERO
    elif order.is_production:
        component = line.production_component
        if component is None:
            raise JobWorkError(f'{line.item.item_no} is not linked to a production component.')
        from manufacturing.models import ProductionOrderComponent
        component = ProductionOrderComponent.objects.select_for_update().select_related('item', 'uom').get(pk=component.pk)
        records = ctx['mfg'].consume(component, qty, unit=unit, location=order.job_worker_location if unit is None else None,
                                     from_state='PICKED' if unit is None else None, source=source,
                                     operation=order.operation)
        value = sum((r.cost_amount for r in records), ZERO)
        inv_entry = records[0].inventory_entry if records else None
    else:
        transaction_type = 'SCRAP' if entry_type == 'LOSS' else 'CONSUMPTION'
        result = ctx['inv'].issue(transaction_type=transaction_type, item=line.item, variant=line.variant, location=order.job_worker_location,
                                  quantity=qty, unit=unit, consume_state='PICKED',
                                  final_unit_status='MISSING' if entry_type == 'LOSS' else 'CONSUMED',
                                  reason_code=f'JOB_WORK:{loss_class or entry_type}', line_no=line.line_no)
        entries = result if isinstance(result, list) else [result]
        value, inv_entry = -sum((e.cost_amount for e in entries), ZERO), entries[0]
    _stock(actor, order, line, entry_type, qty=-qty, gross=-gross, net=-net, stone=-stone, value=-value, unit=unit,
           document_type='PROCESS_REPORT', document_no=ctx['report'].report_no, inventory_entry=inv_entry, loss_class=loss_class)
    field = {'CONSUMED': 'consumed_qty', 'SCRAP': 'scrap_qty', 'LOSS': 'loss_qty'}[entry_type]
    setattr(line, field, getattr(line, field) + qty)
    if entry_type == 'CONSUMED':
        line.consumed_net += net
    line.cost_value += value
    line.save()
    pl.value = money(value)
    return value, net


def _receive_result(actor, ctx, order, pl, value):
    """Output / returned scrap appears at the job worker (inventory for non-production orders, WIP memo for production)."""
    line = JobWorkOrderLine.objects.select_for_update().get(pk=pl.result_line_id)
    qty = pl.quantity
    gross = pl.gross_weight
    net = pl.net_weight or max(gross - pl.stone_weight, ZERO)
    unit, inv_entry = None, None
    if not line_is_memo(line):
        from manufacturing.engine import ensure_sku
        sku = ensure_sku(actor, line.item, line.variant, order.job_worker_location, line.sku)
        if line.item.serial_tracking:
            if qty != 1:
                raise JobWorkError(f'{line.item.item_no} is serialized: report one output line per piece.')
            barcode = pl.barcode or f'{order.order_no}-{line.line_no}-{pl.pk}'
            unit = register_unit(actor, sku=sku, barcode=barcode, serial_no=pl.serial_no or barcode, huid=pl.huid or None,
                                 gross_weight=gross, stone_weight=pl.stone_weight, other_weight=max(gross - pl.stone_weight - net, ZERO),
                                 purity=pl.purity or line.purity, metal_cost=money(value), purchase_cost=money(value) or None,
                                 lot_no=order.order_no)
            pl.output_unit = unit
        inv_entry = ctx['inv'].receive(transaction_type='OUTPUT', item=line.item, variant=line.variant, sku=sku, location=order.job_worker_location,
                                       quantity=qty, unit=unit, unit_cost=money(value) / qty if qty else ZERO, gross_weight=gross, net_weight=net,
                                       into_state='PICKED', line_no=line.line_no, reason_code='JOB_WORK_OUTPUT')
        value = inv_entry.cost_amount
    entry_type = 'OUTPUT' if pl.kind == 'OUTPUT' else 'SCRAP'
    _stock(actor, order, line, entry_type, qty=qty, gross=gross, net=net, stone=pl.stone_weight, value=value, unit=unit,
           document_type='PROCESS_REPORT', document_no=ctx['report'].report_no, inventory_entry=inv_entry)
    line.produced_qty += qty
    line.produced_gross += gross
    line.produced_net += net
    line.cost_value += value
    line.save()
    return line


@transaction.atomic
def post_process_report(actor, report, *, skip_tolerance=False):
    """Post a job worker report atomically: consumption, output, scrap and loss, with cost allocation and loss control."""
    require(actor, 'execute')
    report = _lock(JobWorkProcessReport, actor, report)
    if report.status != 'DRAFT':
        raise JobWorkError(f'{report.report_no} is {report.get_status_display().lower()}.')
    order = _lock(JobWorkOrder, actor, report.order)
    if order.status not in PROCESSABLE:
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    setup = get_setup(actor.tenant)
    ctx = {'report': report, 'inv': _inventory(actor, 'JOB_WORK_PROCESS', report.report_no), 'mfg': None}
    if order.is_production:
        from manufacturing import engine as mfg_engine
        prod = mfg_engine.lock_order(actor, order.production_order)
        mfg_engine.assert_executable(prod)
        ctx['mfg'] = mfg_engine.ManufacturingPostingEngine(actor, prod, kind='SUBCONTRACT', reason=f'Job work {report.report_no}')
        ctx['inv'] = ctx['mfg'].inventory
    lines = list(report.lines.select_related('input_line__item', 'input_line__uom', 'result_line__item', 'input_unit').order_by('line_no'))
    outputs, pooled = [], ZERO
    for pl in lines:
        pl.input_line = JobWorkOrderLine.objects.select_for_update().select_related('item', 'uom', 'order').get(pk=pl.input_line_id)
        if pl.kind in ('OUTPUT', 'CONSUMPTION'):
            value, net = _consume_input(actor, ctx, order, pl, 'CONSUMED')
            _cost(actor, order, 'MATERIAL', value, line=pl.input_line, document_type='PROCESS_REPORT', document_no=report.report_no,
                  description=f'{pl.input_line.item.item_no} x {pl.input_qty} consumed')
            if pl.kind == 'OUTPUT':
                outputs.append((pl, net))
        elif pl.kind == 'SCRAP':
            returned = pl.scrap_disposition in RETURNED_SCRAP
            if order.is_production and returned:
                # returned production scrap is still the component material: it stays at the job worker and comes back as such
                held = jw_balance(order, order_line=pl.input_line)
                if pl.input_qty > held['qty']:
                    raise JobWorkError(f'{pl.input_line.item.item_no}: the job worker holds only {held["qty"]}.')
                pl.input_line.scrap_qty += pl.input_qty
                pl.input_line.save(update_fields=['scrap_qty', 'updated_at'])
            else:
                value, net = _consume_input(actor, ctx, order, pl, 'SCRAP' if returned else 'CONSUMED')
                _cost(actor, order, 'MATERIAL', value, line=pl.input_line, document_type='PROCESS_REPORT', document_no=report.report_no,
                      description=f'{pl.input_line.item.item_no} x {pl.input_qty} to scrap ({pl.get_scrap_disposition_display()})')
                if returned:
                    scrap_value = min(pl.recoverable_value, value) if pl.recoverable_value else value
                    if not pl.gross_weight:
                        pl.gross_weight = pl.net_weight = net
                    if not pl.quantity:
                        pl.quantity = pl.input_qty
                    _receive_result(actor, ctx, order, pl, scrap_value)
                    _cost(actor, order, 'SCRAP', -scrap_value, line=pl.result_line, document_type='PROCESS_REPORT', document_no=report.report_no,
                          description=f'Scrap {pl.result_line.item.item_no} recovered')
        elif pl.kind == 'LOSS':
            value, net = _consume_input(actor, ctx, order, pl, 'LOSS', loss_class=pl.loss_class, source='SUBCONTRACT_LOSS')
            _cost(actor, order, 'LOSS', value, line=pl.input_line, document_type='PROCESS_REPORT', document_no=report.report_no,
                  description=f'{pl.get_loss_class_display()} {pl.input_line.item.item_no} x {pl.input_qty}',
                  absorbed=pl.loss_class in ABSORBED_LOSS and not order.is_production)
        pl.save()
    # Allocate the open material WIP to this report's output (share of the remaining expected output)
    if outputs:
        if not order.is_production:
            _allocate_outputs(actor, ctx, order, outputs, setup)
        else:
            for pl, _ in outputs:
                _receive_result(actor, ctx, order, pl, ZERO)
                pl.save()
    _check_fine_weight(actor, order, report, outputs, setup)
    if not skip_tolerance:
        check_loss_tolerance(actor, order)
    if ctx['mfg'] is not None:
        ctx['mfg'].extra['job_work'] = {'report': report.report_no, 'order': order.order_no}
        ctx['mfg'].finalize(action='job_work_process')
    report.status, report.posted_by, report.posted_at = 'POSTED', actor.user, dj_timezone.now()
    report.summary = {'lines': [{'kind': pl.kind, 'input': pl.input_line.item.item_no, 'input_qty': str(pl.input_qty), 'value': str(pl.value),
                                 'output': pl.result_line.item.item_no if pl.result_line_id else '', 'qty': str(pl.quantity),
                                 'unit': pl.output_unit.barcode if pl.output_unit_id else ''} for pl in lines]}
    report.save()
    refresh_status(order)
    audit(actor, 'post', 'PROCESS_REPORT', report.report_no, order=order, new={'lines': len(lines), 'wip': material_wip(order)})
    return report


def _allocate_outputs(actor, ctx, order, outputs, setup):
    pool = material_wip(order)
    expected = sum((l.quantity for l in order.lines.filter(line_type='OUTPUT')), ZERO)
    produced_before = sum((l.produced_qty for l in order.lines.filter(line_type='OUTPUT')), ZERO)
    this_qty = sum((pl.quantity for pl, _ in outputs), ZERO)
    remaining = expected - produced_before
    share = Decimal('1') if remaining <= this_qty or remaining <= 0 else this_qty / remaining
    total = money(pool * share)
    method = setup.cost_allocation_method

    def basis(pl):
        if method == 'WEIGHT':
            return pl.net_weight or pl.gross_weight or pl.quantity
        if method in ('VALUE', 'STANDARD'):
            return (pl.result_line.item.standard_cost or Decimal('1')) * pl.quantity
        return pl.quantity

    weights = [basis(pl) for pl, _ in outputs]
    base = sum(weights, ZERO) or Decimal('1')
    allocated = ZERO
    for index, ((pl, _), weight) in enumerate(zip(outputs, weights)):
        value = total - allocated if index == len(outputs) - 1 else money(total * weight / base)
        allocated += value
        result_line = _receive_result(actor, ctx, order, pl, value)
        _cost(actor, order, 'OUTPUT', -value, line=result_line, document_type='PROCESS_REPORT', document_no=ctx['report'].report_no,
              description=f'{result_line.item.item_no} x {pl.quantity} valued')
        pl.save()


def _check_fine_weight(actor, order, report, outputs, setup):
    """Output fine metal must equal the fine metal consumed for it: anything else is an unclassified loss/gain."""
    tolerance = setup.weight_tolerance
    for pl, consumed_net in outputs:
        if not is_metal(pl.input_line) or not pl.net_weight:
            continue  # pieces of WIP carry metal consumed earlier - only weighed metal inputs are compared
        consumed_fine = fine_of(consumed_net, pl.input_line.purity, pl.input_line.metal)
        output_fine = fine_of(pl.net_weight, pl.purity or pl.input_line.purity, pl.input_line.metal)
        if abs(consumed_fine - output_fine) > tolerance:
            raise_exception(actor, exception_type='FINE_WEIGHT_VARIANCE', order=order, order_line=pl.input_line, document_type='PROCESS_REPORT',
                            document_no=report.report_no, expected=consumed_fine, actual=output_fine, weight=output_fine - consumed_fine,
                            description=f'{report.report_no} line {pl.line_no}: output fine {output_fine} g vs consumed fine {consumed_fine} g. '
                                        'Report the difference as scrap or classified loss.',
                            metric='METAL_WEIGHT', metric_value=abs(output_fine - consumed_fine))


def metal_totals(order):
    lines = [l for l in order.lines.select_related('uom').all() if l.is_inbound and is_metal(l)]
    sent = sum((l.dispatched_net for l in lines), ZERO)
    loss = q3(JobWorkerStockEntry.objects.filter(order=order, order_line__in=lines, entry_type='LOSS').aggregate(n=Sum('net_weight'))['n'] or 0)
    return sent, -loss


def check_loss_tolerance(actor, order):
    sent, loss = metal_totals(order)
    if not sent or not order.max_loss_percent:
        return None
    percent = (loss * 100 / sent).quantize(Decimal('0.001'))
    if percent <= order.max_loss_percent:
        return None
    covered = order.exceptions.filter(exception_type='EXCESS_LOSS', status__in=('APPROVED', 'RESOLVED', 'CLOSED'), actual_value__gte=percent).exists()
    if covered:
        return None
    return raise_exception(
        actor, exception_type='EXCESS_LOSS', order=order, expected=order.max_loss_percent, actual=percent, weight=loss,
        description=f'{order.order_no}: metal loss {loss} g = {percent}% of {sent} g sent exceeds the allowed {order.max_loss_percent}% '
                    f'(expected {order.expected_loss_percent}%). Approve with a reason and choose recovery or waiver.',
        dedupe_key=f'EXCESS_LOSS:{order.pk}', metric='LOSS_PERCENT', metric_value=percent)


@transaction.atomic
def reverse_process_report(actor, report, *, reason):
    """Reverse a posted non-production report while its output is still at the job worker."""
    require(actor, 'reverse')
    report = _lock(JobWorkProcessReport, actor, report)
    if report.status != 'POSTED':
        raise JobWorkError('Only a posted report can be reversed.')
    order = _lock(JobWorkOrder, actor, report.order)
    if order.is_production:
        raise JobWorkError('Production-linked reports are reversed through the manufacturing reversal of their posting batch.')
    entries = list(JobWorkerStockEntry.objects.filter(order=order, document_type='PROCESS_REPORT', document_no=report.report_no, reversed=False)
                   .select_related('order_line', 'jewellery_unit', 'item').order_by('-id'))
    for entry in entries:
        if entry.quantity > 0 and jw_balance(order, order_line=entry.order_line, unit=entry.jewellery_unit)['qty'] < entry.quantity:
            raise JobWorkError(f'{entry.item.item_no} from {report.report_no} has already left the job worker; reverse that movement first.')
    engine = _inventory(actor, 'JOB_WORK_PROCESS_REVERSAL', report.report_no)
    for entry in entries:
        line = JobWorkOrderLine.objects.select_for_update().get(pk=entry.order_line_id)
        inv = entry.inventory_entry
        if entry.quantity > 0:   # output / scrap received -> issue it back out
            if not entry.memo:
                engine.issue(transaction_type='REVERSAL', item=entry.item, variant=entry.variant, location=entry.location, quantity=entry.quantity,
                             unit=entry.jewellery_unit, consume_state='PICKED', final_unit_status='NOT_IN_STOCK', cost_amount=entry.value,
                             reason_code='PROCESS_REVERSAL', reversal_of=inv)
            line.produced_qty -= entry.quantity
            line.produced_gross -= entry.gross_weight
            line.produced_net -= entry.net_weight
        else:                    # input consumed -> put it back at the job worker
            if not entry.memo:
                engine.receive(transaction_type='REVERSAL', item=entry.item, variant=entry.variant, location=entry.location, quantity=-entry.quantity,
                               unit=entry.jewellery_unit, unit_cost=-entry.value / -entry.quantity, gross_weight=-entry.gross_weight,
                               net_weight=-entry.net_weight, into_state='PICKED', reason_code='PROCESS_REVERSAL', reversal_of=inv)
            field = {'CONSUMED': 'consumed_qty', 'SCRAP': 'scrap_qty', 'LOSS': 'loss_qty'}[entry.entry_type]
            setattr(line, field, getattr(line, field) + entry.quantity)
            if entry.entry_type == 'CONSUMED':
                line.consumed_net += entry.net_weight
        line.cost_value -= entry.value if entry.quantity > 0 else -entry.value
        line.save()
        _stock(actor, order, line, 'REVERSAL', qty=-entry.quantity, gross=-entry.gross_weight, net=-entry.net_weight, stone=-entry.stone_weight,
               value=-entry.value, unit=entry.jewellery_unit, location=entry.location, document_type='PROCESS_REPORT_REVERSAL',
               document_no=report.report_no, reversal_of=entry)
        entry.reversed = True
        entry.save(update_fields=['reversed', 'updated_at'])
    for cost in JobWorkCostEntry.objects.filter(order=order, document_no=report.report_no, reversed=False, reversal_of__isnull=True):
        _cost(actor, order, cost.cost_type, -cost.amount, line=cost.order_line, document_type='PROCESS_REPORT_REVERSAL',
              document_no=report.report_no, description=f'Reversal of {cost.entry_no}', absorbed=cost.absorbed, reversal_of=cost)
        cost.reversed = True
        cost.save(update_fields=['reversed', 'updated_at'])
    report.status, report.reversal_reason = 'REVERSED', reason[:250]
    report.save()
    refresh_status(order)
    audit(actor, 'reverse', 'PROCESS_REPORT', report.report_no, order=order, reason=reason)
    return report


# ---------------------------------------------------------------------------
# Receipt at the principal, short close, QC, rework, production output
# ---------------------------------------------------------------------------

def receive_return(actor, dispatch, *, lines=None, bin=None):
    """Goods back from the job worker: transit -> principal. Output goes to QC (never straight to available stock)."""
    require(actor, 'execute')
    for spec in lines or []:
        dl = spec['dispatch_line']
        qty = Decimal('1') if dl.jewellery_unit_id else q3(spec.get('quantity') or 0)
        open_qty = JobWorkDispatchLine.objects.get(pk=dl.pk).open_qty
        if qty > open_qty:
            with transaction.atomic():  # the exception is recorded even though the receipt is refused
                raise_exception(actor, exception_type='DUPLICATE_RECEIPT', order=dl.dispatch.order, blocking=False, severity='MEDIUM',
                                description=f'{dl.dispatch.dispatch_no} line {dl.line_no}: receipt of {qty} exceeds the {open_qty} in transit.',
                                document_type='JOB_WORK_DISPATCH', document_no=dl.dispatch.dispatch_no,
                                dedupe_key=f'DUPRCPT:{dl.pk}')
            raise JobWorkError(f'{dl.item.item_no}: only {open_qty} is in transit on {dl.dispatch.dispatch_no}.')
    return _receive_return(actor, dispatch, lines=lines, bin=bin)


@transaction.atomic
def _receive_return(actor, dispatch, *, lines=None, bin=None):
    dispatch = _lock(JobWorkDispatch, actor, dispatch)
    if dispatch.movement_type != 'JW_TO_PRINCIPAL':
        raise JobWorkError(f'{dispatch.dispatch_no} is not a return from the job worker.')
    if dispatch.status not in ('IN_TRANSIT', 'PARTIALLY_RECEIVED'):
        raise JobWorkError(f'{dispatch.dispatch_no} is {dispatch.get_status_display().lower()} - nothing left to receive.')
    order = _lock(JobWorkOrder, actor, dispatch.order)
    require_location(actor.tenant, actor.user, dispatch.to_location, 'receive')
    setup = get_setup(actor.tenant)
    to_bin = bin or order.return_bin
    specs = lines if lines is not None else [{'dispatch_line': dl, 'quantity': dl.open_qty} for dl in dispatch.lines.all() if dl.open_qty > 0]
    receipt = JobWorkReceipt.objects.create(tenant=actor.tenant, company=order.company, receipt_no=next_number(actor.tenant, 'JOB_WORK_RECEIPT'),
                                            order=order, dispatch=dispatch, location=dispatch.to_location, bin=to_bin, received_by=actor.user,
                                            created_by=actor.user)
    engine = _inventory(actor, 'JOB_WORK_RECEIPT', receipt.receipt_no)
    needs_qc = False
    for spec in specs:
        dl = JobWorkDispatchLine.objects.select_for_update().select_related('order_line__item', 'jewellery_unit', 'item').get(
            pk=spec['dispatch_line'].pk, dispatch=dispatch)
        qty = Decimal('1') if dl.jewellery_unit_id else q3(spec.get('quantity') or 0)
        if qty <= 0:
            continue
        if qty > dl.open_qty:
            raise JobWorkError(f'{dl.item.item_no}: only {dl.open_qty} is in transit on {dispatch.dispatch_no}.')
        line = JobWorkOrderLine.objects.select_for_update().get(pk=dl.order_line_id)
        qc_line = order.qc_required and line.line_type in ('OUTPUT', 'BY_PRODUCT')
        needs_qc = needs_qc or qc_line
        expected_gross = q3(dl.gross_weight * qty / dl.quantity) if dl.quantity else ZERO
        gross = q3(spec['gross_weight']) if spec.get('gross_weight') not in (None, '') else expected_gross
        net = q3(spec['net_weight']) if spec.get('net_weight') not in (None, '') else q3(dl.net_weight * qty / dl.quantity) if dl.quantity else ZERO
        inv_entry, value = None, money(dl.value * qty / dl.quantity) if dl.quantity else ZERO
        if not dl.memo:
            if order.is_production:
                into = 'PICKED'  # material issued to the production order returns to the production bin
            else:
                into = 'QC' if qc_line else None
            _, ins = engine.move(transaction_type='TRANSFER_RECEIPT', item=dl.item, variant=dl.variant, from_location=dispatch.transit_location,
                                 to_location=dispatch.to_location, to_bin=to_bin, quantity=qty, unit=dl.jewellery_unit, into_state=into,
                                 allow_from_transit=True, line_no=dl.line_no, reason_code='JOB_WORK_RETURN')
            inv_entry, value = ins[0], sum((e.cost_amount for e in ins), ZERO)
        JobWorkReceiptLine.objects.create(
            tenant=actor.tenant, company=order.company, receipt=receipt, dispatch_line=dl, order_line=line, item=dl.item,
            jewellery_unit=dl.jewellery_unit, quantity=qty, gross_weight=gross, net_weight=net, dispatched_gross=expected_gross,
            weight_variance=gross - expected_gross, value=value, qc_pending_qty=qty if qc_line else ZERO, inventory_entry=inv_entry,
            created_by=actor.user)
        if abs(gross - expected_gross) > setup.weight_tolerance:
            raise_exception(actor, exception_type='RECEIPT_WEIGHT_VARIANCE', order=order, order_line=line, expected=expected_gross,
                            actual=gross, weight=gross - expected_gross, document_type='JOB_WORK_RECEIPT', document_no=receipt.receipt_no,
                            description=f'{receipt.receipt_no}: {dl.item.item_no} weighed {gross} g at receipt vs {expected_gross} g dispatched by '
                                        f'the job worker.', metric='METAL_WEIGHT', metric_value=abs(gross - expected_gross))
        dl.received_qty += qty
        dl.received_gross += gross
        dl.save(update_fields=['received_qty', 'received_gross', 'updated_at'])
        line.received_qty += qty
        line.received_gross += gross
        line.received_net += net
        if line.line_type == 'OUTPUT' and not qc_line:
            line.accepted_qty += qty
            order.output_accepted_qty += qty
        line.save()
        if line.line_type == 'OUTPUT' and not qc_line and line.received_qty > line.quantity:
            _excess_output(actor, order, line, receipt)
    if not receipt.lines.exists():
        raise JobWorkError('Nothing received.')
    receipt.qc_status = 'PENDING' if needs_qc else 'NOT_REQUIRED'
    receipt.save()
    dispatch.status = 'RECEIVED' if all(dl.open_qty == 0 for dl in dispatch.lines.all()) else 'PARTIALLY_RECEIVED'
    dispatch.save(update_fields=['status', 'updated_at'])
    order.actual_return_date = receipt.receipt_date
    order.save()
    if order.is_production and not needs_qc:
        _post_production_output(actor, order)
    refresh_status(order)
    audit(actor, 'post', 'JOB_WORK_RECEIPT', receipt.receipt_no, order=order, new={'dispatch': dispatch.dispatch_no, 'qc': receipt.qc_status})
    return receipt


def _excess_output(actor, order, line, receipt):
    raise_exception(actor, exception_type='EXCESS_RETURN', order=order, order_line=line, expected=line.quantity, actual=line.received_qty,
                    document_type='JOB_WORK_RECEIPT', document_no=receipt.receipt_no,
                    description=f'{order.order_no}: {line.received_qty} of {line.item.item_no} returned against {line.quantity} expected. '
                                'Classify: additional output, previously unrecorded material, data correction or vendor-supplied material.',
                    dedupe_key=f'EXCESS_RETURN:{line.pk}')


@transaction.atomic
def short_close_return(actor, dispatch, *, reason, loss_class):
    """Goods dispatched back by the job worker that never arrived. Written off from transit only with a classification,
    and always through an exception that must be approved - missing goods are never silently consumption."""
    require(actor, 'execute')
    if not loss_class:
        raise JobWorkError('Classify the shortage (loss, unexplained shortage, job worker liability...).')
    dispatch = _lock(JobWorkDispatch, actor, dispatch)
    if dispatch.movement_type != 'JW_TO_PRINCIPAL' or dispatch.status not in ('IN_TRANSIT', 'PARTIALLY_RECEIVED'):
        raise JobWorkError(f'{dispatch.dispatch_no} has nothing in transit to short-close.')
    order = _lock(JobWorkOrder, actor, dispatch.order)
    engine = _inventory(actor, 'JOB_WORK_SHORT_CLOSE', dispatch.dispatch_no)
    total_value, total_weight = ZERO, ZERO
    for dl in dispatch.lines.select_related('item', 'jewellery_unit', 'order_line'):
        short = dl.open_qty
        if short <= 0:
            continue
        value = money(dl.value * short / dl.quantity) if dl.quantity else ZERO
        if not dl.memo:
            entry = engine.write_off_transit(item=dl.item, transit_location=dispatch.transit_location, quantity=short, variant=dl.variant,
                                             unit=dl.jewellery_unit, reason_code=f'JOB_WORK:{loss_class}', line_no=dl.line_no,
                                             from_location=dispatch.from_location, to_location=dispatch.to_location)
            value = -entry.cost_amount
        total_value += value
        total_weight += q3(dl.gross_weight * short / dl.quantity) if dl.quantity else ZERO
        _cost(actor, order, 'LOSS', value, line=dl.order_line, document_type='JOB_WORK_SHORT_CLOSE', document_no=dispatch.dispatch_no,
              description=f'Short return {dl.item.item_no} x {short} ({loss_class})', absorbed=False)
        dl.received_qty += short
        dl.save(update_fields=['received_qty', 'updated_at'])
    dispatch.status = 'RECEIVED'
    dispatch.save(update_fields=['status', 'updated_at'])
    exc = raise_exception(actor, exception_type='SHORT_RETURN', order=order, value=total_value, weight=total_weight, loss_class=loss_class,
                          description=f'{dispatch.dispatch_no}: goods worth {total_value} ({total_weight} g) dispatched by the job worker were not '
                                      f'received. Reason: {reason}', document_type='JOB_WORK_RETURN', document_no=dispatch.dispatch_no,
                          metric='ADJUSTMENT', metric_value=total_value)
    refresh_status(order)
    audit(actor, 'short_close', 'JOB_WORK_RETURN', dispatch.dispatch_no, order=order, reason=reason, new={'value': total_value})
    return exc


@transaction.atomic
def record_qc(actor, receipt, *, results, checks=None, remarks=''):
    """QC of received output. results: [{receipt_line, accepted, rejected, rework, hold, measured_gross, purity_result, huid_ok, defect}]."""
    require(actor, 'qc')
    receipt = _lock(JobWorkReceipt, actor, receipt)
    if receipt.qc_status != 'PENDING':
        raise JobWorkError(f'{receipt.receipt_no} has no QC pending.')
    order = _lock(JobWorkOrder, actor, receipt.order)
    engine = _inventory(actor, 'JOB_WORK_QC', receipt.receipt_no)
    qc = JobWorkQC.objects.create(tenant=actor.tenant, company=order.company, qc_no=next_number(actor.tenant, 'JOB_WORK_QC'), order=order,
                                  receipt=receipt, result='ACCEPTED', checks=checks or {}, inspector=actor.user, remarks=remarks,
                                  created_by=actor.user)
    totals = {'accepted': ZERO, 'rejected': ZERO, 'rework': ZERO, 'hold': ZERO}
    rework_specs = []
    for spec in results:
        rl = JobWorkReceiptLine.objects.select_for_update().select_related('order_line', 'item', 'jewellery_unit', 'dispatch_line').get(
            pk=spec['receipt_line'].pk, receipt=receipt)
        accepted, rejected, rework, hold = (q3(spec.get(k) or 0) for k in ('accepted', 'rejected', 'rework', 'hold'))
        if accepted + rejected + rework + hold > rl.qc_pending_qty:
            raise JobWorkError(f'{rl.item.item_no}: QC quantities exceed the {rl.qc_pending_qty} pending.')
        line = JobWorkOrderLine.objects.select_for_update().get(pk=rl.order_line_id)
        if not rl.dispatch_line.memo:
            unit = rl.jewellery_unit
            for qty, state in ((accepted, None), (rejected, 'DAMAGED'), (rework, None)):
                if qty > 0:
                    engine.change_state(item=rl.item, variant=line.variant, location=receipt.location, bin=receipt.bin if unit is None else None,
                                        unit=unit, quantity=qty, from_state='QC', to_state=state, transaction_type='QC', reason_code='JOB_WORK_QC')
        JobWorkQCLine.objects.create(tenant=actor.tenant, company=order.company, qc=qc, receipt_line=rl, accepted_qty=accepted,
                                     rejected_qty=rejected, rework_qty=rework, hold_qty=hold, measured_gross=q3(spec.get('measured_gross') or 0),
                                     purity_result=spec.get('purity_result', ''), huid_ok=spec.get('huid_ok', True),
                                     defect=spec.get('defect', '')[:200], created_by=actor.user)
        rl.qc_pending_qty -= accepted + rejected + rework
        rl.save(update_fields=['qc_pending_qty', 'updated_at'])
        line.accepted_qty += accepted
        line.rejected_qty += rejected + rework
        line.save()
        order.output_accepted_qty += accepted
        order.output_rejected_qty += rejected + rework
        for key, qty in (('accepted', accepted), ('rejected', rejected), ('rework', rework), ('hold', hold)):
            totals[key] += qty
        if not spec.get('huid_ok', True):
            raise_exception(actor, exception_type='WRONG_HUID', order=order, order_line=line, document_type='JOB_WORK_QC', document_no=qc.qc_no,
                            description=f'{qc.qc_no}: HUID check failed for {rl.item.item_no} {rl.jewellery_unit.barcode if rl.jewellery_unit_id else ""}.')
        if spec.get('purity_result') and line.purity and spec['purity_result'].upper() != line.purity.upper():
            raise_exception(actor, exception_type='WRONG_PURITY', order=order, order_line=line, document_type='JOB_WORK_QC', document_no=qc.qc_no,
                            description=f'{qc.qc_no}: {rl.item.item_no} tested {spec["purity_result"]} against {line.purity}.')
        if rework > 0:
            rework_specs.append({'line': line, 'receipt_line': rl, 'quantity': rework})
        if line.received_qty > line.quantity and line.line_type == 'OUTPUT':
            _excess_output(actor, order, line, receipt)
    if totals['rejected'] or totals['rework']:
        raise_exception(actor, exception_type='QC_FAILURE', order=order, blocking=False, severity='MEDIUM', document_type='JOB_WORK_QC',
                        document_no=qc.qc_no, actual=totals['rejected'] + totals['rework'],
                        description=f'{qc.qc_no}: {totals["rejected"]} rejected and {totals["rework"]} sent for rework.')
    qc.result = ('REWORK' if totals['rework'] and not totals['accepted'] else 'REJECTED' if totals['rejected'] and not totals['accepted']
                 else 'HOLD' if totals['hold'] and not totals['accepted'] else 'PARTIAL' if any(totals[k] for k in ('rejected', 'rework', 'hold'))
                 else 'ACCEPTED')
    if rework_specs:
        qc.rework_order = create_rework_order(actor, order, rework_specs, qc=qc)
    qc.save()
    if not receipt.lines.filter(qc_pending_qty__gt=0).exists():
        receipt.qc_status = 'DONE'
        receipt.save(update_fields=['qc_status', 'updated_at'])
    order.save()
    if order.is_production:
        _post_production_output(actor, order)
    refresh_status(order)
    audit(actor, 'post', 'JOB_WORK_QC', qc.qc_no, order=order, new={k: v for k, v in totals.items()})
    return qc


def create_rework_order(actor, order, specs, *, qc=None):
    """A new job work order for pieces that failed QC - the original transaction is never overwritten."""
    lines = []
    for spec in specs:
        line, rl = spec['line'], spec['receipt_line']
        common = {'item': line.item, 'variant': line.variant, 'quantity': spec['quantity'], 'purity': line.purity}
        if rl.jewellery_unit_id:
            common = {'unit': rl.jewellery_unit}
        lines.append({'line_type': 'WIP' if order.is_production else 'INPUT', **common})
        lines.append({'line_type': 'OUTPUT', 'item': line.item, 'variant': line.variant, 'quantity': spec['quantity'], 'purity': line.purity})
    rework = create_order(
        actor, job_worker=order.job_worker, lines=lines, order_kind=order.order_kind, transaction_type='REWORK_SUBCONTRACT'
        if order.is_production else 'REPAIR_JOB_WORK', source_type='REWORK', source_no=order.order_no, rework_of=order,
        production_order=order.production_order, operation=order.operation, source_location=order.return_location,
        return_location=order.return_location, return_bin=order.return_bin, operation_code=order.operation_code, rate=ZERO,
        goods_category=order.goods_category, service_sac=order.service_sac, description=f'Rework of {order.order_no}'
        + (f' ({qc.qc_no})' if qc else ''), check_permission=False)
    audit(actor, 'create', 'REWORK_ORDER', rework.order_no, order=order, new={'qc': qc.qc_no if qc else ''})
    return rework


def _production_final(order):
    from manufacturing import engine as mfg_engine
    last = mfg_engine.final_operation(order.production_order)
    return last is not None and order.operation_id == last.pk


def _post_production_output(actor, order):
    """Accepted output of an intermediate subcontract operation is reported on the operation. Output of the final operation is
    NOT posted automatically: it needs explicit confirmation (confirm_final_output)."""
    if order.operation_id is None or order.rework_of_id:
        return None
    pending = order.output_accepted_qty - order.output_posted_qty
    if pending <= 0 or _production_final(order):
        return None
    from manufacturing import engine as mfg_engine
    prod = mfg_engine.lock_order(actor, order.production_order)
    engine = mfg_engine.ManufacturingPostingEngine(actor, prod, kind='SUBCONTRACT', reason=f'Job work {order.order_no} output')
    engine.output(operation=order.operation, quantity=pending)
    engine.extra['job_work'] = {'order': order.order_no, 'output': str(pending)}
    engine.finalize(action='job_work_output')
    order.output_posted_qty += pending
    order.save(update_fields=['output_posted_qty', 'updated_at'])
    return pending


@transaction.atomic
def confirm_final_output(actor, order, *, quantity, units=None, gross_weight=None, net_weight=None, stone_weight=None):
    """Explicitly post finished output when the subcontract operation is the last one of the production order."""
    require(actor, 'execute')
    order = _lock(JobWorkOrder, actor, order)
    if not order.is_production or not _production_final(order):
        raise JobWorkError('Only the final subcontract operation of a production order needs output confirmation.')
    qty = q3(quantity)
    if qty <= 0 or qty > order.output_accepted_qty - order.output_posted_qty:
        raise JobWorkError(f'Only {order.output_accepted_qty - order.output_posted_qty} accepted pieces await output posting.')
    from manufacturing import engine as mfg_engine
    prod = mfg_engine.lock_order(actor, order.production_order)
    engine = mfg_engine.ManufacturingPostingEngine(actor, prod, kind='SUBCONTRACT', reason=f'Job work {order.order_no} final output')
    engine.output(operation=order.operation, quantity=qty, units=units or [], gross_weight=gross_weight, net_weight=net_weight,
                  stone_weight=stone_weight)
    engine.finalize(action='job_work_final_output')
    order.output_posted_qty += qty
    order.save(update_fields=['output_posted_qty', 'updated_at'])
    refresh_status(order)
    audit(actor, 'confirm_output', 'JOB_WORK_ORDER', order.order_no, order=order, new={'qty': qty})
    return order


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def refresh_status(order):
    """Derive the operational status from the documents (never set by hand after approval)."""
    if order.status in ('DRAFT', 'PENDING_APPROVAL', 'COMPLETED') + JobWorkOrder.TERMINAL_STATUSES:
        return order.status
    dispatches = order.dispatches.exclude(status__in=('REVERSED', 'CANCELLED'))
    outbound = dispatches.exclude(movement_type='JW_TO_PRINCIPAL')
    returns = dispatches.filter(movement_type='JW_TO_PRINCIPAL')
    held = jw_balance(order)['qty']
    outputs = order.lines.filter(line_type='OUTPUT')
    expected = sum((l.quantity for l in outputs), ZERO)
    produced = sum((l.produced_qty for l in outputs), ZERO)
    received = sum((l.received_qty for l in outputs), ZERO)
    qc_pending = JobWorkReceiptLine.objects.filter(receipt__order=order, receipt__status='POSTED', qc_pending_qty__gt=0).exists()
    if returns.filter(status__in=('IN_TRANSIT', 'PARTIALLY_RECEIVED')).exists():
        status = 'RETURN_IN_TRANSIT'
    elif qc_pending:
        status = 'QC_PENDING'
    elif outbound.filter(status='IN_TRANSIT').exists() and held <= 0:
        status = 'IN_TRANSIT'
    elif held > 0:
        if received > 0:
            status = 'PARTIALLY_RETURNED'
        elif expected and produced >= expected:
            status = 'READY_FOR_RETURN'
        elif produced > 0:
            status = 'PARTIALLY_COMPLETED'
        elif order.process_reports.filter(status='POSTED').exists():
            status = 'PROCESSING'
        else:
            status = 'RECEIVED_BY_JOB_WORKER'
    elif not returns.exists() and order.outgoing_transfers.exclude(status__in=('REVERSED', 'CANCELLED')).exists():
        status = 'TRANSFERRED'
    elif returns.exists():
        if order.rework_orders.exclude(status__in=('COMPLETED', 'CLOSED', 'CANCELLED')).exists():
            status = 'REWORK'
        elif order.qc_required and order.qcs.exists():
            status = 'QC_FAILED' if order.output_rejected_qty and not order.output_accepted_qty else 'QC_PASSED'
        else:
            status = 'RECEIVED'
    elif outbound.filter(status='DRAFT').exists():
        status = 'DISPATCH_CREATED'
    elif outbound.exists():
        status = 'RECEIVED_BY_JOB_WORKER'
    else:
        status = order.status if order.status in ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH') else 'APPROVED'
    if status != order.status:
        order.status = status
        order.save(update_fields=['status', 'updated_at'])
    return status
