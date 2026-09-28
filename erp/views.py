from decimal import Decimal, InvalidOperation
from io import BytesIO
from datetime import date

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.forms import modelform_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

from inventory.tenancy import create_tenant_for_user

from .compliance import calculate_gst, estimate_income_tax, round_money
from .excel_templates import build_template
from .forms import (
    ConvertQuantityForm, CustomerForm, EmployeeForm, ExchangeTransactionForm, GSTSlabForm,
    InvoiceSettingForm, MetalRateForm, PaymentReceiptForm, POSSaleForm, ProductForm, PurchaseInvoiceFromReceiptForm,
    PurchaseOrderForm, PurchaseOrderLineFormSet, QuotationForm,
    QuotationLineFormSet, RoleForm,
    SalesApprovalForm, SalesReturnForm, SupplierForm, VendorPaymentForm, WarehouseForm,
    RepairOrderForm, CustomerOrnamentForm,
)
from .models import (
    BankAccount, BinLocation, Brand, Branch, BusinessUnit, Channel, Company, CostCenter,
    Barcode, Category, Customer, CustomerFinanceProfile, CustomerPostingGroup, Department, Division, DimensionSet,
    DocumentNumberSeries, DocumentRelationship, DocumentStatusHistory, Employee, ExchangeTransaction,
    FinanceJournalBatch, FinanceJournalTemplate, FinancePostedVoucher, FinanceVoucher,
    FinanceVoucherType, GLAccount, GSTComponent, GSTGroup, GSTLedgerEntry, GSTRate, GSTRegistration, GSTSlab,
    GSTState, GSTTaxRule, HSNCode, InvoiceSetting, ItemBarcode, ItemBatch, ItemCategory, ItemPrice,
    Item, ItemLocation, ItemUnitOfMeasure, ItemVariant, ItemSerial, JournalEntry, Location, Product, Project, Role, SalesApproval, SalesInvoice,
    SACCode, SalesInvoiceItem, SalesReturn, Staff, StockLedger, Store, Supplier, SupplierInvoice,
    SKU, SpecialGroup, SubCategory, SupplyType, TaxLedger, UnitOfMeasure, UserProfile, VendorFinanceProfile, VendorPostingGroup,
    Warehouse, WarehouseMovement, WarehouseZone, PaymentMethod, PaymentTerm,
    POSStaff, POSTerminal, POSStaffAssignment, POSShift, POSSession,
    JewelleryItemUnit, JewelleryMetalRate, JewelleryPricingRule,
    CustomerAddress, CustomerCommunicationPreference, CustomerIdentityDocument,
    CustomerJewelleryPreference, CustomerKYC, CustomerMarketingProfile,
    InvoicePrintLayout, StoreInvoicePrintSetup, POSTerminalPrintSetup, InvoicePrintLog,
    Karigar, RepairOrder, CustomerOrnament, RepairCustodyEvent, RepairKarigarAssignment, RepairQC,
    PaymentReceipt, Quotation, QuotationLine, SalesOrder, SalesOrderLine,
    GoodsReceipt, PurchaseOrder, VendorPayment, FinancePostingSetup, FinanceVoucher,
    GeneralLedger, GLAccount, JournalEntry,
)
from .services import (
    apply_live_metal_rates, apply_metal_rate, approve_purchase_order, approve_quotation, approve_sales_order,
    build_invoice_dataset, calculate_jewellery_price, convert_quotation_to_sales_order,
    convert_receipt_to_purchase_invoice, convert_sales_order_to_invoice, create_payment_receipt,
    create_purchase_order, create_quotation, create_vendor_payment, get_next_customer_number, get_next_number,
    get_next_repair_number, get_related_documents, move_customer_ornament, post_payment_receipt,
    post_purchase_invoice, post_sales_invoice, post_vendor_payment, receive_goods, submit_for_approval,
    ManualPricingRule, ManualUnit, ProductWeightAsUnit, ResolvedRate, resolve_current_metal_rate,
)


def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('dashboard')
        messages.error(request, 'Invalid username or password.')
    return render(request, 'erp/login.html')


def logout_view(request):
    logout(request)
    return redirect('login')


def index(request):
    if request.user.is_authenticated:
        return dashboard(request)
    context = {
        'features': [
            {'icon': 'fa-cash-register', 'title': 'POS', 'desc': 'Fast jewellery billing.'},
            {'icon': 'fa-coins', 'title': 'Gold & Silver Pricing', 'desc': 'Price jewellery using applicable metal rates.'},
            {'icon': 'fa-boxes-stacked', 'title': 'Inventory', 'desc': 'Track weight, quantity, purity and serialized jewellery.'},
            {'icon': 'fa-users', 'title': 'Customers', 'desc': 'Know every customer and every transaction.'},
            {'icon': 'fa-file-invoice', 'title': 'Sales', 'desc': 'Quotes → Orders → Delivery → Invoice → Payment.'},
            {'icon': 'fa-truck-fast', 'title': 'Purchases', 'desc': 'Vendor → Purchase Order → Receipt → Invoice → Payment.'},
            {'icon': 'fa-scale-balanced', 'title': 'Finance', 'desc': 'Double-entry accounting and financial reporting.'},
            {'icon': 'fa-chart-line', 'title': 'Reports', 'desc': 'Turn business data into decisions.'},
            {'icon': 'fa-store', 'title': 'Multi-Store', 'desc': 'Manage multiple jewellery stores from one environment.'},
        ],
        'trust_items': [
            'Tenant Isolation', 'Secure Authentication', 'Role-Based Access',
            'Audit Trail', 'Cloud Backup', 'Scalable Architecture',
        ],
        'industries': [
            'Gold Retail', 'Diamond Retail', 'Silver Retail', 'Multi-Store Jewellers',
            'Independent Jewellers', 'Jewellery Startups', 'Bullion Businesses', 'Jewellery Brands',
        ],
        'faqs': [
            ('Is Goldio really free?', 'Yes. The initial Goldio Free plan provides access to the complete platform. Goldio may introduce additional plans and limits in the future.'),
            ('Do I need a credit card?', 'No credit card is required to create the Free workspace.'),
            ('Can I create my own business workspace?', 'Yes. Every registration creates an independent business workspace.'),
            ('Can two jewellery businesses use Goldio?', 'Yes. Goldio is designed as a multi-tenant SaaS platform where multiple businesses can operate independently.'),
            ('Will my data be visible to another jewellery business?', 'No. Tenant data is logically isolated and protected by authorization and data-access controls.'),
            ('What is the $2 plan?', 'The $2 Goldio Supporter option is a voluntary way to support the continued development of Goldio. It does not currently remove functionality from the Free plan.'),
        ],
    }
    return render(request, 'marketing/home.html', context)


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        mobile = request.POST.get('mobile', '').strip()
        business_name = request.POST.get('business_name', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        errors = []
        if not first_name:
            errors.append('Please tell us your name.')
        if not email:
            errors.append('Please enter your email address.')
        elif User.objects.filter(username__iexact=email).exists():
            errors.append('An account already exists with this email. Try logging in instead.')
        if not business_name:
            errors.append('Please enter your business name.')
        if not password or len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        elif password != confirm_password:
            errors.append('Passwords do not match.')

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'marketing/register.html', {'form_data': request.POST})

        user = User.objects.create_user(
            username=email, email=email, password=password,
            first_name=first_name, last_name=last_name,
        )
        UserProfile.objects.create(user=user, phone=mobile)
        create_tenant_for_user(user, business_name)
        login(request, user)
        messages.success(request, f'Welcome to Goldio, {first_name}! Your workspace for {business_name} is ready.')
        return redirect('dashboard')

    return render(request, 'marketing/register.html')


@login_required(login_url='login')
def pos_login(request):
    terminal = POSTerminal.objects.filter(is_active=True, status='active').select_related('store').first()
    if request.method == 'POST':
        staff_code = request.POST.get('staff_id', '').strip()
        pin = request.POST.get('pin', '')
        staff = POSStaff.objects.filter(employee_code__iexact=staff_code).select_related('store').first()
        assignment = POSStaffAssignment.objects.filter(
            staff=staff, terminal=terminal, active=True, role__in=['cashier', 'manager', 'sales_staff'],
        ).first() if staff and terminal else None
        if not terminal:
            messages.error(request, 'This POS terminal is not configured.')
        elif not staff or not staff.is_active or staff.is_blocked or not staff.pos_access:
            messages.error(request, 'Access denied. Check your staff ID or account status.')
        elif not check_password(pin, staff.pin_hash):
            messages.error(request, 'Access denied. Invalid staff ID or PIN.')
        elif not assignment or assignment.terminal.store_id != staff.store_id:
            messages.error(request, 'Your staff account is not assigned to this POS terminal.')
        else:
            shift = POSShift.objects.filter(terminal=terminal, status='open').first()
            if not shift:
                shift = POSShift.objects.create(
                    shift_code=f'SHIFT-{timezone.now():%Y%m%d%H%M%S}',
                    store=terminal.store, terminal=terminal, opening_staff=staff,
                )
            if not request.session.session_key:
                request.session.save()
            request.session['pos_session_id'] = POSSession.objects.create(
                session_key=request.session.session_key,
                staff=staff, terminal=terminal, shift=shift,
                ip_address=request.META.get('REMOTE_ADDR'), device_id=terminal.device_id,
            ).pk
            terminal.last_login = timezone.now()
            terminal.save(update_fields=['last_login'])
            return redirect('pos')
    return render(request, 'erp/pos_login.html', {'terminal': terminal})


@login_required(login_url='login')
def pos_logout(request):
    pos_session = POSSession.objects.filter(id=request.session.get('pos_session_id'), status='active').first()
    if pos_session:
        pos_session.status = 'logged_out'
        pos_session.logout_time = timezone.now()
        pos_session.save(update_fields=['status', 'logout_time'])
    request.session.pop('pos_session_id', None)
    return redirect('pos_login')


@login_required(login_url='login')
def dashboard(request):
    total_sales = SalesInvoice.objects.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    total_exchanges = ExchangeTransaction.objects.aggregate(total=Sum('exchange_value'))['total'] or Decimal('0')
    pending_approvals = SalesApproval.objects.filter(status='pending').count()
    products = Product.objects.order_by('-sale_price')[:5]
    masters = {
        'items': Product.objects.count(),
        'customers': Customer.objects.count(),
        'employees': Employee.objects.count(),
        'warehouses': Warehouse.objects.count(),
        'gst': GSTSlab.objects.filter(is_active=True).count(),
    }
    context = {
        'total_sales': round_money(total_sales),
        'total_exchanges': round_money(total_exchanges),
        'pending_approvals': pending_approvals,
        'products': products,
        'masters': masters,
    }
    return render(request, 'erp/dashboard.html', context)


