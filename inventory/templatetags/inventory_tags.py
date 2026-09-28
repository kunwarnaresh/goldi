from decimal import Decimal

from django import template
from django.utils.html import format_html

register = template.Library()

BADGE_COLOURS = {
    'green': ('AVAILABLE', 'CLOSED', 'POSTED', 'RECEIVED', 'APPROVED', 'VALIDATED', 'IMPORTED', 'CONVERTED', 'ACTIVE'),
    'amber': ('PENDING_APPROVAL', 'SUBMITTED', 'PARTIALLY_SHIPPED', 'PARTIALLY_RECEIVED', 'RESERVED', 'PICKED', 'QC', 'REQUESTED',
              'COUNTING', 'SUGGESTED', 'UPLOADED'),
    'sky': ('SHIPPED', 'IN_TRANSIT', 'RELEASED', 'DRAFT', 'OPEN'),
    'red': ('CANCELLED', 'REJECTED', 'DAMAGED', 'MISSING', 'SCRAPPED', 'BLOCKED', 'FAILED', 'REPAIR'),
}
CLASSES = {'green': 'bg-emerald-100 text-emerald-800', 'amber': 'bg-amber-100 text-amber-800', 'sky': 'bg-sky-100 text-sky-800',
           'red': 'bg-rose-100 text-rose-800', 'grey': 'bg-slate-100 text-slate-700'}


@register.simple_tag
def badge(status, label=None):
    colour = next((c for c, statuses in BADGE_COLOURS.items() if status in statuses), 'grey')
    return format_html('<span class="px-2 py-0.5 rounded text-xs font-semibold whitespace-nowrap {}">{}</span>',
                       CLASSES[colour], label or str(status).replace('_', ' ').title())


@register.filter
def qty(value):
    """20.000 -> 20, 12.450 -> 12.45; blank for None."""
    if value in (None, ''):
        return ''
    text = format(Decimal(str(value)).normalize(), 'f')
    return '0' if text == '-0' else text


@register.filter
def grams(value):
    if value in (None, ''):
        return ''
    return f'{Decimal(str(value)):,.3f}'


@register.filter
def inr(value):
    if value in (None, ''):
        return ''
    return f'₹{Decimal(str(value)):,.2f}'


@register.filter
def neg(value):
    return -value if value is not None else None
