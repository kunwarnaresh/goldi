"""Exceptions, reconciliation-gated completion, vendor invoices (3-way match, GST snapshot, cost to manufacturing, G/L),
debit / credit notes and the compliance scan.

No separate accounting logic: vouchers go through the ERP finance engine (``erp.services._post_document_voucher``),
production cost through ``ManufacturingPostingEngine.cost('SUBCONTRACT')``. Accounts come from job work setup.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone as dj_timezone

from inventory.engine import InventoryError
from inventory.models import InventoryBalance, JewelleryUnit
from manufacturing.calc import q3

from . import compliance
from .models import (
    JobWorkAdjustmentNote, JobWorkCostEntry, JobWorkDispatch, JobWorkerStockEntry, JobWorkException, JobWorkOrder, JobWorkOrderLine,
    JobWorkProcessLine, JobWorkReceiptLine, JobWorkTaxSnapshot, JobWorkVendorInvoice,
)
from .pricing import basis_quantity
from .security import JobWorkError, audit, check_maker_checker, get_setup, has_role, next_number, require, required_role
from .services import (
    D, ZERO, _cost, _lock, blocking_exceptions, create_process_report, is_metal, jw_balance, line_is_memo, money, post_process_report,
    raise_exception, refresh_status,
)


def _history(exc, actor, action, note=''):
    exc.history = (exc.history or []) + [{'at': dj_timezone.now().isoformat(), 'by': actor.user.pk, 'action': action, 'note': note[:250]}]


# ---------------------------------------------------------------------------
# Exception workflow: Open -> Under review -> Approved / Rejected -> Resolved -> Closed
# ---------------------------------------------------------------------------

@transaction.atomic
def review_exception(actor, exc, *, note=''):
    require(actor, 'exception')
    exc = _lock(JobWorkException, actor, exc)
    if exc.status not in ('OPEN', 'REJECTED'):
        raise JobWorkError(f'{exc.exception_no} is {exc.get_status_display().lower()}.')
    exc.status, exc.reviewed_by = 'UNDER_REVIEW', actor.user
    _history(exc, actor, 'review', note)
    exc.save()
    return exc


@transaction.atomic
def approve_exception(actor, exc, *, resolution, reason, recovery_amount=0, loss_class=''):
    """Approval per the approval matrix, with a mandatory reason and commercial action. Never self-approved."""
    require(actor, 'exception')
    exc = _lock(JobWorkException, actor, exc)
    if exc.status not in ('OPEN', 'UNDER_REVIEW'):
        raise JobWorkError(f'{exc.exception_no} is {exc.get_status_display().lower()}.')
    if not (reason or '').strip():
        raise JobWorkError('A reason is required to approve an exception.')
    if resolution not in dict(JobWorkException.RESOLUTIONS):
        raise JobWorkError('Choose the commercial action (no charge, recovery, debit note, waiver...).')
    check_maker_checker(actor, exc.raised_by)
    if not has_role(actor, exc.required_role):
        raise JobWorkError(f'{exc.exception_no} needs approval by {exc.required_role.replace("_", " ").lower()}.')
    exc.status, exc.approved_by, exc.approved_at = 'APPROVED', actor.user, dj_timezone.now()
    exc.resolution, exc.reason, exc.recovery_amount = resolution, reason[:250], money(recovery_amount)
    exc.loss_class = loss_class or exc.loss_class
    _history(exc, actor, 'approve', reason)
    exc.save()
    audit(actor, 'approve', 'JOB_WORK_EXCEPTION', exc.exception_no, order=exc.order, new={'resolution': resolution, 'recovery': recovery_amount},
          reason=reason)
    if exc.order_id:
        refresh_status(exc.order)
    return exc


@transaction.atomic
def reject_exception(actor, exc, *, reason):
    require(actor, 'exception')
    exc = _lock(JobWorkException, actor, exc)
    if exc.status not in ('OPEN', 'UNDER_REVIEW'):
        raise JobWorkError(f'{exc.exception_no} is {exc.get_status_display().lower()}.')
    exc.status, exc.reason = 'REJECTED', reason[:250]
    _history(exc, actor, 'reject', reason)
    exc.save()
    audit(actor, 'reject', 'JOB_WORK_EXCEPTION', exc.exception_no, order=exc.order, reason=reason)
    return exc


@transaction.atomic
def resolve_exception(actor, exc, *, resolution, note):
    """Resolve an exception whose cause was fixed (data corrected, material returned). Approval-type exceptions
    (loss, difference, shortage) must be approved first."""
    require(actor, 'exception')
    exc = _lock(JobWorkException, actor, exc)
    needs_approval = exc.exception_type in ('EXCESS_LOSS', 'WEIGHT_DIFFERENCE', 'SHORT_RETURN', 'EXCESS_RETURN', 'INVOICE_MISMATCH', 'GST_MISMATCH')
    allowed = ('APPROVED',) if needs_approval else ('OPEN', 'UNDER_REVIEW', 'APPROVED', 'REJECTED')
    if exc.status not in allowed:
        raise JobWorkError(f'{exc.exception_no} must be approved before it can be resolved.' if needs_approval
                           else f'{exc.exception_no} is {exc.get_status_display().lower()}.')
    exc.status, exc.resolved_by, exc.resolved_at = 'RESOLVED', actor.user, dj_timezone.now()
    exc.resolution = resolution or exc.resolution
    _history(exc, actor, 'resolve', note)
    exc.save()
    audit(actor, 'resolve', 'JOB_WORK_EXCEPTION', exc.exception_no, order=exc.order, reason=note)
    if exc.order_id:
        refresh_status(exc.order)
    return exc


@transaction.atomic
def close_exception(actor, exc):
    require(actor, 'exception')
    exc = _lock(JobWorkException, actor, exc)
    if exc.status not in ('RESOLVED', 'APPROVED'):
        raise JobWorkError('Only approved or resolved exceptions can be closed.')
    exc.status = 'CLOSED'
    _history(exc, actor, 'close')
    exc.save()
    return exc


# ---------------------------------------------------------------------------
# Reconciliation-gated completion and closure
# ---------------------------------------------------------------------------

def residuals(order):
    """Per order line, what the ledger still holds at the job worker."""
    rows = JobWorkerStockEntry.objects.filter(order=order).values('order_line').annotate(
        q=Sum('quantity'), g=Sum('gross_weight'), n=Sum('net_weight'), f=Sum('fine_weight'), v=Sum('value'))
    lines = {l.pk: l for l in order.lines.select_related('item', 'uom')}
    result = [{'line': lines[r['order_line']], 'qty': q3(r['q'] or 0), 'gross': q3(r['g'] or 0), 'net': q3(r['n'] or 0),
               'fine': q3(r['f'] or 0), 'value': money(r['v'] or 0)} for r in rows]
    return [r for r in result if r['qty'] != 0 or r['net'] != 0]


def complete_order(actor, order):
    """Operational completion. Blocked while anything is in transit, pending QC, unexplained at the job worker, or while a blocking
    exception is open. A residual at the job worker raises a WEIGHT_DIFFERENCE exception - it is never silently accepted.

    The check runs in its own transaction so the exceptions it raises survive the refusal."""
    require(actor, 'close')
    with transaction.atomic():
        problems = completion_problems(actor, order)
    if problems:
        raise JobWorkError(f'{order.order_no} cannot be completed: ' + '; '.join(problems) + '.')
    with transaction.atomic():
        order = _lock(JobWorkOrder, actor, order)
        if completion_problems(actor, order):  # re-checked under the lock
            raise JobWorkError(f'{order.order_no} changed while it was being completed - try again.')
        order.status, order.completed_at = 'COMPLETED', dj_timezone.now()
        order.actual_return_date = order.actual_return_date or dj_timezone.localdate()
        order.save()
        audit(actor, 'complete', 'JOB_WORK_ORDER', order.order_no, order=order)
    return order


def completion_problems(actor, order):
    order = _lock(JobWorkOrder, actor, order)
    if order.status in ('DRAFT', 'PENDING_APPROVAL', 'COMPLETED') + JobWorkOrder.TERMINAL_STATUSES:
        return [f'it is {order.get_status_display().lower()}']
    setup = get_setup(actor.tenant)
    problems = []
    if order.dispatches.filter(status__in=('DRAFT', 'IN_TRANSIT', 'PARTIALLY_RECEIVED')).exists():
        problems.append('dispatches are still draft or in transit')
    if JobWorkReceiptLine.objects.filter(receipt__order=order, receipt__status='POSTED', qc_pending_qty__gt=0).exists():
        problems.append('QC is pending')
    if order.rework_orders.exclude(status__in=('COMPLETED', 'CLOSED', 'CANCELLED')).exists():
        problems.append('a rework order is open')
    for row in residuals(order):
        line = row['line']
        if not line.is_inbound:
            problems.append(f'{row["qty"]} of {line.item.item_no} (output) is still at the job worker - return it')
            continue
        weight = row['net'] or row['gross'] or ZERO
        if abs(row['qty']) > 0 and (abs(weight) > setup.weight_tolerance or not is_metal(line)):
            exc = raise_exception(
                actor, exception_type='WEIGHT_DIFFERENCE', order=order, order_line=line, expected=ZERO, actual=weight, weight=weight,
                value=row['value'] or ZERO, dedupe_key=f'DIFFERENCE:{line.pk}', metric='METAL_WEIGHT', metric_value=abs(weight),
                description=f'{order.order_no}: {row["qty"]} {line.uom.code if line.uom_id else ""} / {weight} g of {line.item.item_no} is '
                            'unaccounted for (sent - consumed - scrap - loss - returned). Classify it or get it returned.')
            problems.append(f'unexplained difference {row["qty"]} of {line.item.item_no} ({exc.exception_no})')
    blocking = list(blocking_exceptions(order).values_list('exception_no', flat=True))
    if blocking:
        problems.append(f'blocking exceptions open: {", ".join(blocking)}')
    if order.is_production and order.operation_id and order.output_accepted_qty > order.output_posted_qty and not order.rework_of_id:
        problems.append(f'{order.output_accepted_qty - order.output_posted_qty} accepted pieces await final output confirmation')
    return problems


@transaction.atomic
def write_off_difference(actor, exc):
    """After approval, post the unexplained residual of an input line as a classified loss at the job worker."""
    exc = _lock(JobWorkException, actor, exc)
    if exc.exception_type != 'WEIGHT_DIFFERENCE' or exc.status != 'APPROVED':
        raise JobWorkError('Only an approved weight-difference exception can be written off.')
    line = exc.order_line
    order = exc.order
    held = jw_balance(order, order_line=line, bulk=True)
    specs = []
    if held['qty'] > 0:
        specs.append({'kind': 'LOSS', 'input_line': line, 'input_qty': held['qty'], 'loss_class': exc.loss_class or 'UNEXPLAINED',
                      'reason': f'{exc.exception_no}: {exc.reason}'})
    units = JobWorkerStockEntry.objects.filter(order=order, order_line=line, jewellery_unit__isnull=False) \
        .values('jewellery_unit').annotate(q=Sum('quantity')).filter(q__gt=0)
    for row in units:
        specs.append({'kind': 'LOSS', 'input_line': line, 'input_unit': JewelleryUnit.objects.get(pk=row['jewellery_unit']),
                      'loss_class': exc.loss_class or 'UNEXPLAINED', 'reason': f'{exc.exception_no}: {exc.reason}'})
    if not specs:
        raise JobWorkError('Nothing is left to write off.')
    report = create_process_report(actor, order, lines=specs, reference=exc.exception_no)
    post_process_report(actor, report, skip_tolerance=True)
    if exc.resolution not in ('DEBIT_NOTE', 'VENDOR_RECOVERY'):  # a recovery stays approved until its debit note is posted
        exc.status, exc.resolved_by, exc.resolved_at = 'RESOLVED', actor.user, dj_timezone.now()
    _history(exc, actor, 'write_off', report.report_no)
    exc.save()
    audit(actor, 'write_off', 'JOB_WORK_EXCEPTION', exc.exception_no, order=order, new={'report': report.report_no})
    return report


@transaction.atomic
def close_order(actor, order):
    require(actor, 'close')
    order = _lock(JobWorkOrder, actor, order)
    if order.status != 'COMPLETED':
        raise JobWorkError(f'{order.order_no} must be completed before it is closed.')
    setup = get_setup(actor.tenant)
    if order.expected_charge and not order.invoices.filter(status='POSTED').exists() and not setup.allow_close_without_invoice:
        raise JobWorkError(f'{order.order_no} has no posted job worker invoice.')
    from .services import release_reservations
    release_reservations(actor, order)
    order.status, order.closed_by, order.closed_at = 'CLOSED', actor.user, dj_timezone.now()
    order.save()
    audit(actor, 'close', 'JOB_WORK_ORDER', order.order_no, order=order)
    return order


# ---------------------------------------------------------------------------
# Vendor invoice: 3-way match (order + received / accepted work + invoice), GST, cost, G/L
# ---------------------------------------------------------------------------

def billable(order):
    """What the job worker may bill so far, per the order's rate basis."""
    outputs = order.lines.filter(line_type='OUTPUT')
    accepted = sum((l.accepted_qty for l in outputs), ZERO) if order.qc_required else sum((l.received_qty for l in outputs), ZERO)
    received_net = sum((l.received_net for l in outputs), ZERO)
    carats = sum((l.consumed_qty for l in order.lines.all() if l.is_inbound and l.uom_id and l.uom.code == 'CT'), ZERO)
    done = order.dispatches.filter(movement_type='JW_TO_PRINCIPAL', status='RECEIVED').exists() or order.status in ('COMPLETED', 'CLOSED')
    return {'PER_PIECE': accepted, 'PER_GRAM': received_net, 'PER_CARAT': carats, 'PER_OPERATION': Decimal('1') if done else ZERO,
            'FIXED': Decimal('1') if done else ZERO, 'HOURLY': None, 'PERCENT': Decimal('1') if done else ZERO}[order.rate_basis]


