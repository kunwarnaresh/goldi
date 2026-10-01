import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from .compliance import calculate_gst as calculate_gst_flat
from .models import (
    AuditLog, Company, DocumentNumberSeries, DocumentRelationship, DocumentStatusHistory,
    FinanceJournalBatch, FinanceJournalTemplate, FinancePostedVoucher, FinancePostedVoucherLine,
    FinancePostingSetup, FinanceVoucher, FinanceVoucherLine, FinanceVoucherType,
    GeneralLedger, GLAccount, GSTRate, JournalEntry,
    JournalEntryLine, BankTransaction, CustomerFinanceProfile, JewelleryItemUnit,
    JewelleryMetalRate, PaymentAllocation, PaymentReceipt, Quotation, QuotationLine,
    SalesInvoice, SalesInvoiceItem, SalesOrder, SalesOrderLine,
    GoodsReceipt, GoodsReceiptLine, InventoryMovement, PurchaseOrder, PurchaseOrderLine,
    Supplier, SupplierInvoice, SupplierInvoiceLine, VendorFinanceProfile,
    VendorPayment, VendorPaymentAllocation,
)


def calculate_jewellery_price(*, unit, metal_rate, pricing_rule, tax_rate_code, discount=Decimal('0'), interstate=False, transaction_date=None):
    """Return one authoritative jewellery line price plus its immutable breakdown."""
    transaction_date = transaction_date or date.today()
    money = lambda value: Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    gross_weight = Decimal(str(unit.gross_weight or 0))
    stone_weight = Decimal(str(unit.stone_weight or 0))
    other_weight = Decimal(str(unit.other_weight or 0))
    net_weight = max(gross_weight - stone_weight - other_weight, Decimal('0'))
    rate = Decimal(str(metal_rate.rate_per_gram or 0))
    metal_value = money(net_weight * rate)
    wastage_weight = (net_weight * pricing_rule.wastage_percent / Decimal('100')).quantize(Decimal('0.001'))
    wastage_value = money(wastage_weight * rate if pricing_rule.wastage_method == 'weight' else metal_value * pricing_rule.wastage_percent / Decimal('100'))
    if pricing_rule.making_method == 'per_gram':
        making_value = money(net_weight * pricing_rule.making_rate)
    elif pricing_rule.making_method == 'fixed':
        making_value = money(pricing_rule.making_rate)
    else:
        making_value = money(metal_value * pricing_rule.making_rate / Decimal('100'))
    stone_values = {stone.stone_type: money(sum((stone.net_value for stone in unit.stones.filter()), Decimal('0'))) for stone in unit.stones.all()}
    stone_value = money(sum(stone_values.values(), Decimal('0')))
    taxable_value = max(metal_value + wastage_value + making_value + stone_value + pricing_rule.hallmark_charge + pricing_rule.certification_charge + pricing_rule.other_charges - Decimal(str(discount or 0)), Decimal('0'))
    tax = calculate_gst(amount=taxable_value, rate_code=tax_rate_code, interstate=interstate, transaction_date=transaction_date)
    tax_total = money(tax['cgst'] + tax['sgst'] + tax['utgst'] + tax['igst'] + tax['cess'])
    final_amount = money(taxable_value + tax_total)
    return {
        'gross_weight': gross_weight, 'stone_weight': stone_weight, 'other_weight': other_weight,
        'net_metal_weight': net_weight, 'metal_rate': rate, 'metal_value': metal_value,
        'wastage_percent': pricing_rule.wastage_percent, 'wastage_weight': wastage_weight,
        'wastage_value': wastage_value, 'making_method': pricing_rule.making_method,
        'making_rate': pricing_rule.making_rate, 'making_value': making_value,
        'stone_values': stone_values, 'stone_value': stone_value,
        'hallmark_charge': money(pricing_rule.hallmark_charge),
        'certification_charge': money(pricing_rule.certification_charge),
        'other_charges': money(pricing_rule.other_charges), 'discount': money(discount),
        'taxable_value': money(taxable_value), 'tax': tax, 'tax_total': tax_total,
        'final_amount': final_amount,
    }


DEFAULT_MAX_RATE_AGE_MINUTES = 720  # 12 hours - a jewellery store typically refreshes rates a few times a day, not every minute


class ResolvedRate:
    """Duck-typed stand-in for a JewelleryMetalRate exposing only what calculate_jewellery_price reads,
    so a purity-converted rate can be priced through the exact same engine as a directly-maintained one."""
    def __init__(self, rate_per_gram):
        self.rate_per_gram = rate_per_gram


class ProductWeightAsUnit:
    """Duck-typed stand-in for a JewelleryItemUnit, so a non-serialized product can be priced dynamically
    by its own weight_grams through the exact same engine used for serialized jewellery units."""
    class _NoStones:
        def all(self):
            return []

        def filter(self, *args, **kwargs):
            return []

    def __init__(self, product):
        self.gross_weight = product.weight_grams or Decimal('0')
        self.stone_weight = Decimal('0')
        self.other_weight = Decimal('0')
        self.stones = self._NoStones()


class ManualUnit:
    """Duck-typed stand-in for a JewelleryItemUnit built from raw weights (the Price Simulator's ad-hoc inputs)."""
    def __init__(self, gross_weight, stone_weight=Decimal('0'), other_weight=Decimal('0')):
        self.gross_weight = gross_weight
        self.stone_weight = stone_weight
        self.other_weight = other_weight
        self.stones = ProductWeightAsUnit._NoStones()


class ManualPricingRule:
    """Duck-typed stand-in for a JewelleryPricingRule, for the Price Simulator's ad-hoc 'what-if' inputs
    (no saved product/rule required) - priced through the exact same calculate_jewellery_price engine."""
    def __init__(self, *, making_method='per_gram', making_rate=Decimal('0'), wastage_method='weight',
                 wastage_percent=Decimal('0'), tax_rate_code='GST-3', hallmark_charge=Decimal('0'),
                 certification_charge=Decimal('0'), other_charges=Decimal('0')):
        self.making_method = making_method
        self.making_rate = making_rate
        self.wastage_method = wastage_method
        self.wastage_percent = wastage_percent
        self.tax_rate_code = tax_rate_code
        self.hallmark_charge = hallmark_charge
        self.certification_charge = certification_charge
        self.other_charges = other_charges


