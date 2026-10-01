"""Pure manufacturing calculations: requirements, formulas, purity, runtime, capacity and scheduling.

Nothing in this module touches the database except reading calendar exceptions, so every screen,
the API, the planning engine and the posting engine compute the same numbers.
"""
import ast
import operator
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.utils import timezone as dj_timezone

ZERO = Decimal('0')
QTY_PLACES = Decimal('0.001')
MONEY_PLACES = Decimal('0.01')
DEFAULT_DAY_MINUTES = 480


class FormulaError(ValueError):
    pass


def D(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value if value not in (None, '') else 0))
    except InvalidOperation:
        raise FormulaError(f'Not a number: {value}')


def q3(value):
    return D(value).quantize(QTY_PLACES, rounding=ROUND_HALF_UP)


def money(value):
    return D(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Purity / fine weight
# ---------------------------------------------------------------------------

def purity_factor(purity):
    """'22K' -> 0.9167, '916' -> 0.916, '91.6' -> 0.916, '999' -> 0.999, '' -> 1."""
    text = (purity or '').strip().upper().replace('CT', 'K')
    if not text:
        return Decimal('1')
    try:
        if text.endswith('K'):
            return (D(text[:-1]) / 24).quantize(Decimal('0.0001'))
        number = D(text)
    except (FormulaError, InvalidOperation):
        return Decimal('1')
    if number <= 1:
        return number
    if number <= 100:
        return (number / 100).quantize(Decimal('0.0001'))
    return (number / 1000).quantize(Decimal('0.0001'))


def fine_weight(net_weight, purity):
    return q3(D(net_weight) * purity_factor(purity))


# ---------------------------------------------------------------------------
# Safe formula evaluation (formula-based BOM consumption)
# ---------------------------------------------------------------------------

FORMULA_VARIABLES = ('qty', 'qty_per', 'net_weight', 'gross_weight', 'stone_weight', 'wastage_percent', 'loss_percent', 'scrap_percent')
_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCTIONS = {'min': min, 'max': max, 'round': lambda value, places=3: D(value).quantize(Decimal(1).scaleb(-int(places)))}


def evaluate_formula(expression, variables):
    """Evaluate an arithmetic formula over the whitelisted variables with Decimal precision. No other names or calls."""
    try:
        tree = ast.parse(expression or '', mode='eval')
    except SyntaxError:
        raise FormulaError(f'Invalid formula: {expression}')

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return D(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise FormulaError(f'Unknown variable "{node.id}". Allowed: {", ".join(FORMULA_VARIABLES)}.')
            return D(variables[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise FormulaError('Division by zero in formula.')
            return _OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
            return D(_FUNCTIONS[node.func.id](*[walk(arg) for arg in node.args]))
        raise FormulaError(f'Unsupported expression in formula: {expression}')

    return walk(tree)


# ---------------------------------------------------------------------------
# Material requirement
# ---------------------------------------------------------------------------

def per_piece_requirement(*, basis, quantity, base_quantity=1, formula='', parent):
    """Requirement of a component for ONE finished piece, before scrap and loss.

    `parent` carries the parent's expected weights: net_weight, gross_weight, stone_weight, wastage_percent.
    """
    base = D(base_quantity) or Decimal('1')
    if basis in ('QUANTITY', 'WEIGHT'):
        return D(quantity) / base
    if basis == 'PERCENT':
        return D(parent.get('net_weight')) * D(quantity) / 100
    if basis == 'FORMULA':
        variables = {name: D(parent.get(name)) for name in ('net_weight', 'gross_weight', 'stone_weight', 'wastage_percent')}
        variables.update(qty=parent.get('qty', 1), qty_per=D(quantity) / base, loss_percent=parent.get('loss_percent', 0),
                         scrap_percent=parent.get('scrap_percent', 0))
        return evaluate_formula(formula, variables)
    raise FormulaError(f'Unknown consumption basis {basis}.')


def component_requirement(*, per_piece, production_qty, scrap_percent=0, loss_percent=0, fixed_scrap=0, fixed_loss=0):
    """Gross requirement = per piece x production qty; planned requirement adds scrap %, process loss % and fixed scrap.

    8.50 g/pc x 100 pcs = 850 g; 7% wastage = 59.50 g; planned = 909.50 g.
    """
    gross = q3(D(per_piece) * D(production_qty))
    scrap = q3(gross * D(scrap_percent) / 100 + gross * D(loss_percent) / 100 + D(fixed_scrap) + D(fixed_loss))
    return {'gross': gross, 'scrap': scrap, 'expected': gross + scrap}


def remaining_requirement(*, expected, consumed, issued_open, reserved):
    """Gross requirement - already consumed - issued (not yet consumed) - reserved = still to source."""
    return max(D(expected) - D(consumed) - D(issued_open) - D(reserved), ZERO)


# ---------------------------------------------------------------------------
# Runtime, capacity and KPIs
# ---------------------------------------------------------------------------

def operation_capacity_minutes(*, setup_time, run_time, quantity, efficiency=100, concurrent_capacity=1):
    """Capacity need of an operation: setup + run x qty, adjusted for efficiency and parallel capacity."""
    efficiency = D(efficiency) or Decimal('100')
    parallel = D(concurrent_capacity) or Decimal('1')
    run = D(run_time) * D(quantity) * 100 / efficiency / parallel
    return {'setup': q3(setup_time), 'run': q3(run), 'total': q3(D(setup_time) + run)}


def total_production_minutes(*, setup=0, run=0, wait=0, move=0, queue=0, downtime=0):
    return D(setup) + D(run) + D(wait) + D(move) + D(queue) + D(downtime)


def runtime_cost(*, setup_minutes, run_minutes, labour_rate, machine_rate, overhead_rate, overhead_enabled=True):
    hours = (D(setup_minutes) + D(run_minutes)) / 60
    labour = money(hours * D(labour_rate))
    machine = money(hours * D(machine_rate))
    overhead = money(hours * D(overhead_rate)) if overhead_enabled else ZERO
    return {'hours': hours, 'labour': labour, 'machine': machine, 'overhead': overhead, 'total': labour + machine + overhead}


def efficiency_percent(standard_minutes, actual_minutes):
    return (D(standard_minutes) / D(actual_minutes) * 100).quantize(Decimal('0.1')) if D(actual_minutes) else None


def percent(part, whole):
    return (D(part) / D(whole) * 100).quantize(Decimal('0.1')) if D(whole) else None


# ---------------------------------------------------------------------------
# Shop calendar and scheduling
# ---------------------------------------------------------------------------

def _calendar_exceptions(calendar, start, end):
    if calendar is None:
        return {}
    return {line.date: line for line in calendar.lines.filter(date__gte=start, date__lte=end)}


def day_capacity(calendar, day, exceptions=None, resources=1, override_minutes=0):
    """Working minutes available on `day` (calendar pattern + holidays/overtime) times parallel resources."""
    if calendar is None:
        minutes = override_minutes or DEFAULT_DAY_MINUTES
    else:
        exceptions = exceptions if exceptions is not None else _calendar_exceptions(calendar, day, day)
        line = exceptions.get(day)
        minutes = (override_minutes or calendar.daily_minutes) if calendar.works_on(day) else 0
        if line is not None:
            if line.line_type in ('HOLIDAY', 'SHUTDOWN'):
                minutes = 0
            elif line.line_type == 'OVERTIME':
                minutes += line.minutes
            elif line.line_type == 'REDUCED':
                minutes = min(minutes, line.minutes)
    return D(minutes) * (D(resources) or Decimal('1'))


def _day_start(calendar, day):
    return dj_timezone.make_aware(datetime.combine(day, calendar.start_time if calendar else time(9, 0)))


def schedule_forward(start, minutes, calendar=None, resources=1, override_minutes=0, max_days=730):
    """Walk working days from `start` consuming `minutes` of capacity. Returns the finish datetime."""
    minutes = D(minutes)
    day = dj_timezone.localtime(start).date() if dj_timezone.is_aware(start) else start.date()
    current = start
    exceptions = _calendar_exceptions(calendar, day, day + timedelta(days=max_days))
    for _ in range(max_days):
        available = day_capacity(calendar, day, exceptions, resources, override_minutes)
        if available > 0:
            day_open = max(current, _day_start(calendar, day))
            used_before = D((day_open - _day_start(calendar, day)).total_seconds() / 60) * (D(resources) or 1)
            left_today = max(available - used_before, ZERO)
            if minutes <= left_today:
                return day_open + timedelta(minutes=float(minutes / (D(resources) or 1)))
            minutes -= left_today
        day += timedelta(days=1)
        current = _day_start(calendar, day)
    return current


def schedule_backward(end, minutes, calendar=None, resources=1, override_minutes=0, max_days=730):
    """Walk working days backwards from `end` so the operation finishes by `end`. Returns the start datetime."""
    minutes = D(minutes)
    day = dj_timezone.localtime(end).date() if dj_timezone.is_aware(end) else end.date()
    exceptions = _calendar_exceptions(calendar, day - timedelta(days=max_days), day)
    for _ in range(max_days):
        available = day_capacity(calendar, day, exceptions, resources, override_minutes)
        if available > 0:
            if minutes <= available:
                return _day_start(calendar, day) + timedelta(minutes=float((available - minutes) / (D(resources) or 1)))
            minutes -= available
        day -= timedelta(days=1)
    return _day_start(calendar, day)