def expected_amount(order, billed_qty):
    if order.rate_basis == 'PERCENT':
        amount = order.material_value * order.rate / 100
    else:
        amount = D(billed_qty) * order.rate
    total = max(order.invoiced_amount + amount, order.minimum_charge) if order.minimum_charge else order.invoiced_amount + amount
    return money(total - order.invoiced_amount)


def create_vendor_invoice(actor, order, *, vendor_invoice_no, **fields):
    require(actor, 'invoice')
    vendor_invoice_no = vendor_invoice_no.strip()
    order = JobWorkOrder.objects.filter(tenant=actor.tenant, pk=order.pk).first()
    if order is None:
        raise JobWorkError('Job work order not found.')
    duplicate = JobWorkVendorInvoice.objects.filter(tenant=actor.tenant, job_worker=order.job_worker, vendor_invoice_no__iexact=vendor_invoice_no) \
        .exclude(status='CANCELLED').first()
    if duplicate is not None:
        with transaction.atomic():  # recorded even though the invoice is refused
            raise_exception(actor, exception_type='DUPLICATE_INVOICE', order=order, blocking=False, severity='HIGH',
                            description=f'Invoice {vendor_invoice_no} of {order.job_worker.code} was already entered as {duplicate.document_no}.',
                            document_type='VENDOR_INVOICE', document_no=duplicate.document_no,
                            dedupe_key=f'DUPINV:{order.job_worker_id}:{vendor_invoice_no.upper()}')
        raise JobWorkError(f'Duplicate invoice: {vendor_invoice_no} is already recorded as {duplicate.document_no}.')
    return _create_vendor_invoice(actor, order, vendor_invoice_no=vendor_invoice_no, **fields)


