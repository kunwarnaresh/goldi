from decimal import Decimal, ROUND_HALF_UP
import re


def round_money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def calculate_gst(amount, gst_rate):
    taxable = round_money(amount)
    rate = Decimal(str(gst_rate))
    gst = (taxable * rate / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total = (taxable + gst).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return taxable, gst, total


def validate_gstin(value):
    if not value:
        return False
    value = value.strip().upper()
    return bool(re.fullmatch(r'^[0-9A-Z]{15}$', value))


def validate_pan(value):
    if not value:
        return False
    value = value.strip().upper()
    return bool(re.fullmatch(r'^[A-Z]{5}[0-9]{4}[A-Z]$', value))


def estimate_income_tax(profit_before_tax):
    profit = Decimal(str(profit_before_tax))
    tax = profit * Decimal('0.30')
    cess = tax * Decimal('0.04')
    total = tax + cess
    return round_money(tax), round_money(cess), round_money(total)
