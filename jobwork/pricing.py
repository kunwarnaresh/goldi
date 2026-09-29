"""Job worker price resolution (Business Central subcontractor prices), charge calculation, job worker suggestion
and factual performance indicators."""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

from .models import JobWorker, JobWorkerPrice, JobWorkOrder, JobWorkOrderLine
from .security import get_setup

ZERO = Decimal('0')
DIMENSIONS = ('work_center_id', 'item_id', 'variant_id', 'uom_id')


def D(value):
    return Decimal(str(value if value not in (None, '') else 0))


def money(value):
    return D(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def resolve_price(tenant, job_worker, *, operation_code='', item=None, variant=None, uom=None, work_center=None, on_date=None,
                  quantity=0, currency='INR'):
    """The most specific applicable price: every set dimension must match; blanks are wildcards.

    Ties break on specificity, then the highest minimum quantity reached, then the latest effective date."""
    on_date = on_date or dj_timezone.localdate()
    wanted = {'work_center_id': getattr(work_center, 'pk', None), 'item_id': getattr(item, 'pk', None),
              'variant_id': getattr(variant, 'pk', None), 'uom_id': getattr(uom, 'pk', None)}
    candidates = JobWorkerPrice.objects.filter(
        tenant=tenant, job_worker=job_worker, active=True, currency=currency, effective_from__lte=on_date, minimum_quantity__lte=D(quantity),
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
    best, best_key = None, None
    for price in candidates:
        if price.operation_code and price.operation_code != (operation_code or ''):
            continue
        if any(getattr(price, dim) is not None and getattr(price, dim) != wanted[dim] for dim in DIMENSIONS):
            continue
        specificity = sum(1 for dim in DIMENSIONS if getattr(price, dim) is not None) + (1 if price.operation_code else 0)
        key = (specificity, price.minimum_quantity, price.effective_from, price.pk)
        if best_key is None or key > best_key:
            best, best_key = price, key
    return best


def charge(rate_basis, rate, minimum_amount=0, *, pieces=0, grams=0, carats=0, hours=0, operations=1, base_value=0):
    """Quantity x rate for the basis, never below the minimum charge."""
    rate = D(rate)
    basis_qty = {'PER_PIECE': D(pieces), 'PER_GRAM': D(grams), 'PER_CARAT': D(carats), 'HOURLY': D(hours),
                 'PER_OPERATION': D(operations), 'FIXED': Decimal('1')}
    if rate_basis == 'PERCENT':
        amount = D(base_value) * rate / 100
    else:
        amount = basis_qty.get(rate_basis, ZERO) * rate
    return money(max(amount, D(minimum_amount)))


def basis_quantity(rate_basis, *, pieces=0, grams=0, carats=0, hours=0, operations=1):
    return {'PER_PIECE': D(pieces), 'PER_GRAM': D(grams), 'PER_CARAT': D(carats), 'HOURLY': D(hours), 'PER_OPERATION': D(operations),
            'FIXED': Decimal('1'), 'PERCENT': Decimal('1')}.get(rate_basis, ZERO)


# ---------------------------------------------------------------------------
# Performance (factual indicators - no irreversible rating)
# ---------------------------------------------------------------------------

def performance(job_worker, *, since=None):
    orders = JobWorkOrder.objects.filter(tenant=job_worker.tenant, job_worker=job_worker).exclude(status__in=('DRAFT', 'CANCELLED'))
    if since:
        orders = orders.filter(order_date__gte=since)
    today = dj_timezone.localdate()
    done = orders.filter(status__in=('COMPLETED', 'CLOSED'))
    dated = [o for o in done if o.expected_return_date and o.actual_return_date]
    on_time = sum(1 for o in dated if o.actual_return_date <= o.expected_return_date)
    outputs = JobWorkOrderLine.objects.filter(order__in=orders, line_type='OUTPUT').aggregate(a=Sum('accepted_qty'), r=Sum('rejected_qty'),
                                                                                             p=Sum('produced_qty'))
    accepted, rejected = outputs['a'] or ZERO, outputs['r'] or ZERO
    metal = JobWorkOrderLine.objects.filter(order__in=orders, line_type__in=JobWorkOrderLine.INBOUND_TYPES,
                                            metal__in=('GOLD', 'SILVER', 'PLATINUM')).aggregate(sent=Sum('dispatched_net'), loss=Sum('loss_qty'))
    sent, loss = metal['sent'] or ZERO, metal['loss'] or ZERO
    expected_loss = sum(((o.expected_loss_percent or ZERO) * sum((l.dispatched_net for l in o.lines.all() if l.is_inbound), ZERO) / 100
                         for o in orders), ZERO)
    invoiced = orders.aggregate(a=Sum('invoiced_amount'), q=Sum('invoiced_quantity'))
    standard = orders.filter(invoiced_amount__gt=0).aggregate(e=Sum('expected_charge'))['e'] or ZERO
    total = orders.count()
    rework = orders.filter(rework_of__isnull=False).count()
    pct = lambda num, den: (D(num) * 100 / D(den)).quantize(Decimal('0.01')) if den else None
    return {
        'orders': total, 'open_orders': orders.filter(status__in=JobWorkOrder.OPEN_STATUSES).count(),
        'overdue_orders': orders.filter(status__in=JobWorkOrder.OPEN_STATUSES, expected_return_date__lt=today).count(),
        'on_time_percent': pct(on_time, len(dated)), 'quality_percent': pct(accepted, accepted + rejected),
        'rework_percent': pct(rework, total), 'loss_percent': pct(loss, sent), 'expected_loss_weight': expected_loss.quantize(Decimal('0.001')),
        'actual_loss_weight': loss, 'weight_variance': (loss - expected_loss).quantize(Decimal('0.001')),
        'average_cost': (D(invoiced['a']) / D(invoiced['q'])).quantize(Decimal('0.01')) if invoiced['q'] else None,
        'cost_variance': money(D(invoiced['a']) - standard) if invoiced['a'] else ZERO,
    }


def open_load(job_worker):
    """Pieces and metal grams currently committed to the job worker (open orders)."""
    orders = JobWorkOrder.objects.filter(tenant=job_worker.tenant, job_worker=job_worker, status__in=JobWorkOrder.OPEN_STATUSES)
    pieces = orders.aggregate(q=Sum('planned_quantity'))['q'] or ZERO
    from .models import JobWorkerStockEntry
    grams = JobWorkerStockEntry.objects.filter(tenant=job_worker.tenant, job_worker=job_worker, metal__in=('GOLD', 'SILVER', 'PLATINUM')) \
        .aggregate(w=Sum('net_weight'))['w'] or ZERO
    return pieces, grams


def suggest_job_workers(tenant, *, operation_code='', item=None, quantity=0, grams=0, on_date=None, specialization=None):
    """Rank eligible job workers by configured weights (price, quality, on-time, lead time) - never price alone unless the
    tenant explicitly configured that. Returns dicts; nothing is assigned automatically."""
    setup = get_setup(tenant)
    weights = {'price': 0.4, 'quality': 0.3, 'on_time': 0.2, 'lead_time': 0.1}
    weights.update({k: float(v) for k, v in (setup.selection_weights or {}).items() if k in weights})
    rows = []
    for jw in JobWorker.objects.filter(tenant=tenant, active=True, blocked=False, job_worker_eligible=True).exclude(compliance_status='NON_COMPLIANT'):
        if specialization and specialization not in (jw.specializations or []):
            continue
        price = resolve_price(tenant, jw, operation_code=operation_code, item=item, on_date=on_date, quantity=quantity)
        if price is None:
            continue
        amount = charge(price.rate_basis, price.rate, price.minimum_amount, pieces=quantity, grams=grams)
        perf = performance(jw)
        load_pieces, load_grams = open_load(jw)
        capacity = jw.daily_capacity_qty * jw.lead_time_days if jw.daily_capacity_qty else None
        free = (capacity - load_pieces) if capacity is not None else None
        rows.append({'job_worker': jw, 'price': price, 'amount': amount, 'lead_time_days': jw.lead_time_days, 'performance': perf,
                     'capacity_free': free, 'capacity_ok': free is None or free >= D(quantity),
                     'metal_ok': not jw.metal_capacity_grams or load_grams + D(grams) <= jw.metal_capacity_grams})
    if not rows:
        return rows
    if setup.auto_select_lowest_price:
        rows.sort(key=lambda r: (not r['capacity_ok'], r['amount']))
    else:
        low_price = min(r['amount'] for r in rows) or Decimal('1')
        low_lead = min(r['lead_time_days'] for r in rows) or 1
        for r in rows:
            quality = float(r['performance']['quality_percent'] if r['performance']['quality_percent'] is not None else 100) / 100
            on_time = float(r['performance']['on_time_percent'] if r['performance']['on_time_percent'] is not None else 100) / 100
            r['score'] = round(weights['price'] * float(low_price / r['amount'] if r['amount'] else 1) + weights['quality'] * quality
                               + weights['on_time'] * on_time + weights['lead_time'] * (low_lead / max(r['lead_time_days'], 1)), 4)
        rows.sort(key=lambda r: (not r['capacity_ok'], -r['score']))
    rows[0]['recommended'] = True
    return rows