def resolve_current_metal_rate(*, metal_type, purity, store=None, as_of=None, max_age_minutes=None):
    """The Current Metal Rate Service: resolve the applicable rate for (metal, purity[, store]) by trying,
    in order: (1) a store-specific rate for this exact purity, (2) a company-wide rate for this purity,
    (3) fine-metal conversion from any other purity of the same metal (store-specific, then company-wide),
    using the same purity factors the live-rate fetch uses. Never silently uses a different metal's rate.

    Returns a dict with 'resolved' (bool) and, when True, 'rate_per_gram' (the rate to actually bill at -
    already purity-converted where applicable), 'purity_used', 'is_purity_converted', 'is_store_fallback',
    'is_stale' and 'age_minutes' so callers can surface exactly what was used.
    """
    as_of = as_of or dj_timezone.now()
    max_age_minutes = DEFAULT_MAX_RATE_AGE_MINUTES if max_age_minutes is None else max_age_minutes

    def active_rates(metal_type_, purity_, store_):
        return JewelleryMetalRate.objects.filter(
            metal_type=metal_type_, purity=purity_, store=store_, is_active=True, effective_from__lte=as_of,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of)).order_by('-effective_from')

    def result(rate, rate_per_gram, purity_used, is_purity_converted, is_store_fallback):
        age_minutes = (as_of - rate.effective_from).total_seconds() / 60
        return {
            'resolved': True, 'rate': rate, 'rate_per_gram': rate_per_gram, 'purity_used': purity_used,
            'is_purity_converted': is_purity_converted, 'is_store_fallback': is_store_fallback,
            'is_stale': age_minutes > max_age_minutes, 'age_minutes': round(age_minutes, 1),
        }

    not_resolved = {
        'resolved': False, 'rate': None, 'rate_per_gram': None, 'purity_used': None,
        'is_purity_converted': False, 'is_store_fallback': False, 'is_stale': False, 'age_minutes': None,
    }

    if store is not None:
        rate = active_rates(metal_type, purity, store).first()
        if rate:
            return result(rate, Decimal(str(rate.rate_per_gram)), purity, False, False)

    rate = active_rates(metal_type, purity, None).first()
    if rate:
        return result(rate, Decimal(str(rate.rate_per_gram)), purity, False, store is not None)

    factors = {'gold': GOLD_PURITY_FACTORS, 'silver': SILVER_PURITY_FACTORS}.get(metal_type)
    if factors and purity in factors:
        target_factor = factors[purity]
        candidates = []
        for candidate_store in ([store, None] if store is not None else [None]):
            for candidate_purity, candidate_factor in factors.items():
                if candidate_purity == purity:
                    continue
                candidate_rate = active_rates(metal_type, candidate_purity, candidate_store).first()
                if candidate_rate:
                    candidates.append((candidate_rate, candidate_purity, candidate_factor, candidate_store))
        if candidates:
            candidates.sort(key=lambda c: c[0].effective_from, reverse=True)
            candidate_rate, candidate_purity, candidate_factor, candidate_store = candidates[0]
            converted = (Decimal(str(candidate_rate.rate_per_gram)) * target_factor / candidate_factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return result(candidate_rate, converted, candidate_purity, True, candidate_store is None and store is not None)

    return not_resolved


def amount_in_words(value):
    """Return a compact Indian-style INR amount in words for printed invoices."""
    ones = ['', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE', 'TEN', 'ELEVEN', 'TWELVE', 'THIRTEEN', 'FOURTEEN', 'FIFTEEN', 'SIXTEEN', 'SEVENTEEN', 'EIGHTEEN', 'NINETEEN']
    tens = ['', '', 'TWENTY', 'THIRTY', 'FORTY', 'FIFTY', 'SIXTY', 'SEVENTY', 'EIGHTY', 'NINETY']
    def under_hundred(number):
        return ones[number] if number < 20 else tens[number // 10] + (' ' + ones[number % 10] if number % 10 else '')
    def integer_words(number):
        if number < 100: return under_hundred(number)
        if number < 1000: return ones[number // 100] + ' HUNDRED' + (' ' + under_hundred(number % 100) if number % 100 else '')
        for divisor, label in ((10000000, 'CRORE'), (100000, 'LAKH'), (1000, 'THOUSAND')):
            if number >= divisor:
                return integer_words(number // divisor) + f' {label}' + (' ' + integer_words(number % divisor) if number % divisor else '')
        return ''
    amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    result = (integer_words(rupees) or 'ZERO') + ' RUPEES'
    return result + (f' AND {integer_words(paise)} PAISE' if paise else '') + ' ONLY'


def build_invoice_dataset(invoice, setting=None):
    items = list(invoice.items.select_related('product', 'jewellery_unit').all())
    payments = list(invoice.payments.filter(status__in=['pending', 'posted']).all())
    store = invoice.pos_terminal.store if invoice.pos_terminal_id else None
    return {
        'invoice': invoice, 'items': items, 'payments': payments, 'setting': setting,
        'store': store, 'amount_words': amount_in_words(invoice.total_amount),
        'tax_words': amount_in_words(invoice.gst_amount),
        'subtotal': invoice.subtotal, 'discount': invoice.discount_amount,
        'taxable': invoice.taxable_amount, 'gst': invoice.gst_amount, 'total': invoice.total_amount,
    }


def render_invoice_pdf(dataset, print_type='A4_INVOICE'):
    """Render the premium A4 invoice; A5 uses the same data with a compact layout."""
    buffer = BytesIO()
    page_size = A5 if print_type == 'A5_INVOICE' else A4
    margin = 28 if print_type == 'A5_INVOICE' else 32
    content_width = page_size[0] - (2 * margin)
    document = SimpleDocTemplate(buffer, pagesize=page_size, rightMargin=margin, leftMargin=margin, topMargin=36, bottomMargin=38)
    invoice = dataset['invoice']
    setting = dataset['setting']
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='InvoiceBody', parent=styles['Normal'], fontName='Helvetica', fontSize=8.2, leading=10.5, textColor=colors.HexColor('#26343d')))
    styles.add(ParagraphStyle(name='InvoiceSmall', parent=styles['Normal'], fontName='Helvetica', fontSize=7.2, leading=9, textColor=colors.HexColor('#52636c')))
    styles.add(ParagraphStyle(name='InvoiceSection', parent=styles['Heading3'], fontName='Helvetica-Bold', fontSize=8.5, leading=10, textColor=colors.HexColor('#0d5660'), spaceBefore=7, spaceAfter=4))
    styles.add(ParagraphStyle(name='InvoiceTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=15, leading=18, alignment=1, textColor=colors.HexColor('#10252d'), spaceAfter=5))
    styles.add(ParagraphStyle(name='InvoiceTotal', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12, leading=15, alignment=2, textColor=colors.HexColor('#0d5660')))

    def p(text, style='InvoiceBody', markup=False):
        value = str(text or '')
        if not markup:
            value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return Paragraph(value.replace('\n', '<br/>'), styles[style])

    def money(value):
        return f'₹ {Decimal(str(value or 0)):,.2f}'

    company_name = setting.company_name if setting else 'Goldi ERP'
    company_address = ', '.join(part for part in [setting.address if setting else '', setting.city if setting else '', f'State {setting.state_code}' if setting else '', 'India'] if part)
    company_meta = ' &nbsp; | &nbsp; '.join(part for part in [f'GSTIN: {setting.gstin}' if setting and setting.gstin else '', f'PAN: {setting.pan}' if setting and setting.pan else '', f'Phone: {setting.phone}' if setting and setting.phone else '', f'Email: {setting.email}' if setting and setting.email else ''] if part)
    customer = invoice.customer
    bill_to = '<b>BILL TO</b><br/>' + '<b>' + customer.name + '</b><br/>' + f'Customer No: {getattr(customer, "customer_no", "N/A") or "N/A"}<br/>' + f'Mobile: {customer.phone or "N/A"}<br/>' + f'GSTIN: {customer.gstin or "N/A"}<br/>' + f'PAN: {customer.pan or "N/A"}<br/>' + (customer.address or 'Billing address not recorded')
    ship_to = '<b>SHIP TO</b><br/>Same as Billing Address'
    if getattr(customer, 'shipping_addresses', None):
        shipping = customer.shipping_addresses.filter(is_default=True).first() or customer.shipping_addresses.filter(is_active=True).first()
        if shipping:
            ship_to = '<b>SHIP TO</b><br/>' + f'<b>{shipping.recipient_name or customer.name}</b><br/>{shipping.address_1}<br/>{shipping.city}, {shipping.state} - {shipping.pin_code}<br/>GSTIN: {shipping.gstin or "N/A"}<br/>Mobile: {shipping.mobile or customer.phone or "N/A"}'

    story = [p(company_name, 'InvoiceTitle'), p(company_address, 'InvoiceBody'), p(company_meta, 'InvoiceSmall', markup=True), Spacer(1, 7), p('TAX INVOICE', 'InvoiceTitle')]
    info = [[p(f'<b>Invoice No.</b><br/>{invoice.invoice_no}', markup=True), p(f'<b>Invoice Date</b><br/>{invoice.sales_date:%d-%b-%Y %I:%M %p}', markup=True), p(f'<b>Store</b><br/>{invoice.store_code_snapshot or "N/A"}', markup=True), p(f'<b>Place of Supply</b><br/>{invoice.place_of_supply or "N/A"}', markup=True)]]
    info_table = Table(info, colWidths=[content_width / 4] * 4)
    info_table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f1f5f6')), ('BOX', (0, 0), (-1, -1), .5, colors.HexColor('#c6d3d7')), ('INNERGRID', (0, 0), (-1, -1), .3, colors.HexColor('#d8e1e4')), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7), ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
    story += [info_table, Spacer(1, 8)]
    parties = Table([[p(bill_to, markup=True), p(ship_to, markup=True)]], colWidths=[content_width / 2] * 2)
    parties.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), .5, colors.HexColor('#c6d3d7')), ('INNERGRID', (0, 0), (-1, -1), .3, colors.HexColor('#d8e1e4')), ('BACKGROUND', (0, 0), (-1, -1), colors.white), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
    story += [parties, p('ITEM DETAILS', 'InvoiceSection')]
    rows = [[p('<b>#</b>', 'InvoiceSmall', markup=True), p('<b>ITEM / DESCRIPTION</b>', 'InvoiceSmall', markup=True), p('<b>SKU</b>', 'InvoiceSmall', markup=True), p('<b>HSN</b>', 'InvoiceSmall', markup=True), p('<b>QTY</b>', 'InvoiceSmall', markup=True), p('<b>MRP</b>', 'InvoiceSmall', markup=True), p('<b>DISCOUNT</b>', 'InvoiceSmall', markup=True), p('<b>NET</b>', 'InvoiceSmall', markup=True), p('<b>GROSS</b>', 'InvoiceSmall', markup=True)]]
    for index, item in enumerate(dataset['items'], 1):
        details = item.product.name
        if item.gross_weight:
            details += f'\n{item.product.metal_type} | {item.product.purity} | Gross {item.gross_weight}g | Net {item.net_metal_weight}g'
            if item.barcode: details += f' | HUID/Barcode: {item.barcode}'
        rows.append([p(index), p(details), p(item.product.sku), p(item.product.hsn_code or 'N/A'), p(f'{item.quantity} PCS'), p(money(item.unit_price)), p(money(item.discount_amount)), p(money(item.taxable_amount)), p(money(item.line_total))])
    item_widths = [14, 85, 35, 30, 25, 38, 42, 55, 65] if print_type == 'A5_INVOICE' else [18, 125, 48, 38, 34, 55, 58, 75, 80]
    item_table = Table(rows, repeatRows=1, colWidths=item_widths)
    item_table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8eff1')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#17343d')), ('LINEBELOW', (0, 0), (-1, 0), .7, colors.HexColor('#6e858c')), ('LINEBELOW', (0, 1), (-1, -1), .25, colors.HexColor('#d6e0e3')), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('ALIGN', (4, 1), (-1, -1), 'RIGHT'), ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    story += [item_table, Spacer(1, 8)]
    totals = [[p('<b>AMOUNT SUMMARY</b>', 'InvoiceSmall', markup=True), p('<b>Gross Amount</b><br/>Discount<br/>Net Amount<br/>Taxable Amount<br/>Total GST<br/><b>GRAND TOTAL</b>', markup=True)], [p(f'<b>Amount in words</b><br/>{dataset["amount_words"]}<br/><br/>Cashier: {invoice.cashier_name_snapshot or "N/A"}<br/>Sales Staff: {invoice.sales_staff_name_snapshot or "N/A"}', markup=True), p(f'{money(dataset["subtotal"])}<br/>{money(dataset["discount"])}<br/>{money(dataset["taxable"])}<br/>{money(dataset["taxable"])}<br/>{money(dataset["gst"])}<br/><font size="12" color="#0d5660"><b>{money(dataset["total"])}</b></font>', markup=True)]]
    totals_table = Table(totals, colWidths=[content_width / 2] * 2)
    totals_table.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), .5, colors.HexColor('#c6d3d7')), ('INNERGRID', (0, 0), (-1, -1), .3, colors.HexColor('#d8e1e4')), ('BACKGROUND', (1, 0), (1, -1), colors.HexColor('#f7fafb')), ('ALIGN', (1, 0), (1, -1), 'RIGHT'), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
    story += [totals_table, p('GST SUMMARY', 'InvoiceSection')]
    gst_rate = invoice.gst_rate or 0
    gst_rows = [[p('<b>GST %</b>', 'InvoiceSmall', markup=True), p('<b>Taxable Value</b>', 'InvoiceSmall', markup=True), p('<b>IGST</b>', 'InvoiceSmall', markup=True), p('<b>CGST</b>', 'InvoiceSmall', markup=True), p('<b>SGST</b>', 'InvoiceSmall', markup=True), p('<b>Total Tax</b>', 'InvoiceSmall', markup=True)], [p(f'{gst_rate}%'), p(money(dataset['taxable'])), p(money(0)), p(money(invoice.cgst_amount)), p(money(invoice.sgst_amount)), p(money(dataset['gst']))], [p('<b>Total</b>', markup=True), p(money(dataset['taxable'])), p(money(0)), p(money(invoice.cgst_amount)), p(money(invoice.sgst_amount)), p(money(dataset['gst']))]]
    gst_widths = [40, 88, 65, 65, 65, 66] if print_type == 'A5_INVOICE' else [60, 120, 85, 85, 85, 85]
    gst_table = Table(gst_rows, colWidths=gst_widths)
    gst_table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8eff1')), ('GRID', (0, 0), (-1, -1), .3, colors.HexColor('#c6d3d7')), ('ALIGN', (1, 1), (-1, -1), 'RIGHT'), ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    story += [gst_table, Spacer(1, 7), p(f'<b>Total Tax in words:</b> {dataset["tax_words"]}', markup=True), p('<b>TERMS &amp; CONDITIONS</b>', 'InvoiceSection', markup=True)]
    terms = setting.terms_and_conditions if setting and setting.terms_and_conditions else 'Goods once sold are subject to the applicable exchange and return policy. Please retain this invoice for future reference.'
    story += [p(terms), Spacer(1, 8), p('Thank you for shopping with us. This is an electronically generated document; no signature is required.', 'InvoiceSmall')]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#c6d3d7'))
        canvas.line(margin, 24, page_size[0] - margin, 24)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#52636c'))
        canvas.drawString(margin, 13, f'Invoice: {invoice.invoice_no} | {company_name}')
        canvas.drawRightString(page_size[0] - margin, 13, f'Page {doc.page}')
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def render_thermal_receipt(dataset, width=40):
    invoice = dataset['invoice']; line = '-' * width
    lines = [
        (dataset['setting'].company_name if dataset['setting'] else 'GOLDI ERP').center(width), 'TAX INVOICE'.center(width), line,
        f'Invoice : {invoice.invoice_no}', f'Date    : {invoice.sales_date:%d-%b-%Y %I:%M %p}', f'Cashier : {invoice.cashier_name_snapshot or "N/A"}', f'Sales   : {invoice.sales_staff_name_snapshot or "N/A"}', line,
    ]
    for index, item in enumerate(dataset['items'], 1):
        lines += [f'{index}. {item.product.name[:width - 3]}', f'SKU: {item.product.sku}', f'Qty: {item.quantity}  Total: ₹{item.line_total + item.gst_amount}']
        if item.gross_weight: lines += [f'Gross: {item.gross_weight}g Net: {item.net_metal_weight}g', f'Metal: ₹{item.metal_value} Making: ₹{item.making_charge}', f'Stone: ₹{item.stone_value} Wastage: ₹{item.wastage_value}']
        lines.append(line)
    lines += [f'SUBTOTAL              ₹{dataset["subtotal"]}', f'DISCOUNT              -₹{dataset["discount"]}', f'TAXABLE               ₹{dataset["taxable"]}', f'TOTAL GST              ₹{dataset["gst"]}', f'TOTAL                 ₹{dataset["total"]}', line, 'Amount in Words:', dataset['amount_words'], line, 'Thank You for Shopping With Us', 'This is an electronically generated', 'document, no signature is required.']
    return '\n'.join(lines) + '\n'


