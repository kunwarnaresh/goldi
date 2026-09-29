"""Job work reporting: reconciliation (quantity and weight), material at job workers, registers, dashboard, subcontracting
worksheet and end-to-end traceability. Everything is read from the posted ledgers and documents."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone as dj_timezone

from inventory.models import JewelleryUnit
from inventory.services import find_unit

from . import compliance
from .models import (
    DeliveryChallan, EWayBill, JobWorkDispatch, JobWorkDispatchLine, JobWorkerStockEntry, JobWorkException, JobWorkOrder, JobWorkOrderLine,
    JobWorkProcessLine, JobWorkQCLine, JobWorkReceipt, JobWorkReceiptLine, JobWorkVendorInvoice,
)
from .pricing import charge, resolve_price
from .services import PRECIOUS, is_metal, jw_balance, material_wip

ZERO = Decimal('0')
SENT = ('RECEIVED_BY_JW', 'TRANSFER_IN', 'DIRECT_PURCHASE')


def _by_type(order):
    rows = JobWorkerStockEntry.objects.filter(order=order).values('order_line', 'entry_type').annotate(
        q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), f=Sum('fine_weight'), v=Sum('value'))
    table = defaultdict(lambda: defaultdict(lambda: {'q': ZERO, 'g': ZERO, 'n': ZERO, 'f': ZERO, 'v': ZERO}))
    for r in rows:
        bucket = table[r['order_line']][r['entry_type']]
        for key in ('q', 'g', 'n', 'f', 'v'):  # SQLite sums decimals as floats
            bucket[key] += Decimal(str(r[key] or 0)).quantize(Decimal('0.01') if key == 'v' else Decimal('0.001'))
    return table


def reconcile(order):
    """Opening (0) + sent + transfers in - consumed - scrap - loss - returned - transfers out = at job worker, per line,
    by quantity and by net / fine weight. Reversals are folded into the type they undo."""
    table = _by_type(order)
    in_transit = defaultdict(lambda: ZERO)
    for dl in JobWorkDispatchLine.objects.filter(dispatch__order=order, dispatch__status='IN_TRANSIT').exclude(dispatch__movement_type='JW_TO_PRINCIPAL'):
        in_transit[dl.order_line_id] += dl.quantity
    lines = []
    for line in order.lines.select_related('item', 'uom'):
        t = table.get(line.pk, {})
        get = lambda types, key: sum((t[x][key] for x in types if x in t), ZERO)
        row = {'line': line, 'metal': is_metal(line), 'in_transit': in_transit[line.pk]}
        for key, label in (('q', 'qty'), ('n', 'net'), ('f', 'fine'), ('v', 'value')):
            sent = get(SENT, key)
            row[f'sent_{label}'] = sent
            row[f'consumed_{label}'] = -get(('CONSUMED',), key)
            row[f'output_{label}'] = get(('OUTPUT',), key)
            scrap = get(('SCRAP',), key)  # negative on the input it came from, positive on the scrap item it became
            row[f'scrap_{label}'] = -scrap if line.is_inbound else scrap
            row[f'loss_{label}'] = -get(('LOSS',), key)
            row[f'returned_{label}'] = -get(('RETURN_DISPATCHED',), key)
            row[f'transferred_{label}'] = -get(('TRANSFER_OUT',), key)
            row[f'adjusted_{label}'] = get(('ADJUSTMENT', 'REVERSAL'), key)
            row[f'balance_{label}'] = sum((t[x][key] for x in t), ZERO)
        if order.is_production and line.is_inbound:
            row['scrap_qty_memo'] = line.scrap_qty  # production scrap comes back as the component material
        lines.append(row)
    inbound_metal = [r for r in lines if r['line'].is_inbound and r['metal']]
    output_metal = [r for r in lines if r['line'].line_type in ('OUTPUT', 'BY_PRODUCT') and r['line'].metal in PRECIOUS]
    total = lambda rows, key: sum((r[key] for r in rows), ZERO)
    sent = total(inbound_metal, 'sent_net')
    loss = total(inbound_metal, 'loss_net')
    metal = {
        'sent': sent, 'consumed': total(inbound_metal, 'consumed_net'), 'scrap': total(inbound_metal, 'scrap_net'), 'loss': loss,
        'returned': total(inbound_metal, 'returned_net'), 'transferred': total(inbound_metal, 'transferred_net'),
        'at_job_worker': total(inbound_metal, 'balance_net'), 'output_net': total(output_metal, 'output_net'),
        'sent_fine': total(inbound_metal, 'sent_fine'), 'consumed_fine': total(inbound_metal, 'consumed_fine'),
        'output_fine': total(output_metal, 'output_fine'),
        'loss_percent': (loss * 100 / sent).quantize(Decimal('0.001')) if sent else ZERO,
    }
    metal['accounted'] = metal['consumed'] + metal['scrap'] + metal['loss'] + metal['returned'] + metal['transferred']
    metal['difference'] = metal['sent'] - metal['accounted']
    metal['fine_variance'] = metal['output_fine'] - metal['consumed_fine']
    tolerance = Decimal('0.001')
    status = 'RECONCILED' if abs(metal['difference']) <= tolerance and not any(r['balance_qty'] for r in lines) else \
        ('OPEN' if order.status not in ('COMPLETED', 'CLOSED') else 'EXCEPTION')
    return {'lines': lines, 'metal': metal, 'status': status, 'wip_value': material_wip(order),
            'within_tolerance': not order.max_loss_percent or metal['loss_percent'] <= order.max_loss_percent}


def job_worker_reconciliation(job_worker, *, start=None, end=None):
    """Per item: opening + sent + transfers in - consumed - returned - scrap - loss - transfers out = closing (qty and net g)."""
    end = end or dj_timezone.localdate()
    base = JobWorkerStockEntry.objects.filter(job_worker=job_worker)
    items = defaultdict(lambda: defaultdict(lambda: {'q': ZERO, 'n': ZERO}))
    for r in base.filter(posting_date__lte=end).values('item__item_no', 'entry_type', 'posting_date', 'quantity', 'net_weight'):
        key = 'opening' if start and r['posting_date'] < start else r['entry_type']
        items[r['item__item_no']][key]['q'] += r['quantity']
        items[r['item__item_no']][key]['n'] += r['net_weight']
    result = []
    for item_no, t in sorted(items.items()):
        row = {'item': item_no}
        for key in ('q', 'n'):
            val = lambda *types: sum((t[x][key] for x in types if x in t), ZERO)
            row[f'opening_{key}'] = val('opening')
            row[f'sent_{key}'] = val('RECEIVED_BY_JW', 'DIRECT_PURCHASE')
            row[f'transfer_in_{key}'] = val('TRANSFER_IN')
            row[f'produced_{key}'] = val('OUTPUT') + (val('SCRAP') if val('SCRAP') > 0 else ZERO)
            row[f'consumed_{key}'] = -val('CONSUMED')
            row[f'returned_{key}'] = -val('RETURN_DISPATCHED')
            row[f'scrap_{key}'] = -val('SCRAP') if val('SCRAP') < 0 else ZERO
            row[f'loss_{key}'] = -val('LOSS')
            row[f'transfer_out_{key}'] = -val('TRANSFER_OUT')
            row[f'other_{key}'] = val('ADJUSTMENT', 'REVERSAL')
            row[f'closing_{key}'] = sum((t[x][key] for x in t), ZERO)
        result.append(row)
    return result


def material_at_job_workers(tenant, *, job_worker=None, item=None, metal=None, order=None, huid=None, today=None):
    """The mandatory 'material at job worker' report: what each job worker holds, since when, due when, how old."""
    today = today or dj_timezone.localdate()
    qs = JobWorkerStockEntry.objects.filter(tenant=tenant)
    if job_worker is not None:
        qs = qs.filter(job_worker=job_worker)
    if item is not None:
        qs = qs.filter(item=item)
    if metal:
        qs = qs.filter(metal=metal)
    if order is not None:
        qs = qs.filter(order=order)
    if huid:
        qs = qs.filter(huid=huid.upper())
    rows = qs.values('job_worker', 'job_worker__code', 'job_worker__legal_name', 'order', 'order__order_no', 'order__first_dispatch_date',
                     'order__compliance_due_date', 'order__production_order__order_no', 'item__item_no', 'item__description', 'metal', 'purity',
                     'owner').annotate(q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), f=Sum('fine_weight'), v=Sum('value'),
                                       units=Count('jewellery_unit', distinct=True)).order_by('job_worker__code', 'order__order_no')
    result = []
    for r in rows:
        for key in ('q', 'g', 'n', 'f'):
            r[key] = Decimal(str(r[key] or 0)).quantize(Decimal('0.001'))
        r['v'] = Decimal(str(r['v'] or 0)).quantize(Decimal('0.01'))
        if not r['q'] and not r['n']:
            continue
        dispatched = r['order__first_dispatch_date']
        band = compliance.alert_band(tenant, dispatched, r['order__compliance_due_date'], today) if dispatched else None
        result.append({**r, 'age_days': (today - dispatched).days if dispatched else None, 'band': band})
    return result


def job_work_register(tenant, *, start=None, end=None, job_worker=None):
    """Statutory / operational register: one row per challan line with return, e-way bill, receipt and ITC-04 position."""
    challans = DeliveryChallan.objects.filter(tenant=tenant).select_related('dispatch__order__job_worker', 'dispatch__to_job_worker',
                                                                             'dispatch__from_job_worker')
    if start:
        challans = challans.filter(challan_date__gte=start)
    if end:
        challans = challans.filter(challan_date__lte=end)
    if job_worker is not None:
        challans = challans.filter(Q(dispatch__to_job_worker=job_worker) | Q(dispatch__from_job_worker=job_worker))
    rows = []
    for challan in challans.order_by('challan_date', 'id'):
        dispatch = challan.dispatch
        order = dispatch.order
        worker = dispatch.to_job_worker or dispatch.from_job_worker
        eway = dispatch.eway_bills.exclude(status='CANCELLED').first()
        receipts = ', '.join(dispatch.receipts.values_list('receipt_no', flat=True))
        from .models import ITC04Line
        itc = ITC04Line.objects.filter(tenant=tenant, challan_no=challan.challan_no).select_related('itc04').first()
        for cl in challan.lines.all():
            rows.append({'challan': challan, 'line': cl, 'order': order, 'job_worker': worker, 'movement': dispatch.get_movement_type_display(),
                         'dispatch': dispatch, 'eway_bill': eway, 'receipts': receipts, 'return_date': order.actual_return_date,
                         'due_date': challan.return_due_date or order.compliance_due_date,
                         'itc04': f'{itc.itc04.period_code} {itc.get_match_status_display()}' if itc else 'Not yet reported'})
    return rows


def dashboard(tenant, today=None):
    today = today or dj_timezone.localdate()
    orders = JobWorkOrder.objects.filter(tenant=tenant)
    open_orders = orders.filter(status__in=JobWorkOrder.OPEN_STATUSES)
    by_status = dict(orders.values_list('status').annotate(n=Count('id')))
    held = JobWorkerStockEntry.objects.filter(tenant=tenant)
    metal_held = {m: held.filter(metal=m).aggregate(n=Sum('net_weight'), f=Sum('fine_weight'))
                  for m in ('GOLD', 'SILVER', 'PLATINUM')}
    stones = held.filter(item__base_uom__code='CT').aggregate(q=Sum('quantity'))['q'] or ZERO
    units = held.filter(jewellery_unit__isnull=False).values('jewellery_unit').annotate(q=Sum('quantity')).filter(q__gt=0).count()
    due_rows = material_at_job_workers(tenant, today=today)
    uninvoiced = open_orders.filter(expected_charge__gt=0).exclude(status__in=('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH',
                                                                                'DISPATCH_CREATED', 'IN_TRANSIT'))
    invoices = JobWorkVendorInvoice.objects.filter(tenant=tenant, status='POSTED')
    exceptions = JobWorkException.objects.filter(tenant=tenant, status__in=JobWorkException.OPEN_STATUSES)
    return {
        'operations': {
            'open': open_orders.count(), 'at_job_workers': open_orders.filter(status__in=('RECEIVED_BY_JOB_WORKER', 'PROCESSING',
                                                                                        'PARTIALLY_COMPLETED', 'READY_FOR_RETURN')).count(),
            'in_transit': JobWorkDispatch.objects.filter(tenant=tenant, status='IN_TRANSIT').count(),
            'overdue': open_orders.filter(expected_return_date__lt=today).count(),
            'pending_dispatch': open_orders.filter(status__in=('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH', 'DISPATCH_CREATED')).count(),
            'pending_receipt': JobWorkDispatch.objects.filter(tenant=tenant, movement_type='JW_TO_PRINCIPAL',
                                                              status__in=('IN_TRANSIT', 'PARTIALLY_RECEIVED')).count(),
            'pending_qc': JobWorkReceipt.objects.filter(tenant=tenant, status='POSTED', qc_status='PENDING').count(),
            'pending_approval': by_status.get('PENDING_APPROVAL', 0),
        },
        'inventory': {
            'gold_net': metal_held['GOLD']['n'] or ZERO, 'gold_fine': metal_held['GOLD']['f'] or ZERO,
            'silver_net': metal_held['SILVER']['n'] or ZERO, 'platinum_net': metal_held['PLATINUM']['n'] or ZERO, 'stones_ct': stones,
            'units': units, 'value': held.aggregate(v=Sum('value'))['v'] or ZERO,
        },
        'compliance': {
            'due_soon': sum(1 for r in due_rows if r['band'] and r['band']['level'] == 'DUE_SOON'),
            'overdue': sum(1 for r in due_rows if r['band'] and r['band']['level'] == 'OVERDUE'),
            'no_rule': sum(1 for r in due_rows if r['band'] and r['band']['no_rule']),
            'eway_pending': EWayBill.objects.filter(tenant=tenant, status='PENDING').count(),
            'dc_exceptions': exceptions.filter(exception_type__in=('MISSING_DC', 'GSTIN_MISMATCH', 'HSN_MISMATCH')).count(),
        },
        'finance': {
            'uninvoiced': uninvoiced.count(), 'uninvoiced_value': uninvoiced.aggregate(v=Sum('expected_charge'))['v'] or ZERO,
            'invoiced': invoices.aggregate(v=Sum('taxable_value'))['v'] or ZERO,
            'loss_value': JobWorkerStockEntry.objects.filter(tenant=tenant, entry_type='LOSS').aggregate(v=Sum('value'))['v'] or ZERO,
            'exceptions': exceptions.count(), 'blocking': exceptions.filter(blocking=True).count(),
        },
        'by_status': by_status,
    }


def subcontracting_worksheet(tenant, *, on_date=None):
    """Released production operations that are subcontracted and have no job work order yet (Business Central's worksheet)."""
    from manufacturing.models import ProductionOrder, ProductionOrderRoutingLine
    from .models import JobWorker
    on_date = on_date or dj_timezone.localdate()
    ops = ProductionOrderRoutingLine.objects.filter(tenant=tenant, subcontracting=True, is_rework=False,
                                                    order__status__in=ProductionOrder.EXECUTION_STATUSES) \
        .exclude(status__in=('COMPLETED', 'QC_PASSED')).select_related('order__item', 'subcontractor', 'work_center')
    taken = set(JobWorkOrder.objects.filter(tenant=tenant, operation__in=ops).exclude(status__in=('CANCELLED',))
                .exclude(rework_of__isnull=False).values_list('operation_id', flat=True))
    rows = []
    for op in ops:
        if op.pk in taken:
            continue
        order = op.order
        worker = JobWorker.objects.filter(tenant=tenant, subcontractor=op.subcontractor, active=True).first() if op.subcontractor_id else None
        components = list(order.components.filter(routing_link_code=op.routing_link_code)) if op.routing_link_code else []
        ready = all(c.issued_open_qty >= c.expected_qty - c.consumed_qty for c in components) if components else True
        price = resolve_price(tenant, worker, operation_code=op.routing_link_code or op.work_center.code, item=order.item, on_date=on_date,
                              quantity=order.planned_qty) if worker else None
        rows.append({'operation': op, 'order': order, 'job_worker': worker, 'material_ready': ready, 'components': components,
                     'required_date': order.due_date, 'expected_dispatch': on_date,
                     'expected_return': on_date + timedelta(days=worker.lead_time_days) if worker else None,
                     'quantity': order.planned_qty,
                     'estimated_cost': charge(price.rate_basis, price.rate, price.minimum_amount, pieces=order.planned_qty,
                                              grams=order.expected_net_weight * order.planned_qty) if price else None,
                     'status': 'READY' if ready and worker else ('NO_VENDOR' if not worker else 'MATERIAL_PENDING')})
    return rows


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------

def trace_order(order):
    """Everything about one job work order, both directions: source, documents, material, outputs, cost."""
    return {
        'order': order, 'production_order': order.production_order, 'operation': order.operation, 'parent': order.parent,
        'children': list(order.children.all()), 'rework_of': order.rework_of, 'rework_orders': list(order.rework_orders.all()),
        'dispatches': list(order.dispatches.select_related('challan').prefetch_related('eway_bills', 'lines__item')),
        'outgoing_transfers': list(order.outgoing_transfers.select_related('order')),
        'reports': list(order.process_reports.prefetch_related('lines')), 'receipts': list(order.receipts.prefetch_related('lines')),
        'qcs': list(order.qcs.prefetch_related('lines')), 'invoices': list(order.invoices.select_related('tax_snapshot', 'finance_voucher')),
        'notes': list(order.notes.all()), 'exceptions': list(order.exceptions.all()),
        'stock': list(order.stock_entries.select_related('item', 'jewellery_unit').order_by('id')),
        'costs': list(order.cost_entries.order_by('id')), 'reconciliation': reconcile(order),
    }


def trace_unit(tenant, code):
    """Finished jewellery -> production order -> operation -> job work order -> job worker -> challan -> material -> loss -> QC -> cost,
    from a barcode / serial / HUID."""
    unit = find_unit(tenant, code)
    if unit is None:
        return None
    produced_by = JobWorkProcessLine.objects.filter(tenant=tenant, output_unit=unit).select_related('report__order').first()
    movements = JobWorkerStockEntry.objects.filter(tenant=tenant, jewellery_unit=unit).select_related('order__job_worker', 'dispatch').order_by('id')
    orders = {m.order for m in movements}
    if produced_by is not None:
        orders.add(produced_by.report.order)
    inputs = []
    if produced_by is not None:
        report = produced_by.report
        inputs = list(JobWorkProcessLine.objects.filter(report=report, kind__in=('OUTPUT', 'CONSUMPTION', 'SCRAP', 'LOSS'))
                      .select_related('input_line__item', 'input_unit'))
    challans = DeliveryChallan.objects.filter(tenant=tenant, lines__huid=unit.huid or '__none__').distinct() if unit.huid else \
        DeliveryChallan.objects.filter(tenant=tenant, dispatch__lines__jewellery_unit=unit).distinct()
    qc = JobWorkQCLine.objects.filter(tenant=tenant, receipt_line__jewellery_unit=unit).select_related('qc')
    from manufacturing.models import OutputUnit
    production = OutputUnit.objects.filter(tenant=tenant, jewellery_unit=unit).select_related('order').first()
    return {'unit': unit, 'produced_by': produced_by, 'inputs': inputs, 'movements': list(movements), 'orders': sorted(orders, key=lambda o: o.pk),
            'challans': list(challans), 'qc': list(qc), 'production': production,
            'current': {'location': unit.current_location, 'status': unit.get_status_display(),
                        'at_job_worker': getattr(unit.current_location, 'job_worker_link', None) if unit.current_location else None}}


def trace_lot(tenant, item, lot_no=''):
    """Reverse trace: a material batch -> dispatches -> job workers -> job work orders -> outputs."""
    lines = JobWorkOrderLine.objects.filter(tenant=tenant, item=item, line_type__in=JobWorkOrderLine.INBOUND_TYPES)
    if lot_no:
        lines = lines.filter(lot_no=lot_no)
    result = []
    for line in lines.select_related('order__job_worker'):
        outputs = JobWorkProcessLine.objects.filter(input_line=line, kind='OUTPUT', report__status='POSTED').select_related('result_line__item',
                                                                                                                             'output_unit')
        result.append({'line': line, 'order': line.order, 'job_worker': line.order.job_worker,
                       'dispatches': list(line.dispatch_lines.select_related('dispatch__challan')), 'outputs': list(outputs)})
    return result