@transaction.atomic
def _create_vendor_invoice(actor, order, *, vendor_invoice_no, invoice_date, billed_quantity, rate=None, taxable_value=None, sac_code='',
                           freight=0, other_charges=0, cgst=0, sgst=0, igst=0, cess=0, tds=0, vendor_gstin=None, irn='', ack_no='',
                           ack_date=None, signed_qr='', payment_terms=''):
    order = _lock(JobWorkOrder, actor, order)
    if order.status in ('DRAFT', 'PENDING_APPROVAL', 'CANCELLED'):
        raise JobWorkError(f'{order.order_no} is {order.get_status_display().lower()}.')
    rate = D(rate) if rate not in (None, '') else order.rate
    taxable = money(taxable_value) if taxable_value not in (None, '') else money(D(billed_quantity) * rate)
    invoice = JobWorkVendorInvoice.objects.create(
        tenant=actor.tenant, company=order.company, document_no=next_number(actor.tenant, 'VENDOR_INVOICE'), vendor_invoice_no=vendor_invoice_no,
        invoice_date=invoice_date, job_worker=order.job_worker, order=order,
        vendor_gstin=(vendor_gstin if vendor_gstin is not None else order.job_worker.gstin).upper(),
        sac_code=sac_code or order.service_sac or order.job_worker.default_sac, billed_quantity=q3(billed_quantity), rate=rate,
        taxable_value=taxable, freight=money(freight), other_charges=money(other_charges), vendor_cgst=money(cgst), vendor_sgst=money(sgst),
        vendor_igst=money(igst), vendor_cess=money(cess), tds_amount=money(tds), irn=irn, ack_no=ack_no, ack_date=ack_date, signed_qr=signed_qr,
        payment_terms=payment_terms, created_by=actor.user)
    invoice.total_amount = invoice.taxable_value + invoice.freight + invoice.other_charges + invoice.vendor_tax
    invoice.save()
    audit(actor, 'create', 'VENDOR_INVOICE', invoice.document_no, order=order, new={'vendor_no': vendor_invoice_no, 'taxable': taxable})
    return match_invoice(actor, invoice)