DOCUMENT_TYPE_PREFIXES = {
    'customer': 'CUS',
    'vendor': 'VEN',
    'gl_journal': 'JV',
    'quotation': 'QUO',
    'sales_order': 'SO',
    'payment_receipt': 'REC',
    'purchase_order': 'PO',
    'goods_receipt': 'GRN',
    'vendor_payment': 'VPAY',
    'sales_invoice': 'INV',
    'sales_credit_memo': 'SCM',
    'purchase_invoice': 'PINV',
    'purchase_credit_memo': 'PCM',
    'payment': 'PAY',
    'receipt': 'RCT',
    'fixed_asset': 'FA',
    'gst_invoice': 'GST',
    'e_invoice': 'EINV',
    'e_way_bill': 'EWB',
    'transfer_order': 'TO',
    'warehouse_receipt': 'WR',
    'putaway': 'PA',
    'pick': 'PK',
    'warehouse_shipment': 'WS',
    'movement': 'MV',
    'replenishment': 'RP',
    'inventory_adjustment': 'ADJ',
    'item_journal': 'IJ',
    'stock_take': 'ST',
    'cycle_count': 'CC',
    'qc_inspection': 'QC',
    'customer_return': 'RMA',
    'vendor_return': 'RTV',
    'scrap': 'SCR',
    'reclassification': 'REC',
    'cross_dock': 'CD',
    'journal_voucher': 'JV',
    'sales_journal': 'SJ',
    'purchase_journal': 'PJ',
    'cash_receipt': 'CRV',
    'cash_payment': 'CPV',
    'bank_receipt': 'BRV',
    'bank_payment': 'BPV',
    'contra_voucher': 'CON',
    'payment_journal': 'PV',
    'receipt_journal': 'RV',
    'adjustment_journal': 'ADJ',
    'accrual_journal': 'ACC',
    'provision_journal': 'PROV',
    'reversal_journal': 'REV',
    'production_journal': 'PRJ',
    'scheme_journal': 'SCJ',
    'job_work_journal': 'JWJ',
}


def get_fiscal_year(value=None):
    value = value or date.today()
    return value.year if value.month >= 4 else value.year - 1


@transaction.atomic
def get_next_customer_number(*, company=None, store=None):
    from .models import CustomerNumberSeries

    series, _ = CustomerNumberSeries.objects.select_for_update().get_or_create(
        company=company, store=store, defaults={'prefix': 'CUS', 'next_number': 1, 'padding': 6},
    )
    number = f'{series.prefix}-{series.next_number:0{series.padding}d}'
    series.next_number += 1
    series.save(update_fields=['next_number'])
    return number


@transaction.atomic
def get_next_repair_number(store=None):
    from .models import RepairOrder
    year = dj_timezone.now().year
    prefix = f'REP-{store.code}-' if store else 'REP-'
    last = RepairOrder.objects.filter(order_no__startswith=f'{prefix}{year}-').order_by('-id').first()
    next_number = int(last.order_no.rsplit('-', 1)[-1]) + 1 if last else 1
    return f'{prefix}{year}-{next_number:06d}'


@transaction.atomic
def move_customer_ornament(*, repair_order, to_location, status, performed_by=None, remarks=''):
    from .models import RepairCustodyEvent
    ornament = repair_order.ornament
    if ornament.custody_status == 'returned':
        raise ValueError('A returned customer ornament cannot be moved again.')
    old_location = ornament.current_location
    RepairCustodyEvent.objects.create(
        repair_order=repair_order, ornament=ornament, from_location=old_location,
        to_location=to_location, status=status, performed_by=performed_by, remarks=remarks,
    )
    ornament.current_location = to_location
    ornament.custody_status = 'with_karigar' if to_location.startswith('KARIGAR') else 'returned' if to_location == 'CUSTOMER' else 'at_store'
    ornament.save(update_fields=['current_location', 'custody_status'])
    repair_order.status = status
    repair_order.save(update_fields=['status', 'updated_at'])
    return ornament


@transaction.atomic
def get_next_number(document_type, company=None, document_date=None):
    """Reserve and return the next number for a company and fiscal year."""
    if document_type not in DOCUMENT_TYPE_PREFIXES:
        raise ValueError(f'Unsupported document type: {document_type}')

    fiscal_year = get_fiscal_year(document_date)
    series, _ = DocumentNumberSeries.objects.get_or_create(
        company=company,
        document_type=document_type,
        fiscal_year=fiscal_year,
        defaults={'prefix': DOCUMENT_TYPE_PREFIXES[document_type]},
    )
    series = DocumentNumberSeries.objects.select_for_update().get(pk=series.pk)
    number = f'{series.prefix}-{fiscal_year:04d}-{series.next_number:0{series.padding}d}'
    series.next_number += 1
    series.save(update_fields=['next_number', 'updated_at'])
    return number


def calculate_gst(*, amount, rate_code, interstate=False, transaction_date=None):
    """Calculate GST from an effective-dated configured rate; never embeds rates."""
    transaction_date = transaction_date or date.today()
    taxable_amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    rate = GSTRate.objects.get(
        code=rate_code,
        effective_from__lte=transaction_date,
        status='active',
    )
    if rate.effective_to and rate.effective_to < transaction_date:
        raise ValueError('GST rate is not effective for the transaction date.')

    if not rate.taxable:
        return {
            'taxable_amount': taxable_amount,
            'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'),
            'utgst': Decimal('0.00'), 'igst': Decimal('0.00'),
            'cess': Decimal('0.00'), 'rate_code': rate.code,
            'explanation': {'taxability': 'not taxable', 'rate_code': rate.code},
        }

    def component(rate_value):
        return (taxable_amount * rate_value / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    result = {
        'taxable_amount': taxable_amount,
        'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'),
        'utgst': Decimal('0.00'), 'igst': Decimal('0.00'),
        'cess': component(rate.cess_rate), 'rate_code': rate.code,
    }
    if interstate:
        result['igst'] = component(rate.igst_rate)
        component_rule = 'IGST'
    else:
        result['cgst'] = component(rate.cgst_rate)
        result['sgst'] = component(rate.sgst_rate)
        result['utgst'] = component(rate.utgst_rate)
        component_rule = 'CGST + SGST/UTGST'
    result['explanation'] = {
        'taxability': 'taxable', 'rate_code': rate.code,
        'component_rule': component_rule,
        'transaction_date': transaction_date.isoformat(),
    }
    return result


@transaction.atomic
def create_finance_voucher(*, company, batch, voucher_type, user, lines, narration='', document_no='', external_document_no='', voucher_date=None):
    """Create a numbered draft voucher from normalized line payloads."""
    voucher_date = voucher_date or date.today()
    if voucher_type.template.approval_required:
        approval_status = 'pending'
    else:
        approval_status = 'not_required'
    voucher = FinanceVoucher.objects.create(
        company=company,
        batch=batch,
        voucher_type=voucher_type,
        voucher_no=get_next_number(voucher_type.code, company, voucher_date),
        document_no=document_no,
        external_document_no=external_document_no,
        voucher_date=voucher_date,
        document_date=voucher_date,
        narration=narration,
        approval_status=approval_status,
        created_by=user,
    )
    for line in lines:
        FinanceVoucherLine.objects.create(voucher=voucher, **line)
    refresh_finance_totals(voucher)
    AuditLog.objects.create(
        company=company, actor=user, table_name='finance_voucher', action='create',
        record_id=str(voucher.pk), description=f'Created {voucher.voucher_no}',
    )
    return voucher


