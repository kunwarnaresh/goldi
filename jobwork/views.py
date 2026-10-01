"""Job Work & Subcontracting screens. Every action calls the services; nothing here writes stock, tax or ledgers."""
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from inventory.engine import InventoryError
from inventory.models import Item, Location

from . import compliance, finance, pricing, reports
from . import services as svc
from .models import (
    ComplianceRule, DeliveryChallan, EWayBill, ITC04Return, JobWorkDispatch, JobWorker, JobWorkerPrice, JobWorkException, JobWorkOrder,
    JobWorkReceipt, JobWorkVendorInvoice, TaxRate, TRANSACTION_TYPES,
)
from .security import actor_from_request, can, get_setup, permission_map

MENU = [
    ('Dashboard', [('jobwork_dashboard', 'Job work dashboard'), ('jobwork_board', 'Status board'), ('jobwork_material', 'Material at job workers'),
                   ('jobwork_exceptions', 'Exceptions & approvals')]),
    ('Masters', [('jobwork_workers', 'Job workers'), ('jobwork_prices', 'Job worker price list'), ('jobwork_tax_rates', 'GST / HSN-SAC master'),
                 ('jobwork_rules', 'Compliance rules')]),
    ('Transactions', [('jobwork_orders', 'Job work & subcontract orders'), ('jobwork_order_new', 'New job work order'),
                      ('jobwork_dispatches', 'Dispatches & challans'), ('jobwork_invoices', 'Vendor invoices')]),
    ('Planning', [('jobwork_worksheet', 'Subcontracting worksheet')]),
    ('Compliance', [('jobwork_register', 'Job work register'), ('jobwork_compliance', 'Goods at job worker / due dates'),
                    ('jobwork_itc04', 'ITC-04 preparation')]),
    ('Reports', [('jobwork_trace', 'Traceability')]),
]


def menu_links():
    return [(group, [(reverse(name), label) for name, label in entries]) for group, entries in MENU]


def jobwork_view(view):
    @login_required(login_url='login')
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        actor = actor_from_request(request)
        if not can(actor, 'view'):
            raise PermissionDenied('You do not have access to job work.')
        try:
            return view(request, actor, *args, **kwargs)
        except (InventoryError, PermissionDenied) as exc:
            if request.method != 'POST':
                raise PermissionDenied(str(exc)) if isinstance(exc, PermissionDenied) else Http404(str(exc))
            messages.error(request, str(exc) or 'You do not have permission to do that.')
            return redirect(request.get_full_path())
    return wrapper


def page(request, actor, template, **context):
    context.setdefault('jw_menu', menu_links())
    context.setdefault('perms_jw', permission_map(actor))
    context.setdefault('tax_disclaimer', compliance.TAX_DISCLAIMER)
    return render(request, f'jobwork/{template}', context)


def get_obj(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=pk).first()
    if obj is None:
        raise Http404
    return obj


