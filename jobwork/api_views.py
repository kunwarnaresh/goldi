"""Job work REST API. Authentication and permission come from DRF settings; the tenant always comes from the authenticated
user's workspace (never the payload); ids of other tenants 404. Every POST honours an `Idempotency-Key` header and runs in
the services' own transaction; every action is audited by the services."""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils.dateparse import parse_date
from rest_framework.decorators import api_view
from rest_framework.response import Response

from inventory.engine import InventoryError
from inventory.models import Item, JewelleryUnit, Location

from . import compliance, finance, reports
from . import services as svc
from .models import (
    ITC04Return, JobWorkDispatch, JobWorker, JobWorkIdempotencyKey, JobWorkOrder, JobWorkOrderLine, JobWorkReceipt, WeightCapture,
)
from .security import actor_from_request, require
from .views import dec


def jobwork_api(view):
    """Actor from the session; posting errors -> 400, permission -> 403; POST replays of an Idempotency-Key return the stored reply."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        actor = actor_from_request(request)
        actor.source = 'API'
        key = request.headers.get('Idempotency-Key', '').strip()[:80] if request.method == 'POST' else ''
        if key:
            stored = JobWorkIdempotencyKey.objects.filter(tenant=actor.tenant, key=key).first()
            if stored is not None:
                if stored.endpoint != request.path:
                    return Response({'error': 'Idempotency-Key was already used for another request.'}, status=409)
                return Response(stored.response_body, status=stored.response_status, headers={'Idempotent-Replay': 'true'})
        try:
            response = view(request, actor, *args, **kwargs)
        except InventoryError as exc:
            return Response({'error': str(exc)}, status=400)
        except PermissionDenied as exc:
            return Response({'error': str(exc) or 'Forbidden.'}, status=403)
        except (KeyError, ValueError) as exc:
            return Response({'error': f'Invalid request: {exc}'}, status=400)
        if key and 200 <= response.status_code < 300:  # only successful replies are replayed
            try:
                with transaction.atomic():
                    JobWorkIdempotencyKey.objects.create(tenant=actor.tenant, key=key, endpoint=request.path,
                                                         response_status=response.status_code, response_body=response.data)
            except IntegrityError:
                pass
        return response
    return wrapper


def obj(model, actor, pk):
    try:
        return model.objects.get(tenant=actor.tenant, pk=int(pk))
    except (model.DoesNotExist, ValueError, TypeError):
        raise Http404(f'{model.__name__} not found.')


def num(value):
    return str(value) if value is not None else None


def worker_json(w):
    return {'id': w.pk, 'code': w.code, 'legal_name': w.legal_name, 'trade_name': w.trade_name, 'gstin': w.gstin, 'state_code': w.state_code,
            'registration_type': w.registration_type, 'location': w.default_location.code if w.default_location else None,
            'specializations': w.specializations, 'lead_time_days': w.lead_time_days, 'compliance_status': w.compliance_status,
            'default_loss_percent': num(w.default_loss_percent), 'max_loss_percent': num(w.max_loss_percent), 'active': w.active,
            'blocked': w.blocked}


def order_json(o, detail=False):
    data = {'id': o.pk, 'order_no': o.order_no, 'kind': o.order_kind, 'transaction_type': o.transaction_type, 'status': o.status,
            'job_worker': o.job_worker.code, 'production_order': o.production_order.order_no if o.production_order_id else None,
            'operation': o.operation.operation_no if o.operation_id else None, 'parent': o.parent.order_no if o.parent_id else None,
            'source_location': o.source_location.code, 'job_worker_location': o.job_worker_location.code,
            'order_date': o.order_date, 'expected_return_date': o.expected_return_date, 'compliance_due_date': o.compliance_due_date,
            'rate_basis': o.rate_basis, 'rate': num(o.rate), 'minimum_charge': num(o.minimum_charge), 'expected_charge': num(o.expected_charge),
            'material_value': num(o.material_value), 'invoiced_amount': num(o.invoiced_amount)}
    if detail:
        rec = reports.reconcile(o)
        data['lines'] = [{'id': r['line'].pk, 'line_no': r['line'].line_no, 'type': r['line'].line_type, 'item': r['line'].item.item_no if r['line'].item_id else None,
                          'supply_method': r['line'].supply_method, 'quantity': num(r['line'].quantity), 'sent': num(r['sent_qty']),
                          'consumed': num(r['consumed_qty']), 'produced': num(r['output_qty']), 'scrap': num(r['scrap_qty']), 'loss': num(r['loss_qty']),
                          'returned': num(r['returned_qty']), 'at_job_worker': num(r['balance_qty']), 'net_at_job_worker': num(r['balance_net'])}
                         for r in rec['lines']]
        data['metal_reconciliation'] = {k: num(v) for k, v in rec['metal'].items()}
        data['reconciliation_status'] = rec['status']
        data['dispatches'] = [dispatch_json(d) for d in o.dispatches.exclude(status='DRAFT')]
        data['exceptions'] = [{'exception_no': e.exception_no, 'type': e.exception_type, 'status': e.status, 'blocking': e.blocking,
                               'description': e.description} for e in o.exceptions.all()]
    return data


def dispatch_json(d):
    challan = getattr(d, 'challan', None)
    return {'id': d.pk, 'dispatch_no': d.dispatch_no, 'movement': d.movement_type, 'status': d.status, 'date': d.dispatch_date,
            'challan_no': challan.challan_no if challan else None, 'total_qty': num(d.total_qty), 'total_net': num(d.total_net),
            'total_value': num(d.total_value), 'eway_bill_required': d.eway_bill_required, 'eway_bill_reason': d.eway_bill_reason,
            'eway_bills': [{'id': e.pk, 'ewb_no': e.ewb_no, 'status': e.status} for e in d.eway_bills.all()],
            'compliance_due_date': d.compliance_due_date}


def _line(actor, order, pk):
    if pk in (None, ''):
        return None
    line = JobWorkOrderLine.objects.filter(order=order, pk=int(pk)).first()
    if line is None:
        raise Http404('Order line not found.')
    return line


def _unit(actor, pk):
    return obj(JewelleryUnit, actor, pk) if pk not in (None, '') else None


# ---------------------------------------------------------------------------
# Job workers
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@jobwork_api
def job_workers(request, actor):
    if request.method == 'POST':
        d = request.data
        worker = svc.create_job_worker(actor, code=d['code'], legal_name=d['legal_name'], trade_name=d.get('trade_name', ''),
                                       gstin=(d.get('gstin') or '').upper(), gst_registered=bool(d.get('gstin')), state=d.get('state', ''),
                                       city=d.get('city', ''), address_1=d.get('address', ''), default_sac=d.get('default_sac', ''),
                                       lead_time_days=int(d.get('lead_time_days') or 7), specializations=d.get('specializations') or [],
                                       default_loss_percent=dec(d.get('default_loss_percent'), 0), max_loss_percent=dec(d.get('max_loss_percent'), 0))
        return Response(worker_json(worker), status=201)
    require(actor, 'view')
    return Response({'results': [worker_json(w) for w in JobWorker.objects.filter(tenant=actor.tenant)]})


@api_view(['GET'])
@jobwork_api
def job_worker_stock(request, actor, pk):
    require(actor, 'view')
    worker = obj(JobWorker, actor, pk)
    rows = reports.material_at_job_workers(actor.tenant, job_worker=worker)
    return Response({'job_worker': worker.code, 'results': [
        {'order': r['order__order_no'], 'item': r['item__item_no'], 'owner': r['owner'], 'quantity': num(r['q']), 'gross': num(r['g']),
         'net': num(r['n']), 'fine': num(r['f']), 'value': num(r['v']), 'dispatched': r['order__first_dispatch_date'],
         'due': r['order__compliance_due_date'], 'age_days': r['age_days'], 'band': r['band']} for r in rows],
        'reconciliation': [{k: num(v) if k != 'item' else v for k, v in row.items()} for row in reports.job_worker_reconciliation(worker)]})


@api_view(['GET'])
@jobwork_api
def job_worker_ledger(request, actor, pk):
    require(actor, 'view')
    worker = obj(JobWorker, actor, pk)
    entries = worker.stock_entries.select_related('item', 'order', 'jewellery_unit').order_by('-id')[:500]
    return Response({'results': [{'entry_no': e.entry_no, 'date': e.posting_date, 'type': e.entry_type, 'order': e.order.order_no,
                                  'document': e.document_no, 'item': e.item.item_no, 'unit': e.jewellery_unit.barcode if e.jewellery_unit_id else None,
                                  'huid': e.huid, 'quantity': num(e.quantity), 'net': num(e.net_weight), 'fine': num(e.fine_weight),
                                  'value': num(e.value), 'owner': e.owner, 'memo': e.memo, 'loss_class': e.loss_class} for e in entries]})


# ---------------------------------------------------------------------------
# Orders and their actions
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@jobwork_api
def orders(request, actor):
    if request.method == 'POST':
        d = request.data
        if d.get('production_operation'):
            from manufacturing.models import ProductionOrderRoutingLine
            op = obj(ProductionOrderRoutingLine, actor, d['production_operation'])
            worker = obj(JobWorker, actor, d['job_worker']) if d.get('job_worker') else None
            order = svc.create_from_production_operation(actor, op, job_worker=worker)
        else:
            lines = []
            for spec in d.get('lines') or []:
                item = Item.objects.filter(tenant=actor.tenant, item_no=spec.get('item_no')).first() if spec.get('item_no') else None
                if spec.get('item_no') and item is None:
                    raise InventoryError(f'Item {spec["item_no"]} not found.')
                lines.append({'line_type': spec.get('type', 'INPUT'), 'item': item, 'unit': _unit(actor, spec.get('unit')),
                              'quantity': dec(spec.get('quantity'), 0), 'gross_weight': dec(spec.get('gross_weight')),
                              'net_weight': dec(spec.get('net_weight')), 'supply_method': spec.get('supply_method', 'PRINCIPAL'),
                              'lot_no': spec.get('lot_no', '')})
            order = svc.create_order(actor, job_worker=obj(JobWorker, actor, d['job_worker']), source_location=obj(Location, actor, d['source_location']),
                                     lines=lines, transaction_type=d.get('transaction_type', 'JOB_WORK'), operation_code=d.get('operation_code', ''),
                                     goods_category=d.get('goods_category', 'INPUTS'), expected_return_date=parse_date(d.get('expected_return_date') or ''),
                                     parent=obj(JobWorkOrder, actor, d['parent']) if d.get('parent') else None, description=d.get('description', ''))
        if d.get('submit'):
            svc.submit_order(actor, order)
        return Response(order_json(JobWorkOrder.objects.get(pk=order.pk), detail=True), status=201)
    require(actor, 'view')
    qs = JobWorkOrder.objects.filter(tenant=actor.tenant).select_related('job_worker', 'production_order', 'operation', 'parent', 'source_location',
                                                                         'job_worker_location')
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    return Response({'results': [order_json(o) for o in qs[:300]]})


@api_view(['GET'])
@jobwork_api
def order_detail(request, actor, pk):
    require(actor, 'view')
    return Response(order_json(obj(JobWorkOrder, actor, pk), detail=True))


def _process(actor, order, kind, specs):
    lines = [{'kind': kind if kind != 'CONSUMPTION' else s.get('kind', 'CONSUMPTION'), 'input_line': _line(actor, order, s.get('input_line')),
              'input_qty': dec(s.get('input_qty'), 0), 'input_unit': _unit(actor, s.get('input_unit')),
              'result_line': _line(actor, order, s.get('result_line')), 'quantity': dec(s.get('quantity'), 0),
              'gross_weight': dec(s.get('gross_weight')), 'net_weight': dec(s.get('net_weight')), 'stone_weight': dec(s.get('stone_weight')),
              'barcode': s.get('barcode', ''), 'serial_no': s.get('serial_no', ''), 'huid': s.get('huid', ''), 'purity': s.get('purity', ''),
              'loss_class': s.get('loss_class', ''), 'scrap_disposition': s.get('scrap_disposition', ''),
              'recoverable_value': dec(s.get('recoverable_value'), 0), 'reason': s.get('reason', '')} for s in specs]
    return svc.post_process_report(actor, svc.create_process_report(actor, order, lines=lines))


@api_view(['POST'])
@jobwork_api
def order_action(request, actor, pk, action):
    order = obj(JobWorkOrder, actor, pk)
    d = request.data
    transport = {k: d.get(k, '') for k in ('vehicle_no', 'transporter', 'transporter_id')}
    if action == 'submit':
        svc.submit_order(actor, order)
    elif action == 'approve':
        svc.approve_order(actor, order)
    elif action == 'reserve':
        _, short = svc.reserve_material(actor, order)
        return Response({**order_json(JobWorkOrder.objects.get(pk=order.pk)), 'shortages': short})
    elif action == 'dispatch':
        lines = [{'order_line': _line(actor, order, l['order_line']), 'quantity': dec(l.get('quantity'), 0), 'unit': _unit(actor, l.get('unit')),
                  'gross_weight': dec(l.get('gross_weight')), 'net_weight': dec(l.get('net_weight')),
                  'weight_capture': l.get('weight_capture')} for l in d['lines']] if d.get('lines') else None
        dispatch = svc.create_dispatch(actor, order, lines=lines, barcodes=d.get('barcodes') or None, **transport)
        if d.get('post', True):
            dispatch = svc.post_dispatch(actor, dispatch)
        return Response(dispatch_json(dispatch), status=201)
    elif action == 'delivery-challan':
        return Response({'results': [{'challan_no': d.challan.challan_no, 'date': d.challan.challan_date, 'reason': d.challan.reason,
                                      'status': d.challan.status, 'dispatch': d.dispatch_no, 'value': num(d.challan.total_value)}
                                     for d in order.dispatches.filter(challan__isnull=False).select_related('challan')]})
    elif action == 'ewaybill':
        dispatch = obj(JobWorkDispatch, actor, d['dispatch'])
        op = d.get('operation', 'generate')
        eway = dispatch.eway_bills.exclude(status='CANCELLED').first() if op != 'generate' else dispatch.eway_bills.filter(status='PENDING').first()
        if eway is None or dispatch.order_id != order.id:
            raise InventoryError('No e-way bill in the right state for this dispatch.')
        if op == 'generate':
            compliance.generate_eway_bill(actor, eway, ewb_no=d.get('ewb_no', ''), valid_until=None, vehicle_no=d.get('vehicle_no'))
        elif op == 'cancel':
            compliance.cancel_eway_bill(actor, eway, reason=d.get('reason', ''))
        elif op == 'update_vehicle':
            compliance.update_eway_vehicle(actor, eway, vehicle_no=d['vehicle_no'], reason=d.get('reason', ''))
        return Response(dispatch_json(dispatch))
    elif action == 'deliver':
        svc.deliver_dispatch(actor, obj(JobWorkDispatch, actor, d['dispatch']))
    elif action == 'return':
        dispatch = svc.post_dispatch(actor, svc.create_return(actor, order, barcodes=d.get('barcodes') or None, **transport))
        return Response(dispatch_json(dispatch), status=201)
    elif action == 'receipt':
        dispatch = obj(JobWorkDispatch, actor, d['dispatch'])
        lines = [{'dispatch_line': dispatch.lines.get(pk=int(l['dispatch_line'])), 'quantity': dec(l.get('quantity'), 0),
                  'gross_weight': dec(l.get('gross_weight')), 'net_weight': dec(l.get('net_weight'))} for l in d['lines']] if d.get('lines') else None
        receipt = svc.receive_return(actor, dispatch, lines=lines)
        return Response({'receipt_no': receipt.receipt_no, 'qc_status': receipt.qc_status,
                         'lines': [{'id': l.pk, 'item': l.item.item_no, 'quantity': num(l.quantity), 'qc_pending': num(l.qc_pending_qty),
                                    'weight_variance': num(l.weight_variance)} for l in receipt.lines.all()]}, status=201)
    elif action in ('consumption', 'scrap', 'loss', 'process'):
        kind = {'consumption': 'CONSUMPTION', 'scrap': 'SCRAP', 'loss': 'LOSS', 'process': 'CONSUMPTION'}[action]
        report = _process(actor, order, kind, d['lines'])
        return Response({'report_no': report.report_no, 'summary': report.summary}, status=201)
    elif action == 'qc':
        receipt = obj(JobWorkReceipt, actor, d['receipt'])
        results = [{'receipt_line': receipt.lines.get(pk=int(l['receipt_line'])), **{k: dec(l.get(k), 0) for k in ('accepted', 'rejected', 'rework', 'hold')},
                    'purity_result': l.get('purity_result', ''), 'huid_ok': l.get('huid_ok', True), 'defect': l.get('defect', '')} for l in d['lines']]
        qc = svc.record_qc(actor, receipt, results=results, checks=d.get('checks') or {}, remarks=d.get('remarks', ''))
        return Response({'qc_no': qc.qc_no, 'result': qc.result, 'rework_order': qc.rework_order.order_no if qc.rework_order else None}, status=201)
    elif action == 'complete':
        finance.complete_order(actor, order)
    elif action == 'close':
        finance.close_order(actor, order)
    elif action == 'invoice':
        invoice = finance.create_vendor_invoice(actor, order, vendor_invoice_no=d['vendor_invoice_no'],
                                                invoice_date=parse_date(d.get('invoice_date') or '') or order.order_date,
                                                billed_quantity=dec(d.get('billed_quantity'), 0), rate=dec(d.get('rate')),
                                                taxable_value=dec(d.get('taxable_value')), cgst=dec(d.get('cgst'), 0), sgst=dec(d.get('sgst'), 0),
                                                igst=dec(d.get('igst'), 0), cess=dec(d.get('cess'), 0), irn=d.get('irn', ''))
        return Response({'document_no': invoice.document_no, 'status': invoice.status, 'match': invoice.match_result}, status=201)
    else:
        raise Http404('Unknown action.')
    return Response(order_json(JobWorkOrder.objects.get(pk=order.pk), detail=True))


# ---------------------------------------------------------------------------
# Compliance and devices
# ---------------------------------------------------------------------------

@api_view(['GET'])
@jobwork_api
def itc04(request, actor):
    require(actor, 'compliance')
    record = ITC04Return.objects.filter(tenant=actor.tenant, period_code=request.GET['period']).first() if request.GET.get('period') \
        else ITC04Return.objects.filter(tenant=actor.tenant).first()
    if record is None:
        return Response({'results': [], 'periods': list(ITC04Return.objects.filter(tenant=actor.tenant).values_list('period_code', flat=True))})
    return Response({'reference_no': record.reference_no, 'period': record.period_code, 'frequency': record.frequency, 'status': record.status,
                     'summary': record.summary, 'lines': [{'table': l.table, 'gstin': l.job_worker_gstin, 'state': l.job_worker_state_code,
                                                           'challan_no': l.challan_no, 'challan_date': l.challan_date,
                                                           'original_challan_no': l.original_challan_no, 'hsn': l.hsn_code, 'item': l.item_no,
                                                           'uom': l.uom, 'quantity': num(l.quantity), 'loss_quantity': num(l.loss_quantity),
                                                           'taxable_value': num(l.taxable_value), 'category': l.goods_category, 'match': l.match_status}
                                                          for l in record.lines.all()]})


@api_view(['GET'])
@jobwork_api
def compliance_status(request, actor):
    require(actor, 'view')
    rows = reports.material_at_job_workers(actor.tenant)
    return Response({'disclaimer': compliance.TAX_DISCLAIMER, 'dashboard': reports.dashboard(actor.tenant)['compliance'], 'results': [
        {'job_worker': r['job_worker__code'], 'order': r['order__order_no'], 'item': r['item__item_no'], 'quantity': num(r['q']),
         'net': num(r['n']), 'value': num(r['v']), 'dispatched': r['order__first_dispatch_date'], 'due': r['order__compliance_due_date'],
         'band': r['band']} for r in rows]})


@api_view(['POST'])
@jobwork_api
def weight_capture(request, actor):
    """Digital weighing scale push: the reading can then be referenced (once) by a dispatch line instead of a typed weight."""
    require(actor, 'execute')
    d = request.data
    capture = WeightCapture.objects.create(tenant=actor.tenant, device_id=d['device_id'][:60], weight=dec(d['weight']),
                                           unit=(d.get('unit') or 'GM')[:5], operator=actor.user, transaction_ref=d.get('transaction_ref', '')[:60],
                                           created_by=actor.user)
    return Response({'id': capture.pk, 'weight': num(capture.weight), 'captured_at': capture.captured_at}, status=201)