def refresh_finance_totals(voucher):
    lines = voucher.lines.all()
    voucher.total_debit = sum((line.debit_amount for line in lines), Decimal('0.00'))
    voucher.total_credit = sum((line.credit_amount for line in lines), Decimal('0.00'))
    voucher.total_tax = sum((line.gst_amount for line in lines), Decimal('0.00'))
    voucher.save(update_fields=['total_debit', 'total_credit', 'total_tax'])
    return voucher


@transaction.atomic
def approve_finance_voucher(voucher, user, comments=''):
    voucher = FinanceVoucher.objects.select_for_update().get(pk=voucher.pk)
    refresh_finance_totals(voucher)
    if not voucher.is_balanced:
        raise ValueError('Voucher must be balanced before approval.')
    voucher.status = 'approved'
    voucher.approval_status = 'approved'
    voucher.approved_by = user
    voucher.approved_at = dj_timezone.now()
    voucher.save(update_fields=['status', 'approval_status', 'approved_by', 'approved_at'])
    voucher.approvals.create(approver=user, status='approved', comments=comments, acted_at=dj_timezone.now())
    AuditLog.objects.create(company=voucher.company, actor=user, table_name='finance_voucher', action='approve', record_id=str(voucher.pk), description=f'Approved {voucher.voucher_no}')
    return voucher


@transaction.atomic
def post_finance_voucher(voucher, user):
    voucher = FinanceVoucher.objects.select_for_update().get(pk=voucher.pk)
    refresh_finance_totals(voucher)
    if voucher.status == 'posted':
        return voucher
    if not voucher.is_balanced:
        raise ValueError('Voucher is not balanced.')
    if voucher.voucher_type.template.approval_required and voucher.approval_status != 'approved':
        raise ValueError('Voucher approval is required before posting.')
    if voucher.posting_date:
        raise ValueError('Voucher has already been posted.')

    posting_no = get_next_number('gl_journal', voucher.company, voucher.voucher_date)
    entry = JournalEntry.objects.create(
        company=voucher.company, entry_no=posting_no, reference=voucher.voucher_no,
        narration=voucher.narration, status='draft', entry_date=dj_timezone.now(),
    )
    for line in voucher.lines.select_related('account'):
        JournalEntryLine.objects.create(
            entry=entry, account=line.account, description=line.description,
            debit_amount=line.debit_amount, credit_amount=line.credit_amount,
        )
    entry.post()
    posted = FinancePostedVoucher.objects.create(
        voucher=voucher, company_name=voucher.company.company_name,
        voucher_no=voucher.voucher_no, posting_no=posting_no,
        posting_date=voucher.voucher_date, total_debit=voucher.total_debit,
        total_credit=voucher.total_credit, narration=voucher.narration, posted_by=user,
    )
    FinancePostedVoucherLine.objects.bulk_create([
        FinancePostedVoucherLine(
            posted_voucher=posted, line_no=line.line_no,
            account_code=line.account.account_code, account_name=line.account.account_name,
            description=line.description, debit_amount=line.debit_amount,
            credit_amount=line.credit_amount, gst_amount=line.gst_amount,
            tds_amount=line.tds_amount,
        )
        for line in voucher.lines.select_related('account')
    ])
    voucher.posting_no = posting_no
    voucher.posting_date = voucher.voucher_date
    voucher.status = 'posted'
    voucher.posted_by = user
    voucher.posted_at = dj_timezone.now()
    voucher.save(update_fields=['posting_no', 'posting_date', 'status', 'posted_by', 'posted_at'])
    AuditLog.objects.create(company=voucher.company, actor=user, table_name='finance_voucher', action='post', record_id=str(voucher.pk), description=f'Posted {voucher.voucher_no} as {posting_no}')
    return voucher


# --- Sales & Receivables: Quotation -> Sales Order -> Invoice -> Payment Receipt ---

DOCUMENT_TYPE_MODELS = {
    'quotation': Quotation,
    'sales_order': SalesOrder,
    'sales_invoice': SalesInvoice,
    'payment_receipt': PaymentReceipt,
    'purchase_order': PurchaseOrder,
    'goods_receipt': GoodsReceipt,
    'purchase_invoice': SupplierInvoice,
    'vendor_payment': VendorPayment,
    'finance_voucher': FinanceVoucher,
}


def link_documents(source, source_type, target, target_type, relationship_type='derived_from', user=None):
    DocumentRelationship.objects.get_or_create(
        source_type=source_type, source_id=source.pk,
        target_type=target_type, target_id=target.pk,
        relationship_type=relationship_type,
        defaults={'created_by': user},
    )


def record_status_change(document, doc_type, from_status, to_status, user, reason=''):
    DocumentStatusHistory.objects.create(
        document_type=doc_type, document_id=document.pk,
        from_status=from_status or '', to_status=to_status,
        changed_by=user, reason=reason,
    )


def get_related_documents(obj, doc_type):
    """Resolve the full Related Documents chain (both directions) for a Quotation/SalesOrder/Invoice/PaymentReceipt."""
    related = {}
    relationships = DocumentRelationship.objects.filter(
        Q(source_type=doc_type, source_id=obj.pk) | Q(target_type=doc_type, target_id=obj.pk)
    ).order_by('created_at')
    for rel in relationships:
        if rel.source_type == doc_type and rel.source_id == obj.pk:
            other_type, other_id = rel.target_type, rel.target_id
        else:
            other_type, other_id = rel.source_type, rel.source_id
        model = DOCUMENT_TYPE_MODELS.get(other_type)
        instance = model.objects.filter(pk=other_id).first() if model else None
        if instance:
            related.setdefault(other_type, []).append(instance)
    return related


def price_document_line(*, product, quantity, store, discount_amount=Decimal('0'), jewellery_unit=None, unit_price_override=None, transaction_date=None):
    """The single authoritative line-pricing calculation, reused by Quotation, Sales Order and Invoice lines (spec S35)."""
    quantity = Decimal(str(quantity or 1))
    discount_amount = Decimal(str(discount_amount or 0))
    jewellery_rule = getattr(product, 'jewellery_pricing_rule', None)
    metal_rate = None
    if jewellery_unit and jewellery_rule and store:
        now = dj_timezone.now()
        metal_rate = JewelleryMetalRate.objects.filter(
            metal_type=jewellery_unit.metal_type, purity=jewellery_unit.purity,
            store=store, is_active=True, effective_from__lte=now,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=now)).first()

    if jewellery_unit and jewellery_rule and metal_rate:
        breakdown = calculate_jewellery_price(
            unit=jewellery_unit, metal_rate=metal_rate, pricing_rule=jewellery_rule,
            tax_rate_code=jewellery_rule.tax_rate_code, discount=discount_amount,
            transaction_date=transaction_date,
        )
        tax = breakdown['tax']
        tax_total = tax['cgst'] + tax['sgst'] + tax['igst']
        effective_rate = (tax_total / breakdown['taxable_value'] * Decimal('100')).quantize(Decimal('0.01')) if breakdown['taxable_value'] else Decimal('0')
        return {
            'unit_price': breakdown['metal_value'], 'taxable_amount': breakdown['taxable_value'],
            'cgst_amount': tax['cgst'], 'sgst_amount': tax['sgst'], 'igst_amount': tax['igst'],
            'line_total': breakdown['final_amount'], 'tax_rate': effective_rate,
            'gross_weight': breakdown['gross_weight'], 'stone_weight': breakdown['stone_weight'],
            'net_metal_weight': breakdown['net_metal_weight'], 'metal_rate': breakdown['metal_rate'],
            'metal_value': breakdown['metal_value'], 'wastage_value': breakdown['wastage_value'],
            'making_charge': breakdown['making_value'], 'stone_value': breakdown['stone_value'],
            'hallmark_charge': breakdown['hallmark_charge'], 'certification_charge': breakdown['certification_charge'],
            'other_charges': breakdown['other_charges'],
            'pricing_snapshot': {str(key): str(value) for key, value in breakdown.items()},
        }

    unit_price = Decimal(str(unit_price_override)) if unit_price_override is not None else (product.sale_price or product.mrp or Decimal('0'))
    gross = max((unit_price * quantity) - discount_amount, Decimal('0'))
    tax_rate = Decimal('3.00')
    taxable, gst, total = calculate_gst_flat(gross, tax_rate)
    half_gst = (gst / Decimal('2')).quantize(Decimal('0.01'))
    return {
        'unit_price': unit_price, 'taxable_amount': taxable,
        'cgst_amount': half_gst, 'sgst_amount': gst - half_gst, 'igst_amount': Decimal('0'),
        'line_total': total, 'tax_rate': tax_rate,
        'gross_weight': Decimal('0'), 'stone_weight': Decimal('0'), 'net_metal_weight': Decimal('0'),
        'metal_rate': Decimal('0'), 'metal_value': Decimal('0'), 'wastage_value': Decimal('0'),
        'making_charge': Decimal('0'), 'stone_value': Decimal('0'), 'hallmark_charge': Decimal('0'),
        'certification_charge': Decimal('0'), 'other_charges': Decimal('0'), 'pricing_snapshot': {},
    }


def build_document_lines(*, line_model, parent_field, parent, lines_data, store):
    """Create Quotation/SalesOrder line rows from raw entries; returns (lines, header totals)."""
    totals = {'subtotal': Decimal('0'), 'discount_amount': Decimal('0'), 'taxable_amount': Decimal('0'), 'gst_amount': Decimal('0'), 'total_amount': Decimal('0')}
    lines = []
    for entry in lines_data:
        discount_amount = Decimal(str(entry.get('discount_amount') or 0))
        pricing = price_document_line(
            product=entry['product'], quantity=entry['quantity'], store=store,
            discount_amount=discount_amount, jewellery_unit=entry.get('jewellery_unit'),
            unit_price_override=entry.get('unit_price'),
        )
        line = line_model.objects.create(
            **{parent_field: parent}, product=entry['product'], quantity=entry['quantity'],
            discount_amount=discount_amount, jewellery_unit=entry.get('jewellery_unit'), **pricing,
        )
        lines.append(line)
        totals['subtotal'] += line.line_total
        totals['discount_amount'] += discount_amount
        totals['taxable_amount'] += line.taxable_amount
        totals['gst_amount'] += line.gst_amount
        totals['total_amount'] += line.line_total
    return lines, totals


