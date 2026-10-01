"""Jewellery Savings reporting - every figure is read from the scheme ledger / documents, never recomputed ad hoc."""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone as dj_timezone

from .benefit import ZERO, money
from .models import (
    BenefitCalculation, SchemeCancellation, SchemeEnrollment, SchemeInstallment, SchemeLedgerEntry, SchemePayment, SchemeRedemption,
    SchemeRefund,
)


def _sum(qs, field):
    return money(qs.aggregate(v=Sum(field))['v'] or ZERO)


def liability(tenant, *, scheme=None, location=None):
    """Customer contribution vs jeweller benefit vs total entitlement still owed - kept separate."""
    entries = SchemeLedgerEntry.objects.filter(tenant=tenant)
    if scheme:
        entries = entries.filter(enrollment__scheme=scheme)
    if location:
        entries = entries.filter(enrollment__branch=location)
    t = entries.aggregate(**{k: Sum(k) for k in ('contribution', 'benefit', 'contribution_redeemed', 'benefit_redeemed', 'refund',
                                                 'contribution_forfeited', 'benefit_forfeited', 'credit_transfer', 'penalty')})
    t = {k: money(v or 0) for k, v in t.items()}
    contribution = t['contribution'] - t['contribution_redeemed'] - t['refund'] - t['contribution_forfeited'] - t['credit_transfer']
    benefit = t['benefit'] - t['benefit_redeemed'] - t['benefit_forfeited']
    return {**t, 'contribution_liability': contribution, 'benefit_liability': benefit, 'total_liability': contribution + benefit}


def dashboard(tenant, today=None):
    today = today or dj_timezone.localdate()
    month_start = today.replace(day=1)
    enrollments = SchemeEnrollment.objects.filter(tenant=tenant)
    by_status = dict(enrollments.values_list('status').annotate(n=Count('id')))
    payments = SchemePayment.objects.filter(tenant=tenant)
    installments = SchemeInstallment.objects.filter(tenant=tenant, enrollment__status__in=SchemeEnrollment.CONTRIBUTING)
    expected_benefit = enrollments.filter(status__in=SchemeEnrollment.CONTRIBUTING).aggregate(v=Sum('expected_benefit'))['v'] or ZERO
    window_end = today + timedelta(days=30)
    return {
        'active': sum(by_status.get(s, 0) for s in SchemeEnrollment.CONTRIBUTING),
        'pending_approval': by_status.get('PENDING_APPROVAL', 0) + by_status.get('DRAFT', 0),
        'matured': by_status.get('MATURED', 0),
        'pending_redemption': by_status.get('BENEFIT_APPROVED', 0) + by_status.get('PARTIALLY_REDEEMED', 0),
        'redeemed': by_status.get('REDEEMED', 0),
        'cancelled': by_status.get('CANCELLED', 0) + by_status.get('REFUNDED', 0),
        'collection_today': _sum(payments.filter(payment_date=today), 'amount'),
        'collection_month': _sum(payments.filter(payment_date__gte=month_start, payment_date__lte=today), 'amount'),
        'due_count': installments.filter(status__in=('DUE', 'GRACE')).count(),
        'due_amount': money(sum((i.outstanding for i in installments.filter(status__in=('DUE', 'GRACE', 'PARTIALLY_PAID'))), ZERO)),
        'overdue_count': installments.filter(status='OVERDUE').count(),
        'overdue_amount': money(sum((i.outstanding for i in installments.filter(status='OVERDUE')), ZERO)),
        'expected_benefit': money(expected_benefit),
        'expiring': enrollments.filter(status__in=SchemeEnrollment.REDEEMABLE, redemption_valid_until__lte=window_end).count(),
        'expired': enrollments.filter(status__in=SchemeEnrollment.REDEEMABLE, redemption_valid_until__lt=today).count(),
        'benefit_pending': BenefitCalculation.objects.filter(tenant=tenant, status='CALCULATED').count(),
        'refunds_pending': SchemeRefund.objects.filter(tenant=tenant, status__in=('REQUESTED', 'APPROVED')).count(),
        'cancellations_pending': SchemeCancellation.objects.filter(tenant=tenant, status='REQUESTED').count(),
        'liability': liability(tenant),
    }


