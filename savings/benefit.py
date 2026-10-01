"""Scheme Benefit Engine - the only place the jeweller benefit is calculated.

Pure functions over an enrolment's rule snapshot and its installment history. Nothing here writes to the
database; ``services.mature`` stores the result as a ``BenefitCalculation`` and approval posts it.

Tiered benefit is a slab rate: the tier the eligible contribution falls into sets the % applied to the whole
eligible contribution.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal('0')
CENT = Decimal('0.01')


def money(value):
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def d(value):
    return Decimal(str(value or 0))


@dataclass
class BenefitResult:
    scheduled_contribution: Decimal = ZERO
    paid_contribution: Decimal = ZERO
    eligible_contribution: Decimal = ZERO
    outstanding_contribution: Decimal = ZERO
    installments_paid: int = 0
    installments_missed: int = 0
    installments_late: int = 0
    installments_partial: int = 0
    gross_benefit: Decimal = ZERO
    benefit: Decimal = ZERO
    eligibility: str = 'ELIGIBLE'
    explanation: list = field(default_factory=list)

    @property
    def entitlement(self):
        return self.paid_contribution + self.benefit


def installment_eligible_amount(rules, installment):
    """Contribution of one installment that counts for the benefit."""
    paid = min(d(installment.paid_amount), d(installment.cap_amount) or d(installment.scheduled_amount))
    if installment.status in ('WAIVED', 'CANCELLED'):
        return ZERO
    if rules.get('benefit_eligibility') == 'ON_TIME' and installment.settled_date and not installment.paid_within_grace:
        return ZERO
    return money(paid)


def gross_benefit(rules, *, installment_amount, paid, eligible):
    kind, value = rules.get('benefit_type'), d(rules.get('benefit_value'))
    if kind == 'FIXED':
        return money(value), f'Fixed benefit ₹{money(value)}'
    if kind == 'ONE_INSTALLMENT':
        return money(installment_amount), f'One installment = ₹{money(installment_amount)}'
    if kind == 'PERCENT_CONTRIBUTION':
        return money(paid * value / 100), f'{value.normalize():f}% of contribution ₹{money(paid)}'
    if kind == 'PERCENT_ELIGIBLE':
        return money(eligible * value / 100), f'{value.normalize():f}% of eligible contribution ₹{money(eligible)}'
    if kind == 'TIERED':
        for tier in rules.get('benefit_tiers') or []:
            low, high = d(tier.get('from')), tier.get('to')
            if eligible >= low and (high in (None, '') or eligible <= d(high)):
                pct = d(tier.get('percent'))
                return money(eligible * pct / 100), f'Tier ₹{money(low)}–{"∞" if high in (None, "") else f"₹{money(d(high))}"}: {pct.normalize():f}% of ₹{money(eligible)}'
        return ZERO, 'No tier matches the eligible contribution'
    return ZERO, 'Scheme has no benefit'


def _clamp(rules, benefit, paid, notes):
    minimum, maximum = d(rules.get('min_benefit')), d(rules.get('max_benefit'))
    if maximum and benefit > maximum:
        notes.append(f'Capped at maximum benefit ₹{money(maximum)}')
        benefit = maximum
    if minimum and ZERO < benefit < minimum:
        notes.append(f'Raised to minimum benefit ₹{money(minimum)}')
        benefit = minimum
    cap = d(rules.get('max_entitlement'))
    if cap and paid + benefit > cap:
        benefit = max(cap - paid, ZERO)
        notes.append(f'Limited by maximum entitlement ₹{money(cap)}')
    return money(benefit)


def calculate(enrollment, installments, as_of=None):
    """Benefit for an enrolment as of a date, from its signed rules and actual installment history."""
    rules = enrollment.rules
    as_of = as_of or date.today()
    result = BenefitResult()
    notes = result.explanation
    required = len(installments)
    grace = int(rules.get('grace_days') or 0)
    fully_paid_eligible = 0
    for inst in installments:
        scheduled, paid = d(inst.scheduled_amount), d(inst.paid_amount)
        if inst.status in ('WAIVED', 'CANCELLED'):
            continue
        result.scheduled_contribution += scheduled
        result.paid_contribution += paid
        eligible = installment_eligible_amount(rules, inst)
        result.eligible_contribution += eligible
        settled = paid >= scheduled and scheduled > 0
        if settled:
            result.installments_paid += 1
            if inst.paid_within_grace:
                if eligible:
                    fully_paid_eligible += 1
            else:
                result.installments_late += 1
                if rules.get('benefit_eligibility') != 'ON_TIME':
                    fully_paid_eligible += 1
        else:
            result.outstanding_contribution += scheduled - paid
            if paid > 0:
                result.installments_partial += 1
            if inst.due_date + timedelta(days=grace) < as_of:
                result.installments_missed += 1
    result.paid_contribution = money(result.paid_contribution + d(enrollment.unallocated_advance))
    result.eligible_contribution = money(result.eligible_contribution)
    gross, why = gross_benefit(rules, installment_amount=d(enrollment.installment_amount),
                               paid=result.paid_contribution, eligible=result.eligible_contribution)
    result.gross_benefit = gross
    notes.append(why)
    notes.append(f'{result.installments_paid}/{required} installments paid, {result.installments_late} late, '
                 f'{result.installments_missed} missed, {result.installments_partial} partially paid')

    factor, eligibility = Decimal('1'), 'ELIGIBLE'
    missed = result.installments_missed
    rule = rules.get('missed_installment_rule')
    max_missed = int(rules.get('max_missed_installments') or 0)
    if missed:
        if rule == 'INELIGIBLE':
            factor, eligibility = ZERO, 'NOT_ELIGIBLE'
            notes.append(f'{missed} missed installment(s): scheme rule forfeits the benefit')
        elif max_missed and missed > max_missed:
            factor, eligibility = ZERO, 'NOT_ELIGIBLE'
            notes.append(f'{missed} missed installment(s) exceed the allowed {max_missed}')
        elif rule == 'REDUCE_BENEFIT':
            cut = d(rules.get('missed_reduction_percent')) * missed
            factor -= cut / 100
            eligibility = 'REDUCED'
            notes.append(f'Reduced {cut.normalize():f}% for {missed} missed installment(s)')
        else:
            factor, eligibility = ZERO, 'PENDING'
            notes.append(f'{missed} installment(s) still to be paid before the benefit is due')

    if eligibility not in ('NOT_ELIGIBLE', 'PENDING'):
        if rules.get('benefit_eligibility') == 'PRO_RATA' and rules.get('benefit_type') in ('FIXED', 'ONE_INSTALLMENT') and required:
            share = Decimal(fully_paid_eligible) / Decimal(required)
            if share < 1:
                factor *= share
                eligibility = 'REDUCED'
                notes.append(f'Pro-rata: {fully_paid_eligible}/{required} eligible installments')
        late, late_rule = result.installments_late, rules.get('late_payment_rule')
        if late:
            if late_rule == 'BENEFIT_REDUCTION':
                cut = d(rules.get('late_payment_value')) * late
                factor -= cut / 100
                eligibility = 'REDUCED'
                notes.append(f'Reduced {cut.normalize():f}% for {late} late installment(s)')
            elif rules.get('benefit_eligibility') == 'ON_TIME' and rules.get('benefit_type') in ('FIXED', 'ONE_INSTALLMENT'):
                factor, eligibility = ZERO, 'NOT_ELIGIBLE'
                notes.append(f'{late} installment(s) paid after the grace period; benefit requires on-time payment')
        elif rules.get('benefit_eligibility') in ('ALL_PAID', 'ON_TIME') and result.installments_paid < required and eligibility == 'ELIGIBLE':
            factor, eligibility = ZERO, 'PENDING'
            notes.append('All installments must be paid for the benefit')

    benefit = money(gross * max(factor, ZERO))
    if gross and not benefit and eligibility == 'REDUCED':
        eligibility = 'NOT_ELIGIBLE'
    result.benefit = _clamp(rules, benefit, result.paid_contribution, notes) if benefit else ZERO
    result.eligibility = eligibility if (gross or rules.get('benefit_type') == 'NONE') else 'NOT_ELIGIBLE'
    if rules.get('benefit_type') == 'NONE':
        result.eligibility = 'ELIGIBLE'
    return result


def expected(rules, installment_amount, installments):
    """Benefit if every installment is paid on time - shown at enrolment and on the agreement."""
    planned = money(d(installment_amount) * installments)
    gross, _ = gross_benefit(rules, installment_amount=d(installment_amount), paid=planned, eligible=planned)
    return _clamp(rules, gross, planned, [])