@transaction.atomic
def create_quotation(*, customer, store, salesperson, lines_data, valid_until=None, payment_terms=None, delivery_terms='', remarks='', user=None):
    if not lines_data:
        raise ValueError('A quotation needs at least one line.')
    quotation = Quotation.objects.create(
        quotation_no=get_next_number('quotation'), customer=customer, store=store, salesperson=salesperson,
        valid_until=valid_until, payment_terms=payment_terms, delivery_terms=delivery_terms, remarks=remarks,
        created_by=user,
    )
    _, totals = build_document_lines(line_model=QuotationLine, parent_field='quotation', parent=quotation, lines_data=lines_data, store=store)
    for field, value in totals.items():
        setattr(quotation, field, value)
    quotation.save(update_fields=list(totals.keys()) + ['updated_at'])
    record_status_change(quotation, 'quotation', '', 'draft', user)
    return quotation


@transaction.atomic
def submit_for_approval(document, doc_type, user):
    if document.status != 'draft':
        raise ValueError(f'Only a draft {doc_type.replace("_", " ")} can be submitted for approval.')
    document.status = 'pending_approval'
    document.save(update_fields=['status', 'updated_at'])
    record_status_change(document, doc_type, 'draft', 'pending_approval', user)
    return document


@transaction.atomic
def approve_quotation(quotation, user, approved=True, notes=''):
    if quotation.status != 'pending_approval':
        raise ValueError('Only a quotation pending approval can be approved or rejected.')
    from_status = quotation.status
    quotation.status = 'approved' if approved else 'rejected'
    quotation.save(update_fields=['status', 'updated_at'])
    record_status_change(quotation, 'quotation', from_status, quotation.status, user, reason=notes)
    return quotation


@transaction.atomic
def approve_sales_order(sales_order, user, approved=True, notes=''):
    """Approve/reject a sales order; runs credit control (spec S49) and reserves jewellery stock on approval."""
    if sales_order.status != 'pending_approval':
        raise ValueError('Only a sales order pending approval can be approved or rejected.')
    from_status = sales_order.status
    warning = ''
    if approved:
        profile = CustomerFinanceProfile.objects.filter(customer=sales_order.customer).first()
        if profile:
            if profile.credit_hold or not profile.allow_sales:
                raise ValueError(f'{sales_order.customer.name} is on credit hold; clear the hold before approving this order.')
            open_invoices = SalesInvoice.objects.filter(customer=sales_order.customer).exclude(status='cancelled')
            outstanding_balance = sum((inv.balance_amount for inv in open_invoices), Decimal('0'))
            open_orders_total = SalesOrder.objects.filter(
                customer=sales_order.customer, status__in=['approved', 'released', 'partially_fulfilled'],
            ).exclude(pk=sales_order.pk).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
            projected = outstanding_balance + open_orders_total + sales_order.total_amount
            if profile.credit_limit and projected > profile.credit_limit:
                warning = (
                    f'{sales_order.customer.name} will be at Rs.{projected} against a credit limit of '
                    f'Rs.{profile.credit_limit} after this order.'
                )
        sales_order.status = 'approved'
        sales_order.save(update_fields=['status', 'updated_at'])
        for line in sales_order.lines.select_related('jewellery_unit').all():
            line.reserved_quantity = line.quantity
            line.save(update_fields=['reserved_quantity'])
            if line.jewellery_unit_id and line.jewellery_unit.current_status == 'available':
                line.jewellery_unit.current_status = 'reserved'
                line.jewellery_unit.save(update_fields=['current_status'])
    else:
        sales_order.status = 'rejected'
        sales_order.save(update_fields=['status', 'updated_at'])
    record_status_change(sales_order, 'sales_order', from_status, sales_order.status, user, reason=notes)
    return sales_order, warning


@transaction.atomic
def convert_quotation_to_sales_order(quotation, user, line_quantities, expected_delivery_date=None):
    if quotation.status not in ('approved', 'sent', 'customer_accepted'):
        raise ValueError('Only an approved quotation can be converted to a sales order.')
    quotation_lines_by_id = {line.pk: line for line in quotation.lines.select_related('product', 'jewellery_unit').all()}
    entries = []
    for line_id, qty in line_quantities.items():
        line = quotation_lines_by_id.get(int(line_id))
        qty = Decimal(str(qty or 0))
        if not line or qty <= 0:
            continue
        if qty > line.remaining_quantity:
            raise ValueError(f'Cannot convert {qty} of "{line.product}" - only {line.remaining_quantity} remains on the quotation.')
        entries.append((line, qty))
    if not entries:
        raise ValueError('Select at least one line with a quantity to convert.')

    sales_order = SalesOrder.objects.create(
        order_no=get_next_number('sales_order'), customer=quotation.customer, quotation=quotation,
        store=quotation.store, salesperson=quotation.salesperson, expected_delivery_date=expected_delivery_date,
        payment_terms=quotation.payment_terms, created_by=user,
    )
    totals = {'subtotal': Decimal('0'), 'discount_amount': Decimal('0'), 'taxable_amount': Decimal('0'), 'gst_amount': Decimal('0'), 'total_amount': Decimal('0')}
    for line, qty in entries:
        discount_amount = (line.discount_amount * qty / line.quantity) if line.quantity else Decimal('0')
        pricing = price_document_line(product=line.product, quantity=qty, store=quotation.store, discount_amount=discount_amount, jewellery_unit=line.jewellery_unit)
        so_line = SalesOrderLine.objects.create(
            sales_order=sales_order, quotation_line=line, product=line.product, quantity=qty,
            discount_amount=discount_amount, jewellery_unit=line.jewellery_unit, **pricing,
        )
        line.converted_quantity += qty
        line.save(update_fields=['converted_quantity'])
        totals['subtotal'] += so_line.line_total
        totals['discount_amount'] += discount_amount
        totals['taxable_amount'] += so_line.taxable_amount
        totals['gst_amount'] += so_line.gst_amount
        totals['total_amount'] += so_line.line_total
    for field, value in totals.items():
        setattr(sales_order, field, value)
    sales_order.save(update_fields=list(totals.keys()) + ['updated_at'])

    if all(line.remaining_quantity <= 0 for line in quotation.lines.all()):
        from_status = quotation.status
        quotation.status = 'converted'
        quotation.save(update_fields=['status', 'updated_at'])
        record_status_change(quotation, 'quotation', from_status, 'converted', user)

    link_documents(quotation, 'quotation', sales_order, 'sales_order', 'converted_to', user)
    record_status_change(sales_order, 'sales_order', '', 'draft', user)
    return sales_order


@transaction.atomic
def convert_sales_order_to_invoice(sales_order, user, line_quantities):
    if sales_order.status not in ('approved', 'released', 'partially_fulfilled'):
        raise ValueError('Only an approved sales order can be invoiced.')
    so_lines_by_id = {line.pk: line for line in sales_order.lines.select_related('product', 'jewellery_unit').all()}
    entries = []
    for line_id, qty in line_quantities.items():
        line = so_lines_by_id.get(int(line_id))
        qty = Decimal(str(qty or 0))
        if not line or qty <= 0:
            continue
        if qty > line.remaining_quantity:
            raise ValueError(f'Cannot invoice {qty} of "{line.product}" - only {line.remaining_quantity} remains on the sales order.')
        entries.append((line, qty))
    if not entries:
        raise ValueError('Select at least one line with a quantity to invoice.')

    invoice = SalesInvoice.objects.create(
        invoice_no=get_next_number('sales_invoice'), customer=sales_order.customer, sales_order=sales_order,
        quotation=sales_order.quotation, customer_phone=sales_order.customer.phone or '',
        customer_gstin=sales_order.customer.gstin or '', status='approved',  # the sales order already cleared maker-checker approval
    )
    totals = {'subtotal': Decimal('0'), 'discount_amount': Decimal('0'), 'taxable_amount': Decimal('0'), 'gst_amount': Decimal('0'), 'total_amount': Decimal('0')}
    for line, qty in entries:
        discount_amount = (line.discount_amount * qty / line.quantity) if line.quantity else Decimal('0')
        pricing = price_document_line(product=line.product, quantity=qty, store=sales_order.store, discount_amount=discount_amount, jewellery_unit=line.jewellery_unit)
        SalesInvoiceItem.objects.create(
            invoice=invoice, product=line.product, quantity=qty, discount_amount=discount_amount,
            jewellery_unit=line.jewellery_unit, barcode=line.jewellery_unit.barcode if line.jewellery_unit else '',
            **pricing,
        )
        line.invoiced_quantity += qty
        line.save(update_fields=['invoiced_quantity'])
        totals['subtotal'] += pricing['line_total']
        totals['discount_amount'] += discount_amount
        totals['taxable_amount'] += pricing['taxable_amount']
        totals['gst_amount'] += pricing['cgst_amount'] + pricing['sgst_amount'] + pricing['igst_amount']
        totals['total_amount'] += pricing['line_total']
    invoice.subtotal = totals['subtotal']
    invoice.discount_amount = totals['discount_amount']
    invoice.taxable_amount = totals['taxable_amount']
    invoice.gst_amount = totals['gst_amount']
    invoice.total_amount = totals['total_amount']
    invoice.save(update_fields=['subtotal', 'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount'])

    sales_order.status = 'completed' if all(line.remaining_quantity <= 0 for line in sales_order.lines.all()) else 'partially_fulfilled'
    sales_order.save(update_fields=['status', 'updated_at'])
    record_status_change(sales_order, 'sales_order', 'approved', sales_order.status, user)

    link_documents(sales_order, 'sales_order', invoice, 'sales_invoice', 'converted_to', user)
    if sales_order.quotation_id:
        link_documents(sales_order.quotation, 'quotation', invoice, 'sales_invoice', 'converted_to', user)
    record_status_change(invoice, 'sales_invoice', '', invoice.status, user)
    return invoice


