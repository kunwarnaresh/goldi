from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.db import transaction
from django.utils import timezone as dj_timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from .models import (
    AuditLog, DocumentNumberSeries, FinancePostedVoucher, FinancePostedVoucherLine,
    FinanceVoucher, FinanceVoucherLine, GeneralLedger, GSTRate, JournalEntry,
    JournalEntryLine,
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
    """Render A4/A5 from the common dataset; thermal uses render_thermal_receipt."""
    buffer = BytesIO()
    page_size = A5 if print_type == 'A5_INVOICE' else A4
    document = SimpleDocTemplate(buffer, pagesize=page_size, rightMargin=28, leftMargin=28, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    invoice = dataset['invoice']; setting = dataset['setting']; story = []
    title = 'TAX INVOICE'
    story.append(Paragraph(f'<b>{setting.company_name if setting else "Goldi ERP"}</b>', styles['Title']))
    story.append(Paragraph(f'{title} | Invoice No: {invoice.invoice_no} | Date: {invoice.sales_date:%d-%b-%Y %I:%M %p}', styles['Normal']))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f'<b>Billed By:</b> {setting.address if setting else ""} | GSTIN: {setting.gstin if setting else "N/A"} | PAN: {setting.pan if setting else "N/A"}', styles['Normal']))
    story.append(Paragraph(f'<b>Billed To:</b> {invoice.customer.name} | Mobile: {invoice.customer.phone or "N/A"} | GSTIN: {invoice.customer.gstin or "N/A"} | PAN: {invoice.customer.pan or "N/A"}', styles['Normal']))
    story.append(Paragraph(f'Country of Supply: India | Place of Supply: {invoice.place_of_supply or "N/A"} | POS Terminal: {invoice.terminal_code_snapshot or "N/A"}', styles['Normal']))
    story.append(Spacer(1, 12))
    rows = [['Sr.', 'Item / SKU / HSN', 'Qty', 'Rate', 'Taxable', 'GST', 'Total']]
    for index, item in enumerate(dataset['items'], 1):
        description = f'{item.product.name}\nSKU: {item.product.sku}\nHSN/SAC: {item.product.hsn_code or "N/A"}'
        rows.append([str(index), description, str(item.quantity), f'₹{item.unit_price}', f'₹{item.taxable_amount}', f'₹{item.gst_amount}', f'₹{item.line_total + item.gst_amount}'])
        if item.gross_weight:
            rows.append(['', f'Jewellery: Gross {item.gross_weight}g | Stone {item.stone_weight}g | Net {item.net_metal_weight}g | Metal ₹{item.metal_value} | Making ₹{item.making_charge} | Wastage ₹{item.wastage_value} | Stones ₹{item.stone_value}', '', '', '', '', ''])
    table = Table(rows, repeatRows=1, colWidths=None)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d5660')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('GRID', (0, 0), (-1, -1), .4, colors.HexColor('#b8c8d0')), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('FONTSIZE', (0, 0), (-1, -1), 8)]))
    story.append(table); story.append(Spacer(1, 10))
    story.append(Paragraph(f'Subtotal: ₹{dataset["subtotal"]} | Discount: ₹{dataset["discount"]} | Taxable: ₹{dataset["taxable"]}', styles['Normal']))
    story.append(Paragraph(f'CGST: ₹{invoice.cgst_amount} | SGST: ₹{invoice.sgst_amount} | IGST: ₹0.00 | Total GST: ₹{dataset["gst"]}', styles['Normal']))
    story.append(Paragraph(f'<b>TOTAL (INR): ₹{dataset["total"]}</b>', styles['Heading2']))
    story.append(Paragraph(f'<b>Total in words:</b> {dataset["amount_words"]}', styles['Normal']))
    if dataset['payments']:
        story.append(Paragraph('<b>Payment Summary:</b> ' + ' | '.join(f'{payment.get_payment_method_display()}: ₹{payment.amount}' for payment in dataset['payments']), styles['Normal']))
    story.append(Spacer(1, 10)); story.append(Paragraph(f'Cashier: {invoice.cashier_name_snapshot or "N/A"} | Sales Staff: {invoice.sales_staff_name_snapshot or "N/A"}', styles['Normal']))
    story.append(Paragraph((setting.terms_and_conditions if setting else '') or 'This is an electronically generated document, no signature is required.', styles['Normal']))
    story.append(Spacer(1, 8)); story.append(Paragraph('This is an electronically generated document, no signature is required. | Page 1 of 1', styles['Normal']))
    document.build(story)
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