def _tax_result(actor, invoice):
    rate = compliance.resolve_tax_rate(actor.tenant, invoice.sac_code, invoice.invoice_date, invoice.order.transaction_type)
    supplier_state = invoice.job_worker.state_code or (invoice.vendor_gstin or '')[:2]
    return rate, compliance.compute_tax(actor.tenant, rate, invoice.taxable_value, supplier_state_code=supplier_state,
                                        recipient_state_code=compliance.principal_state_code(actor.tenant), on_date=invoice.invoice_date)


@transaction.atomic
def match_invoice(actor, invoice):
    """Order rate x received/accepted work vs the invoice, GSTIN, SAC and GST from the tax master. Mismatches become exceptions."""
    invoice = _lock(JobWorkVendorInvoice, actor, invoice)
    if invoice.status not in ('DRAFT', 'MATCHED', 'EXCEPTION'):
        raise JobWorkError(f'{invoice.document_no} is {invoice.get_status_display().lower()}.')
    order, setup = invoice.order, get_setup(actor.tenant)
    tolerance = setup.invoice_tolerance_amount
    issues = []
    available = billable(order)
    invoiced_before = order.invoiced_quantity
    billable_open = None if available is None else max(available - invoiced_before, ZERO)
    expected = expected_amount(order, invoice.billed_quantity if billable_open is None else min(invoice.billed_quantity, billable_open))
    rate_variance = money((invoice.rate - order.rate) * invoice.billed_quantity) if order.rate_basis != 'PERCENT' else ZERO
    quantity_variance = q3(invoice.billed_quantity - billable_open) if billable_open is not None else ZERO
    if order.rate and invoice.rate != order.rate and order.rate_basis != 'PERCENT':
        issues.append(('INVOICE_MISMATCH', f'Rate {invoice.rate} differs from the order rate {order.rate}.'))
    if billable_open is not None and invoice.billed_quantity > billable_open:
        issues.append(('INVOICE_MISMATCH', f'Billed {invoice.billed_quantity} but only {billable_open} '
                                           f'{order.get_rate_basis_display().lower().replace("per ", "")} is received / accepted and not yet invoiced.'))
    if abs(invoice.taxable_value - expected) > tolerance:
        issues.append(('INVOICE_MISMATCH', f'Taxable value {invoice.taxable_value} vs expected {expected} (tolerance {tolerance}).'))
    if order.job_worker.gstin and invoice.vendor_gstin != order.job_worker.gstin.upper():
        issues.append(('GSTIN_MISMATCH', f'Invoice GSTIN {invoice.vendor_gstin} is not {order.job_worker.gstin}.'))
    expected_sac = order.service_sac or order.job_worker.default_sac
    if expected_sac and invoice.sac_code != expected_sac:
        issues.append(('HSN_MISMATCH', f'SAC {invoice.sac_code} differs from {expected_sac} on the order.'))
    tax = None
    try:
        rate, tax = _tax_result(actor, invoice)
    except JobWorkError as exc:
        issues.append(('GST_MISMATCH', str(exc)))
    if tax is not None:
        for component in ('cgst', 'sgst', 'igst', 'cess'):
            computed = ZERO if tax['reverse_charge'] else tax[component] + (tax['utgst'] if component == 'sgst' else ZERO)
            stated = getattr(invoice, f'vendor_{component}')
            if abs(stated - computed) > Decimal('1.00'):
                issues.append(('GST_MISMATCH', f'{component.upper()} {stated} vs {computed} from the tax master ({tax["supply_type"]}).'))
        invoice.total_amount = invoice.taxable_value + invoice.freight + invoice.other_charges + invoice.vendor_tax
    invoice.match_result = {
        'billable': str(available) if available is not None else 'not verifiable (hourly)', 'already_invoiced': str(invoiced_before),
        'expected_amount': str(expected), 'rate_variance': str(rate_variance), 'quantity_variance': str(quantity_variance),
        'tax': {k: str(v) for k, v in (tax or {}).items() if k != 'explanation'}, 'issues': [msg for _, msg in issues],
        'disclaimer': compliance.TAX_DISCLAIMER,
    }
    for exc_type, message in issues:
        raise_exception(actor, exception_type=exc_type, order=order, description=f'{invoice.document_no}: {message}', document_type='VENDOR_INVOICE',
                        document_no=invoice.document_no, dedupe_key=f'INV:{invoice.pk}:{exc_type}:{message[:40]}', metric='VENDOR_INVOICE',
                        metric_value=invoice.total_amount, value=invoice.total_amount)
    invoice.status = 'EXCEPTION' if issues else 'MATCHED'
    invoice.save()
    audit(actor, 'match', 'VENDOR_INVOICE', invoice.document_no, order=order, new={'status': invoice.status, 'issues': len(issues)})
    return invoice