MASTER_SPECS = {
    'companies': (Company, ['company_code', 'company_name', 'status']),
    'branches': (Branch, ['branch_code', 'branch_name', 'company', 'status']),
    'locations': (Location, ['location_code', 'location_name', 'company', 'warehouse', 'status']),
    'departments': (Department, ['department_code', 'department_name', 'company', 'status']),
    'business-units': (BusinessUnit, ['business_unit_code', 'business_unit_name', 'company', 'status']),
    'projects': (Project, ['project_code', 'project_name', 'company', 'status']),
    'cost-centers': (CostCenter, ['code', 'name', 'company', 'is_active']),
    'stores': (Store, ['code', 'name', 'company', 'branch', 'is_active']),
    'brands': (Brand, ['code', 'name', 'company', 'is_active']),
    'channels': (Channel, ['code', 'name', 'company', 'is_active']),
    'divisions': (Division, ['code', 'name', 'company', 'active', 'blocked']),
    'special-groups': (SpecialGroup, ['code', 'name', 'division', 'active', 'blocked']),
    'categories': (Category, ['code', 'name', 'special_group', 'parent', 'active', 'blocked']),
    'subcategories': (SubCategory, ['code', 'name', 'category', 'active', 'blocked']),
    'units-of-measure': (UnitOfMeasure, ['code', 'name', 'company', 'is_active']),
    'retail-items': (Item, ['item_number', 'name', 'company', 'division', 'category', 'subcategory', 'base_uom', 'status']),
    'item-uoms': (ItemUnitOfMeasure, ['item', 'uom', 'quantity_per_uom', 'is_inventory_uom']),
    'item-variants': (ItemVariant, ['code', 'name', 'item', 'color', 'size', 'active', 'blocked']),
    'skus': (SKU, ['code', 'item', 'variant', 'location', 'cost', 'price', 'active', 'blocked']),
    'retail-barcodes': (Barcode, ['value', 'barcode_type', 'sku', 'uom', 'is_primary', 'active']),
    'item-locations': (ItemLocation, ['item', 'variant', 'location', 'default_bin', 'reorder_point', 'active']),
    'gl-accounts': (GLAccount, ['account_code', 'account_name', 'company', 'account_type', 'status']),
    'bank-accounts': (BankAccount, ['bank_name', 'account_name', 'company', 'account_type', 'status']),
    'number-series': (DocumentNumberSeries, ['document_type', 'company', 'fiscal_year', 'prefix', 'next_number']),
    'finance-journal-templates': (FinanceJournalTemplate, ['code', 'name', 'operation_type', 'voucher_type', 'approval_required', 'is_active']),
    'finance-journal-batches': (FinanceJournalBatch, ['code', 'name', 'company', 'template', 'user', 'status']),
    'finance-voucher-types': (FinanceVoucherType, ['code', 'name', 'category', 'company', 'template', 'approval_required', 'active']),
    'finance-vouchers': (FinanceVoucher, ['voucher_no', 'company', 'batch', 'voucher_type', 'total_debit', 'total_credit', 'status']),
    'posted-vouchers': (FinancePostedVoucher, ['posting_no', 'voucher_no', 'company_name', 'posting_date', 'total_debit', 'total_credit']),
    'payment-terms': (PaymentTerm, ['code', 'name', 'company', 'due_days', 'discount_percent', 'is_active']),
    'payment-methods': (PaymentMethod, ['code', 'name', 'company', 'method_type', 'is_active']),
    'customer-posting-groups': (CustomerPostingGroup, ['code', 'name', 'company', 'receivable_account', 'advance_account', 'is_active']),
    'vendor-posting-groups': (VendorPostingGroup, ['code', 'name', 'company', 'payable_account', 'advance_account', 'is_active']),
    'customer-finance-profiles': (CustomerFinanceProfile, ['customer', 'payment_term', 'payment_method', 'posting_group', 'credit_limit', 'credit_hold']),
    'vendor-finance-profiles': (VendorFinanceProfile, ['vendor', 'payment_term', 'payment_method', 'posting_group', 'payment_hold']),
    'finance-posting-setup': (FinancePostingSetup, ['company', 'sales_revenue_account', 'gst_output_account', 'purchase_expense_account', 'gst_input_account', 'default_cash_account', 'rounding_account']),
    'document-relationships': (DocumentRelationship, ['source_type', 'source_id', 'target_type', 'target_id', 'relationship_type', 'created_at']),
    'document-status-history': (DocumentStatusHistory, ['document_type', 'document_id', 'from_status', 'to_status', 'changed_by', 'changed_at']),
    'suppliers': (Supplier, ['name', 'phone', 'gstin', 'is_active']),
    'customers': (Customer, ['name', 'phone', 'gstin', 'customer_type', 'is_active']),
    'products': (Product, ['sku', 'name', 'item_category', 'sale_price', 'is_active']),
    'warehouses': (Warehouse, ['code', 'name', 'company', 'location_type', 'status']),
    'zones': (WarehouseZone, ['code', 'name', 'warehouse', 'zone_type', 'priority', 'status']),
    'bins': (BinLocation, ['code', 'warehouse', 'zone', 'bin_type', 'rank', 'capacity', 'is_active']),
    'gst-slabs': (GSTSlab, ['name', 'gst_rate', 'hsn_code', 'is_active']),
    'gst-registrations': (GSTRegistration, ['gstin', 'legal_name', 'company', 'state', 'registration_type', 'status']),
    'gst-states': (GSTState, ['state_code', 'state_name', 'gst_state_code', 'is_union_territory', 'is_active']),
    'gst-groups': (GSTGroup, ['code', 'description', 'taxability', 'reverse_charge', 'itc_allowed', 'status']),
    'gst-components': (GSTComponent, ['code', 'name', 'recoverable', 'payable', 'receivable', 'status']),
    'gst-rates': (GSTRate, ['code', 'description', 'cgst_rate', 'sgst_rate', 'igst_rate', 'cess_rate', 'effective_from', 'status']),
    'hsn-codes': (HSNCode, ['code', 'description', 'gst_group', 'effective_from', 'status']),
    'sac-codes': (SACCode, ['code', 'description', 'service_category', 'gst_group', 'effective_from', 'status']),
    'supply-types': (SupplyType, ['code', 'description', 'status']),
    'gst-tax-rules': (GSTTaxRule, ['code', 'description', 'priority', 'supply_type', 'rate', 'effective_from', 'status']),
    'gst-ledger': (GSTLedgerEntry, ['entry_no', 'company', 'registration', 'posting_date', 'document_type', 'document_no', 'taxable_value', 'igst', 'status']),
    'roles': (Role, ['name', 'description', 'is_system']),
    'employees': (Employee, ['employee_code', 'full_name', 'designation', 'is_active']),
}


@login_required(login_url='login')
def finance_management(request):
    context = {
        'gl_accounts': GLAccount.objects.select_related('company').order_by('account_code')[:12],
        'journal_count': JournalEntry.objects.count(),
        'posted_journals': JournalEntry.objects.filter(status='posted').count(),
        'bank_balance': BankAccount.objects.aggregate(total=Sum('current_balance'))['total'] or Decimal('0'),
        'payables': SupplierInvoice.objects.aggregate(total=Sum('net_amount'))['total'] or Decimal('0'),
        'tax_posted': TaxLedger.objects.aggregate(total=Sum('tax_amount'))['total'] or Decimal('0'),
        'dimension_count': DimensionSet.objects.count(),
        'recent_journals': JournalEntry.objects.select_related('company').order_by('-entry_date')[:10],
        'recent_payables': SupplierInvoice.objects.select_related('supplier', 'company').order_by('-invoice_date')[:10],
    }
    return render(request, 'erp/finance_management.html', context)


@login_required(login_url='login')
def finance_operations(request):
    return render(request, 'erp/finance_operations.html', {
        'drafts': FinanceVoucher.objects.filter(status__in=['draft', 'saved']).select_related('voucher_type', 'created_by').order_by('-created_at')[:20],
        'approvals': FinanceVoucher.objects.filter(status__in=['submitted', 'under_review']).select_related('voucher_type', 'created_by').order_by('-created_at')[:20],
        'posted': FinanceVoucher.objects.filter(status='posted').select_related('voucher_type', 'posted_by').order_by('-posted_at')[:20],
    })


@login_required(login_url='login')
def sales_receivables(request):
    """Sales & Receivables hub — links to the 8 document sub-sections in spec order."""
    return render(request, 'erp/sales_receivables.html', {
        'customers': Customer.objects.filter(is_active=True).count(),
        'open_quotations': Quotation.objects.exclude(status__in=['converted', 'expired', 'rejected', 'cancelled']).count(),
        'open_sales_orders': SalesOrder.objects.exclude(status__in=['completed', 'cancelled', 'rejected']).count(),
        'sales_invoices': SalesInvoice.objects.count(),
        'sales_value': SalesInvoice.objects.aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
        'open_receipts': PaymentReceipt.objects.filter(status='draft').count(),
    })


@login_required(login_url='login')
def sales_receivables_placeholder(request, section):
    labels = {
        'proforma': 'Proforma Invoices',
        'delivery_challans': 'Delivery Challans',
        'credit_notes': 'Credit Notes',
    }
    return render(request, 'erp/sales_receivables_placeholder.html', {
        'section_label': labels.get(section, section.replace('_', ' ').title()),
    })


@login_required(login_url='login')
def invoice_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    invoices = SalesInvoice.objects.select_related('customer').order_by('-sales_date')
    if query:
        invoices = invoices.filter(Q(invoice_no__icontains=query) | Q(customer__name__icontains=query) | Q(customer__phone__icontains=query))
    if status:
        invoices = invoices.filter(status=status)
    open_invoices = SalesInvoice.objects.exclude(status='cancelled')
    receivables_balance = sum((inv.balance_amount for inv in open_invoices), Decimal('0'))
    return render(request, 'erp/invoice_list.html', {
        'customers': Customer.objects.filter(is_active=True).count(),
        'sales_invoices': SalesInvoice.objects.count(),
        'sales_value': SalesInvoice.objects.aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
        'receivables_balance': receivables_balance,
        'recent_invoices': invoices,
        'query': query,
        'status_filter': status,
        'status_choices': SalesInvoice.STATUS_CHOICES,
    })