@transaction.atomic
def post_sales_invoice(invoice, user):
    if invoice.status == 'posted':
        return invoice
    if invoice.status not in ('approved', 'completed'):
        raise ValueError('Only an approved invoice can be posted.')
    from_status = invoice.status
    for item in invoice.items.select_related('jewellery_unit').all():
        if item.jewellery_unit_id:
            item.jewellery_unit.current_status = 'sold'
            item.jewellery_unit.save(update_fields=['current_status'])
    invoice.status = 'posted'
    invoice.save(update_fields=['status'])
    record_status_change(invoice, 'sales_invoice', from_status, 'posted', user)
    post_sales_invoice_to_gl(invoice, user)
    return invoice


@transaction.atomic
def create_payment_receipt(*, customer, store, payment_method, amount, allocations, user, bank_account=None, reference_no='', transaction_id='', remarks=''):
    """Create a draft Payment Receipt with its intended invoice allocations (spec S23-S24); financial effect lands on post."""
    amount = Decimal(str(amount))
    allocations = {int(invoice_id): Decimal(str(value)) for invoice_id, value in allocations.items() if Decimal(str(value or 0)) > 0}
    if sum(allocations.values(), Decimal('0')) > amount:
        raise ValueError('Allocated amount cannot exceed the receipt amount.')
    receipt = PaymentReceipt.objects.create(
        receipt_no=get_next_number('payment_receipt'), customer=customer, store=store, payment_method=payment_method,
        bank_account=bank_account, amount=amount, reference_no=reference_no, transaction_id=transaction_id,
        remarks=remarks, created_by=user,
    )
    record_status_change(receipt, 'payment_receipt', '', 'draft', user)
    invoices_by_id = {inv.pk: inv for inv in SalesInvoice.objects.filter(pk__in=allocations.keys())}
    for invoice_id, allocated_amount in allocations.items():
        invoice = invoices_by_id.get(invoice_id)
        if not invoice:
            continue
        PaymentAllocation.objects.create(receipt=receipt, invoice=invoice, allocated_amount=allocated_amount)
        link_documents(receipt, 'payment_receipt', invoice, 'sales_invoice', 'applied_to', user)
    return receipt


@transaction.atomic
def add_payment_allocation(receipt, invoice, allocated_amount, user):
    """Allocate (or top up) a draft receipt against one invoice — the granular counterpart to create_payment_receipt's bulk allocations."""
    if receipt.status != 'draft':
        raise ValueError('Only a draft payment receipt can be allocated.')
    allocated_amount = Decimal(str(allocated_amount))
    if allocated_amount <= 0:
        raise ValueError('Allocated amount must be greater than zero.')
    if receipt.allocated_amount + allocated_amount > receipt.amount:
        raise ValueError('Allocated amount cannot exceed the receipt amount.')
    allocation, created = PaymentAllocation.objects.get_or_create(
        receipt=receipt, invoice=invoice, defaults={'allocated_amount': allocated_amount},
    )
    if not created:
        allocation.allocated_amount += allocated_amount
        allocation.save(update_fields=['allocated_amount'])
    link_documents(receipt, 'payment_receipt', invoice, 'sales_invoice', 'applied_to', user)
    return allocation


@transaction.atomic
def post_payment_receipt(receipt, user):
    if receipt.status == 'posted':
        return receipt
    if receipt.status != 'draft':
        raise ValueError('Only a draft payment receipt can be posted.')
    invoices_by_id = {
        inv.pk: inv for inv in
        SalesInvoice.objects.select_for_update().filter(pk__in=receipt.allocations.values_list('invoice_id', flat=True))
    }
    for allocation in receipt.allocations.all():
        invoice = invoices_by_id.get(allocation.invoice_id)
        if not invoice:
            continue
        invoice.paid_amount += allocation.allocated_amount
        if invoice.paid_amount <= 0:
            invoice.payment_status = 'unpaid'
        elif invoice.paid_amount < invoice.total_amount:
            invoice.payment_status = 'partially_paid'
        elif invoice.paid_amount == invoice.total_amount:
            invoice.payment_status = 'paid'
        else:
            invoice.payment_status = 'overpaid'
        invoice.save(update_fields=['paid_amount', 'payment_status'])
    if receipt.bank_account_id:
        receipt.bank_account.current_balance = (receipt.bank_account.current_balance or Decimal('0')) + receipt.amount
        receipt.bank_account.save(update_fields=['current_balance', 'updated_at'])
        BankTransaction.objects.create(
            bank_account=receipt.bank_account, transaction_type='credit', amount=receipt.amount,
            description=f'Payment receipt {receipt.receipt_no} from {receipt.customer.name}', reference=receipt.reference_no,
        )
    receipt.status = 'posted'
    receipt.save(update_fields=['status', 'updated_at'])
    record_status_change(receipt, 'payment_receipt', 'draft', 'posted', user)
    post_payment_receipt_to_gl(receipt, user)
    return receipt


# --- Purchase & Payables: Vendor -> Purchase Order -> Goods Receipt -> Purchase Invoice -> Vendor Payment ---

def price_purchase_line(*, unit_cost, quantity, discount_amount=Decimal('0'), tax_rate=Decimal('18.00')):
    """Shared costing calculation for Purchase Order / Purchase Invoice lines (no jewellery dimension)."""
    quantity = Decimal(str(quantity or 1))
    unit_cost = Decimal(str(unit_cost or 0))
    discount_amount = Decimal(str(discount_amount or 0))
    tax_rate = Decimal(str(tax_rate if tax_rate is not None else '18.00'))
    gross = max((unit_cost * quantity) - discount_amount, Decimal('0'))
    taxable, gst, total = calculate_gst_flat(gross, tax_rate)
    half_gst = (gst / Decimal('2')).quantize(Decimal('0.01'))
    return {
        'unit_price': unit_cost, 'taxable_amount': taxable, 'tax_rate': tax_rate,
        'cgst_amount': half_gst, 'sgst_amount': gst - half_gst, 'igst_amount': Decimal('0'),
        'line_total': total,
    }


@transaction.atomic
def create_purchase_order(*, vendor, warehouse, buyer, lines_data, expected_delivery_date=None, payment_terms=None, remarks='', user=None):
    if not lines_data:
        raise ValueError('A purchase order needs at least one line.')
    po = PurchaseOrder.objects.create(
        order_no=get_next_number('purchase_order'), vendor=vendor, warehouse=warehouse, buyer=buyer,
        expected_delivery_date=expected_delivery_date, payment_terms=payment_terms, remarks=remarks, created_by=user,
    )
    totals = {'subtotal': Decimal('0'), 'discount_amount': Decimal('0'), 'taxable_amount': Decimal('0'), 'gst_amount': Decimal('0'), 'total_amount': Decimal('0')}
    for entry in lines_data:
        discount_amount = Decimal(str(entry.get('discount_amount') or 0))
        pricing = price_purchase_line(
            unit_cost=entry['unit_price'], quantity=entry['quantity'], discount_amount=discount_amount,
            tax_rate=entry.get('tax_rate', Decimal('18.00')),
        )
        line = PurchaseOrderLine.objects.create(
            purchase_order=po, product=entry['product'], quantity=entry['quantity'], discount_amount=discount_amount, **pricing,
        )
        totals['subtotal'] += line.line_total
        totals['discount_amount'] += discount_amount
        totals['taxable_amount'] += line.taxable_amount
        totals['gst_amount'] += line.gst_amount
        totals['total_amount'] += line.line_total
    for field, value in totals.items():
        setattr(po, field, value)
    po.save(update_fields=list(totals.keys()) + ['updated_at'])
    record_status_change(po, 'purchase_order', '', 'draft', user)
    return po


@transaction.atomic
def approve_purchase_order(po, user, approved=True, notes=''):
    if po.status != 'pending_approval':
        raise ValueError('Only a purchase order pending approval can be approved or rejected.')
    from_status = po.status
    if approved:
        profile = VendorFinanceProfile.objects.filter(vendor=po.vendor).first()
        if profile and profile.purchase_hold:
            raise ValueError(f'{po.vendor.name} is on purchase hold; clear the hold before approving this order.')
        po.status = 'approved'
    else:
        po.status = 'rejected'
    po.save(update_fields=['status', 'updated_at'])
    record_status_change(po, 'purchase_order', from_status, po.status, user, reason=notes)
    return po


@transaction.atomic
def receive_goods(purchase_order, user, line_quantities, rejected_quantities=None, warehouse=None, remarks=''):
    """Post a (partial) Goods Receipt against an approved PO; posts inventory via InventoryMovement.post()."""
    if purchase_order.status not in ('approved', 'sent_to_vendor', 'partially_received'):
        raise ValueError('Only an approved purchase order can receive goods.')
    rejected_quantities = rejected_quantities or {}
    po_lines_by_id = {line.pk: line for line in purchase_order.lines.select_related('product').all()}
    entries = []
    for line_id, qty in line_quantities.items():
        line = po_lines_by_id.get(int(line_id))
        qty = Decimal(str(qty or 0))
        if not line or qty <= 0:
            continue
        if qty > line.remaining_quantity:
            raise ValueError(f'Cannot receive {qty} of "{line.product}" - only {line.remaining_quantity} remains on the purchase order.')
        rejected = Decimal(str(rejected_quantities.get(str(line_id)) or rejected_quantities.get(line_id) or 0))
        if rejected > qty:
            raise ValueError('Rejected quantity cannot exceed received quantity.')
        entries.append((line, qty, rejected))
    if not entries:
        raise ValueError('Enter at least one quantity to receive.')

    receipt = GoodsReceipt.objects.create(
        receipt_no=get_next_number('goods_receipt'), purchase_order=purchase_order, vendor=purchase_order.vendor,
        warehouse=warehouse or purchase_order.warehouse, received_by=user, remarks=remarks, status='posted',
    )
    for line, qty, rejected in entries:
        receipt_line = GoodsReceiptLine.objects.create(
            goods_receipt=receipt, purchase_order_line=line, product=line.product,
            quantity_received=qty, quantity_rejected=rejected,
        )
        accepted = receipt_line.accepted_quantity
        if accepted > 0:
            InventoryMovement.objects.create(
                product=line.product, movement_type='inward', quantity=accepted, reference=receipt.receipt_no,
            ).post()
        line.received_quantity += qty
        line.save(update_fields=['received_quantity'])

    from_status = purchase_order.status
    purchase_order.status = 'fully_received' if all(l.remaining_quantity <= 0 for l in purchase_order.lines.all()) else 'partially_received'
    purchase_order.save(update_fields=['status', 'updated_at'])
    record_status_change(purchase_order, 'purchase_order', from_status, purchase_order.status, user)
    record_status_change(receipt, 'goods_receipt', '', 'posted', user)
    link_documents(purchase_order, 'purchase_order', receipt, 'goods_receipt', 'converted_to', user)
    return receipt