def collections(tenant, *, start, end, group_by='day'):
    """Collection report grouped by day / staff / location / method / scheme (reversals included as negatives)."""
    field = {'day': 'payment_date', 'staff': 'collected_by_name', 'location': 'location__code', 'method': 'method_type',
             'scheme': 'enrollment__scheme__code'}[group_by]
    qs = SchemePayment.objects.filter(tenant=tenant, payment_date__range=(start, end))
    rows = qs.values(field).annotate(receipts=Count('id', filter=Q(status__in=('POSTED', 'REVERSED'))),
                                     reversals=Count('id', filter=Q(status='REVERSAL')), amount=Sum('amount'),
                                     contribution=Sum('contribution_amount'), penalty=Sum('penalty_amount')).order_by(field)
    return [{'key': r[field] or '—', **{k: r[k] for k in ('receipts', 'reversals')},
             **{k: money(r[k] or 0) for k in ('amount', 'contribution', 'penalty')}} for r in rows], _sum(qs, 'amount')


def installments_due(tenant, *, statuses, location=None):
    qs = SchemeInstallment.objects.filter(tenant=tenant, status__in=statuses, enrollment__status__in=SchemeEnrollment.CONTRIBUTING) \
        .select_related('enrollment', 'enrollment__scheme').order_by('due_date')
    if location:
        qs = qs.filter(enrollment__branch=location)
    return qs


def branch_performance(tenant, *, start, end):
    rows = {}
    for r in SchemeEnrollment.objects.filter(tenant=tenant, enrollment_date__range=(start, end)).values('branch__code') \
            .annotate(n=Count('id'), planned=Sum('planned_contribution')):
        rows.setdefault(r['branch__code'] or '—', {})['enrolled'] = r['n']
        rows[r['branch__code'] or '—']['planned'] = money(r['planned'] or 0)
    for r in SchemePayment.objects.filter(tenant=tenant, payment_date__range=(start, end)).values('location__code').annotate(v=Sum('amount')):
        rows.setdefault(r['location__code'] or '—', {})['collected'] = money(r['v'] or 0)
    for r in SchemeRedemption.objects.filter(tenant=tenant, redemption_date__range=(start, end)).values('location__code').annotate(v=Sum('amount')):
        rows.setdefault(r['location__code'] or '—', {})['redeemed'] = money(r['v'] or 0)
    return sorted(rows.items())


def scheme_performance(tenant):
    """Per scheme: enrolments, collected, benefit granted/redeemed, redemption & cancellation ratios."""
    rows = []
    from .models import JewellerySavingsScheme
    for scheme in JewellerySavingsScheme.objects.filter(tenant=tenant):
        e = SchemeEnrollment.objects.filter(tenant=tenant, scheme=scheme)
        total = e.count()
        led = liability(tenant, scheme=scheme)
        matured = e.filter(status__in=SchemeEnrollment.REDEEMABLE + ('REDEEMED', 'MATURED', 'CLOSED')).count()
        rows.append({
            'scheme': scheme, 'enrollments': total, 'active': e.filter(status__in=SchemeEnrollment.CONTRIBUTING).count(),
            'collected': led['contribution'], 'benefit_granted': led['benefit'], 'benefit_redeemed': led['benefit_redeemed'],
            'redeemed': led['contribution_redeemed'] + led['benefit_redeemed'], 'refunded': led['refund'],
            'liability': led['total_liability'], 'average_value': money(led['contribution'] / total) if total else ZERO,
            'redemption_ratio': int(e.filter(status__in=('REDEEMED', 'PARTIALLY_REDEEMED')).count() / matured * 100) if matured else 0,
            'cancellation_ratio': int(e.filter(status__in=('CANCELLED', 'REFUNDED')).count() / total * 100) if total else 0,
            'benefit_cost_percent': money(led['benefit'] / led['contribution'] * 100) if led['contribution'] else ZERO,
        })
    return rows