def invoice_exceptions(invoice):
    return JobWorkException.objects.filter(tenant=invoice.tenant, document_type='VENDOR_INVOICE', document_no=invoice.document_no)


@transaction.atomic
def approve_invoice(actor, invoice):
    require(actor, 'invoice')
    invoice = _lock(JobWorkVendorInvoice, actor, invoice)
    if invoice.status not in ('MATCHED', 'EXCEPTION'):
        raise JobWorkError(f'{invoice.document_no} is {invoice.get_status_display().lower()}.')
    open_exc = invoice_exceptions(invoice).filter(status__in=JobWorkException.OPEN_STATUSES)
    if open_exc.exists():
        raise JobWorkError(f'Resolve or approve invoice exceptions first: {", ".join(open_exc.values_list("exception_no", flat=True))}.')
    check_maker_checker(actor, invoice.created_by)
    role = required_role(actor.tenant, 'VENDOR_INVOICE', invoice.total_amount)
    if not has_role(actor, role):
        raise JobWorkError(f'{invoice.document_no} needs approval by {role.replace("_", " ").lower()}.')
    invoice.status, invoice.approved_by = 'APPROVED', actor.user
    invoice.save()
    audit(actor, 'approve', 'VENDOR_INVOICE', invoice.document_no, order=invoice.order)
    return invoice


def _payable_account(setup, job_worker):
    supplier = job_worker.supplier
    profile = getattr(supplier, 'finance_profile', None) if supplier else None
    if profile is not None and profile.posting_group_id:
        return profile.posting_group.payable_account
    return setup.vendor_payable_account


def _post_voucher(actor, *, lines, narration, document_no, voucher_date, source):
    """Balanced voucher through the ERP finance engine, or ('DISABLED', preview) when G/L posting is off."""
    setup = get_setup(actor.tenant)
    lines = [(account, money(amount), label) for account, amount, label in lines if money(amount) != 0]
    preview = [{'account': getattr(a, 'account_code', '?') if a else 'MISSING', 'label': label,
                'debit': str(amt) if amt > 0 else '', 'credit': str(-amt) if amt < 0 else ''} for a, amt, label in lines]
    if not setup.gl_posting_enabled or not lines:
        return None, 'DISABLED' if lines else 'NOT_REQUIRED', preview
    missing = [label for account, _, label in lines if account is None]
    if missing:
        raise JobWorkError(f'Job work posting setup is missing G/L accounts: {", ".join(missing)}.')
    if sum(amount for _, amount, _ in lines) != 0:
        raise JobWorkError('Internal error: job work voucher does not balance.')
    from erp import services as erp_services
    company = compliance.principal_company(actor.tenant)
    if company is None:
        raise JobWorkError('No company is configured for G/L posting.')
    voucher_lines = []
    for number, (account, amount, label) in enumerate(lines, 1):
        row = {'line_no': number, 'account': account, 'description': f'{document_no} {label}'[:250]}
        row['debit_amount' if amount > 0 else 'credit_amount'] = abs(amount)
        voucher_lines.append(row)
    try:
        voucher = erp_services._post_document_voucher(
            company=company, voucher_type_code='job_work_journal', user=actor.user, lines=voucher_lines, narration=narration[:250],
            document_no=document_no, voucher_date=voucher_date, source_doc=source, source_doc_type='job_work')
    except ValueError as exc:
        raise JobWorkError(f'G/L posting failed: {exc}')
    return voucher, 'POSTED', preview