@transaction.atomic
def convert_receipt_to_purchase_invoice(goods_receipt, user, *, vendor_invoice_no, line_quantities, vendor_invoice_date=None, company=None):
    purchase_order = goods_receipt.purchase_order
    po_lines_by_id = {line.pk: line for line in purchase_order.lines.select_related('product').all()}
    entries = []
    for line_id, qty in line_quantities.items():
        line = po_lines_by_id.get(int(line_id))
        qty = Decimal(str(qty or 0))
        if not line or qty <= 0:
            continue
        if qty > line.remaining_to_invoice:
            raise ValueError(f'Cannot invoice {qty} of "{line.product}" - only {line.remaining_to_invoice} remains to invoice.')
        entries.append((line, qty))
    if not entries:
        raise ValueError('Select at least one line with a quantity to invoice.')

    if SupplierInvoice.objects.filter(supplier=purchase_order.vendor, invoice_no=vendor_invoice_no).exclude(status='cancelled').exists():
        raise ValueError(f'A purchase invoice with vendor invoice number "{vendor_invoice_no}" already exists for this vendor.')

    vendor_invoice_date = vendor_invoice_date or date.today()
    due_date = vendor_invoice_date + timedelta(days=purchase_order.payment_terms.due_days) if purchase_order.payment_terms else None
    invoice = SupplierInvoice.objects.create(
        company=company or Company.objects.filter(status='active').first() or Company.objects.first(),
        supplier=purchase_order.vendor, document_no=get_next_number('purchase_invoice'),
        purchase_order=purchase_order, goods_receipt=goods_receipt,
        workflow_status='approved',  # the purchase order already cleared maker-checker approval
        invoice_no=vendor_invoice_no, invoice_date=vendor_invoice_date, due_date=due_date, status='open',
    )
    totals = {'gross_amount': Decimal('0'), 'tax_amount': Decimal('0'), 'net_amount': Decimal('0')}
    for line, qty in entries:
        discount_amount = (line.discount_amount * qty / line.quantity) if line.quantity else Decimal('0')
        pricing = price_purchase_line(unit_cost=line.unit_price, quantity=qty, discount_amount=discount_amount, tax_rate=line.tax_rate)
        SupplierInvoiceLine.objects.create(
            supplier_invoice=invoice, product=line.product, purchase_order_line=line, quantity=qty,
            unit_cost=pricing['unit_price'], discount_amount=discount_amount, taxable_amount=pricing['taxable_amount'],
            tax_rate=pricing['tax_rate'], cgst_amount=pricing['cgst_amount'], sgst_amount=pricing['sgst_amount'],
            igst_amount=pricing['igst_amount'], line_total=pricing['line_total'],
        )
        line.invoiced_quantity += qty
        line.save(update_fields=['invoiced_quantity'])
        totals['gross_amount'] += pricing['taxable_amount'] + discount_amount
        totals['tax_amount'] += pricing['cgst_amount'] + pricing['sgst_amount'] + pricing['igst_amount']
        totals['net_amount'] += pricing['line_total']
    invoice.gross_amount = totals['gross_amount']
    invoice.tax_amount = totals['tax_amount']
    invoice.net_amount = totals['net_amount']
    invoice.save(update_fields=['gross_amount', 'tax_amount', 'net_amount'])

    link_documents(purchase_order, 'purchase_order', invoice, 'purchase_invoice', 'converted_to', user)
    link_documents(goods_receipt, 'goods_receipt', invoice, 'purchase_invoice', 'converted_to', user)
    record_status_change(invoice, 'purchase_invoice', '', invoice.workflow_status, user)
    return invoice


@transaction.atomic
def post_purchase_invoice(invoice, user):
    if invoice.workflow_status == 'posted':
        return invoice
    if invoice.workflow_status not in ('draft', 'approved'):
        raise ValueError('Only a draft or approved purchase invoice can be posted.')
    from_status = invoice.workflow_status
    invoice.workflow_status = 'posted'
    invoice.save(update_fields=['workflow_status'])
    record_status_change(invoice, 'purchase_invoice', from_status, 'posted', user)
    post_purchase_invoice_to_gl(invoice, user)
    return invoice


@transaction.atomic
def create_vendor_payment(*, vendor, bank_account, payment_method, amount, allocations, user, reference_no='', transaction_id='', remarks=''):
    """Create a draft Vendor Payment (Payout Receipt) with its intended invoice allocations; financial effect lands on post."""
    amount = Decimal(str(amount))
    allocations = {int(invoice_id): Decimal(str(value)) for invoice_id, value in allocations.items() if Decimal(str(value or 0)) > 0}
    if sum(allocations.values(), Decimal('0')) > amount:
        raise ValueError('Allocated amount cannot exceed the payment amount.')
    payment = VendorPayment.objects.create(
        payment_no=get_next_number('vendor_payment'), vendor=vendor, bank_account=bank_account, payment_method=payment_method,
        amount=amount, reference_no=reference_no, transaction_id=transaction_id, remarks=remarks, created_by=user,
    )
    record_status_change(payment, 'vendor_payment', '', 'draft', user)
    invoices_by_id = {inv.pk: inv for inv in SupplierInvoice.objects.filter(pk__in=allocations.keys())}
    for invoice_id, allocated_amount in allocations.items():
        invoice = invoices_by_id.get(invoice_id)
        if not invoice:
            continue
        VendorPaymentAllocation.objects.create(payment=payment, supplier_invoice=invoice, allocated_amount=allocated_amount)
        link_documents(payment, 'vendor_payment', invoice, 'purchase_invoice', 'applied_to', user)
    return payment


@transaction.atomic
def post_vendor_payment(payment, user):
    if payment.status == 'posted':
        return payment
    if payment.status != 'draft':
        raise ValueError('Only a draft vendor payment can be posted.')
    for allocation in payment.allocations.select_related('supplier_invoice'):
        allocation.supplier_invoice.apply_payment(allocation.allocated_amount)  # reuse the existing tested payable-application logic
    if payment.bank_account_id:
        payment.bank_account.current_balance = (payment.bank_account.current_balance or Decimal('0')) - payment.amount
        payment.bank_account.save(update_fields=['current_balance', 'updated_at'])
        BankTransaction.objects.create(
            bank_account=payment.bank_account, transaction_type='debit', amount=payment.amount,
            description=f'Vendor payment {payment.payment_no} to {payment.vendor.name}', reference=payment.reference_no,
        )
    payment.status = 'posted'
    payment.save(update_fields=['status', 'updated_at'])
    record_status_change(payment, 'vendor_payment', 'draft', 'posted', user)
    post_vendor_payment_to_gl(payment, user)
    return payment


# --- Gold / Silver rate updates (manual + live API) ---

TROY_OUNCE_GRAMS = Decimal('31.1034768')

GOLD_PURITY_FACTORS = {
    '24K': Decimal('1.000'), '22K': Decimal('0.916'), '18K': Decimal('0.750'), '14K': Decimal('0.585'),
}
SILVER_PURITY_FACTORS = {
    '999': Decimal('1.000'), '925': Decimal('0.925'),
}

GOLD_SPOT_API_URL = 'https://api.gold-api.com/price/XAU'
SILVER_SPOT_API_URL = 'https://api.gold-api.com/price/XAG'
USD_INR_API_URL = 'https://open.er-api.com/v6/latest/USD'