def dec(value, default=None):
    try:
        return Decimal(str(value).strip()) if str(value).strip() != '' else default
    except (InvalidOperation, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Dashboard, status board, material at job workers, exceptions
# ---------------------------------------------------------------------------

@jobwork_view
def dashboard(request, actor):
    data = reports.dashboard(actor.tenant)
    recent = JobWorkOrder.objects.filter(tenant=actor.tenant).select_related('job_worker')[:10]
    exceptions = JobWorkException.objects.filter(tenant=actor.tenant, status__in=JobWorkException.OPEN_STATUSES).select_related('order')[:10]
    return page(request, actor, 'dashboard.html', data=data, recent=recent, exceptions=exceptions, setup=get_setup(actor.tenant))


BOARD = [('Pending', ('DRAFT', 'PENDING_APPROVAL')), ('Approved / material', ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH', 'DISPATCH_CREATED')),
         ('In transit', ('IN_TRANSIT',)), ('At job worker', ('RECEIVED_BY_JOB_WORKER', 'PROCESSING', 'PARTIALLY_COMPLETED', 'TRANSFERRED')),
         ('Ready for return', ('READY_FOR_RETURN',)), ('Returning', ('RETURN_IN_TRANSIT', 'PARTIALLY_RETURNED')),
         ('Received / QC', ('RECEIVED', 'QC_PENDING', 'QC_PASSED', 'QC_FAILED')), ('Rework', ('REWORK',)), ('Completed', ('COMPLETED',))]


@jobwork_view
def board(request, actor):
    orders = JobWorkOrder.objects.filter(tenant=actor.tenant).exclude(status__in=('CLOSED', 'CANCELLED')).select_related('job_worker')
    today = timezone.localdate()
    columns = [(title, [o for o in orders if o.status in statuses]) for title, statuses in BOARD]
    columns.append(('Overdue', [o for o in orders if o.is_open and o.expected_return_date and o.expected_return_date < today]))
    return page(request, actor, 'board.html', columns=columns)


@jobwork_view
def material(request, actor):
    worker = get_obj(JobWorker, actor, request.GET['job_worker']) if request.GET.get('job_worker') else None
    rows = reports.material_at_job_workers(actor.tenant, job_worker=worker, metal=request.GET.get('metal') or None,
                                           huid=request.GET.get('huid') or None)
    return page(request, actor, 'material.html', rows=rows, workers=JobWorker.objects.filter(tenant=actor.tenant), worker=worker)


@jobwork_view
def exceptions(request, actor):
    qs = JobWorkException.objects.filter(tenant=actor.tenant).select_related('order', 'job_worker')
    if request.method == 'POST':
        exc = get_obj(JobWorkException, actor, request.POST['exception'])
        action, reason = request.POST.get('action'), request.POST.get('reason', '')
        if action == 'review':
            finance.review_exception(actor, exc, note=reason)
        elif action == 'approve':
            finance.approve_exception(actor, exc, resolution=request.POST.get('resolution', ''), reason=reason,
                                      recovery_amount=dec(request.POST.get('recovery_amount'), 0), loss_class=request.POST.get('loss_class', ''))
        elif action == 'reject':
            finance.reject_exception(actor, exc, reason=reason)
        elif action == 'resolve':
            finance.resolve_exception(actor, exc, resolution=request.POST.get('resolution', ''), note=reason)
        elif action == 'write_off':
            report = finance.write_off_difference(actor, exc)
            messages.success(request, f'Difference written off through {report.report_no}.')
        elif action == 'close':
            finance.close_exception(actor, exc)
        messages.success(request, f'{exc.exception_no}: {action.replace("_", " ")} done.')
        return redirect(request.get_full_path())
    status = request.GET.get('status', 'open')
    if status == 'open':
        qs = qs.filter(status__in=JobWorkException.OPEN_STATUSES)
    elif status:
        qs = qs.filter(status=status)
    return page(request, actor, 'exceptions.html', exceptions=qs[:300], status=status, resolutions=JobWorkException.RESOLUTIONS,
                loss_classes=JobWorkException._meta.get_field('loss_class').choices)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@jobwork_view
def order_list(request, actor):
    orders = JobWorkOrder.objects.filter(tenant=actor.tenant).select_related('job_worker', 'production_order')
    status, q = request.GET.get('status', ''), request.GET.get('q', '').strip()
    if status == 'open':
        orders = orders.filter(status__in=JobWorkOrder.OPEN_STATUSES)
    elif status:
        orders = orders.filter(status=status)
    if q:
        orders = orders.filter(Q(order_no__icontains=q) | Q(job_worker__code__icontains=q) | Q(production_order__order_no__icontains=q))
    return page(request, actor, 'order_list.html', orders=orders[:300], status=status, q=q, statuses=JobWorkOrder.STATUSES)


@jobwork_view
def order_new(request, actor):
    if request.method == 'POST':
        worker = get_obj(JobWorker, actor, request.POST['job_worker'])
        location = get_obj(Location, actor, request.POST['source_location'])
        lines = []
        for index in range(1, 9):
            item_no = request.POST.get(f'item_{index}', '').strip()
            if not item_no:
                continue
            item = Item.objects.filter(tenant=actor.tenant, item_no=item_no).first()
            if item is None:
                raise InventoryError(f'Line {index}: item {item_no} not found.')
            lines.append({'line_type': request.POST.get(f'type_{index}', 'INPUT'), 'item': item, 'quantity': dec(request.POST.get(f'qty_{index}'), 0),
                          'gross_weight': dec(request.POST.get(f'gross_{index}')), 'net_weight': dec(request.POST.get(f'net_{index}')),
                          'supply_method': request.POST.get(f'supply_{index}', 'PRINCIPAL')})
        order = svc.create_order(actor, job_worker=worker, source_location=location, lines=lines,
                                 transaction_type=request.POST.get('transaction_type', 'JOB_WORK'),
                                 operation_code=request.POST.get('operation_code', '').strip().upper(),
                                 goods_category=request.POST.get('goods_category', 'INPUTS'),
                                 expected_return_date=parse_date(request.POST.get('expected_return_date') or ''),
                                 description=request.POST.get('description', ''), remarks=request.POST.get('remarks', ''))
        messages.success(request, f'{order.order_no} created.')
        return redirect('jobwork_order_detail', order.pk)
    return page(request, actor, 'order_new.html', workers=JobWorker.objects.filter(tenant=actor.tenant, active=True, blocked=False),
                locations=Location.objects.filter(tenant=actor.tenant, active=True).exclude(location_type__in=('TRANSIT', 'JOB_WORKER')),
                transaction_types=TRANSACTION_TYPES, rows=range(1, 7))


@jobwork_view
def order_detail(request, actor, pk):
    order = get_obj(JobWorkOrder, actor, pk)
    if request.method == 'POST':
        return _order_action(request, actor, order)
    trace = reports.trace_order(order)
    suggestions = []
    if order.status == 'DRAFT':
        output = order.lines.filter(line_type='OUTPUT').first()
        suggestions = pricing.suggest_job_workers(actor.tenant, operation_code=order.operation_code, item=output.item if output else None,
                                                  quantity=order.planned_quantity)
    return page(request, actor, 'order_detail.html', order=order, trace=trace, lines=order.lines.select_related('item', 'uom', 'jewellery_unit'),
                suggestions=suggestions, is_final=order.is_production and order.operation_id and svc._production_final(order),
                loss_classes=JobWorkException._meta.get_field('loss_class').choices, today=timezone.localdate())


def _order_action(request, actor, order):
    action = request.POST.get('action')
    reason = request.POST.get('reason', '')
    done = {'submit': lambda: svc.submit_order(actor, order), 'approve': lambda: svc.approve_order(actor, order),
            'reject': lambda: svc.reject_order(actor, order, reason=reason), 'cancel': lambda: svc.cancel_order(actor, order, reason=reason),
            'reserve': lambda: svc.reserve_material(actor, order), 'complete': lambda: finance.complete_order(actor, order),
            'close': lambda: finance.close_order(actor, order)}
    transport = {k: request.POST.get(k, '') for k in ('vehicle_no', 'transporter', 'transporter_id')}
    if action in done:
        done[action]()
        messages.success(request, f'{order.order_no}: {action} done.')
    elif action == 'dispatch':
        barcodes = [b for b in request.POST.get('barcodes', '').split() if b]
        dispatch = svc.post_dispatch(actor, svc.create_dispatch(actor, order, barcodes=barcodes or None, **transport))
        messages.success(request, f'{dispatch.dispatch_no} posted with challan {dispatch.challan.challan_no}'
                                  + (' - e-way bill required.' if dispatch.eway_bill_required else '.'))
    elif action == 'return':
        dispatch = svc.post_dispatch(actor, svc.create_return(actor, order, **transport))
        messages.success(request, f'Return {dispatch.dispatch_no} posted (challan {dispatch.challan.challan_no}).')
    elif action == 'deliver':
        svc.deliver_dispatch(actor, get_obj(JobWorkDispatch, actor, request.POST['dispatch']))
        messages.success(request, 'Receipt at the job worker recorded.')
    elif action == 'receive':
        receipt = svc.receive_return(actor, get_obj(JobWorkDispatch, actor, request.POST['dispatch']))
        messages.success(request, f'{receipt.receipt_no} posted' + (' - QC pending.' if receipt.qc_status == 'PENDING' else '.'))
    elif action == 'reverse_dispatch':
        svc.reverse_dispatch(actor, get_obj(JobWorkDispatch, actor, request.POST['dispatch']), reason=reason)
        messages.success(request, 'Dispatch reversed.')
    elif action == 'short_close':
        svc.short_close_return(actor, get_obj(JobWorkDispatch, actor, request.POST['dispatch']), reason=reason,
                               loss_class=request.POST.get('loss_class', ''))
        messages.warning(request, 'Shortage written off from transit - a short-return exception needs approval.')
    elif action == 'eway':
        eway = get_obj(EWayBill, actor, request.POST['eway'])
        compliance.generate_eway_bill(actor, eway, ewb_no=request.POST.get('ewb_no', '').strip(), vehicle_no=request.POST.get('vehicle_no') or None)
        messages.success(request, f'E-way bill {eway.ewb_no or request.POST.get("ewb_no")} recorded.')
    elif action == 'qc':
        receipt = get_obj(JobWorkReceipt, actor, request.POST['receipt'])
        results = []
        for rl in receipt.lines.filter(qc_pending_qty__gt=0):
            results.append({'receipt_line': rl, **{k: dec(request.POST.get(f'{k}_{rl.pk}'), 0) for k in ('accepted', 'rejected', 'rework', 'hold')},
                            'purity_result': request.POST.get(f'purity_{rl.pk}', ''), 'huid_ok': request.POST.get(f'huid_{rl.pk}') != 'bad',
                            'defect': request.POST.get(f'defect_{rl.pk}', '')})
        qc = svc.record_qc(actor, receipt, results=results, remarks=reason)
        messages.success(request, f'{qc.qc_no}: {qc.get_result_display()}' + (f' - rework order {qc.rework_order.order_no}.' if qc.rework_order else '.'))
    elif action == 'process':
        report = svc.post_process_report(actor, svc.create_process_report(actor, order, lines=_process_lines(request, actor, order),
                                                                          reference=request.POST.get('reference', '')))
        messages.success(request, f'{report.report_no} posted.')
    elif action == 'confirm_output':
        svc.confirm_final_output(actor, order, quantity=dec(request.POST.get('quantity'), 0))
        messages.success(request, 'Final output posted to the production order.')
    elif action == 'invoice':
        invoice = finance.create_vendor_invoice(
            actor, order, vendor_invoice_no=request.POST['vendor_invoice_no'], invoice_date=parse_date(request.POST.get('invoice_date') or '')
            or timezone.localdate(), billed_quantity=dec(request.POST.get('billed_quantity'), 0), rate=dec(request.POST.get('rate')),
            taxable_value=dec(request.POST.get('taxable_value')), cgst=dec(request.POST.get('cgst'), 0), sgst=dec(request.POST.get('sgst'), 0),
            igst=dec(request.POST.get('igst'), 0), freight=dec(request.POST.get('freight'), 0), irn=request.POST.get('irn', ''))
        messages.success(request, f'{invoice.document_no}: {invoice.get_status_display()}.')
    else:
        messages.error(request, 'Unknown action.')
    return redirect('jobwork_order_detail', order.pk)


def _process_lines(request, actor, order):
    lines = {str(l.pk): l for l in order.lines.all()}
    specs = []
    for index in range(1, 11):
        kind = request.POST.get(f'kind_{index}')
        if not kind or not request.POST.get(f'input_{index}'):
            continue
        specs.append({'kind': kind, 'input_line': lines.get(request.POST[f'input_{index}']), 'input_qty': dec(request.POST.get(f'input_qty_{index}'), 0),
                      'result_line': lines.get(request.POST.get(f'result_{index}', '')), 'quantity': dec(request.POST.get(f'qty_{index}'), 0),
                      'gross_weight': dec(request.POST.get(f'gross_{index}')), 'net_weight': dec(request.POST.get(f'net_{index}')),
                      'barcode': request.POST.get(f'barcode_{index}', ''), 'huid': request.POST.get(f'huid_{index}', ''),
                      'loss_class': request.POST.get(f'loss_class_{index}', ''), 'scrap_disposition': request.POST.get(f'disposition_{index}', ''),
                      'reason': request.POST.get(f'reason_{index}', '')})
    return specs


@jobwork_view
def challan(request, actor, pk):
    challan = get_obj(DeliveryChallan, actor, pk)
    return page(request, actor, 'challan.html', challan=challan, lines=challan.lines.all(), dispatch=challan.dispatch)


@jobwork_view
def dispatch_list(request, actor):
    dispatches = JobWorkDispatch.objects.filter(tenant=actor.tenant).exclude(status='DRAFT').select_related('order', 'challan', 'from_location',
                                                                                                            'to_location')
    return page(request, actor, 'dispatch_list.html', dispatches=dispatches[:300])


@jobwork_view
def invoice_list(request, actor):
    invoices = JobWorkVendorInvoice.objects.filter(tenant=actor.tenant).select_related('order', 'job_worker', 'tax_snapshot')
    if request.method == 'POST':
        invoice = get_obj(JobWorkVendorInvoice, actor, request.POST['invoice'])
        action = request.POST.get('action')
        {'match': finance.match_invoice, 'approve': finance.approve_invoice, 'post': finance.post_invoice}[action](actor, invoice)
        messages.success(request, f'{invoice.document_no}: {action} done.')
        return redirect(request.get_full_path())
    return page(request, actor, 'invoice_list.html', invoices=invoices[:300])


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

@jobwork_view
def worker_list(request, actor):
    if request.method == 'POST':
        worker = svc.create_job_worker(
            actor, code=request.POST['code'], legal_name=request.POST['legal_name'], trade_name=request.POST.get('trade_name', ''),
            gstin=request.POST.get('gstin', '').strip().upper(), gst_registered=bool(request.POST.get('gstin')), state=request.POST.get('state', ''),
            city=request.POST.get('city', ''), address_1=request.POST.get('address', ''), default_sac=request.POST.get('default_sac', ''),
            lead_time_days=int(request.POST.get('lead_time_days') or 7), default_loss_percent=dec(request.POST.get('default_loss_percent'), 0),
            max_loss_percent=dec(request.POST.get('max_loss_percent'), 0), phone=request.POST.get('phone', ''),
            specializations=request.POST.getlist('specializations'))
        messages.success(request, f'Job worker {worker.code} created with location {worker.default_location.code}.')
        return redirect('jobwork_worker_detail', worker.pk)
    from .models import SPECIALIZATIONS
    return page(request, actor, 'worker_list.html', workers=JobWorker.objects.filter(tenant=actor.tenant), specializations=SPECIALIZATIONS)


@jobwork_view
def worker_detail(request, actor, pk):
    worker = get_obj(JobWorker, actor, pk)
    start = parse_date(request.GET.get('start') or '')
    return page(request, actor, 'worker_detail.html', worker=worker, stock=reports.material_at_job_workers(actor.tenant, job_worker=worker),
                reconciliation=reports.job_worker_reconciliation(worker, start=start), performance=pricing.performance(worker), start=start,
                ledger=worker.stock_entries.select_related('item', 'order', 'jewellery_unit').order_by('-id')[:200],
                orders=worker.orders.all()[:50], prices=worker.prices.select_related('item', 'work_center'))


@jobwork_view
def price_list(request, actor):
    if request.method == 'POST':
        from .security import require
        require(actor, 'masters')
        worker = get_obj(JobWorker, actor, request.POST['job_worker'])
        item = Item.objects.filter(tenant=actor.tenant, item_no=request.POST.get('item_no', '').strip()).first() if request.POST.get('item_no') else None
        JobWorkerPrice.objects.create(tenant=actor.tenant, job_worker=worker, item=item, operation_code=request.POST.get('operation_code', '').strip().upper(),
                                      rate_basis=request.POST['rate_basis'], rate=dec(request.POST.get('rate'), 0),
                                      minimum_amount=dec(request.POST.get('minimum_amount'), 0), minimum_quantity=dec(request.POST.get('minimum_quantity'), 0),
                                      effective_from=parse_date(request.POST.get('effective_from') or '') or timezone.localdate(), created_by=actor.user)
        messages.success(request, 'Price added.')
        return redirect(request.path)
    from .models import RATE_BASES
    return page(request, actor, 'price_list.html', prices=JobWorkerPrice.objects.filter(tenant=actor.tenant).select_related('job_worker', 'item'),
                workers=JobWorker.objects.filter(tenant=actor.tenant), rate_bases=RATE_BASES)


@jobwork_view
def tax_rates(request, actor):
    if request.method == 'POST':
        if request.POST.get('action') == 'approve':
            compliance.approve_master(actor, get_obj(TaxRate, actor, request.POST['rate']))
            messages.success(request, 'Rate approved.')
        else:
            from .security import require
            require(actor, 'tax_admin')
            TaxRate.objects.create(tenant=actor.tenant, code=request.POST['code'].strip(), code_type=request.POST.get('code_type', 'SAC'),
                                   description=request.POST['description'], cgst_rate=dec(request.POST.get('cgst_rate'), 0),
                                   sgst_rate=dec(request.POST.get('sgst_rate'), 0), igst_rate=dec(request.POST.get('igst_rate'), 0),
                                   cess_rate=dec(request.POST.get('cess_rate'), 0), reverse_charge=bool(request.POST.get('reverse_charge')),
                                   effective_from=parse_date(request.POST.get('effective_from') or '') or timezone.localdate(),
                                   notification=request.POST.get('notification', ''), created_by=actor.user)
            messages.success(request, 'Draft rate saved - a second tax administrator must approve it.')
        return redirect(request.path)
    return page(request, actor, 'tax_rates.html', rates=TaxRate.objects.filter(tenant=actor.tenant))


@jobwork_view
def rules(request, actor):
    if request.method == 'POST':
        if request.POST.get('action') == 'seed':
            created = compliance.seed_statutory_rules(actor)
            messages.success(request, f'{len(created)} statutory rules proposed as drafts for tax administrator review.')
        elif request.POST.get('action') == 'approve':
            compliance.approve_master(actor, get_obj(ComplianceRule, actor, request.POST['rule']))
            messages.success(request, 'Rule approved.')
        return redirect(request.path)
    return page(request, actor, 'rules.html', rules=ComplianceRule.objects.filter(tenant=actor.tenant))


# ---------------------------------------------------------------------------
# Planning and compliance
# ---------------------------------------------------------------------------

@jobwork_view
def worksheet(request, actor):
    if request.method == 'POST':
        from manufacturing.models import ProductionOrderRoutingLine
        created = []
        for op_id in request.POST.getlist('operation'):
            op = ProductionOrderRoutingLine.objects.filter(tenant=actor.tenant, pk=op_id).first()
            worker_id = request.POST.get(f'worker_{op_id}')
            worker = get_obj(JobWorker, actor, worker_id) if worker_id else None
            if op is not None:
                created.append(svc.create_from_production_operation(actor, op, job_worker=worker).order_no)
        messages.success(request, f'Created: {", ".join(created) or "nothing selected"}.')
        return redirect(request.path)
    return page(request, actor, 'worksheet.html', rows=reports.subcontracting_worksheet(actor.tenant),
                workers=JobWorker.objects.filter(tenant=actor.tenant, active=True))


@jobwork_view
def register(request, actor):
    start = parse_date(request.GET.get('start') or '') or timezone.localdate().replace(day=1)
    end = parse_date(request.GET.get('end') or '') or timezone.localdate()
    return page(request, actor, 'register.html', rows=reports.job_work_register(actor.tenant, start=start, end=end), start=start, end=end)


@jobwork_view
def compliance_view(request, actor):
    if request.method == 'POST':
        raised = finance.scan_compliance(actor)
        messages.success(request, f'Compliance scan done - {len(raised)} open alert(s).')
        return redirect(request.path)
    rows = reports.material_at_job_workers(actor.tenant)
    rows.sort(key=lambda r: (r['band']['days_remaining'] if r['band'] and r['band']['days_remaining'] is not None else 10 ** 6))
    return page(request, actor, 'compliance.html', rows=rows,
                eway_pending=EWayBill.objects.filter(tenant=actor.tenant, status='PENDING').select_related('dispatch'))


@jobwork_view
def itc04(request, actor):
    today = timezone.localdate()
    fy = today.year if today.month >= 4 else today.year - 1
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'generate':
            year = int(request.POST.get('fy') or fy)
            frequency, _ = compliance.itc04_frequency(actor.tenant, date(year, 4, 1))
            start, end, code = compliance.itc04_period(frequency, year, int(request.POST.get('half') or 1))
            record = compliance.generate_itc04(actor, period_start=start, period_end=end, period_code=code, frequency=frequency)
            messages.success(request, f'ITC-04 {record.period_code}: {record.get_status_display()}.')
            return redirect(f'{request.path}?id={record.pk}')
        record = get_obj(ITC04Return, actor, request.POST['itc04'])
        if action == 'prepare':
            compliance.mark_itc04_prepared(actor, record)
        elif action == 'filed':
            compliance.mark_itc04_filed(actor, record, filed_reference=request.POST.get('filed_reference', ''))
        messages.success(request, f'ITC-04 {record.period_code} updated.')
        return redirect(f'{request.path}?id={record.pk}')
    returns = ITC04Return.objects.filter(tenant=actor.tenant)
    record = returns.filter(pk=request.GET.get('id')).first() if request.GET.get('id') else returns.first()
    return page(request, actor, 'itc04.html', returns=returns, record=record, lines=record.lines.all() if record else [], fy=fy,
                can_compliance=can(actor, 'compliance'))


@jobwork_view
def trace(request, actor):
    code = request.GET.get('code', '').strip()
    result = reports.trace_unit(actor.tenant, code) if code else None
    lot = None
    if code and result is None:
        item = Item.objects.filter(tenant=actor.tenant, item_no=code).first()
        lot = reports.trace_lot(actor.tenant, item) if item else None
    return page(request, actor, 'trace.html', code=code, result=result, lot=lot)