@login_required(login_url='login')
def invoice_post(request, pk):
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    if request.method == 'POST':
        try:
            post_sales_invoice(invoice, request.user)
            messages.success(request, f'Invoice {invoice.invoice_no} posted.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('invoice_detail', pk=pk)


@login_required(login_url='login')
def quotation_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    tab = request.GET.get('tab', 'open')
    closed_statuses = ['converted', 'expired', 'rejected', 'cancelled']
    quotations = Quotation.objects.select_related('customer', 'store').order_by('-quotation_date')
    if query:
        quotations = quotations.filter(Q(quotation_no__icontains=query) | Q(customer__name__icontains=query) | Q(customer__phone__icontains=query))
    if status:
        quotations = quotations.filter(status=status)
    elif tab == 'history':
        quotations = quotations.filter(status__in=closed_statuses)
    else:
        quotations = quotations.exclude(status__in=closed_statuses)
    return render(request, 'erp/quotation_list.html', {
        'quotations': quotations, 'query': query, 'status_filter': status, 'tab': tab,
        'status_choices': Quotation.STATUS_CHOICES,
    })


@login_required(login_url='login')
def quotation_create(request):
    store = None
    session = POSSession.objects.filter(id=request.session.get('pos_session_id'), status='active').select_related('terminal__store').first()
    if session:
        store = session.terminal.store
    else:
        store = Store.objects.filter(is_active=True).first()

    if request.method == 'POST':
        form = QuotationForm(request.POST)
        formset = QuotationLineFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            lines_data = []
            for line_form in formset:
                cleaned = line_form.cleaned_data
                if not cleaned or cleaned.get('DELETE') or not cleaned.get('product'):
                    continue
                barcode = cleaned.get('jewellery_barcode')
                jewellery_unit = JewelleryItemUnit.objects.filter(barcode=barcode, current_status='available').first() if barcode else None
                lines_data.append({
                    'product': cleaned['product'], 'quantity': cleaned['quantity'],
                    'discount_amount': cleaned.get('discount_amount') or Decimal('0'),
                    'jewellery_unit': jewellery_unit,
                })
            if not lines_data:
                messages.error(request, 'Add at least one line to the quotation.')
            else:
                try:
                    quotation = create_quotation(
                        customer=form.cleaned_data['customer'], store=form.cleaned_data.get('store') or store,
                        salesperson=form.cleaned_data.get('salesperson'), lines_data=lines_data,
                        valid_until=form.cleaned_data.get('valid_until'), payment_terms=form.cleaned_data.get('payment_terms'),
                        delivery_terms=form.cleaned_data.get('delivery_terms', ''), remarks=form.cleaned_data.get('remarks', ''),
                        user=request.user,
                    )
                    messages.success(request, f'Quotation {quotation.quotation_no} created.')
                    return redirect('quotation_detail', pk=quotation.pk)
                except ValueError as exc:
                    messages.error(request, str(exc))
    else:
        form = QuotationForm(initial={'store': store})
        formset = QuotationLineFormSet()
    return render(request, 'erp/quotation_form.html', {'form': form, 'formset': formset})


@login_required(login_url='login')
def quotation_detail(request, pk):
    quotation = get_object_or_404(Quotation.objects.select_related('customer', 'store', 'salesperson'), pk=pk)
    return render(request, 'erp/quotation_detail.html', {
        'quotation': quotation,
        'lines': quotation.lines.select_related('product', 'jewellery_unit').all(),
        'related': get_related_documents(quotation, 'quotation'),
        'history': DocumentStatusHistory.objects.filter(document_type='quotation', document_id=quotation.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def quotation_submit(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    if request.method == 'POST':
        try:
            submit_for_approval(quotation, 'quotation', request.user)
            messages.success(request, f'Quotation {quotation.quotation_no} submitted for approval.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('quotation_detail', pk=pk)


@login_required(login_url='login')
def quotation_approve(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    if request.method == 'POST':
        approved = request.POST.get('decision') == 'approve'
        try:
            approve_quotation(quotation, request.user, approved=approved, notes=request.POST.get('notes', ''))
            messages.success(request, f'Quotation {quotation.quotation_no} {"approved" if approved else "rejected"}.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('quotation_detail', pk=pk)


@login_required(login_url='login')
def quotation_convert(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    lines = [line for line in quotation.lines.select_related('product').all() if line.remaining_quantity > 0]
    if request.method == 'POST':
        form = ConvertQuantityForm(request.POST, source_lines=lines)
        if form.is_valid():
            line_quantities = {key.split('_', 1)[1]: value for key, value in form.cleaned_data.items() if value}
            try:
                sales_order = convert_quotation_to_sales_order(quotation, request.user, line_quantities)
                messages.success(request, f'Sales Order {sales_order.order_no} created from {quotation.quotation_no}.')
                return redirect('sales_order_detail', pk=sales_order.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = ConvertQuantityForm(source_lines=lines)
    return render(request, 'erp/quotation_convert.html', {'quotation': quotation, 'form': form, 'lines': lines})


@login_required(login_url='login')
def sales_order_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    tab = request.GET.get('tab', 'open')
    closed_statuses = ['completed', 'cancelled', 'rejected']
    orders = SalesOrder.objects.select_related('customer', 'store').order_by('-order_date')
    if query:
        orders = orders.filter(Q(order_no__icontains=query) | Q(customer__name__icontains=query) | Q(customer__phone__icontains=query))
    if status:
        orders = orders.filter(status=status)
    elif tab == 'history':
        orders = orders.filter(status__in=closed_statuses)
    else:
        orders = orders.exclude(status__in=closed_statuses)
    return render(request, 'erp/sales_order_list.html', {
        'orders': orders, 'query': query, 'status_filter': status, 'tab': tab,
        'status_choices': SalesOrder.STATUS_CHOICES,
    })


@login_required(login_url='login')
def sales_order_detail(request, pk):
    order = get_object_or_404(SalesOrder.objects.select_related('customer', 'store', 'salesperson', 'quotation'), pk=pk)
    return render(request, 'erp/sales_order_detail.html', {
        'order': order,
        'lines': order.lines.select_related('product', 'jewellery_unit').all(),
        'related': get_related_documents(order, 'sales_order'),
        'history': DocumentStatusHistory.objects.filter(document_type='sales_order', document_id=order.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def sales_order_submit(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    if request.method == 'POST':
        try:
            submit_for_approval(order, 'sales_order', request.user)
            messages.success(request, f'Sales Order {order.order_no} submitted for approval.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('sales_order_detail', pk=pk)


@login_required(login_url='login')
def sales_order_approve(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    if request.method == 'POST':
        approved = request.POST.get('decision') == 'approve'
        try:
            order, warning = approve_sales_order(order, request.user, approved=approved, notes=request.POST.get('notes', ''))
            if warning:
                messages.warning(request, warning)
            messages.success(request, f'Sales Order {order.order_no} {"approved" if approved else "rejected"}.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('sales_order_detail', pk=pk)


@login_required(login_url='login')
def sales_order_convert(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    lines = [line for line in order.lines.select_related('product').all() if line.remaining_quantity > 0]
    if request.method == 'POST':
        form = ConvertQuantityForm(request.POST, source_lines=lines)
        if form.is_valid():
            line_quantities = {key.split('_', 1)[1]: value for key, value in form.cleaned_data.items() if value}
            try:
                invoice = convert_sales_order_to_invoice(order, request.user, line_quantities)
                messages.success(request, f'Invoice {invoice.invoice_no} created from {order.order_no}.')
                return redirect('invoice_detail', pk=invoice.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = ConvertQuantityForm(source_lines=lines)
    return render(request, 'erp/sales_order_convert.html', {'order': order, 'form': form, 'lines': lines})


@login_required(login_url='login')
def payment_receipt_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    receipts = PaymentReceipt.objects.select_related('customer').order_by('-receipt_date')
    if query:
        receipts = receipts.filter(Q(receipt_no__icontains=query) | Q(customer__name__icontains=query))
    if status:
        receipts = receipts.filter(status=status)
    return render(request, 'erp/payment_receipt_list.html', {
        'receipts': receipts, 'query': query, 'status_filter': status, 'status_choices': PaymentReceipt.STATUS_CHOICES,
    })


@login_required(login_url='login')
def payment_receipt_detail(request, pk):
    receipt = get_object_or_404(PaymentReceipt.objects.select_related('customer', 'store', 'payment_method', 'bank_account'), pk=pk)
    return render(request, 'erp/payment_receipt_detail.html', {
        'receipt': receipt,
        'allocations': receipt.allocations.select_related('invoice').all(),
        'related': get_related_documents(receipt, 'payment_receipt'),
        'history': DocumentStatusHistory.objects.filter(document_type='payment_receipt', document_id=receipt.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def payment_receipt_create(request):
    customer_id = request.GET.get('customer') or request.POST.get('customer')
    outstanding_invoices = []
    if customer_id:
        outstanding_invoices = list(
            SalesInvoice.objects.filter(customer_id=customer_id).exclude(payment_status='paid').exclude(status='cancelled').order_by('sales_date')
        )
    if request.method == 'POST':
        form = PaymentReceiptForm(request.POST)
        if form.is_valid():
            allocations = {}
            for invoice in outstanding_invoices:
                raw = request.POST.get(f'allocate_{invoice.pk}')
                if raw:
                    try:
                        value = Decimal(raw)
                    except Exception:
                        value = Decimal('0')
                    if value > 0:
                        allocations[invoice.pk] = value
            try:
                receipt = create_payment_receipt(
                    customer=form.cleaned_data['customer'], store=form.cleaned_data.get('store'),
                    payment_method=form.cleaned_data['payment_method'], amount=form.cleaned_data['amount'],
                    allocations=allocations, user=request.user, bank_account=form.cleaned_data.get('bank_account'),
                    reference_no=form.cleaned_data.get('reference_no', ''), transaction_id=form.cleaned_data.get('transaction_id', ''),
                    remarks=form.cleaned_data.get('remarks', ''),
                )
                messages.success(request, f'Payment Receipt {receipt.receipt_no} created.')
                return redirect('payment_receipt_detail', pk=receipt.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = PaymentReceiptForm(initial={'customer': customer_id} if customer_id else None)
    return render(request, 'erp/payment_receipt_form.html', {
        'form': form, 'outstanding_invoices': outstanding_invoices,
        'customers': Customer.objects.filter(is_active=True).order_by('name'),
        'selected_customer_id': int(customer_id) if customer_id else None,
    })


@login_required(login_url='login')
def payment_receipt_post(request, pk):
    receipt = get_object_or_404(PaymentReceipt, pk=pk)
    if request.method == 'POST':
        try:
            post_payment_receipt(receipt, request.user)
            messages.success(request, f'Payment Receipt {receipt.receipt_no} posted.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('payment_receipt_detail', pk=pk)


@login_required(login_url='login')
def repair_order_list(request):
    repairs = RepairOrder.objects.select_related('customer', 'store').order_by('-created_at')
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    if query:
        repairs = repairs.filter(Q(order_no__icontains=query) | Q(customer__name__icontains=query) | Q(customer__phone__icontains=query))
    if status:
        repairs = repairs.filter(status=status)
    return render(request, 'erp/repair_order_list.html', {'repairs': repairs, 'query': query, 'status_filter': status, 'status_choices': RepairOrder.STATUS_CHOICES})


@login_required(login_url='login')
def repair_order_create(request):
    store = None
    session = POSSession.objects.filter(id=request.session.get('pos_session_id'), status='active').select_related('terminal__store', 'staff').first()
    if session:
        store = session.terminal.store
    else:
        store = Store.objects.filter(is_active=True).first()
    if request.method == 'POST':
        order_form = RepairOrderForm(request.POST)
        ornament_form = CustomerOrnamentForm(request.POST)
        if order_form.is_valid() and ornament_form.is_valid() and store:
            order = order_form.save(commit=False)
            order.order_no = get_next_repair_number(store)
            order.store = store
            order.customer_name_snapshot = order.customer.name
            order.customer_phone_snapshot = order.customer.phone
            order.created_by = request.user
            order.received_by = session.staff if session else None
            order.save()
            ornament = ornament_form.save(commit=False)
            ornament.ornament_id = f'CO-{timezone.now():%Y%m%d}-{order.pk:06d}'
            ornament.repair_order = order
            ornament.customer = order.customer
            ornament.save()
            messages.success(request, f'Repair order {order.order_no} created in draft.')
            return redirect('repair_order_detail', pk=order.pk)
    else:
        order_form = RepairOrderForm()
        ornament_form = CustomerOrnamentForm()
    return render(request, 'erp/repair_order_form.html', {'order_form': order_form, 'ornament_form': ornament_form, 'store': store})


@login_required(login_url='login')
def repair_order_detail(request, pk):
    order = get_object_or_404(RepairOrder.objects.select_related('customer', 'store', 'ornament'), pk=pk)
    return render(request, 'erp/repair_order_detail.html', {'order': order, 'events': order.custody_events.select_related('performed_by'), 'assignments': order.karigar_assignment if hasattr(order, 'karigar_assignment') else None})


@login_required(login_url='login')
def repair_order_accept(request, pk):
    order = get_object_or_404(RepairOrder, pk=pk)
    if order.status != 'draft':
        messages.error(request, 'Only draft repair orders can be accepted.')
    elif not order.ornament.condition_at_receipt:
        messages.error(request, 'Capture the ornament condition before acceptance.')
    else:
        order.status = 'accepted'
        order.save(update_fields=['status', 'updated_at'])
        order.ornament.accepted_at = timezone.now()
        order.ornament.save(update_fields=['accepted_at'])
        RepairCustodyEvent.objects.create(repair_order=order, ornament=order.ornament, to_location='STORE', status='accepted', remarks='Customer ornament accepted into custody.')
        messages.success(request, 'Customer ornament accepted and custody started.')
    return redirect('repair_order_detail', pk=order.pk)


@login_required(login_url='login')
def repair_order_issue(request, pk):
    order = get_object_or_404(RepairOrder.objects.select_related('ornament'), pk=pk)
    karigar = Karigar.objects.filter(is_active=True, store=order.store).first()
    if order.status not in {'accepted', 'assigned'} or not karigar:
        messages.error(request, 'Repair must be accepted and an active Karigar must be configured.')
    else:
        RepairKarigarAssignment.objects.update_or_create(repair_order=order, defaults={'karigar': karigar, 'karigar_name_snapshot': karigar.name})
        move_customer_ornament(repair_order=order, to_location=f'KARIGAR:{karigar.code}', status='with_karigar', remarks='Issued for repair work.')
        messages.success(request, f'Ornament issued to {karigar.name}.')
    return redirect('repair_order_detail', pk=order.pk)


@login_required(login_url='login')
def repair_order_qc(request, pk):
    order = get_object_or_404(RepairOrder.objects.select_related('ornament'), pk=pk)
    if order.status not in {'returned_by_karigar', 'qc_pending'}:
        messages.error(request, 'QC is available after the Karigar returns the ornament.')
    else:
        RepairQC.objects.update_or_create(repair_order=order, defaults={'passed': True})
        order.status = 'qc_passed'
        order.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'QC passed. Repair is ready for billing.')
    return redirect('repair_order_detail', pk=order.pk)


@login_required(login_url='login')
def repair_order_receive(request, pk):
    order = get_object_or_404(RepairOrder.objects.select_related('ornament'), pk=pk)
    if order.status != 'with_karigar':
        messages.error(request, 'The repair must be with a Karigar before receiving it back.')
    else:
        move_customer_ornament(repair_order=order, to_location='STORE', status='returned_by_karigar', remarks='Returned by Karigar for inspection.')
        order.status = 'qc_pending'
        order.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Ornament received from Karigar and queued for QC.')
    return redirect('repair_order_detail', pk=order.pk)


@login_required(login_url='login')
def repair_order_return(request, pk):
    order = get_object_or_404(RepairOrder.objects.select_related('ornament'), pk=pk)
    if order.status != 'paid':
        messages.error(request, 'The repair invoice must be paid before returning the customer ornament.')
    else:
        move_customer_ornament(repair_order=order, to_location='CUSTOMER', status='returned_to_customer', remarks='Delivered to customer.')
        order.status = 'closed'
        order.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Ornament returned to customer and repair order closed.')
    return redirect('repair_order_detail', pk=order.pk)


@login_required(login_url='login')
def purchase_payables(request):
    """Purchase & Payables hub - links to the 7 document sub-sections in spec order."""
    open_invoices = SupplierInvoice.objects.exclude(status='cancelled')
    return render(request, 'erp/purchase_payables.html', {
        'vendors': Supplier.objects.filter(is_active=True).count(),
        'open_purchase_orders': PurchaseOrder.objects.exclude(status__in=['fully_received', 'cancelled', 'rejected']).count(),
        'purchase_invoices': SupplierInvoice.objects.count(),
        'purchase_value': SupplierInvoice.objects.aggregate(total=Sum('net_amount'))['total'] or Decimal('0'),
        'open_payables': sum((invoice.outstanding_amount for invoice in open_invoices), Decimal('0')),
        'draft_payments': VendorPayment.objects.filter(status='draft').count(),
    })


@login_required(login_url='login')
def purchase_payables_placeholder(request, section):
    labels = {
        'vendor-leads': 'Vendor Leads',
        'debit-notes': 'Debit Notes',
        'hire-best-vendors': 'Hire The Best Vendors',
    }
    return render(request, 'erp/sales_receivables_placeholder.html', {
        'section_label': labels.get(section, section.replace('-', ' ').title()),
        'back_url_name': 'purchase_payables',
        'back_label': 'Purchase & Payables',
    })


@login_required(login_url='login')
def purchase_invoice_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    invoices = SupplierInvoice.objects.select_related('supplier').order_by('-invoice_date')
    if query:
        invoices = invoices.filter(Q(invoice_no__icontains=query) | Q(document_no__icontains=query) | Q(supplier__name__icontains=query))
    if status:
        invoices = invoices.filter(status=status)
    return render(request, 'erp/purchase_invoice_list.html', {
        'invoices': invoices, 'query': query, 'status_filter': status, 'status_choices': SupplierInvoice.STATUS_CHOICES,
    })


@login_required(login_url='login')
def purchase_invoice_detail(request, pk):
    invoice = get_object_or_404(SupplierInvoice.objects.select_related('supplier', 'purchase_order', 'goods_receipt'), pk=pk)
    return render(request, 'erp/purchase_invoice_detail.html', {
        'invoice': invoice,
        'lines': invoice.lines.select_related('product').all(),
        'related': get_related_documents(invoice, 'purchase_invoice'),
        'history': DocumentStatusHistory.objects.filter(document_type='purchase_invoice', document_id=invoice.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def purchase_invoice_post(request, pk):
    invoice = get_object_or_404(SupplierInvoice, pk=pk)
    if request.method == 'POST':
        try:
            post_purchase_invoice(invoice, request.user)
            messages.success(request, f'Purchase Invoice {invoice.document_no or invoice.invoice_no} posted.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('purchase_invoice_detail', pk=pk)


@login_required(login_url='login')
def purchase_invoice_create_from_receipt(request, receipt_pk):
    receipt = get_object_or_404(GoodsReceipt.objects.select_related('purchase_order', 'vendor'), pk=receipt_pk)
    lines = [line for line in receipt.purchase_order.lines.select_related('product').all() if line.remaining_to_invoice > 0]
    if request.method == 'POST':
        form = PurchaseInvoiceFromReceiptForm(request.POST)
        qty_form = ConvertQuantityForm(request.POST, source_lines=lines, quantity_attr='remaining_to_invoice')
        if form.is_valid() and qty_form.is_valid():
            line_quantities = {key.split('_', 1)[1]: value for key, value in qty_form.cleaned_data.items() if value}
            try:
                invoice = convert_receipt_to_purchase_invoice(
                    receipt, request.user, vendor_invoice_no=form.cleaned_data['vendor_invoice_no'],
                    vendor_invoice_date=form.cleaned_data.get('vendor_invoice_date'), line_quantities=line_quantities,
                )
                messages.success(request, f'Purchase Invoice {invoice.document_no} created from {receipt.receipt_no}.')
                return redirect('purchase_invoice_detail', pk=invoice.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = PurchaseInvoiceFromReceiptForm()
        qty_form = ConvertQuantityForm(source_lines=lines, quantity_attr='remaining_to_invoice')
    return render(request, 'erp/purchase_invoice_from_receipt.html', {
        'receipt': receipt, 'form': form, 'qty_form': qty_form, 'lines': lines,
    })


@login_required(login_url='login')
def purchase_order_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    tab = request.GET.get('tab', 'open')
    closed_statuses = ['fully_received', 'cancelled', 'rejected']
    orders = PurchaseOrder.objects.select_related('vendor', 'warehouse').order_by('-order_date')
    if query:
        orders = orders.filter(Q(order_no__icontains=query) | Q(vendor__name__icontains=query))
    if status:
        orders = orders.filter(status=status)
    elif tab == 'history':
        orders = orders.filter(status__in=closed_statuses)
    else:
        orders = orders.exclude(status__in=closed_statuses)
    return render(request, 'erp/purchase_order_list.html', {
        'orders': orders, 'query': query, 'status_filter': status, 'tab': tab,
        'status_choices': PurchaseOrder.STATUS_CHOICES,
    })


@login_required(login_url='login')
def purchase_order_create(request):
    if request.method == 'POST':
        form = PurchaseOrderForm(request.POST)
        formset = PurchaseOrderLineFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            lines_data = []
            for line_form in formset:
                cleaned = line_form.cleaned_data
                if not cleaned or cleaned.get('DELETE') or not cleaned.get('product'):
                    continue
                lines_data.append({
                    'product': cleaned['product'], 'quantity': cleaned['quantity'],
                    'unit_price': cleaned.get('unit_price') or Decimal('0'),
                    'discount_amount': cleaned.get('discount_amount') or Decimal('0'),
                    'tax_rate': cleaned.get('tax_rate') or Decimal('18.00'),
                })
            if not lines_data:
                messages.error(request, 'Add at least one line to the purchase order.')
            else:
                try:
                    po = create_purchase_order(
                        vendor=form.cleaned_data['vendor'], warehouse=form.cleaned_data.get('warehouse'),
                        buyer=form.cleaned_data.get('buyer'), lines_data=lines_data,
                        expected_delivery_date=form.cleaned_data.get('expected_delivery_date'),
                        payment_terms=form.cleaned_data.get('payment_terms'), remarks=form.cleaned_data.get('remarks', ''),
                        user=request.user,
                    )
                    messages.success(request, f'Purchase Order {po.order_no} created.')
                    return redirect('purchase_order_detail', pk=po.pk)
                except ValueError as exc:
                    messages.error(request, str(exc))
    else:
        form = PurchaseOrderForm()
        formset = PurchaseOrderLineFormSet()
    return render(request, 'erp/purchase_order_form.html', {'form': form, 'formset': formset})


@login_required(login_url='login')
def purchase_order_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related('vendor', 'warehouse', 'buyer'), pk=pk)
    return render(request, 'erp/purchase_order_detail.html', {
        'order': po,
        'lines': po.lines.select_related('product').all(),
        'related': get_related_documents(po, 'purchase_order'),
        'history': DocumentStatusHistory.objects.filter(document_type='purchase_order', document_id=po.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def purchase_order_submit(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method == 'POST':
        try:
            submit_for_approval(po, 'purchase_order', request.user)
            messages.success(request, f'Purchase Order {po.order_no} submitted for approval.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('purchase_order_detail', pk=pk)


@login_required(login_url='login')
def purchase_order_approve(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method == 'POST':
        approved = request.POST.get('decision') == 'approve'
        try:
            approve_purchase_order(po, request.user, approved=approved, notes=request.POST.get('notes', ''))
            messages.success(request, f'Purchase Order {po.order_no} {"approved" if approved else "rejected"}.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('purchase_order_detail', pk=pk)


@login_required(login_url='login')
def purchase_order_receive(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    lines = [line for line in po.lines.select_related('product').all() if line.remaining_quantity > 0]
    if request.method == 'POST':
        form = ConvertQuantityForm(request.POST, source_lines=lines)
        if form.is_valid():
            line_quantities = {key.split('_', 1)[1]: value for key, value in form.cleaned_data.items() if value}
            try:
                receipt = receive_goods(po, request.user, line_quantities)
                messages.success(request, f'Goods Receipt {receipt.receipt_no} posted against {po.order_no}.')
                return redirect('purchase_order_detail', pk=po.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = ConvertQuantityForm(source_lines=lines)
    return render(request, 'erp/purchase_order_receive.html', {'order': po, 'form': form, 'lines': lines})


@login_required(login_url='login')
def vendor_payment_list(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    payments = VendorPayment.objects.select_related('vendor').order_by('-payment_date')
    if query:
        payments = payments.filter(Q(payment_no__icontains=query) | Q(vendor__name__icontains=query))
    if status:
        payments = payments.filter(status=status)
    return render(request, 'erp/vendor_payment_list.html', {
        'payments': payments, 'query': query, 'status_filter': status, 'status_choices': VendorPayment.STATUS_CHOICES,
    })


@login_required(login_url='login')
def vendor_payment_detail(request, pk):
    payment = get_object_or_404(VendorPayment.objects.select_related('vendor', 'bank_account', 'payment_method'), pk=pk)
    return render(request, 'erp/vendor_payment_detail.html', {
        'payment': payment,
        'allocations': payment.allocations.select_related('supplier_invoice').all(),
        'related': get_related_documents(payment, 'vendor_payment'),
        'history': DocumentStatusHistory.objects.filter(document_type='vendor_payment', document_id=payment.pk).order_by('changed_at'),
    })


@login_required(login_url='login')
def vendor_payment_create(request):
    vendor_id = request.GET.get('vendor') or request.POST.get('vendor')
    outstanding_invoices = []
    if vendor_id:
        outstanding_invoices = list(
            SupplierInvoice.objects.filter(supplier_id=vendor_id).exclude(status__in=['paid', 'cancelled']).order_by('invoice_date')
        )
    if request.method == 'POST':
        form = VendorPaymentForm(request.POST)
        if form.is_valid():
            allocations = {}
            for invoice in outstanding_invoices:
                raw = request.POST.get(f'allocate_{invoice.pk}')
                if raw:
                    try:
                        value = Decimal(raw)
                    except Exception:
                        value = Decimal('0')
                    if value > 0:
                        allocations[invoice.pk] = value
            try:
                payment = create_vendor_payment(
                    vendor=form.cleaned_data['vendor'], bank_account=form.cleaned_data.get('bank_account'),
                    payment_method=form.cleaned_data['payment_method'], amount=form.cleaned_data['amount'],
                    allocations=allocations, user=request.user, reference_no=form.cleaned_data.get('reference_no', ''),
                    transaction_id=form.cleaned_data.get('transaction_id', ''), remarks=form.cleaned_data.get('remarks', ''),
                )
                messages.success(request, f'Vendor Payment {payment.payment_no} created.')
                return redirect('vendor_payment_detail', pk=payment.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = VendorPaymentForm(initial={'vendor': vendor_id} if vendor_id else None)
    return render(request, 'erp/vendor_payment_form.html', {
        'form': form, 'outstanding_invoices': outstanding_invoices,
        'vendors': Supplier.objects.filter(is_active=True).order_by('name'),
        'selected_vendor_id': int(vendor_id) if vendor_id else None,
    })


@login_required(login_url='login')
def vendor_payment_post(request, pk):
    payment = get_object_or_404(VendorPayment, pk=pk)
    if request.method == 'POST':
        try:
            post_vendor_payment(payment, request.user)
            messages.success(request, f'Vendor Payment {payment.payment_no} posted.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('vendor_payment_detail', pk=pk)


def _metal_rate_context(form):
    return {
        'latest_rates': JewelleryMetalRate.objects.filter(effective_to__isnull=True, is_active=True)
            .select_related('store').order_by('metal_type', 'purity', 'store__name'),
        'history': JewelleryMetalRate.objects.select_related('store').order_by('-effective_from')[:30],
        'form': form,
    }


@login_required(login_url='login')
def metal_rate_list(request):
    return render(request, 'erp/metal_rate_list.html', _metal_rate_context(MetalRateForm()))


@login_required(login_url='login')
def metal_rate_create(request):
    if request.method == 'POST':
        form = MetalRateForm(request.POST)
        if form.is_valid():
            store = form.cleaned_data.get('store')
            stores = [store] if store else list(Store.objects.filter(is_active=True))
            if not stores:
                messages.error(request, 'No active store found to apply this rate to.')
            else:
                apply_metal_rate(
                    metal_type=form.cleaned_data['metal_type'], purity=form.cleaned_data['purity'],
                    rate_per_gram=form.cleaned_data['rate_per_gram'], stores=stores,
                    rate_type=form.cleaned_data['rate_type'], source='manual', user=request.user,
                )
                messages.success(
                    request,
                    f"{form.cleaned_data['metal_type'].title()} {form.cleaned_data['purity']} rate updated for {len(stores)} store(s)."
                )
                return redirect('metal_rate_list')
    else:
        form = MetalRateForm()
    return render(request, 'erp/metal_rate_list.html', _metal_rate_context(form))


@login_required(login_url='login')
def metal_rate_fetch_live(request):
    if request.method == 'POST':
        stores = list(Store.objects.filter(is_active=True))
        if not stores:
            messages.error(request, 'No active store found to apply live rates to.')
        else:
            try:
                data, created = apply_live_metal_rates(stores, user=request.user)
                messages.success(
                    request,
                    f"Live rates fetched (USD/INR {data['usd_inr_rate']:.2f}): "
                    f"24K gold Rs.{data['gold']['24K']}/g, 999 silver Rs.{data['silver']['999']}/g "
                    f"- applied to {len(stores)} store(s)."
                )
            except ValueError as exc:
                messages.error(request, f'Could not fetch live rates: {exc}')
    return redirect('metal_rate_list')


@login_required(login_url='login')
def metal_price_simulator(request):
    """Quote a jewellery price using the exact same pricing engine as POS billing, without creating any transaction."""
    products = Product.objects.filter(is_active=True).order_by('name')
    stores = Store.objects.filter(is_active=True).order_by('name')
    result = None

    if request.GET.get('simulate'):
        try:
            product = Product.objects.filter(pk=request.GET.get('product')).first() if request.GET.get('product') else None
            jewellery_rule = getattr(product, 'jewellery_pricing_rule', None) if product else None
            store = Store.objects.filter(pk=request.GET.get('store')).first() if request.GET.get('store') else None

            metal_type = request.GET.get('metal_type') or (product.metal_type if product else 'gold')
            purity = (request.GET.get('purity') or (product.purity if product else '') or '').strip()
            gross_weight = Decimal(request.GET.get('gross_weight') or (str(product.weight_grams) if product else '0') or '0')
            stone_weight = Decimal(request.GET.get('stone_weight') or '0')
            other_weight = Decimal(request.GET.get('other_weight') or '0')
            discount = Decimal(request.GET.get('discount') or '0')

            if not purity:
                raise ValueError('Enter a purity (e.g. 22K, 999) or select a saved jewellery product.')

            if jewellery_rule:
                pricing_rule = jewellery_rule
            else:
                pricing_rule = ManualPricingRule(
                    making_method=request.GET.get('making_method', 'per_gram'),
                    making_rate=Decimal(request.GET.get('making_rate') or '0'),
                    wastage_method=request.GET.get('wastage_method', 'weight'),
                    wastage_percent=Decimal(request.GET.get('wastage_percent') or '0'),
                    tax_rate_code=request.GET.get('tax_rate_code') or 'GST-3',
                )

            unit = ManualUnit(gross_weight=gross_weight, stone_weight=stone_weight, other_weight=other_weight)
            rate_resolution = resolve_current_metal_rate(metal_type=metal_type, purity=purity, store=store)
            if rate_resolution['resolved']:
                breakdown = calculate_jewellery_price(
                    unit=unit, metal_rate=ResolvedRate(rate_resolution['rate_per_gram']), pricing_rule=pricing_rule,
                    tax_rate_code=pricing_rule.tax_rate_code, discount=discount,
                )
                result = {'breakdown': breakdown, 'rate_resolution': rate_resolution, 'metal_type': metal_type, 'purity': purity}
                if rate_resolution['is_purity_converted']:
                    messages.info(request, f"No {purity} rate configured; derived from the {rate_resolution['purity_used']} rate.")
                if rate_resolution['is_stale']:
                    messages.warning(request, f"This rate is {rate_resolution['age_minutes']:.0f} minutes old.")
            else:
                messages.warning(request, f'No current {metal_type} {purity} rate is configured.')
        except GSTRate.DoesNotExist:
            messages.error(request, f'No active GST rate is configured for tax code "{request.GET.get("tax_rate_code") or "GST-3"}".')
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, f'Could not simulate this price: {exc}')

    return render(request, 'erp/metal_price_simulator.html', {
        'products': products, 'stores': stores, 'result': result, 'query': request.GET,
    })


@login_required(login_url='login')
def posted_voucher_detail(request, pk):
    posted = get_object_or_404(FinancePostedVoucher.objects.prefetch_related('lines'), pk=pk)
    return render(request, 'erp/posted_voucher_detail.html', {'posted': posted})


@login_required(login_url='login')
def trial_balance_report(request):
    """Every posted JournalEntry keeps GLAccount.current_balance in a debit-positive convention; summing it always balances."""
    rows = []
    total_debit = Decimal('0')
    total_credit = Decimal('0')
    for account in GLAccount.objects.filter(status='active').order_by('account_code'):
        balance = account.current_balance or Decimal('0')
        debit = balance if balance > 0 else Decimal('0')
        credit = -balance if balance < 0 else Decimal('0')
        total_debit += debit
        total_credit += credit
        if balance != 0:
            rows.append({'account': account, 'debit': debit, 'credit': credit, 'balance': balance})
    return render(request, 'erp/trial_balance.html', {
        'rows': rows, 'total_debit': total_debit, 'total_credit': total_credit,
        'is_balanced': total_debit == total_credit,
    })


@login_required(login_url='login')
def gst_control_center(request):
    return render(request, 'erp/gst_control_center.html', {
        'registrations': GSTRegistration.objects.filter(status='active').count(),
        'rates': GSTRate.objects.filter(status='active').count(),
        'rules': GSTTaxRule.objects.filter(status='active').count(),
        'ledger_entries': GSTLedgerEntry.objects.count(),
        'taxable_value': GSTLedgerEntry.objects.aggregate(total=Sum('taxable_value'))['total'] or Decimal('0'),
        'output_tax': GSTLedgerEntry.objects.aggregate(total=Sum('cgst') + Sum('sgst') + Sum('utgst') + Sum('igst') + Sum('cess'))['total'] or Decimal('0'),
        'recent_entries': GSTLedgerEntry.objects.select_related('registration').order_by('-posting_date', '-id')[:12],
    })


@login_required(login_url='login')
def wms_hub(request):
    context = {
        'warehouses': Warehouse.objects.filter(is_active=True).count(),
        'zones': WarehouseZone.objects.filter(status='active').count(),
        'bins': BinLocation.objects.filter(is_active=True).count(),
        'movements': WarehouseMovement.objects.count(),
        'modules': [
            ('transfer-orders', 'Transfer Orders', 'fa-right-left', 'Plan and release stock transfers.'),
            ('warehouse-receipts', 'Warehouse Receipts', 'fa-arrow-down-to-bracket', 'Receive inbound stock with traceability.'),
            ('putaways', 'Put-away', 'fa-boxes-stacked', 'Move received goods into directed bins.'),
            ('picks', 'Picking', 'fa-hand-pointer', 'Allocate and confirm outbound picks.'),
            ('shipments', 'Warehouse Shipments', 'fa-truck-fast', 'Stage and ship released work.'),
            ('movements', 'Internal Movement', 'fa-arrows-left-right', 'Control bin-to-bin movements.'),
            ('replenishment', 'Replenishment', 'fa-rotate', 'Keep pick faces supplied.'),
            ('adjustments', 'Inventory Adjustments', 'fa-sliders', 'Post approved stock corrections.'),
            ('item-journals', 'Item Journals', 'fa-book', 'Validate and post item journals.'),
            ('stock-takes', 'Stock Takes', 'fa-clipboard-check', 'Count, review, approve, and post variance.'),
            ('cycle-counts', 'Cycle Counts', 'fa-calendar-check', 'Run scheduled count programs.'),
            ('quality', 'QC and Quarantine', 'fa-shield-halved', 'Inspect and release controlled stock.'),
            ('returns', 'Returns', 'fa-rotate-left', 'Process customer and vendor returns.'),
            ('cross-dock', 'Cross Dock', 'fa-arrows-to-circle', 'Move inbound demand directly to dispatch.'),
        ],
    }
    return render(request, 'erp/wms_hub.html', context)


@login_required(login_url='login')
def wms_module(request, module):
    labels = {
        'transfer-orders': 'Transfer Orders', 'warehouse-receipts': 'Warehouse Receipts',
        'putaways': 'Put-away', 'picks': 'Picking', 'shipments': 'Warehouse Shipments',
        'movements': 'Internal Movement', 'replenishment': 'Replenishment',
        'adjustments': 'Inventory Adjustments', 'item-journals': 'Item Journals',
        'stock-takes': 'Stock Takes', 'cycle-counts': 'Cycle Counts', 'quality': 'QC and Quarantine',
        'returns': 'Returns', 'cross-dock': 'Cross Dock',
    }
    return render(request, 'erp/wms_module.html', {
        'module': labels.get(module, module.replace('-', ' ').title()),
        'module_slug': module,
        'warehouses': Warehouse.objects.filter(is_active=True),
        'recent_movements': WarehouseMovement.objects.select_related('item', 'from_bin', 'to_bin').order_by('-created_at')[:20],
    })


@login_required(login_url='login')
def wms_reports(request):
    return render(request, 'erp/wms_reports.html', {
        'warehouse_rows': Warehouse.objects.annotate(bin_count=Count('bins')).order_by('name'),
        'movement_rows': WarehouseMovement.objects.select_related('item', 'from_bin', 'to_bin', 'created_by').order_by('-created_at')[:30],
        'stock_total': Product.objects.aggregate(total=Sum('stock_quantity'))['total'] or Decimal('0'),
    })


@login_required(login_url='login')
def master_catalog(request):
    return render(request, 'erp/master_catalog.html', {
        'master_groups': [
            ('Finance', ['companies', 'branches', 'departments', 'business-units', 'projects', 'cost-centers', 'gl-accounts', 'bank-accounts', 'number-series', 'finance-journal-templates', 'finance-journal-batches', 'finance-voucher-types', 'finance-vouchers', 'posted-vouchers', 'payment-terms', 'payment-methods', 'customer-posting-groups', 'vendor-posting-groups']),
            ('Sales and Receivables', ['customers', 'customer-finance-profiles', 'document-relationships', 'document-status-history']),
            ('Purchase and Payables', ['suppliers', 'vendor-finance-profiles']),
            ('Warehouse', ['warehouses', 'locations', 'zones', 'bins']),
            ('Commercial', ['customers', 'suppliers', 'products', 'gst-slabs']),
            ('GST and Tax Engine', ['gst-registrations', 'gst-states', 'gst-groups', 'gst-components', 'gst-rates', 'hsn-codes', 'sac-codes', 'supply-types', 'gst-tax-rules', 'gst-ledger']),
            ('People and Channels', ['roles', 'employees', 'stores', 'brands', 'channels']),
            ('Merchandise and Item Master', ['divisions', 'special-groups', 'categories', 'subcategories', 'units-of-measure', 'retail-items', 'item-uoms', 'item-variants', 'skus', 'retail-barcodes', 'item-locations']),
        ],
        'specs': MASTER_SPECS,
    })


@login_required(login_url='login')
def master_crud(request, slug, action='list', pk=None):
    if slug not in MASTER_SPECS:
        return HttpResponse('Master not found', status=404)
    model, fields = MASTER_SPECS[slug]
    instance = get_object_or_404(model, pk=pk) if pk else None
    if action == 'delete':
        if request.method == 'POST':
            instance.delete()
            messages.success(request, f'{model._meta.verbose_name.title()} deleted.')
            return redirect('master_crud', slug=slug)
        return render(request, 'erp/master_delete.html', {'object': instance, 'slug': slug, 'model_name': model._meta.verbose_name.title()})
    form = modelform_factory(
        model,
        fields='__all__',
        exclude=('created_at', 'updated_at'),
    )(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{model._meta.verbose_name.title()} saved successfully.')
        if request.POST.get('save_and_new'):
            return redirect('master_create', slug=slug)
        return redirect('master_crud', slug=slug)
    objects = model.objects.all().order_by('-pk')
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    searchable_fields = [field for field in fields if field in {
        'code', 'name', 'company_code', 'company_name', 'account_code', 'account_name',
        'voucher_no', 'posting_no', 'document_no', 'gstin', 'sku', 'description',
        'state_code', 'state_name', 'invoice_no', 'employee_code', 'full_name',
    }]
    if query and searchable_fields:
        search_filter = Q()
        for field in searchable_fields:
            search_filter |= Q(**{f'{field}__icontains': query})
        objects = objects.filter(search_filter)
    if status and any(field == 'status' for field in fields):
        objects = objects.filter(status=status)
    objects = objects[:100]
    rows = [
        {
            'object': obj,
            'values': [str(getattr(obj, field, '') or '-') for field in fields],
        }
        for obj in objects
    ]
    return render(request, 'erp/master_crud.html', {
        'object_list': objects, 'rows': rows, 'form': form, 'slug': slug,
        'model_name': model._meta.verbose_name.title(), 'fields': fields,
        'editing': instance is not None, 'query': query, 'status_filter': status,
    })


@login_required(login_url='login')
def download_template(request, slug):
    if slug not in MASTER_SPECS:
        return HttpResponse('Template not found', status=404)
    model, fields = MASTER_SPECS[slug]
    workbook = build_template(slug, model._meta.verbose_name.title(), fields)
    response = HttpResponse(
        workbook,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{slug.replace("-", "_").upper()}_IMPORT_TEMPLATE.xlsx"'
    return response


@login_required(login_url='login')
def upload_import_file(request, slug):
    if slug not in MASTER_SPECS:
        return HttpResponse('Import process not found', status=404)
    upload = request.FILES.get('file')
    error = ''
    if request.method == 'POST':
        if not upload:
            error = 'Choose an Excel file before uploading.'
        elif not upload.name.lower().endswith(('.xlsx', '.xlsm')):
            error = 'Only .xlsx or .xlsm files are supported.'
        elif upload.size > 10 * 1024 * 1024:
            error = 'The upload must be smaller than 10 MB.'
    return render(request, 'erp/import_upload.html', {
        'slug': slug,
        'process_name': MASTER_SPECS[slug][0]._meta.verbose_name.title(),
        'upload': upload,
        'error': error,
        'uploaded': bool(upload and not error),
    })


@login_required(login_url='login')
def import_center(request):
    return render(request, 'erp/import_center.html', {
        'imports': [
            ('Customer Master', 'customers'), ('Vendor Master', 'suppliers'),
            ('Item Master', 'products'), ('Warehouse Master', 'warehouses'),
            ('Journal Voucher', 'finance-vouchers'), ('GST Rates', 'gst-rates'),
            ('HSN Codes', 'hsn-codes'), ('Opening Stock', 'bins'),
        ],
    })


@login_required(login_url='login')
def master_management(request):
    product_form = ProductForm(request.POST or None, prefix='product')
    customer_form = CustomerForm(request.POST or None, prefix='customer')
    supplier_form = SupplierForm(request.POST or None, prefix='supplier')
    role_form = RoleForm(request.POST or None, prefix='role')
    employee_form = EmployeeForm(request.POST or None, prefix='employee')
    warehouse_form = WarehouseForm(request.POST or None, prefix='warehouse')
    gst_form = GSTSlabForm(request.POST or None, prefix='gst')

    if request.method == 'POST':
        if 'product_submit' in request.POST and product_form.is_valid():
            product_form.save()
            messages.success(request, 'Product master created successfully.')
            return redirect('master_management')
        if 'customer_submit' in request.POST and customer_form.is_valid():
            customer_form.save()
            messages.success(request, 'Customer master created successfully.')
            return redirect('master_management')
        if 'supplier_submit' in request.POST and supplier_form.is_valid():
            supplier_form.save()
            messages.success(request, 'Supplier master created successfully.')
            return redirect('master_management')
        if 'role_submit' in request.POST and role_form.is_valid():
            role_form.save()
            messages.success(request, 'Role master created successfully.')
            return redirect('master_management')
        if 'employee_submit' in request.POST and employee_form.is_valid():
            employee_form.save()
            messages.success(request, 'Employee master created successfully.')
            return redirect('master_management')
        if 'warehouse_submit' in request.POST and warehouse_form.is_valid():
            warehouse_form.save()
            messages.success(request, 'Warehouse master created successfully.')
            return redirect('master_management')
        if 'gst_submit' in request.POST and gst_form.is_valid():
            gst_form.save()
            messages.success(request, 'GST slab created successfully.')
            return redirect('master_management')

    context = {
        'item_count': Product.objects.count(),
        'price_count': ItemPrice.objects.count(),
        'barcode_count': ItemBarcode.objects.count(),
        'batch_count': ItemBatch.objects.count(),
        'serial_count': ItemSerial.objects.count(),
        'customer_count': Customer.objects.count(),
        'user_count': UserProfile.objects.count(),
        'employee_count': Employee.objects.count(),
        'role_count': Role.objects.count(),
        'categories': ItemCategory.objects.all()[:10],
        'items': Product.objects.select_related('item_category').order_by('-created_at')[:10],
        'prices': ItemPrice.objects.select_related('item').order_by('-valid_from')[:10],
        'barcodes': ItemBarcode.objects.select_related('item').order_by('-item_id')[:10],
        'batches': ItemBatch.objects.select_related('item').order_by('-created_at')[:10],
        'serials': ItemSerial.objects.select_related('item').order_by('-created_at')[:10],
        'product_form': product_form,
        'customer_form': customer_form,
        'supplier_form': supplier_form,
        'role_form': role_form,
        'employee_form': employee_form,
        'warehouse_form': warehouse_form,
        'gst_form': gst_form,
    }
    return render(request, 'erp/master_management.html', context)


@login_required(login_url='login')
def warehouse_management(request):
    warehouses = Warehouse.objects.select_related('manager').all().order_by('name')
    bins = BinLocation.objects.select_related('warehouse').all().order_by('warehouse__name', 'code')
    movements = WarehouseMovement.objects.select_related('item', 'batch', 'serial', 'from_bin', 'to_bin', 'created_by').order_by('-created_at')[:15]
    context = {
        'warehouses': warehouses,
        'bins': bins,
        'movements': movements,
    }
    return render(request, 'erp/warehouse_management.html', context)


@login_required(login_url='login')
def gst_management(request):
    gst_rates = GSTSlab.objects.filter(is_active=True).order_by('state_code', 'gst_rate')
    total_tax = sum([float(rate.gst_rate) for rate in gst_rates], 0)
    context = {
        'gst_rates': gst_rates,
        'total_tax': total_tax,
    }
    return render(request, 'erp/gst_management.html', context)


@login_required(login_url='login')
def pos(request):
    pos_session_id = request.session.get('pos_session_id')
    pos_session = POSSession.objects.select_related('staff', 'terminal__store', 'shift').filter(
        id=pos_session_id, status='active'
    ).first()
    if not pos_session:
        return render(request, 'erp/pos_login.html', {
            'terminal': POSTerminal.objects.filter(is_active=True, status='active').select_related('store').first(),
        })

    pos_session.last_activity = timezone.now()
    pos_session.save(update_fields=['last_activity'])
    products = Product.objects.filter(is_active=True).order_by('name')
    customer_form = CustomerForm(prefix='customer')
    sales_staff = POSStaff.objects.filter(
        is_active=True, is_blocked=False, store=pos_session.terminal.store,
        assignments__terminal=pos_session.terminal, assignments__role='sales_staff', assignments__active=True,
    ).distinct().order_by('name')
    jewellery_units = JewelleryItemUnit.objects.filter(current_status='available').select_related('product')[:200]
    post_data = request.POST.copy() if request.method == 'POST' else None

    if request.method == 'POST':
        customer_name = post_data.get('customer_name')
        customer_phone = post_data.get('customer_phone')
        customer_gstin = post_data.get('customer_gstin')

        if not post_data.get('customer'):
            customer = Customer.objects.create(
                name=customer_name or 'Walk-in Customer',
                phone=customer_phone or '',
                gstin=customer_gstin or '',
                customer_type='retail',
            )
            post_data['customer'] = str(customer.pk)

        form = POSSaleForm(post_data)
        if form.is_valid():
            invoice = form.save(commit=False)
            if not invoice.customer_id:
                messages.error(request, 'Please select or create a customer before creating a sale.')
                return render(request, 'erp/pos.html', {'form': form, 'products': products, 'customer_form': customer_form})

            barcode = request.POST.get('barcode')
            item_id = request.POST.get('item_id')
            quantity = Decimal(str(request.POST.get('quantity', '1') or '1'))
            making_charge = Decimal(str(request.POST.get('making_charge', '0') or '0'))
            stone_value = Decimal(str(request.POST.get('stone_value', '0') or '0'))
            discount_amount = Decimal(str(request.POST.get('discount_amount', '0') or '0'))
            serial_no = request.POST.get('serial_no', '')
            batch_no = request.POST.get('batch_no', '')
            stone_name = request.POST.get('stone_name', '')

            product = None
            jewellery_unit = JewelleryItemUnit.objects.select_related('product').filter(
                barcode=barcode, current_status='available'
            ).first() if barcode else None
            if item_id:
                product = get_object_or_404(Product, pk=item_id)
            elif jewellery_unit:
                product = jewellery_unit.product
            elif barcode:
                product = Product.objects.filter(barcode=barcode, is_active=True).first()
            if not product:
                product = products.first()
            if not product:
                messages.error(request, 'No active products are available to sell.')
                return render(request, 'erp/pos.html', {'form': form, 'products': products, 'customer_form': customer_form})

            pricing_breakdown = None
            jewellery_rule = getattr(product, 'jewellery_pricing_rule', None)
            pricing_unit = jewellery_unit or (ProductWeightAsUnit(product) if jewellery_rule else None)
            rate_resolution = None
            if pricing_unit and jewellery_rule:
                requested_metal = jewellery_unit.metal_type if jewellery_unit else product.metal_type
                requested_purity = jewellery_unit.purity if jewellery_unit else product.purity
                rate_resolution = resolve_current_metal_rate(
                    metal_type=requested_metal, purity=requested_purity, store=pos_session.terminal.store,
                )
            if rate_resolution and rate_resolution['resolved']:
                pricing_breakdown = calculate_jewellery_price(
                    unit=pricing_unit, metal_rate=ResolvedRate(rate_resolution['rate_per_gram']), pricing_rule=jewellery_rule,
                    tax_rate_code=jewellery_rule.tax_rate_code, discount=discount_amount,
                )
                pricing_breakdown['rate_resolution'] = {
                    'requested_purity': requested_purity, 'purity_used': rate_resolution['purity_used'],
                    'is_purity_converted': rate_resolution['is_purity_converted'],
                    'is_store_fallback': rate_resolution['is_store_fallback'],
                    'is_stale': rate_resolution['is_stale'], 'age_minutes': rate_resolution['age_minutes'],
                }
                unit_price = pricing_breakdown['metal_value']
                line_total = pricing_breakdown['final_amount']
                if rate_resolution['is_purity_converted']:
                    messages.info(request, f"No {requested_purity} rate configured; derived from the {rate_resolution['purity_used']} rate.")
                elif rate_resolution['is_store_fallback']:
                    messages.info(request, f"No store-specific {requested_purity} rate configured; used the company-wide rate.")
                if rate_resolution['is_stale']:
                    messages.warning(request, f"This {requested_metal} {requested_purity} rate is {rate_resolution['age_minutes']:.0f} minutes old — consider refreshing metal rates.")
            else:
                unit_price = product.sale_price or product.mrp or Decimal('0')
                line_total = (unit_price * quantity) + making_charge + stone_value - discount_amount
                if jewellery_rule:
                    messages.warning(request, f"No current metal rate available for {product.name} — sold at the flat listed price instead of a live metal rate.")
            invoice.invoice_no = invoice.invoice_no or get_next_number('sales_invoice')
            invoice.cashier_staff = pos_session.staff
            invoice.cashier_name_snapshot = pos_session.staff.name
            invoice.cashier_role_snapshot = pos_session.staff.role
            invoice.pos_terminal = pos_session.terminal
            invoice.pos_session = pos_session
            invoice.store_code_snapshot = pos_session.terminal.store.code
            invoice.terminal_code_snapshot = pos_session.terminal.code
            terminal_print_setup = POSTerminalPrintSetup.objects.filter(terminal=pos_session.terminal, active=True).first()
            store_print_setup = StoreInvoicePrintSetup.objects.filter(store=pos_session.terminal.store, active=True).first()
            selected_print_type = terminal_print_setup.print_type if terminal_print_setup and terminal_print_setup.print_type else store_print_setup.invoice_print_type if store_print_setup else 'A4_INVOICE'
            invoice.print_type_snapshot = selected_print_type
            invoice.print_layout_snapshot = ((terminal_print_setup.layout if terminal_print_setup else None) or (store_print_setup.layout if store_print_setup else None)).code if ((terminal_print_setup and terminal_print_setup.layout) or (store_print_setup and store_print_setup.layout)) else ''
            invoice.print_layout_version_snapshot = ((terminal_print_setup.layout if terminal_print_setup else None) or (store_print_setup.layout if store_print_setup else None)).version if ((terminal_print_setup and terminal_print_setup.layout) or (store_print_setup and store_print_setup.layout)) else 1
            sales_staff_id = request.POST.get('sales_staff') or None
            selected_sales_staff = sales_staff.filter(pk=sales_staff_id).first() if sales_staff_id else None
            if selected_sales_staff:
                invoice.sales_staff = selected_sales_staff
                invoice.sales_staff_name_snapshot = selected_sales_staff.name
            invoice.customer_phone = invoice.customer.phone or ''
            invoice.customer_gstin = invoice.customer.gstin or ''
            invoice.place_of_supply = request.POST.get('place_of_supply') or '27'
            invoice.terms_conditions = request.POST.get('terms_conditions') or invoice.terms_conditions
            invoice.subtotal = line_total
            invoice.taxable_amount = pricing_breakdown['taxable_value'] if pricing_breakdown else max(line_total - invoice.discount_amount, Decimal('0'))
            if pricing_breakdown:
                tax = pricing_breakdown['tax']
                invoice.gst_amount = pricing_breakdown['tax_total']
                invoice.total_amount = pricing_breakdown['final_amount']
            else:
                taxable, gst, total = calculate_gst(invoice.taxable_amount, invoice.gst_rate)
                invoice.gst_amount = gst
                invoice.total_amount = total
            invoice.status = 'pending_approval'
            invoice.save()

            SalesInvoiceItem.objects.create(
                invoice=invoice,
                product=product,
                quantity=quantity,
                unit_price=unit_price,
                tax_rate=invoice.gst_rate,
                line_total=line_total,
                barcode=barcode or product.barcode,
                serial_no=serial_no,
                batch_no=batch_no,
                stone_name=stone_name,
                stone_value=stone_value,
                making_charge=making_charge,
                discount_amount=discount_amount,
                taxable_amount=invoice.taxable_amount,
                cgst_amount=tax['cgst'] if pricing_breakdown else gst / Decimal('2'),
                sgst_amount=tax['sgst'] if pricing_breakdown else gst / Decimal('2'),
                igst_amount=tax['igst'] if pricing_breakdown else Decimal('0'),
                jewellery_unit=jewellery_unit,
                gross_weight=pricing_breakdown['gross_weight'] if pricing_breakdown else 0,
                stone_weight=pricing_breakdown['stone_weight'] if pricing_breakdown else 0,
                net_metal_weight=pricing_breakdown['net_metal_weight'] if pricing_breakdown else 0,
                metal_rate=pricing_breakdown['metal_rate'] if pricing_breakdown else 0,
                metal_value=pricing_breakdown['metal_value'] if pricing_breakdown else 0,
                wastage_value=pricing_breakdown['wastage_value'] if pricing_breakdown else 0,
                hallmark_charge=pricing_breakdown['hallmark_charge'] if pricing_breakdown else 0,
                certification_charge=pricing_breakdown['certification_charge'] if pricing_breakdown else 0,
                other_charges=pricing_breakdown['other_charges'] if pricing_breakdown else 0,
                pricing_snapshot={str(key): str(value) for key, value in pricing_breakdown.items()} if pricing_breakdown else {},
            )
            SalesApproval.objects.create(invoice=invoice, status='pending')
            messages.success(request, f'Bill {invoice.invoice_no} posted successfully.')
            return redirect(f"{reverse('pos')}?completed={invoice.pk}")

    form = POSSaleForm(post_data)
    return render(request, 'erp/pos.html', {
        'form': form, 'products': products, 'customer_form': customer_form,
        'pos_session': pos_session, 'sales_staff': sales_staff,
        'jewellery_units': jewellery_units,
    })


@login_required(login_url='login')
def sales_approval(request):
    approvals = SalesApproval.objects.select_related('invoice__customer').all().order_by('-created_at')
    form = SalesApprovalForm()
    return render(request, 'erp/sales_approval.html', {'approvals': approvals, 'form': form})


@login_required(login_url='login')
def approve_sale(request, pk):
    approval = get_object_or_404(SalesApproval, pk=pk)
    if request.method == 'POST':
        form = SalesApprovalForm(request.POST, instance=approval)
        if form.is_valid():
            approval = form.save(commit=False)
            approval.approved_by = Staff.objects.first()
            approval.approved_at = timezone.now()
            approval.save()
            invoice = approval.invoice
            if approval.status == 'approved':
                invoice.status = 'approved'
            else:
                invoice.status = 'rejected'
            invoice.save()
            messages.success(request, f'Sale {invoice.invoice_no} updated successfully.')
            return redirect('sales_approval')
    return redirect('sales_approval')


@login_required(login_url='login')
def exchange(request):
    form = ExchangeTransactionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        exchange = form.save(commit=False)
        exchange.is_approved = False
        exchange.save()
        messages.success(request, 'Gold/Silver exchange request submitted for approval.')
        return redirect('dashboard')
    return render(request, 'erp/exchange.html', {'form': form})


@login_required(login_url='login')
def invoice_detail(request, pk):
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    tax = estimate_income_tax(invoice.total_amount)
    setting = InvoiceSetting.objects.filter(is_default=True).first() or InvoiceSetting.objects.first()
    context = {
        'invoice': invoice,
        'items': invoice.items.all(),
        'income_tax': tax,
        'invoice_setting': setting,
        'related': get_related_documents(invoice, 'sales_invoice'),
        'history': DocumentStatusHistory.objects.filter(document_type='sales_invoice', document_id=invoice.pk).order_by('changed_at'),
    }
    return render(request, 'erp/invoice_detail.html', context)


@login_required(login_url='login')
def invoice_pdf(request, pk):
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    setting = InvoiceSetting.objects.filter(is_default=True).first() or InvoiceSetting.objects.first()
    print_type = invoice.print_type_snapshot or 'A4_INVOICE'
    if request.GET.get('format') in {'A4_INVOICE', 'A5_INVOICE', 'THERMAL_40COL_RECEIPT'}:
        print_type = request.GET['format']
    elif invoice.pos_terminal_id:
        terminal_setup = POSTerminalPrintSetup.objects.filter(terminal=invoice.pos_terminal, active=True).first()
        store_setup = StoreInvoicePrintSetup.objects.filter(store=invoice.pos_terminal.store, active=True).first()
        print_type = (terminal_setup.print_type if terminal_setup and terminal_setup.print_type else store_setup.invoice_print_type if store_setup else print_type)
    dataset = build_invoice_dataset(invoice, setting)
    payload = render_thermal_receipt(dataset) if print_type == 'THERMAL_40COL_RECEIPT' else render_invoice_pdf(dataset, print_type)
    content_type = 'text/plain; charset=utf-8' if print_type == 'THERMAL_40COL_RECEIPT' else 'application/pdf'
    InvoicePrintLog.objects.create(
        invoice=invoice, print_type=print_type, layout_code=invoice.print_layout_snapshot,
        layout_version=invoice.print_layout_version_snapshot, printer_name='', printed_by=request.user,
        reason=request.GET.get('reason', 'reprint'), original=False,
    )
    response = HttpResponse(payload, content_type=content_type)
    extension = 'txt' if print_type == 'THERMAL_40COL_RECEIPT' else 'pdf'
    response['Content-Disposition'] = f'inline; filename="{invoice.invoice_no}.{extension}"'
    return response


@login_required(login_url='login')
def sales_return(request):
    form = SalesReturnForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Sales return request recorded successfully.')
        return redirect('dashboard')
    return render(request, 'erp/sales_return.html', {'form': form})


@login_required(login_url='login')
def invoice_settings(request):
    setting = InvoiceSetting.objects.filter(is_default=True).first() or InvoiceSetting.objects.first()
    form = InvoiceSettingForm(request.POST or None, instance=setting)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Invoice settings updated successfully.')
        return redirect('invoice_settings')
    return render(request, 'erp/invoice_settings.html', {'form': form})


@login_required(login_url='login')
def customer_list(request):
    customers = Customer.objects.all().order_by('name')
    return render(request, 'erp/customer_list.html', {'customers': customers})


@login_required(login_url='login')
def supplier_list(request):
    suppliers = Supplier.objects.all().order_by('name')
    return render(request, 'erp/supplier_list.html', {'suppliers': suppliers})


@login_required(login_url='login')
def stock_ledger(request):
    ledger = StockLedger.objects.select_related('product').order_by('-created_at')
    return render(request, 'erp/stock_ledger.html', {'ledger': ledger})


# ===== PRODUCT CRUD =====
@login_required(login_url='login')
def product_list(request):
    products = Product.objects.select_related('item_category').order_by('name')
    return render(request, 'erp/product_list.html', {'products': products})


@login_required(login_url='login')
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    prices = product.prices.all()
    barcodes = product.barcodes.all()
    batches = product.batches.all()
    return render(request, 'erp/product_detail.html', {
        'product': product,
        'prices': prices,
        'barcodes': barcodes,
        'batches': batches,
    })


@login_required(login_url='login')
def product_create(request):
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Product created successfully.')
            return redirect('product_list')
    else:
        form = ProductForm()
    return render(request, 'erp/product_form.html', {'form': form, 'title': 'Create Product'})


@login_required(login_url='login')
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        form = ProductForm(request.POST, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, 'Product updated successfully.')
            return redirect('product_detail', pk=product.pk)
    else:
        form = ProductForm(instance=product)
    return render(request, 'erp/product_form.html', {'form': form, 'title': f'Edit {product.name}'})


@login_required(login_url='login')
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        product.delete()
        messages.success(request, 'Product deleted successfully.')
        return redirect('product_list')
    return render(request, 'erp/product_confirm_delete.html', {'product': product})


# ===== CUSTOMER CRUD =====
@login_required(login_url='login')
def customer_detail(request, pk):
    customer = get_object_or_404(Customer.objects.select_related(
        'customer_posting_group', 'customer_price_group', 'customer_discount_group',
    ), pk=pk)
    invoices = SalesInvoice.objects.filter(customer=customer).order_by('-sales_date')[:10]
    return render(request, 'erp/customer_detail.html', {
        'customer': customer, 'invoices': invoices,
        'billing_address': getattr(customer, 'billing_address', None),
        'shipping_addresses': customer.shipping_addresses.filter(is_active=True),
        'identity_documents': customer.identity_documents.filter(is_active=True),
        'kyc': getattr(customer, 'kyc', None),
        'preferences': getattr(customer, 'jewellery_preferences', None),
        'communication': getattr(customer, 'communication_preferences', None),
        'marketing': getattr(customer, 'marketing_profile', None),
    })


@login_required(login_url='login')
def customer_create(request):
    if request.method == 'POST':
        form = CustomerForm(request.POST)
        if form.is_valid():
            mobile = form.cleaned_data.get('phone')
            email = form.cleaned_data.get('email')
            if mobile and Customer.objects.filter(phone=mobile, is_active=True).exists():
                form.add_error('phone', 'An active customer already uses this mobile number.')
            elif email and Customer.objects.filter(email__iexact=email, is_active=True).exists():
                form.add_error('email', 'An active customer already uses this email address.')
            else:
                customer = form.save(commit=False)
                customer.customer_no = get_next_customer_number()
                customer.save()
                messages.success(request, f'Customer {customer.customer_no} created successfully.')
                return redirect('customer_detail', pk=customer.pk)
    else:
        form = CustomerForm()
    return render(request, 'erp/customer_form.html', {'form': form, 'title': 'Create Customer'})


@login_required(login_url='login')
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == 'POST':
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, 'Customer updated successfully.')
            return redirect('customer_detail', pk=customer.pk)
    else:
        form = CustomerForm(instance=customer)
    return render(request, 'erp/customer_form.html', {'form': form, 'title': f'Edit {customer.name}'})


@login_required(login_url='login')
def customer_delete(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == 'POST':
        customer.delete()
        messages.success(request, 'Customer deleted successfully.')
        return redirect('customer_list')
    return render(request, 'erp/customer_confirm_delete.html', {'customer': customer})


# ===== WAREHOUSE CRUD =====
@login_required(login_url='login')
def warehouse_detail(request, pk):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    bins = BinLocation.objects.filter(warehouse=warehouse).order_by('code')
    movements = WarehouseMovement.objects.select_related('item', 'batch', 'serial').filter(
        from_bin__warehouse=warehouse
    ).order_by('-created_at')[:10]
    return render(request, 'erp/warehouse_detail.html', {
        'warehouse': warehouse,
        'bins': bins,
        'movements': movements,
    })


@login_required(login_url='login')
def warehouse_create(request):
    if request.method == 'POST':
        form = WarehouseForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Warehouse created successfully.')
            return redirect('warehouse_management')
    else:
        form = WarehouseForm()
    return render(request, 'erp/warehouse_form.html', {'form': form, 'title': 'Create Warehouse'})


@login_required(login_url='login')
def warehouse_edit(request, pk):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    if request.method == 'POST':
        form = WarehouseForm(request.POST, instance=warehouse)
        if form.is_valid():
            form.save()
            messages.success(request, 'Warehouse updated successfully.')
            return redirect('warehouse_detail', pk=warehouse.pk)
    else:
        form = WarehouseForm(instance=warehouse)
    return render(request, 'erp/warehouse_form.html', {'form': form, 'title': f'Edit {warehouse.name}'})


@login_required(login_url='login')
def warehouse_delete(request, pk):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    if request.method == 'POST':
        warehouse.delete()
        messages.success(request, 'Warehouse deleted successfully.')
        return redirect('warehouse_management')
    return render(request, 'erp/warehouse_confirm_delete.html', {'warehouse': warehouse})


@login_required(login_url='login')
def warehouse_pick(request, pk):
    bin_location = get_object_or_404(BinLocation, pk=pk)
    if request.method == 'POST':
        item_id = request.POST.get('item_id')
        quantity = Decimal(request.POST.get('quantity', 0))
        to_bin_id = request.POST.get('to_bin_id')
        
        if quantity <= 0:
            messages.error(request, 'Quantity must be greater than zero.')
        elif quantity > bin_location.current_stock:
            messages.error(request, 'Insufficient stock in bin.')
        else:
            product = get_object_or_404(Product, pk=item_id)
            bin_location.current_stock -= quantity
            bin_location.save()
            
            if to_bin_id:
                to_bin = BinLocation.objects.get(pk=to_bin_id)
                to_bin.current_stock += quantity
                to_bin.save()
            
            WarehouseMovement.objects.create(
                item=product,
                from_bin=bin_location,
                to_bin=BinLocation.objects.get(pk=to_bin_id) if to_bin_id else None,
                quantity=quantity,
                movement_type='pick',
                created_by=request.user,
            )
            messages.success(request, f'Picked {quantity} units successfully.')
            return redirect('warehouse_detail', pk=bin_location.warehouse.pk)
    
    return render(request, 'erp/warehouse_pick.html', {'bin': bin_location})


@login_required(login_url='login')
def warehouse_putaway(request, pk):
    bin_location = get_object_or_404(BinLocation, pk=pk)
    if request.method == 'POST':
        item_id = request.POST.get('item_id')
        quantity = Decimal(request.POST.get('quantity', 0))
        
        if quantity <= 0:
            messages.error(request, 'Quantity must be greater than zero.')
        else:
            product = get_object_or_404(Product, pk=item_id)
            bin_location.current_stock += quantity
            bin_location.save()
            
            product.stock_quantity += quantity
            product.save()
            
            WarehouseMovement.objects.create(
                item=product,
                to_bin=bin_location,
                quantity=quantity,
                movement_type='putaway',
                created_by=request.user,
            )
            messages.success(request, f'Put away {quantity} units successfully.')
            return redirect('warehouse_detail', pk=bin_location.warehouse.pk)
    
    return render(request, 'erp/warehouse_putaway.html', {'bin': bin_location})


# ===== ROLE CRUD =====
@login_required(login_url='login')
def role_list(request):
    roles = Role.objects.all().order_by('name')
    return render(request, 'erp/role_list.html', {'roles': roles})


@login_required(login_url='login')
def role_create(request):
    if request.method == 'POST':
        form = RoleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Role created successfully.')
            return redirect('role_list')
    else:
        form = RoleForm()
    return render(request, 'erp/role_form.html', {'form': form, 'title': 'Create Role'})


@login_required(login_url='login')
def role_edit(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if request.method == 'POST':
        form = RoleForm(request.POST, instance=role)
        if form.is_valid():
            form.save()
            messages.success(request, 'Role updated successfully.')
            return redirect('role_list')
    else:
        form = RoleForm(instance=role)
    return render(request, 'erp/role_form.html', {'form': form, 'title': f'Edit {role.name}'})


@login_required(login_url='login')
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if request.method == 'POST':
        role.delete()
        messages.success(request, 'Role deleted successfully.')
        return redirect('role_list')
    return render(request, 'erp/role_confirm_delete.html', {'role': role})


# ===== EMPLOYEE CRUD =====
@login_required(login_url='login')
def employee_list(request):
    employees = Employee.objects.select_related('role').all().order_by('full_name')
    return render(request, 'erp/employee_list.html', {'employees': employees})


@login_required(login_url='login')
def employee_create(request):
    if request.method == 'POST':
        form = EmployeeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Employee created successfully.')
            return redirect('employee_list')
    else:
        form = EmployeeForm()
    return render(request, 'erp/employee_form.html', {'form': form, 'title': 'Create Employee'})


@login_required(login_url='login')
def employee_edit(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        form = EmployeeForm(request.POST, instance=employee)
        if form.is_valid():
            form.save()
            messages.success(request, 'Employee updated successfully.')
            return redirect('employee_list')
    else:
        form = EmployeeForm(instance=employee)
    return render(request, 'erp/employee_form.html', {'form': form, 'title': f'Edit {employee.full_name}'})


@login_required(login_url='login')
def employee_delete(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        employee.delete()
        messages.success(request, 'Employee deleted successfully.')
        return redirect('employee_list')
    return render(request, 'erp/employee_confirm_delete.html', {'employee': employee})


# ===== GST CRUD =====
@login_required(login_url='login')
def gst_create(request):
    if request.method == 'POST':
        form = GSTSlabForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'GST slab created successfully.')
            return redirect('gst_management')
    else:
        form = GSTSlabForm()
    return render(request, 'erp/gst_form.html', {'form': form, 'title': 'Create GST Slab'})


@login_required(login_url='login')
def gst_edit(request, pk):
    gst = get_object_or_404(GSTSlab, pk=pk)
    if request.method == 'POST':
        form = GSTSlabForm(request.POST, instance=gst)
        if form.is_valid():
            form.save()
            messages.success(request, 'GST slab updated successfully.')
            return redirect('gst_management')
    else:
        form = GSTSlabForm(instance=gst)
    return render(request, 'erp/gst_form.html', {'form': form, 'title': f'Edit GST {gst.gst_rate}%'})


@login_required(login_url='login')
def gst_delete(request, pk):
    gst = get_object_or_404(GSTSlab, pk=pk)
    if request.method == 'POST':
        gst.delete()
        messages.success(request, 'GST slab deleted successfully.')
        return redirect('gst_management')
    return render(request, 'erp/gst_confirm_delete.html', {'gst': gst})