def statement(enrollment):
    return {
        'installments': enrollment.installments.all(),
        'ledger': enrollment.ledger.all(),
        'paid_count': enrollment.installments.filter(status='PAID').count(),
        'remaining_count': enrollment.installments.exclude(status__in=SchemeInstallment.SETTLED).count(),
        'next_due': enrollment.installments.exclude(status__in=SchemeInstallment.SETTLED).order_by('installment_no').first(),
    }


def enrollment_summary(tenant, *, start=None, end=None, scheme=None):
    """How many members enrolled - by status, by scheme and by month of enrolment."""
    qs = SchemeEnrollment.objects.filter(tenant=tenant)
    if start:
        qs = qs.filter(enrollment_date__gte=start)
    if end:
        qs = qs.filter(enrollment_date__lte=end)
    if scheme:
        qs = qs.filter(scheme=scheme)
    labels = dict(SchemeEnrollment.STATUSES)
    by_status = [{'status': s, 'label': labels.get(s, s), 'count': n, 'planned': money(p or 0)}
                 for s, n, p in qs.values_list('status').annotate(n=Count('id'), p=Sum('planned_contribution')).order_by('status')]
    by_scheme = [{'code': r['scheme__code'], 'name': r['scheme__name'], 'count': r['n'], 'members': r['members'],
                  'planned': money(r['p'] or 0), 'paid': money(r['paid'] or 0)}
                 for r in qs.values('scheme__code', 'scheme__name').annotate(n=Count('id'), members=Count('customer', distinct=True),
                                                                             p=Sum('planned_contribution'), paid=Sum('contribution_paid'))
                 .order_by('scheme__code')]
    from django.db.models.functions import TruncMonth
    by_month = [{'month': r['m'], 'count': r['n'], 'planned': money(r['p'] or 0)}
                for r in qs.annotate(m=TruncMonth('enrollment_date')).values('m').annotate(n=Count('id'), p=Sum('planned_contribution')).order_by('-m')]
    return {'total': qs.count(), 'members': qs.values('customer').distinct().count(), 'by_status': by_status, 'by_scheme': by_scheme,
            'by_month': by_month}


def pending_by_member(tenant, *, as_of=None, scheme=None, location=None, overdue_only=False):
    """Per scheme account: installments due up to today but not fully paid, and what is still to come."""
    as_of = as_of or dj_timezone.localdate()
    qs = SchemeInstallment.objects.filter(tenant=tenant, enrollment__status__in=SchemeEnrollment.CONTRIBUTING) \
        .exclude(status__in=SchemeInstallment.SETTLED).select_related('enrollment', 'enrollment__scheme', 'enrollment__branch')
    if scheme:
        qs = qs.filter(enrollment__scheme=scheme)
    if location:
        qs = qs.filter(enrollment__branch=location)
    rows = {}
    for inst in qs.order_by('enrollment_id', 'installment_no'):
        e = inst.enrollment
        row = rows.setdefault(e.pk, {'enrollment': e, 'pending_count': 0, 'pending_amount': ZERO, 'overdue_count': 0, 'overdue_amount': ZERO,
                                     'oldest_due': None, 'days_late': 0, 'future_amount': ZERO, 'next_due': None})
        if inst.due_date <= as_of:
            row['pending_count'] += 1
            row['pending_amount'] += inst.outstanding
            row['oldest_due'] = row['oldest_due'] or inst.due_date
            row['days_late'] = max(row['days_late'], (as_of - inst.due_date).days)
            if inst.grace_end < as_of:
                row['overdue_count'] += 1
                row['overdue_amount'] += inst.outstanding
        else:
            row['future_amount'] += inst.outstanding
            row['next_due'] = row['next_due'] or inst
    result = [r for r in rows.values() if r['pending_count'] and (r['overdue_count'] or not overdue_only)]
    result.sort(key=lambda r: (-r['days_late'], r['enrollment'].account_no))
    totals = {'members': len(result), 'pending_amount': money(sum((r['pending_amount'] for r in result), ZERO)),
              'overdue_amount': money(sum((r['overdue_amount'] for r in result), ZERO)),
              'installments': sum(r['pending_count'] for r in result)}
    return result, totals