def _capitalize_standalone(actor, invoice, amount):
    """Non-production job work: the charge becomes part of the output's inventory value where the output is still on hand;
    output already sold / consumed takes it as a variance. Historical cost is never re-priced."""
    from inventory.engine import InventoryPostingEngine
    order, setup = invoice.order, get_setup(actor.tenant)
    outputs = [l for l in order.lines.filter(line_type__in=('OUTPUT', 'BY_PRODUCT')) if l.produced_qty > 0]
    if not outputs:
        _cost(actor, order, 'VARIANCE', amount, document_type='VENDOR_INVOICE', document_no=invoice.document_no,
              description='Job charge with no output', absorbed=False)
        return
    method = setup.cost_allocation_method
    weights = [(l.produced_net or l.produced_qty) if method == 'WEIGHT' else l.produced_qty for l in outputs]
    base = sum(weights, ZERO) or Decimal('1')
    engine = InventoryPostingEngine(actor.tenant, actor.user, document_type='JOB_WORK_INVOICE', document_no=invoice.document_no)
    allocated = ZERO
    for index, (line, weight) in enumerate(zip(outputs, weights)):
        share = amount - allocated if index == len(outputs) - 1 else money(amount * weight / base)
        allocated += share
        capitalized = ZERO
        units = list(JewelleryUnit.objects.filter(pk__in=JobWorkProcessLine.objects.filter(
            report__order=order, report__status='POSTED', result_line=line, output_unit__isnull=False).values('output_unit'))
            .filter(status__in=JewelleryUnit.IN_STOCK_STATUSES))
        if units:
            each = money(share / line.produced_qty)
            for unit in units:
                engine.revalue(item=line.item, location=unit.current_location, unit=unit, cost_delta=each, reason_code='JOB_CHARGE')
                capitalized += each
        elif not line.item.serial_tracking:
            for location, bin_ in ((order.return_location, order.return_bin), (order.job_worker_location, None)):
                balance = InventoryBalance.objects.filter(tenant=actor.tenant, item=line.item, variant=line.variant, location=location, bin=bin_,
                                                          jewellery_unit__isnull=True, on_hand_qty__gt=0).first()
                if balance is not None:
                    engine.revalue(item=line.item, variant=line.variant, location=location, bin=bin_, cost_delta=share, reason_code='JOB_CHARGE')
                    capitalized = share
                    break
        if capitalized:
            _cost(actor, order, 'OUTPUT', -capitalized, line=line, document_type='VENDOR_INVOICE', document_no=invoice.document_no,
                  description=f'Job charge capitalized to {line.item.item_no}', absorbed=False)
        if share - capitalized:
            _cost(actor, order, 'VARIANCE', share - capitalized, line=line, document_type='VENDOR_INVOICE', document_no=invoice.document_no,
                  description=f'Job charge on {line.item.item_no} no longer in stock', absorbed=False)