def _fetch_json(url, timeout=8):
    """Free, keyless public endpoints; no auth header needed. Never used for anything but read-only public spot data."""
    request = urllib.request.Request(url, headers={'User-Agent': 'Goldi-ERP/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        raise ValueError(f'Could not reach {url}: {exc}') from exc


def fetch_live_metal_rates():
    """Fetch live XAU/XAG spot (gold-api.com, USD/troy-oz) and USD->INR (open.er-api.com), derive INR/gram per purity."""
    gold = _fetch_json(GOLD_SPOT_API_URL)
    silver = _fetch_json(SILVER_SPOT_API_URL)
    fx = _fetch_json(USD_INR_API_URL)

    try:
        usd_inr = Decimal(str(fx['rates']['INR']))
        gold_usd_oz = Decimal(str(gold['price']))
        silver_usd_oz = Decimal(str(silver['price']))
    except (KeyError, TypeError) as exc:
        raise ValueError(f'Unexpected response shape from rate provider: {exc}') from exc

    def money(value):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    gold_inr_gram_fine = (gold_usd_oz / TROY_OUNCE_GRAMS) * usd_inr
    silver_inr_gram_fine = (silver_usd_oz / TROY_OUNCE_GRAMS) * usd_inr

    return {
        'gold': {purity: money(gold_inr_gram_fine * factor) for purity, factor in GOLD_PURITY_FACTORS.items()},
        'silver': {purity: money(silver_inr_gram_fine * factor) for purity, factor in SILVER_PURITY_FACTORS.items()},
        'usd_inr_rate': usd_inr,
        'source_reference': 'gold-api.com (XAU/XAG spot) + open.er-api.com (USD->INR)',
        'fetched_at': dj_timezone.now(),
    }


@transaction.atomic
def apply_metal_rate(*, metal_type, purity, rate_per_gram, stores, rate_type='selling', source='manual', source_reference='', effective_from=None, user=None):
    """Close out the previously effective rate for each store and open a new one (never overwrites history)."""
    effective_from = effective_from or dj_timezone.now()
    created = []
    for store in stores:
        JewelleryMetalRate.objects.filter(
            metal_type=metal_type, purity=purity, store=store, rate_type=rate_type,
            is_active=True, effective_to__isnull=True,
        ).update(effective_to=effective_from)
        created.append(JewelleryMetalRate.objects.create(
            metal_type=metal_type, purity=purity, rate_per_gram=rate_per_gram, rate_type=rate_type,
            store=store, effective_from=effective_from, is_active=True,
            source=source, source_reference=source_reference, created_by=user,
        ))
    return created


@transaction.atomic
def apply_live_metal_rates(stores, user=None):
    """Fetch live gold/silver rates and apply them (by purity) across the given stores. Returns (fetched_data, created_rows)."""
    data = fetch_live_metal_rates()
    created = []
    for metal_type, purities in (('gold', data['gold']), ('silver', data['silver'])):
        for purity, rate in purities.items():
            created += apply_metal_rate(
                metal_type=metal_type, purity=purity, rate_per_gram=rate, stores=stores,
                source='api', source_reference=data['source_reference'], effective_from=data['fetched_at'], user=user,
            )
    return data, created


# --- Finance Posting Bridge: route Sales/Purchase/Payment documents through the existing FinanceVoucher/GL engine ---

_AUTO_VOUCHER_NAMES = {
    'sales_journal': 'Sales Journal (Auto)',
    'purchase_journal': 'Purchase Journal (Auto)',
    'receipt_journal': 'Receipt Journal (Auto)',
    'payment_journal': 'Payment Journal (Auto)',
    'production_journal': 'Production Journal (Auto)',
    'scheme_journal': 'Jewellery Savings Journal (Auto)',
    'job_work_journal': 'Job Work Journal (Auto)',
}


def get_default_company():
    company = Company.objects.filter(status='active').first() or Company.objects.first()
    if not company:
        raise ValueError('No company is configured; cannot post to the general ledger.')
    return company


def get_posting_setup(company=None):
    """The minimal General Posting Matrix (spec S5/S6): default accounts for sales/purchase/GST/cash."""
    setup = FinancePostingSetup.objects.filter(company=company).first() if company else None
    if not setup:
        setup = FinancePostingSetup.objects.select_related('company').first()
    if not setup:
        raise ValueError(
            'Finance posting setup is not configured. Add one under Finance Setup '
            '(Company, Sales Revenue, GST Output, Purchase Expense, GST Input and Default Cash accounts) '
            'before posting financial transactions to the general ledger.'
        )
    return setup


def resolve_bank_gl_account(bank_account, posting_setup):
    if bank_account is not None and bank_account.gl_account_id:
        return bank_account.gl_account
    return posting_setup.default_cash_account


def _resolve_customer_posting_group(customer):
    posting_group = customer.customer_posting_group
    if not posting_group:
        profile = getattr(customer, 'finance_profile', None)
        posting_group = profile.posting_group if profile else None
    if not posting_group:
        raise ValueError(f'{customer.name} has no customer posting group configured; cannot post to the general ledger.')
    return posting_group


def _resolve_vendor_posting_group(vendor):
    profile = getattr(vendor, 'finance_profile', None)
    posting_group = profile.posting_group if profile else None
    if not posting_group:
        raise ValueError(f'{vendor.name} has no vendor posting group configured; cannot post to the general ledger.')
    return posting_group


def _get_auto_voucher_type(code):
    """Lazily provision the (workflow-only, non-financial) template/voucher-type buckets auto-postings file into."""
    name = _AUTO_VOUCHER_NAMES[code]
    template, _ = FinanceJournalTemplate.objects.get_or_create(
        code=f'{code}_auto',
        defaults={
            'name': name, 'operation_type': code, 'voucher_type': code,
            'approval_required': False, 'auto_post_allowed': True, 'recurring_allowed': False,
        },
    )
    voucher_type, _ = FinanceVoucherType.objects.get_or_create(
        company=None, code=code,
        defaults={'name': name, 'template': template, 'approval_required': False, 'auto_post': True},
    )
    return voucher_type


def _get_auto_batch(company, template, user):
    batch, _ = FinanceJournalBatch.objects.get_or_create(
        company=company, code='AUTO-POSTING',
        defaults={'template': template, 'user': user, 'name': 'Automatic Document Postings'},
    )
    return batch


def _as_date(value):
    return value.date() if hasattr(value, 'date') else value


def _post_document_voucher(*, company, voucher_type_code, user, lines, narration, document_no, voucher_date, source_doc, source_doc_type):
    voucher_type = _get_auto_voucher_type(voucher_type_code)
    batch = _get_auto_batch(company, voucher_type.template, user)
    voucher = create_finance_voucher(
        company=company, batch=batch, voucher_type=voucher_type, user=user, lines=lines,
        narration=narration, document_no=document_no, voucher_date=_as_date(voucher_date),
    )
    post_finance_voucher(voucher, user)
    link_documents(source_doc, source_doc_type, voucher, 'finance_voucher', 'posted_as', user)
    return voucher


def post_sales_invoice_to_gl(invoice, user):
    """Dr Customer Receivable, Cr Sales Revenue, Cr GST Output — balances by construction (revenue = total - GST)."""
    company = get_default_company()
    setup = get_posting_setup(company)
    posting_group = _resolve_customer_posting_group(invoice.customer)
    revenue_amount = invoice.total_amount - invoice.gst_amount

    lines = [
        {'line_no': 1, 'account': posting_group.receivable_account, 'description': f'Sales Invoice {invoice.invoice_no}', 'debit_amount': invoice.total_amount},
        {'line_no': 2, 'account': setup.sales_revenue_account, 'description': f'Sales Invoice {invoice.invoice_no}', 'credit_amount': revenue_amount},
    ]
    if invoice.gst_amount:
        lines.append({'line_no': 3, 'account': setup.gst_output_account, 'description': f'GST on {invoice.invoice_no}', 'credit_amount': invoice.gst_amount})

    return _post_document_voucher(
        company=company, voucher_type_code='sales_journal', user=user, lines=lines,
        narration=f'Sales Invoice {invoice.invoice_no}', document_no=invoice.invoice_no,
        voucher_date=invoice.sales_date, source_doc=invoice, source_doc_type='sales_invoice',
    )


def post_purchase_invoice_to_gl(invoice, user):
    """Dr Purchase Expense, Dr GST Input, Cr Vendor Payable — balances by construction (expense = net - GST)."""
    company = get_default_company()
    posting_setup = get_posting_setup(company)
    posting_group = _resolve_vendor_posting_group(invoice.supplier)
    expense_amount = invoice.net_amount - invoice.tax_amount

    lines = [
        {'line_no': 1, 'account': posting_setup.purchase_expense_account, 'description': f'Purchase Invoice {invoice.invoice_no}', 'debit_amount': expense_amount},
    ]
    line_no = 2
    if invoice.tax_amount:
        lines.append({'line_no': line_no, 'account': posting_setup.gst_input_account, 'description': f'GST on {invoice.invoice_no}', 'debit_amount': invoice.tax_amount})
        line_no += 1
    lines.append({'line_no': line_no, 'account': posting_group.payable_account, 'description': f'Purchase Invoice {invoice.invoice_no}', 'credit_amount': invoice.net_amount})

    return _post_document_voucher(
        company=company, voucher_type_code='purchase_journal', user=user, lines=lines,
        narration=f'Purchase Invoice {invoice.document_no or invoice.invoice_no}', document_no=invoice.invoice_no,
        voucher_date=invoice.invoice_date, source_doc=invoice, source_doc_type='purchase_invoice',
    )


def post_payment_receipt_to_gl(receipt, user):
    """Dr Bank/Cash, Cr Customer Receivable (applied) and/or Cr Customer Advance (unapplied)."""
    company = get_default_company()
    setup = get_posting_setup(company)
    posting_group = _resolve_customer_posting_group(receipt.customer)
    bank_account = resolve_bank_gl_account(receipt.bank_account, setup)
    allocated_total = receipt.allocated_amount
    unapplied = receipt.unapplied_amount

    lines = [{'line_no': 1, 'account': bank_account, 'description': f'Payment Receipt {receipt.receipt_no}', 'debit_amount': receipt.amount}]
    line_no = 2
    if allocated_total:
        lines.append({'line_no': line_no, 'account': posting_group.receivable_account, 'description': f'Applied to invoices - {receipt.receipt_no}', 'credit_amount': allocated_total})
        line_no += 1
    if unapplied:
        if not posting_group.advance_account:
            raise ValueError(f'{receipt.customer.name} has an unapplied amount but no advance account is configured on their posting group.')
        lines.append({'line_no': line_no, 'account': posting_group.advance_account, 'description': f'Unapplied advance - {receipt.receipt_no}', 'credit_amount': unapplied})

    return _post_document_voucher(
        company=company, voucher_type_code='receipt_journal', user=user, lines=lines,
        narration=f'Payment Receipt {receipt.receipt_no}', document_no=receipt.receipt_no,
        voucher_date=receipt.receipt_date, source_doc=receipt, source_doc_type='payment_receipt',
    )


def post_vendor_payment_to_gl(payment, user):
    """Dr Vendor Payable (applied) and/or Dr Vendor Advance (unapplied), Cr Bank/Cash."""
    company = get_default_company()
    setup = get_posting_setup(company)
    posting_group = _resolve_vendor_posting_group(payment.vendor)
    bank_account = resolve_bank_gl_account(payment.bank_account, setup)
    allocated_total = payment.allocated_amount
    unapplied = payment.unapplied_amount

    lines = []
    line_no = 1
    if allocated_total:
        lines.append({'line_no': line_no, 'account': posting_group.payable_account, 'description': f'Applied to invoices - {payment.payment_no}', 'debit_amount': allocated_total})
        line_no += 1
    if unapplied:
        if not posting_group.advance_account:
            raise ValueError(f'{payment.vendor.name} has an unapplied amount but no advance account is configured on their posting group.')
        lines.append({'line_no': line_no, 'account': posting_group.advance_account, 'description': f'Advance - {payment.payment_no}', 'debit_amount': unapplied})
        line_no += 1
    lines.append({'line_no': line_no, 'account': bank_account, 'description': f'Vendor Payment {payment.payment_no}', 'credit_amount': payment.amount})

    return _post_document_voucher(
        company=company, voucher_type_code='payment_journal', user=user, lines=lines,
        narration=f'Vendor Payment {payment.payment_no}', document_no=payment.payment_no,
        voucher_date=payment.payment_date, source_doc=payment, source_doc_type='vendor_payment',
    )