"""GST compliance engine for job work: tax master resolution, effective-dated compliance rules, Section 143 due dates,
e-way bill applicability and provider interface, and ITC-04 preparation/reconciliation from the transaction ledgers.

No statutory number lives in this module. Rates, periods, thresholds and frequencies come from ``TaxRate`` and
``ComplianceRule`` rows that a tax administrator has approved for the transaction date. ``seed_statutory_rules``
only proposes DRAFT rows (with citations) for that administrator to review.

Tax treatment is determined from the configured GST rule applicable to the transaction. The ERP does not provide
legal or tax advice.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

from .models import (
    ComplianceRule, DeliveryChallan, EWayBill, ITC04Line, ITC04Return, JobWorkDispatch, JobWorkerStockEntry, JobWorkProcessLine,
    JobWorkReceiptLine, TaxRate,
)
from .security import JobWorkError, audit, get_setup, next_number, require

ZERO = Decimal('0')
TAX_DISCLAIMER = ('Tax treatment is determined from the configured GST rule applicable to this transaction. '
                  'The ERP does not provide legal or tax advice.')


def D(value):
    return Decimal(str(value if value not in (None, '') else 0))


def money(value):
    return D(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Principal identity
# ---------------------------------------------------------------------------

def principal_company(tenant):
    setup = get_setup(tenant)
    if setup.company_id:
        return setup.company
    from erp.models import Company
    return Company.objects.filter(status='active').order_by('id').first() or Company.objects.order_by('id').first()


def principal_gstin(tenant):
    setup = get_setup(tenant)
    if setup.principal_gstin:
        return setup.principal_gstin.upper()
    company = principal_company(tenant)
    return (company.gstin or '').upper() if company else ''


def principal_state_code(tenant):
    setup = get_setup(tenant)
    if setup.principal_state_code:
        return setup.principal_state_code
    gstin = principal_gstin(tenant)
    return gstin[:2] if len(gstin) >= 2 else ''


def valid_gstin(gstin):
    gstin = (gstin or '').strip().upper()
    return len(gstin) == 15 and gstin[:2].isdigit() and gstin.isalnum()


# ---------------------------------------------------------------------------
# Effective-dated rule and tax master resolution
# ---------------------------------------------------------------------------

def _effective(qs, on_date):
    return qs.filter(status='APPROVED', effective_from__lte=on_date).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))


def approved_rule(tenant, rule_type, on_date, **match):
    """The latest approved rule of `rule_type` effective on `on_date` whose parameters contain `match`."""
    rules = _effective(ComplianceRule.objects.filter(tenant=tenant, rule_type=rule_type), on_date).order_by('-effective_from', '-id')
    for rule in rules:
        params = rule.parameters or {}
        if all(str(params.get(key)) == str(value) for key, value in match.items()):
            return rule
    return None


def resolve_tax_rate(tenant, code, on_date, transaction_type=''):
    """The approved GST rate for an HSN/SAC on a date. Never falls back to a built-in rate."""
    code = (code or '').strip()
    if not code:
        raise JobWorkError('No HSN/SAC code: GST cannot be determined. Set the SAC on the order or the job worker.')
    rates = _effective(TaxRate.objects.filter(tenant=tenant, code=code), on_date)
    rate = (rates.filter(applies_to=transaction_type).order_by('-effective_from', '-id').first() if transaction_type else None) \
        or rates.filter(applies_to='').order_by('-effective_from', '-id').first()
    if rate is None:
        raise JobWorkError(f'No approved GST rate for {code} effective on {on_date}. A tax administrator must approve one in the GST tax master.')
    return rate


def compute_tax(tenant, rate, taxable_value, *, supplier_state_code, recipient_state_code, on_date=None):
    """Split tax per the resolved rate. Supply is intra-state when supplier and recipient state codes match."""
    taxable = money(taxable_value)
    supply_type = 'INTRA' if supplier_state_code and supplier_state_code == recipient_state_code else 'INTER'
    result = {'taxable_value': taxable, 'supply_type': supply_type, 'place_of_supply': recipient_state_code, 'code': rate.code,
              'cgst': ZERO, 'sgst': ZERO, 'utgst': ZERO, 'igst': ZERO, 'cess': ZERO, 'reverse_charge': rate.reverse_charge,
              'cgst_rate': ZERO, 'sgst_rate': ZERO, 'igst_rate': ZERO, 'cess_rate': rate.cess_rate}
    component = lambda pct: money(taxable * D(pct) / 100)
    if rate.taxability == 'TAXABLE':
        result['cess'] = component(rate.cess_rate)
        if supply_type == 'INTRA':
            result['cgst'], result['cgst_rate'] = component(rate.cgst_rate), rate.cgst_rate
            if rate.utgst_rate:
                result['utgst'], result['sgst_rate'] = component(rate.utgst_rate), rate.utgst_rate
            else:
                result['sgst'], result['sgst_rate'] = component(rate.sgst_rate), rate.sgst_rate
        else:
            result['igst'], result['igst_rate'] = component(rate.igst_rate), rate.igst_rate
    result['total_tax'] = result['cgst'] + result['sgst'] + result['utgst'] + result['igst'] + result['cess']
    result['explanation'] = {
        'rate_id': rate.pk, 'code': rate.code, 'taxability': rate.taxability, 'effective_from': str(rate.effective_from),
        'notification': rate.notification, 'circular': rate.circular, 'supplier_state': supplier_state_code,
        'recipient_state': recipient_state_code, 'rule': 'CGST + SGST/UTGST' if supply_type == 'INTRA' else 'IGST',
        'on_date': str(on_date or dj_timezone.localdate()), 'disclaimer': TAX_DISCLAIMER,
    }
    return result


# ---------------------------------------------------------------------------
# Draft statutory defaults for the tax administrator
# ---------------------------------------------------------------------------

STATUTORY_PROPOSALS = [
    ('JW-RETURN-INPUTS', 'RETURN_PERIOD', 'Inputs sent for job work to be received back or supplied within one year',
     {'goods_category': 'INPUTS', 'period_months': 12}, 'CGST Act s.143(1)(a); s.19(3)'),
    ('JW-RETURN-CAPITAL', 'RETURN_PERIOD', 'Capital goods (other than moulds, dies, jigs, fixtures, tools) within three years',
     {'goods_category': 'CAPITAL_GOODS', 'period_months': 36}, 'CGST Act s.143(1)(b); s.19(6)'),
    ('EWB-CONSIGNMENT', 'EWAY_BILL', 'E-way bill above the consignment value threshold; inter-state job work movement irrespective of value',
     {'threshold_value': '50000', 'interstate_job_work_any_value': True,
      'movement_types': ['PRINCIPAL_TO_JW', 'JW_TO_JW', 'JW_TO_PRINCIPAL']}, 'CGST Rules r.138(1) and provisos'),
    ('ITC04-FREQUENCY', 'ITC04_FREQUENCY', 'ITC-04 half-yearly above the turnover threshold, annual otherwise',
     {'turnover_threshold': '50000000', 'above': 'HALF_YEARLY', 'at_or_below': 'ANNUAL'}, 'CGST Rules r.45(3) as amended'),
    ('JW-ALERT-BANDS', 'ALERT_THRESHOLDS', 'Internal review bands for goods lying with job workers (do not change the statutory date)',
     {'review_days': 90, 'warning_days': 180, 'critical_days': 270, 'due_soon_days': 30}, 'Internal control'),
    ('JW-DELIVERY-CHALLAN', 'DELIVERY_CHALLAN', 'Movement for job work under delivery challan (no supply)',
     {'required': True}, 'CGST Rules r.55'),
]


@transaction.atomic
def seed_statutory_rules(actor, *, effective_from=None):
    """Propose the statutory defaults as DRAFT rules. Nothing is used until a tax administrator approves it."""
    require(actor, 'compliance')
    effective_from = effective_from or date(2017, 7, 1)
    created = []
    for code, rule_type, description, params, citation in STATUTORY_PROPOSALS:
        rule, made = ComplianceRule.objects.get_or_create(
            tenant=actor.tenant, rule_code=code,
            defaults={'rule_type': rule_type, 'description': description, 'parameters': params, 'effective_from': effective_from,
                      'notification': citation, 'status': 'DRAFT', 'created_by': actor.user})
        if made:
            created.append(rule)
            audit(actor, 'propose', 'COMPLIANCE_RULE', code, new={'status': 'DRAFT', 'citation': citation})
    return created


@transaction.atomic
def approve_master(actor, obj):
    """Tax administrator approval of a TaxRate / ComplianceRule. Effective windows of the same code may not overlap."""
    require(actor, 'tax_admin')
    obj = type(obj).objects.select_for_update().get(pk=obj.pk, tenant=actor.tenant)
    if obj.status != 'DRAFT':
        raise JobWorkError(f'Only draft masters can be approved (this one is {obj.get_status_display().lower()}).')
    if obj.created_by_id == actor.user.pk and not get_setup(actor.tenant).allow_self_approval:
        raise JobWorkError('Maker-checker: a different tax administrator must approve this master.')
    if isinstance(obj, TaxRate):
        clash = TaxRate.objects.filter(tenant=actor.tenant, code=obj.code, applies_to=obj.applies_to, status='APPROVED') \
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=obj.effective_from))
        if obj.effective_to:
            clash = clash.filter(effective_from__lte=obj.effective_to)
        for old in clash:
            if old.effective_from >= obj.effective_from:
                raise JobWorkError(f'An approved rate for {obj.code} already starts on {old.effective_from}; retire it first.')
            old.effective_to = obj.effective_from - timedelta(days=1)  # close the old version; its snapshots are untouched
            old.save(update_fields=['effective_to', 'updated_at'])
    obj.status, obj.approved_by, obj.approved_at = 'APPROVED', actor.user, dj_timezone.now()
    obj.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    audit(actor, 'approve', type(obj).__name__.upper(), getattr(obj, 'rule_code', None) or obj.code, new={'status': 'APPROVED'})
    return obj


# ---------------------------------------------------------------------------
# Section 143 due dates and alert bands
# ---------------------------------------------------------------------------

def add_months(start, months):
    month = start.month - 1 + int(months)
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def return_due_date(tenant, goods_category, dispatch_date):
    """(due date, rule) from the approved RETURN_PERIOD rule for the goods category, or (None, None)."""
    rule = approved_rule(tenant, 'RETURN_PERIOD', dispatch_date, goods_category=goods_category)
    if rule is None:
        return None, None
    params = rule.parameters or {}
    if params.get('period_months') not in (None, ''):
        return add_months(dispatch_date, params['period_months']), rule
    if params.get('period_days') not in (None, ''):
        return dispatch_date + timedelta(days=int(params['period_days'])), rule
    return None, rule


def alert_band(tenant, dispatch_date, due_date, today=None):
    """Days elapsed/remaining and the colour band. Internal thresholds never move the statutory due date."""
    today = today or dj_timezone.localdate()
    rule = approved_rule(tenant, 'ALERT_THRESHOLDS', today)
    params = rule.parameters if rule else {}
    elapsed = (today - dispatch_date).days if dispatch_date else 0
    remaining = (due_date - today).days if due_date else None
    level = 'NORMAL'
    for key, name in (('review_days', 'REVIEW'), ('warning_days', 'WARNING'), ('critical_days', 'CRITICAL')):
        if params.get(key) not in (None, '') and elapsed >= int(params[key]):
            level = name
    if remaining is not None and remaining < 0:
        level, band = 'OVERDUE', 'RED'
    elif remaining is not None and params.get('due_soon_days') not in (None, '') and remaining <= int(params['due_soon_days']):
        level, band = 'DUE_SOON', 'RED'
    elif level in ('WARNING', 'CRITICAL'):
        band = 'AMBER' if level == 'WARNING' else 'RED'
    else:
        band = 'GREEN'
    return {'days_elapsed': elapsed, 'days_remaining': remaining, 'days_overdue': -remaining if remaining is not None and remaining < 0 else 0,
            'level': level, 'band': band, 'no_rule': due_date is None}


# ---------------------------------------------------------------------------
# E-way bill
# ---------------------------------------------------------------------------

def eway_bill_requirement(tenant, dispatch):
    """(required, reason, rule). `required` is None when no approved rule exists - the caller raises an exception."""
    rule = approved_rule(tenant, 'EWAY_BILL', dispatch.dispatch_date)
    if rule is None:
        return None, 'No approved e-way bill rule for this date - applicability must be verified manually.', None
    params = rule.parameters or {}
    movements = params.get('movement_types')
    if movements and dispatch.movement_type not in movements:
        return False, f'Movement {dispatch.movement_type} is outside rule {rule.rule_code}.', rule
    from_state = (dispatch.from_location.gstin or '')[:2] or principal_state_code(tenant)
    to_state = (dispatch.to_location.gstin or '')[:2] or principal_state_code(tenant)
    link = getattr(dispatch.to_location, 'job_worker_link', None) or getattr(dispatch.from_location, 'job_worker_link', None)
    if link is not None and link.state_code:
        if dispatch.movement_type == 'PRINCIPAL_TO_JW':
            to_state = link.state_code
        elif dispatch.movement_type == 'JW_TO_PRINCIPAL':
            from_state = link.state_code
    interstate = bool(from_state and to_state and from_state != to_state)
    if interstate and params.get('interstate_job_work_any_value'):
        return True, f'Inter-state job work movement ({from_state} -> {to_state}) per rule {rule.rule_code}, irrespective of value.', rule
    threshold = D(params.get('threshold_value'))
    if dispatch.total_value > threshold:
        return True, f'Consignment value {dispatch.total_value} exceeds {threshold} per rule {rule.rule_code}.', rule
    return False, f'Consignment value {dispatch.total_value} within {threshold} per rule {rule.rule_code}.', rule


class ManualEWayBillProvider:
    """Records e-way bills generated on the portal. A GSP/API provider implements the same five methods."""
    name = 'MANUAL'

    def generate(self, eway, *, ewb_no, valid_until=None, **payload):
        if not ewb_no:
            raise JobWorkError('Enter the e-way bill number generated on the portal.')
        now = dj_timezone.now()
        return {'ewbNo': ewb_no, 'ewayBillDate': now.isoformat(), 'validUpto': valid_until.isoformat() if valid_until else None}

    def cancel(self, eway, *, reason):
        return {'ewbNo': eway.ewb_no, 'cancelDate': dj_timezone.now().isoformat(), 'reason': reason}

    def update_vehicle(self, eway, *, vehicle_no, reason=''):
        return {'ewbNo': eway.ewb_no, 'vehicleNo': vehicle_no, 'vehUpdDate': dj_timezone.now().isoformat(), 'reason': reason}

    def extend(self, eway, *, valid_until, reason=''):
        return {'ewbNo': eway.ewb_no, 'validUpto': valid_until.isoformat(), 'reason': reason}

    def get_status(self, eway):
        return {'ewbNo': eway.ewb_no, 'status': eway.status}


PROVIDERS = {'MANUAL': ManualEWayBillProvider()}


def eway_payload(dispatch):
    challan = getattr(dispatch, 'challan', None)
    return {
        'supplyType': 'O' if dispatch.movement_type != 'JW_TO_PRINCIPAL' else 'I', 'subSupplyType': 'JOB_WORK',
        'docType': 'CHL', 'docNo': challan.challan_no if challan else dispatch.dispatch_no, 'docDate': str(dispatch.dispatch_date),
        'fromGstin': challan.consignor_gstin if challan else '', 'toGstin': challan.consignee_gstin if challan else '',
        'totalValue': str(dispatch.total_value), 'transMode': dispatch.transport_mode, 'transDistance': dispatch.distance_km,
        'transporterId': dispatch.transporter_id, 'transporterName': dispatch.transporter, 'vehicleNo': dispatch.vehicle_no,
        'itemList': [{'productName': l.item.item_no, 'hsnCode': l.hsn_code, 'quantity': str(l.quantity), 'taxableAmount': str(l.value)}
                     for l in dispatch.lines.select_related('item')],
    }


def _eway_call(actor, eway, action, **kwargs):
    require(actor, 'execute')
    provider = PROVIDERS.get(eway.provider) or PROVIDERS['MANUAL']
    response = getattr(provider, action)(eway, **kwargs)
    eway.history = (eway.history or []) + [{'action': action, 'at': dj_timezone.now().isoformat(), 'by': actor.user.pk, 'response': response}]
    eway.response_payload = response
    return response


@transaction.atomic
def generate_eway_bill(actor, eway, *, ewb_no='', valid_until=None, vehicle_no=None):
    eway = EWayBill.objects.select_for_update().get(pk=eway.pk, tenant=actor.tenant)
    if eway.status != 'PENDING':
        raise JobWorkError(f'E-way bill is {eway.get_status_display().lower()}.')
    eway.request_payload = eway_payload(eway.dispatch)
    response = _eway_call(actor, eway, 'generate', ewb_no=ewb_no, valid_until=valid_until)
    eway.ewb_no = response['ewbNo']
    eway.status, eway.generated_at, eway.valid_from, eway.valid_until = 'GENERATED', dj_timezone.now(), dj_timezone.now(), valid_until
    if vehicle_no:
        eway.vehicle_no = vehicle_no
    eway.save()
    challan = getattr(eway.dispatch, 'challan', None)
    if challan is not None:
        challan.eway_bill_no = eway.ewb_no
        challan.save(update_fields=['eway_bill_no', 'updated_at'])
    audit(actor, 'generate', 'EWAY_BILL', eway.ewb_no, order=eway.dispatch.order, new={'dispatch': eway.dispatch.dispatch_no})
    return eway


@transaction.atomic
def cancel_eway_bill(actor, eway, *, reason):
    eway = EWayBill.objects.select_for_update().get(pk=eway.pk, tenant=actor.tenant)
    if eway.status != 'GENERATED':
        raise JobWorkError('Only a generated e-way bill can be cancelled.')
    _eway_call(actor, eway, 'cancel', reason=reason)
    eway.status, eway.cancel_reason = 'CANCELLED', reason[:250]
    eway.save()
    audit(actor, 'cancel', 'EWAY_BILL', eway.ewb_no, order=eway.dispatch.order, reason=reason)
    return eway


@transaction.atomic
def update_eway_vehicle(actor, eway, *, vehicle_no, reason=''):
    eway = EWayBill.objects.select_for_update().get(pk=eway.pk, tenant=actor.tenant)
    if eway.status != 'GENERATED':
        raise JobWorkError('Only a generated e-way bill can be updated.')
    _eway_call(actor, eway, 'update_vehicle', vehicle_no=vehicle_no, reason=reason)
    eway.vehicle_no = vehicle_no
    eway.save()
    return eway


@transaction.atomic
def extend_eway_bill(actor, eway, *, valid_until, reason=''):
    eway = EWayBill.objects.select_for_update().get(pk=eway.pk, tenant=actor.tenant)
    if eway.status != 'GENERATED':
        raise JobWorkError('Only a generated e-way bill can be extended.')
    _eway_call(actor, eway, 'extend', valid_until=valid_until, reason=reason)
    eway.valid_until = valid_until
    eway.save()
    return eway


# ---------------------------------------------------------------------------
# ITC-04 (generated only from posted challans, receipts and loss reports)
# ---------------------------------------------------------------------------

def itc04_frequency(tenant, on_date):
    rule = approved_rule(tenant, 'ITC04_FREQUENCY', on_date)
    if rule is None:
        raise JobWorkError('No approved ITC-04 frequency rule for this period. A tax administrator must approve one.')
    params = rule.parameters or {}
    turnover = get_setup(tenant).aggregate_turnover_previous_fy
    frequency = params['above'] if turnover > D(params.get('turnover_threshold')) else params['at_or_below']
    return frequency, rule


def itc04_period(frequency, fy_start_year, half=1):
    """Indian financial year periods: FY 2026-27 = 1 Apr 2026 - 31 Mar 2027; H1 = Apr-Sep, H2 = Oct-Mar."""
    fy = f'{fy_start_year}-{str(fy_start_year + 1)[-2:]}'
    if frequency == 'HALF_YEARLY':
        if half == 1:
            return date(fy_start_year, 4, 1), date(fy_start_year, 9, 30), f'{fy}-H1'
        return date(fy_start_year, 10, 1), date(fy_start_year + 1, 3, 31), f'{fy}-H2'
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31), fy


def _original_challan(order_line, before=None):
    """The first principal->job worker challan that carried this order line (walking up multi-level orders)."""
    line = order_line
    for _ in range(8):
        challans = DeliveryChallan.objects.filter(dispatch__lines__order_line=line, dispatch__movement_type='PRINCIPAL_TO_JW', status='ISSUED')
        challan = challans.order_by('challan_date', 'id').first()
        if challan is not None:
            return challan
        source = line.dispatch_lines.filter(source_line__isnull=False).exclude(dispatch__status__in=('REVERSED', 'CANCELLED')).first()
        if source is None:
            # an output/scrap line: the challan of the order's first inbound dispatch
            challans = DeliveryChallan.objects.filter(dispatch__order=line.order, dispatch__movement_type='PRINCIPAL_TO_JW', status='ISSUED')
            challan = challans.order_by('challan_date', 'id').first()
            if challan is not None or line.order.parent_id is None:
                return challan
            parent_line = line.order.parent.lines.filter(line_type__in=('INPUT', 'WIP', 'COMPONENT')).first()
            if parent_line is None:
                return None
            line = parent_line
            continue
        line = source.source_line
    return None


def _jw_identity(job_worker):
    return {'job_worker_gstin': job_worker.gstin, 'job_worker_state_code': job_worker.state_code,
            'job_worker_name': job_worker.trade_name or job_worker.legal_name}


def _gstin_ok(job_worker):
    return valid_gstin(job_worker.gstin) if job_worker.gst_registered else bool(job_worker.state_code)


@transaction.atomic
def generate_itc04(actor, *, period_start, period_end, period_code, frequency=None):
    """Build (or rebuild, while not yet prepared) the ITC-04 working for a period and reconcile it."""
    require(actor, 'compliance')
    tenant = actor.tenant
    rule = None
    if frequency is None:
        frequency, rule = itc04_frequency(tenant, period_start)
    record = ITC04Return.objects.select_for_update().filter(tenant=tenant, period_code=period_code).first()
    if record is not None and record.status in ('PREPARED', 'FILED'):
        raise JobWorkError(f'ITC-04 {period_code} is already {record.get_status_display().lower()}.')
    if record is None:
        record = ITC04Return.objects.create(tenant=tenant, reference_no=next_number(tenant, 'ITC04'), period_code=period_code,
                                            frequency=frequency, period_start=period_start, period_end=period_end, rule=rule,
                                            principal_gstin=principal_gstin(tenant), created_by=actor.user)
    record.lines.all().delete()
    lines = []
    in_period = dict(challan_date__gte=period_start, challan_date__lte=period_end, status='ISSUED')

    def challan_lines(challan, table):
        dispatch = challan.dispatch
        job_worker = dispatch.to_job_worker if table in ('4', '5B') else dispatch.from_job_worker
        for cl in challan.lines.select_related('dispatch_line__order_line__order'):
            dl = cl.dispatch_line
            if dl.memo and dl.owner != 'PRINCIPAL':
                continue  # vendor / customer goods are not the principal's inputs
            status = 'MATCHED'
            if dl.quantity != cl.quantity:
                status = 'WRONG_QUANTITY'
            elif not cl.hsn_code:
                status = 'WRONG_HSN'
            elif not _gstin_ok(job_worker):
                status = 'WRONG_GSTIN'
            original = _original_challan(dl.source_line) if table == '5B' and dl.source_line_id else None
            lines.append(ITC04Line(
                tenant=tenant, itc04=record, table=table, challan_no=challan.challan_no, challan_date=challan.challan_date,
                original_challan_no=original.challan_no if original else '', original_challan_date=original.challan_date if original else None,
                order_no=challan.order_no, item_no=cl.item_no, description=cl.description, hsn_code=cl.hsn_code, uom=cl.uom,
                quantity=cl.quantity, taxable_value=cl.value, goods_category=challan.goods_category,
                nature_of_job_work=dl.order_line.order.get_transaction_type_display(), match_status=status,
                source_type='DeliveryChallanLine', source_id=cl.pk, created_by=actor.user, **_jw_identity(job_worker)))

    for challan in DeliveryChallan.objects.filter(tenant=tenant, dispatch__movement_type='PRINCIPAL_TO_JW', **in_period).select_related('dispatch__to_job_worker'):
        challan_lines(challan, '4')
    for challan in DeliveryChallan.objects.filter(tenant=tenant, dispatch__movement_type='JW_TO_JW', **in_period).select_related('dispatch__to_job_worker'):
        challan_lines(challan, '5B')

    # 5A - goods received back at the principal (from receipts), with the original challan they were sent under
    receipt_lines = JobWorkReceiptLine.objects.filter(tenant=tenant, receipt__status='POSTED', receipt__receipt_date__gte=period_start,
                                                      receipt__receipt_date__lte=period_end) \
        .select_related('receipt__order__job_worker', 'order_line__order', 'item', 'dispatch_line__dispatch__challan')
    for rl in receipt_lines:
        order_line, order = rl.order_line, rl.receipt.order
        if rl.dispatch_line.memo and rl.dispatch_line.owner != 'PRINCIPAL':
            continue
        original = _original_challan(order_line)
        return_challan = getattr(rl.dispatch_line.dispatch, 'challan', None)
        status = 'MATCHED'
        if original is None:
            status = 'WRONG_CHALLAN'
        elif order_line.is_inbound and order_line.received_qty > order_line.dispatched_qty:
            status = 'EXCESS_RECEIPT'
        elif not (order_line.hsn_code or rl.item.hsn_code):
            status = 'WRONG_HSN'
        elif not _gstin_ok(order.job_worker):
            status = 'WRONG_GSTIN'
        lines.append(ITC04Line(
            tenant=tenant, itc04=record, table='5A', challan_no=return_challan.challan_no if return_challan else rl.receipt.receipt_no,
            challan_date=rl.receipt.receipt_date, original_challan_no=original.challan_no if original else '',
            original_challan_date=original.challan_date if original else None, order_no=order.order_no, item_no=rl.item.item_no,
            description=rl.item.description, hsn_code=order_line.hsn_code or rl.item.hsn_code, uom=order_line.uom.code if order_line.uom_id else '',
            quantity=rl.quantity, taxable_value=rl.value, goods_category=order.goods_category,
            nature_of_job_work=order.get_transaction_type_display(), match_status=status, source_type='JobWorkReceiptLine', source_id=rl.pk,
            created_by=actor.user, **_jw_identity(order.job_worker)))

    # 5A - losses and wastes reported by the job worker in the period
    losses = JobWorkProcessLine.objects.filter(tenant=tenant, kind='LOSS', report__status='POSTED', report__report_date__gte=period_start,
                                               report__report_date__lte=period_end).select_related('report__order__job_worker', 'input_line__item')
    for pl in losses:
        order, line = pl.report.order, pl.input_line
        original = _original_challan(line)
        lines.append(ITC04Line(
            tenant=tenant, itc04=record, table='5A', challan_no=pl.report.report_no, challan_date=pl.report.report_date,
            original_challan_no=original.challan_no if original else '', original_challan_date=original.challan_date if original else None,
            order_no=order.order_no, item_no=line.item.item_no, description=f'Loss / waste - {pl.get_loss_class_display()}',
            hsn_code=line.hsn_code or line.item.hsn_code, uom=line.uom.code if line.uom_id else '', quantity=ZERO, loss_quantity=pl.input_qty,
            taxable_value=pl.value, goods_category=order.goods_category, nature_of_job_work=order.get_transaction_type_display(),
            match_status='MATCHED' if original else 'WRONG_CHALLAN', source_type='JobWorkProcessLine', source_id=pl.pk,
            created_by=actor.user, **_jw_identity(order.job_worker)))

    # Pending: table 4 lines whose order still holds goods at the job worker at period end (informational)
    ITC04Line.objects.bulk_create(lines)
    for line in record.lines.filter(table='4', match_status='MATCHED'):
        dl = JobWorkerStockEntry.objects.filter(tenant=tenant, order__order_no=line.order_no, posting_date__lte=period_end) \
            .aggregate(q=Sum('quantity'))
        if D(dl['q'] or 0).quantize(Decimal('0.001')) > 0:
            line.match_status = 'PENDING'
            line.save(update_fields=['match_status', 'updated_at'])
    return reconcile_itc04(actor, record)


def reconcile_itc04(actor, record):
    lines = record.lines.all()
    counts = {}
    for line in lines:
        counts[line.match_status] = counts.get(line.match_status, 0) + 1
    tables = {}
    for line in lines:
        bucket = tables.setdefault(line.table, {'lines': 0, 'quantity': ZERO, 'loss_quantity': ZERO, 'value': ZERO})
        bucket['lines'] += 1
        bucket['quantity'] += line.quantity
        bucket['loss_quantity'] += line.loss_quantity
        bucket['value'] += line.taxable_value
    errors = sum(n for status, n in counts.items() if status in ITC04Line.ERROR_STATUSES)
    record.status = 'EXCEPTIONS' if errors else 'RECONCILED'
    record.summary = {'counts': counts, 'errors': errors,
                      'tables': {k: {kk: str(vv) for kk, vv in v.items()} for k, v in tables.items()},
                      'challans': len({l.challan_no for l in lines if l.table == '4'}),
                      'job_workers': len({l.job_worker_gstin or l.job_worker_name for l in lines})}
    record.generated_by, record.generated_at = actor.user, dj_timezone.now()
    record.save()
    audit(actor, 'generate', 'ITC04', record.reference_no, new={'period': record.period_code, 'status': record.status, 'errors': errors})
    return record


@transaction.atomic
def mark_itc04_prepared(actor, record):
    require(actor, 'compliance')
    record = ITC04Return.objects.select_for_update().get(pk=record.pk, tenant=actor.tenant)
    if record.status != 'RECONCILED':
        raise JobWorkError('ITC-04 preparation can be completed only when every record is reconciled.')
    record.status, record.prepared_by = 'PREPARED', actor.user
    record.save(update_fields=['status', 'prepared_by', 'updated_at'])
    audit(actor, 'prepare', 'ITC04', record.reference_no)
    return record


@transaction.atomic
def mark_itc04_filed(actor, record, *, filed_reference):
    require(actor, 'compliance')
    record = ITC04Return.objects.select_for_update().get(pk=record.pk, tenant=actor.tenant)
    if record.status != 'PREPARED':
        raise JobWorkError('Only a prepared ITC-04 can be marked as filed.')
    record.status, record.filed_reference = 'FILED', filed_reference[:60]
    record.save(update_fields=['status', 'filed_reference', 'updated_at'])
    audit(actor, 'file', 'ITC04', record.reference_no, new={'reference': filed_reference})
    return record