@transaction.atomic
def post_invoice(actor, invoice):
    """Atomic: GST snapshot + job work cost (to manufacturing WIP or the output's value) + AP invoice + G/L voucher + audit."""
    require(actor, 'post_invoice')
    invoice = _lock(JobWorkVendorInvoice, actor, invoice)
    if invoice.status != 'APPROVED':
        raise JobWorkError(f'{invoice.document_no} must be approved before posting.')
    order = _lock(JobWorkOrder, actor, invoice.order)
    setup = get_setup(actor.tenant)
    rate, tax = _tax_result(actor, invoice)
    snapshot = JobWorkTaxSnapshot.objects.create(
        tenant=actor.tenant, company=order.company, posting_date=dj_timezone.localdate(), document_type='VENDOR_INVOICE',
        document_no=invoice.document_no, tax_rate=rate, code=rate.code, supply_type=tax['supply_type'], place_of_supply=tax['place_of_supply'],
        supplier_state_code=tax['explanation']['supplier_state'], recipient_state_code=tax['explanation']['recipient_state'],
        taxable_value=tax['taxable_value'], cgst_rate=tax['cgst_rate'], sgst_rate=tax['sgst_rate'], igst_rate=tax['igst_rate'],
        cess_rate=tax['cess_rate'], cgst=tax['cgst'], sgst=tax['sgst'], utgst=tax['utgst'], igst=tax['igst'], cess=tax['cess'],
        reverse_charge=tax['reverse_charge'], explanation=tax['explanation'], created_by=actor.user)
    service = invoice.taxable_value + invoice.freight + invoice.other_charges
    _cost(actor, order, 'JOB_CHARGE', invoice.taxable_value, document_type='VENDOR_INVOICE', document_no=invoice.document_no,
          description=f'{invoice.vendor_invoice_no} {invoice.billed_quantity} x {invoice.rate}', absorbed=False)
    _cost(actor, order, 'FREIGHT', invoice.freight, document_type='VENDOR_INVOICE', document_no=invoice.document_no, absorbed=False)
    _cost(actor, order, 'OTHER', invoice.other_charges, document_type='VENDOR_INVOICE', document_no=invoice.document_no, absorbed=False)
    if order.is_production:
        from manufacturing import engine as mfg_engine
        from manufacturing.security import get_setup as mfg_setup
        prod = mfg_engine.lock_order(actor, order.production_order)
        engine = mfg_engine.ManufacturingPostingEngine(actor, prod, kind='SUBCONTRACT', reason=f'Job work invoice {invoice.document_no}')
        engine.cost('SUBCONTRACT', service, source=invoice, description=f'{order.job_worker.code} {invoice.vendor_invoice_no} ({order.order_no})')
        if order.operation_id:
            op = type(order.operation).objects.select_for_update().get(pk=order.operation_id)
            op.actual_cost += service
            op.save(update_fields=['actual_cost', 'updated_at'])
        engine.extra['job_work'] = {'invoice': invoice.document_no, 'order': order.order_no, 'amount': str(service)}
        engine.finalize(action='job_work_invoice')
        debit_account, debit_label = mfg_setup(actor.tenant).subcontract_applied_account, 'Subcontracting applied'
    else:
        _capitalize_standalone(actor, invoice, service)
        debit_account, debit_label = setup.job_work_cost_account, 'Job work cost'
    tax_total = snapshot.total_tax
    payable = service + (ZERO if snapshot.reverse_charge else tax_total)
    lines = [(debit_account, service, debit_label), (setup.gst_input_account, tax_total, 'GST input'),
             (_payable_account(setup, order.job_worker), -payable, 'Vendor payable')]
    if snapshot.reverse_charge:
        lines.append((setup.gst_rcm_payable_account, -tax_total, 'GST payable (reverse charge)'))
    voucher, gl_status, preview = _post_voucher(actor, lines=lines, narration=f'Job work invoice {invoice.vendor_invoice_no} {order.order_no}',
                                                document_no=invoice.document_no, voucher_date=invoice.invoice_date, source=invoice)
    invoice.supplier_invoice = _ap_invoice(actor, invoice, service, ZERO if snapshot.reverse_charge else tax_total)
    invoice.tax_snapshot, invoice.finance_voucher, invoice.gl_status = snapshot, voucher, gl_status
    invoice.status, invoice.posted_by, invoice.posted_at = 'POSTED', actor.user, dj_timezone.now()
    invoice.match_result = {**invoice.match_result, 'gl': preview}
    invoice.save()
    order.invoiced_amount += invoice.taxable_value
    order.invoiced_quantity += invoice.billed_quantity
    order.save(update_fields=['invoiced_amount', 'invoiced_quantity', 'updated_at'])
    audit(actor, 'post', 'VENDOR_INVOICE', invoice.document_no, order=order,
          new={'taxable': invoice.taxable_value, 'tax': tax_total, 'gl': gl_status, 'rcm': snapshot.reverse_charge})
    return invoice


def _ap_invoice(actor, invoice, service, tax):
    """Mirror the posted job work invoice into Purchase & Payables so it can be paid (no second G/L posting)."""
    supplier = invoice.job_worker.supplier
    company = compliance.principal_company(actor.tenant)
    if supplier is None or company is None:
        return None
    from erp.models import SupplierInvoice
    number = f'{invoice.job_worker.code}/{invoice.vendor_invoice_no}'[:100]
    if SupplierInvoice.objects.filter(invoice_no=number).exists():
        number = f'{number}/{invoice.document_no}'[:100]
    return SupplierInvoice.objects.create(company=company, supplier=supplier, document_no=invoice.document_no, workflow_status='posted',
                                          invoice_no=number, invoice_date=invoice.invoice_date, gross_amount=service, tax_amount=tax,
                                          net_amount=service + tax, status='open',
                                          notes=f'Job work {invoice.order.order_no} (posted by the job work module)')


# ---------------------------------------------------------------------------
# Debit / credit notes (never automatic)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_note(actor, *, note_type, order, amount, reason, exception=None, invoice=None):
    require(actor, 'invoice')
    order = _lock(JobWorkOrder, actor, order)
    if note_type == 'DEBIT':
        if exception is None:
            raise JobWorkError('A debit note must come from an approved job work exception.')
        exception = _lock(JobWorkException, actor, exception)
        if exception.order_id != order.id or exception.status != 'APPROVED' or exception.resolution not in ('DEBIT_NOTE', 'VENDOR_RECOVERY'):
            raise JobWorkError(f'{exception.exception_no} is not an approved exception with a recovery / debit-note resolution.')
        amount = amount if amount not in (None, '') else exception.recovery_amount
    if money(amount) <= 0:
        raise JobWorkError('The note amount must be greater than zero.')
    note = JobWorkAdjustmentNote.objects.create(
        tenant=actor.tenant, company=order.company, note_no=next_number(actor.tenant, 'DEBIT_NOTE' if note_type == 'DEBIT' else 'CREDIT_NOTE'),
        note_type=note_type, job_worker=order.job_worker, order=order, exception=exception, invoice=invoice, reason=reason[:250],
        amount=money(amount), created_by=actor.user)
    audit(actor, 'create', f'{note_type}_NOTE', note.note_no, order=order, new={'amount': note.amount, 'exception': exception})
    return note


@transaction.atomic
def approve_note(actor, note):
    require(actor, 'approve')
    note = _lock(JobWorkAdjustmentNote, actor, note)
    if note.status != 'DRAFT':
        raise JobWorkError(f'{note.note_no} is {note.get_status_display().lower()}.')
    check_maker_checker(actor, note.created_by)
    role = required_role(actor.tenant, 'DEBIT_NOTE', note.amount)
    if not has_role(actor, role):
        raise JobWorkError(f'{note.note_no} needs approval by {role.replace("_", " ").lower()}.')
    note.status, note.approved_by = 'APPROVED', actor.user
    note.save()
    audit(actor, 'approve', f'{note.note_type}_NOTE', note.note_no, order=note.order)
    return note


@transaction.atomic
def post_note(actor, note):
    require(actor, 'post_invoice')
    note = _lock(JobWorkAdjustmentNote, actor, note)
    if note.status != 'APPROVED':
        raise JobWorkError(f'{note.note_no} must be approved first.')
    setup, order = get_setup(actor.tenant), note.order
    payable = _payable_account(setup, note.job_worker)
    if note.note_type == 'DEBIT':
        credit, label = setup.recovery_account, 'Recovery from job worker'
        _cost(actor, order, 'RECOVERY', -note.amount, document_type='DEBIT_NOTE', document_no=note.note_no,
              description=f'Recovery {note.reason}', absorbed=False)
    else:
        if order.is_production:
            from manufacturing.security import get_setup as mfg_setup
            credit, label = mfg_setup(actor.tenant).subcontract_applied_account, 'Subcontracting applied'
        else:
            credit, label = setup.job_work_cost_account, 'Job work cost'
        _cost(actor, order, 'JOB_CHARGE', -note.amount, document_type='CREDIT_NOTE', document_no=note.note_no,
              description=f'Vendor credit {note.reason}', absorbed=False)
    voucher, gl_status, _ = _post_voucher(actor, lines=[(payable, note.amount, 'Vendor payable'), (credit, -note.amount, label)],
                                          narration=f'{note.get_note_type_display()} {note.note_no} {order.order_no}', document_no=note.note_no,
                                          voucher_date=note.note_date, source=note)
    note.status, note.finance_voucher, note.gl_status = 'POSTED', voucher, gl_status
    note.save()
    if note.exception_id and note.exception.status == 'APPROVED':
        exc = note.exception
        exc.status, exc.resolved_by, exc.resolved_at = 'RESOLVED', actor.user, dj_timezone.now()
        _history(exc, actor, 'debit_note', note.note_no)
        exc.save()
    audit(actor, 'post', f'{note.note_type}_NOTE', note.note_no, order=order, new={'amount': note.amount, 'gl': gl_status})
    return note


# ---------------------------------------------------------------------------
# Compliance scan (run daily: management command `jobwork_compliance_scan`)
# ---------------------------------------------------------------------------

def scan_compliance(actor, today=None):
    """Raise overdue / missing-e-way-bill exceptions from the ledgers. Idempotent (deduplicated)."""
    today = today or dj_timezone.localdate()
    raised = []
    orders = JobWorkOrder.objects.filter(tenant=actor.tenant, status__in=JobWorkOrder.OPEN_STATUSES).select_related('job_worker')
    for order in orders:
        held = jw_balance(order)
        if held['qty'] <= 0:
            continue
        if order.compliance_due_date:
            band = compliance.alert_band(actor.tenant, order.first_dispatch_date, order.compliance_due_date, today)
            if band['level'] in ('OVERDUE', 'DUE_SOON', 'CRITICAL'):
                raised.append(raise_exception(
                    actor, exception_type='MATERIAL_OVERDUE', order=order, blocking=False,
                    severity='CRITICAL' if band['level'] == 'OVERDUE' else 'HIGH', value=held['value'], weight=held['net'],
                    description=f'{order.order_no}: goods with {order.job_worker.code} since {order.first_dispatch_date}; statutory due '
                                f'{order.compliance_due_date} ({band["level"].replace("_", " ").lower()}, {band["days_remaining"]} days).',
                    dedupe_key=f'OVERDUE:{order.pk}:{band["level"]}'))
        if order.expected_return_date and order.expected_return_date < today:
            raised.append(raise_exception(
                actor, exception_type='MATERIAL_OVERDUE', order=order, blocking=False, severity='MEDIUM', value=held['value'],
                description=f'{order.order_no}: expected back {order.expected_return_date} (operational SLA).', dedupe_key=f'SLA:{order.pk}'))
    for dispatch in JobWorkDispatch.objects.filter(tenant=actor.tenant, status='IN_TRANSIT', eway_bill_required=True) \
            .exclude(eway_bills__status='GENERATED'):
        raised.append(raise_exception(actor, exception_type='MISSING_EWAY_BILL', order=dispatch.order, blocking=False,
                                      description=f'{dispatch.dispatch_no} is in transit without a generated e-way bill.',
                                      dedupe_key=f'EWB:{dispatch.pk}', document_type='JOB_WORK_DISPATCH', document_no=dispatch.dispatch_no))
    return raised
