from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone as dj_timezone


class Company(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company_code = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250, blank=True)
    registration_number = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=3, default='IN')
    currency_code = models.CharField(max_length=10, default='INR')
    base_currency_code = models.CharField(max_length=10, default='INR')
    fiscal_year_start = models.PositiveSmallIntegerField(default=4)
    timezone = models.CharField(max_length=100, default='Asia/Kolkata')
    date_format = models.CharField(max_length=30, default='DD/MM/YYYY')
    language = models.CharField(max_length=20, default='en-in')
    tax_registration_number = models.CharField(max_length=100, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    tan = models.CharField(max_length=10, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.company_name


class DocumentNumberSeries(models.Model):
    DOCUMENT_TYPE_CHOICES = [
        ('customer', 'Customer'),
        ('vendor', 'Vendor'),
        ('gl_journal', 'G/L Journal'),
        ('quotation', 'Quotation'),
        ('sales_order', 'Sales Order'),
        ('payment_receipt', 'Payment Receipt'),
        ('sales_invoice', 'Sales Invoice'),
        ('sales_credit_memo', 'Sales Credit Memo'),
        ('purchase_order', 'Purchase Order'),
        ('goods_receipt', 'Goods Receipt'),
        ('vendor_payment', 'Vendor Payment'),
        ('purchase_invoice', 'Purchase Invoice'),
        ('purchase_credit_memo', 'Purchase Credit Memo'),
        ('payment', 'Payment'),
        ('receipt', 'Receipt'),
        ('fixed_asset', 'Fixed Asset'),
        ('gst_invoice', 'GST Invoice'),
        ('e_invoice', 'E-Invoice'),
        ('e_way_bill', 'E-Way Bill'),
        ('transfer_order', 'Transfer Order'),
        ('warehouse_receipt', 'Warehouse Receipt'),
        ('putaway', 'Put-away'),
        ('pick', 'Pick'),
        ('warehouse_shipment', 'Warehouse Shipment'),
        ('movement', 'Warehouse Movement'),
        ('replenishment', 'Replenishment'),
        ('inventory_adjustment', 'Inventory Adjustment'),
        ('item_journal', 'Item Journal'),
        ('stock_take', 'Stock Take'),
        ('cycle_count', 'Cycle Count'),
        ('qc_inspection', 'QC Inspection'),
        ('customer_return', 'Customer Return'),
        ('vendor_return', 'Vendor Return'),
        ('scrap', 'Scrap'),
        ('reclassification', 'Reclassification'),
        ('cross_dock', 'Cross Dock'),
        ('journal_voucher', 'Journal Voucher'),
        ('sales_journal', 'Sales Journal'),
        ('purchase_journal', 'Purchase Journal'),
        ('cash_receipt', 'Cash Receipt Voucher'),
        ('cash_payment', 'Cash Payment Voucher'),
        ('bank_receipt', 'Bank Receipt Voucher'),
        ('bank_payment', 'Bank Payment Voucher'),
        ('contra_voucher', 'Contra Voucher'),
        ('payment_journal', 'Payment Journal'),
        ('receipt_journal', 'Receipt Journal'),
        ('adjustment_journal', 'Adjustment Journal'),
        ('accrual_journal', 'Accrual Journal'),
        ('provision_journal', 'Provision Journal'),
        ('reversal_journal', 'Reversal Journal'),
        ('production_journal', 'Production Journal'),
        ('scheme_journal', 'Jewellery Savings Journal'),
        ('job_work_journal', 'Job Work Journal'),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='document_number_series',
    )
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPE_CHOICES)
    fiscal_year = models.PositiveIntegerField()
    prefix = models.CharField(max_length=20)
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=4)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('company', 'document_type', 'fiscal_year'),
                name='unique_document_series_per_company_year',
            ),
        ]

    def __str__(self):
        scope = self.company.company_code if self.company else 'GLOBAL'
        return f'{scope} {self.document_type} {self.fiscal_year}'


class Branch(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='branches')
    branch_code = models.CharField(max_length=30)
    branch_name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=10, blank=True)
    country = models.CharField(max_length=3, default='IN')
    currency_code = models.CharField(max_length=10, default='INR')
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'branch_code')

    def __str__(self):
        return f'{self.company.company_code}-{self.branch_code}'


class Location(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='locations')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    warehouse = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    location_code = models.CharField(max_length=30)
    location_name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=10, blank=True)
    country = models.CharField(max_length=3, default='IN')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'location_code')

    def __str__(self):
        return f'{self.company.company_code}-{self.location_code}'


class Department(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='departments')
    department_code = models.CharField(max_length=30)
    department_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'department_code')

    def __str__(self):
        return self.department_name


class BusinessUnit(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='business_units')
    business_unit_code = models.CharField(max_length=30)
    business_unit_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'business_unit_code')

    def __str__(self):
        return self.business_unit_name


class Project(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('blocked', 'Blocked'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='projects')
    project_code = models.CharField(max_length=30)
    project_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.project_name


class CostCenter(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='cost_centers')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class Store(models.Model):
    """The operational hub of a retail site: exactly one Location, and many staff, POS terminals and tenders."""
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive'), ('blocked', 'Blocked'), ('closed', 'Closed')]
    STORE_TYPES = [('flagship', 'Flagship'), ('showroom', 'Showroom'), ('outlet', 'Outlet'), ('franchise', 'Franchise'),
                   ('kiosk', 'Kiosk'), ('online', 'Online'), ('other', 'Other')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='stores')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='stores')
    # OneToOne => database-level UNIQUE: one Location can back only one Store. Nullable only for legacy rows;
    # forms, imports and POS login all require it.
    location = models.OneToOneField(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='store')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    store_type = models.CharField(max_length=20, choices=STORE_TYPES, default='showroom')
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=60, default='India')
    pin_code = models.CharField(max_length=10, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    manager = models.ForeignKey('POSStaff', on_delete=models.SET_NULL, null=True, blank=True, related_name='managed_stores')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    opening_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'

    def clean(self):
        if self.location_id and self.company_id and self.location.company_id != self.company_id:
            raise ValidationError({'location': 'Location belongs to a different company.'})
        if self.manager_id and self.pk and self.manager.store_id != self.pk:
            raise ValidationError({'manager': 'Store manager must be a staff member of this store.'})
        if self.closing_date and self.opening_date and self.closing_date < self.opening_date:
            raise ValidationError({'closing_date': 'Closing date cannot be before the opening date.'})

    def save(self, *args, **kwargs):
        self.is_active = self.status == 'active'
        super().save(*args, **kwargs)


class InvoicePrintLayout(models.Model):
    PRINT_TYPES = [('A4_INVOICE', 'A4 Invoice'), ('A5_INVOICE', 'A5 Invoice'), ('THERMAL_40COL_RECEIPT', '40 Column Receipt')]
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    print_type = models.CharField(max_length=40, choices=PRINT_TYPES)
    version = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)


class StoreInvoicePrintSetup(models.Model):
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name='invoice_print_setup')
    invoice_print_type = models.CharField(max_length=40, choices=InvoicePrintLayout.PRINT_TYPES, default='A4_INVOICE')
    layout = models.ForeignKey(InvoicePrintLayout, on_delete=models.PROTECT, null=True, blank=True, related_name='store_setups')
    printer_name = models.CharField(max_length=120, blank=True)
    auto_print = models.BooleanField(default=False)
    copies = models.PositiveSmallIntegerField(default=1)
    print_jewellery_details = models.BooleanField(default=True)
    print_weight = models.BooleanField(default=True)
    print_huid = models.BooleanField(default=True)
    print_payment_details = models.BooleanField(default=True)
    print_tax_summary = models.BooleanField(default=True)
    print_terms = models.BooleanField(default=True)
    print_customer_address = models.BooleanField(default=True)
    active = models.BooleanField(default=True)


class POSTerminalPrintSetup(models.Model):
    terminal = models.OneToOneField('POSTerminal', on_delete=models.CASCADE, related_name='print_setup')
    print_type = models.CharField(max_length=40, choices=InvoicePrintLayout.PRINT_TYPES, blank=True)
    layout = models.ForeignKey(InvoicePrintLayout, on_delete=models.PROTECT, null=True, blank=True, related_name='terminal_setups')
    printer_name = models.CharField(max_length=120, blank=True)
    auto_print = models.BooleanField(default=False)
    copies = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)


class InvoicePrintLog(models.Model):
    invoice = models.ForeignKey('SalesInvoice', on_delete=models.PROTECT, related_name='print_logs')
    print_type = models.CharField(max_length=40)
    layout_code = models.CharField(max_length=40, blank=True)
    layout_version = models.PositiveIntegerField(default=1)
    printer_name = models.CharField(max_length=120, blank=True)
    printed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_print_logs')
    printed_at = models.DateTimeField(default=dj_timezone.now)
    reason = models.CharField(max_length=200, default='original')
    original = models.BooleanField(default=True)
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)


class POSRole(models.Model):
    """Configurable POS staff role. `permissions` is a list of codes from erp.retail.permissions.CATALOG;
    `base_role` maps onto the legacy POSStaff.role used for sales-staff attribution."""
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    base_role = models.CharField(max_length=20, choices=[('cashier', 'Cashier'), ('manager', 'Manager'), ('sales_staff', 'Sales Staff')],
                                 default='cashier')
    description = models.TextField(blank=True)
    pos_access = models.BooleanField(default=True, help_text='Staff with this role may log in to POS terminals.')
    permissions = models.JSONField(default=list, blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class POSStaff(models.Model):
    """Staff master. One staff member belongs to exactly one Store (and so to that store's Location)."""
    ROLE_CHOICES = [
        ('cashier', 'Cashier'),
        ('manager', 'Manager'),
        ('sales_staff', 'Sales Staff'),
    ]
    employee_code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    mobile = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    department = models.CharField(max_length=100, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='cashier')
    staff_role = models.ForeignKey(POSRole, on_delete=models.PROTECT, null=True, blank=True, related_name='staff')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='pos_staff')
    default_terminal = models.ForeignKey('POSTerminal', on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_staff')
    login_id = models.CharField(max_length=60, null=True, blank=True)
    # Salted hash from django.contrib.auth.hashers.make_password - never the password itself.
    pin_hash = models.CharField(max_length=128)
    pos_access = models.BooleanField(default=True, verbose_name='POS login enabled')
    is_active = models.BooleanField(default=True)
    is_blocked = models.BooleanField(default=False, verbose_name='Account locked')
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    last_login = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    password_change_required = models.BooleanField(default=False)
    joining_date = models.DateField(null=True, blank=True)
    leaving_date = models.DateField(null=True, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(models.functions.Lower('login_id'), name='unique_pos_staff_login_id_ci')]

    def __str__(self):
        return f'{self.employee_code} - {self.name}'

    @property
    def location(self):
        return self.store.location if self.store_id else None

    def clean(self):
        if self.default_terminal_id and self.store_id and self.default_terminal.store_id != self.store_id:
            raise ValidationError({'default_terminal': 'Default POS terminal must belong to the staff member\'s store.'})
        if self.leaving_date and self.joining_date and self.leaving_date < self.joining_date:
            raise ValidationError({'leaving_date': 'Leaving date cannot be before the joining date.'})

    def save(self, *args, **kwargs):
        if self.login_id:
            self.login_id = self.login_id.strip().lower()
        else:
            self.login_id = None
        if self.staff_role_id:
            self.role = self.staff_role.base_role
        if self.default_terminal_id and self.default_terminal.store_id != self.store_id:
            raise ValidationError('Default POS terminal must belong to the staff member\'s store.')
        super().save(*args, **kwargs)


class Tender(models.Model):
    """Central tender master. Stores opt in to tenders through StoreTender - nothing is hard-coded in POS."""
    TENDER_TYPES = [('cash', 'Cash'), ('card', 'Card'), ('digital', 'Digital / UPI'), ('online', 'Online gateway'),
                    ('bank', 'Bank transfer'), ('cheque', 'Cheque'), ('gift_card', 'Gift card'),
                    ('store_credit', 'Store credit'), ('wallet', 'Wallet'), ('other', 'Other')]
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    tender_type = models.CharField(max_length=20, choices=TENDER_TYPES, default='cash')
    description = models.TextField(blank=True)
    payment_gateway = models.CharField(max_length=100, blank=True)
    payment_method = models.ForeignKey('PaymentMethod', on_delete=models.SET_NULL, null=True, blank=True, related_name='tenders',
                                       help_text='Payment method used when this tender is posted to receivables.')
    gl_account = models.ForeignKey('GLAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='tenders')
    bank_account = models.ForeignKey('BankAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='tenders')
    currency = models.CharField(max_length=3, default='INR')
    requires_reference = models.BooleanField(default=False)
    requires_approval = models.BooleanField(default=False)
    allow_refund = models.BooleanField(default=True)
    allow_change = models.BooleanField(default=False)
    allow_split_payment = models.BooleanField(default=True)
    allow_partial_payment = models.BooleanField(default=True)
    minimum_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    maximum_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ('name',)
        constraints = [models.CheckConstraint(
            condition=models.Q(minimum_amount__isnull=True) | models.Q(maximum_amount__isnull=True) | models.Q(maximum_amount__gte=models.F('minimum_amount')),
            name='tender_min_le_max')]

    def __str__(self):
        return f'{self.code} - {self.name}'

    # Classification flags are derived from the tender type so they can never disagree with it.
    @property
    def is_cash(self):
        return self.tender_type == 'cash'

    @property
    def is_card(self):
        return self.tender_type == 'card'

    @property
    def is_digital(self):
        return self.tender_type in ('digital', 'wallet')

    @property
    def is_online(self):
        return self.tender_type in ('online', 'bank')

    def clean(self):
        if self.minimum_amount is not None and self.maximum_amount is not None and self.maximum_amount < self.minimum_amount:
            raise ValidationError({'maximum_amount': 'Maximum amount cannot be below the minimum amount.'})


class POSTerminal(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('blocked', 'Blocked'), ('inactive', 'Inactive')]
    TERMINAL_TYPES = [('counter', 'Billing counter'), ('mobile', 'Mobile / tablet'), ('kiosk', 'Self-service kiosk'),
                      ('back_office', 'Back office')]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='pos_terminals')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    terminal_type = models.CharField(max_length=20, choices=TERMINAL_TYPES, default='counter')
    device_id = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    mac_address = models.CharField(max_length=17, blank=True)
    printer = models.CharField(max_length=100, blank=True)
    receipt_printer = models.CharField(max_length=100, blank=True)
    auto_print = models.BooleanField(default=False)
    cash_drawer = models.CharField(max_length=100, blank=True)
    barcode_scanner = models.CharField(max_length=100, blank=True)
    customer_display = models.CharField(max_length=100, blank=True)
    payment_device = models.CharField(max_length=100, blank=True)
    default_tender = models.ForeignKey(Tender, on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_terminals')
    active_from = models.DateField(null=True, blank=True)
    active_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    is_active = models.BooleanField(default=True)
    last_login = models.DateTimeField(null=True, blank=True)
    last_transaction = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('store', 'code'), name='unique_pos_terminal_store_code')]

    def __str__(self):
        return f'{self.store.code} / {self.code}'

    @property
    def location(self):
        return self.store.location if self.store_id else None

    def clean(self):
        if self.active_to and self.active_from and self.active_to < self.active_from:
            raise ValidationError({'active_to': 'Active-to date cannot be before active-from date.'})
        if self.default_tender_id and self.store_id and not StoreTender.objects.filter(
                store_id=self.store_id, tender_id=self.default_tender_id, active=True).exists():
            raise ValidationError({'default_tender': 'Default tender must be an active tender of this store.'})

    def save(self, *args, **kwargs):
        # `status` is the single source of truth; is_active mirrors it for existing queries.
        self.is_active = self.status == 'active'
        super().save(*args, **kwargs)


class StoreTender(models.Model):
    """Tenders a store accepts, with store-level overrides of the tender master rules."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='store_tenders')
    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name='store_tenders')
    tender_name_snapshot = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    sequence = models.PositiveSmallIntegerField(default=10)
    minimum_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    maximum_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    requires_reference = models.BooleanField(default=False)
    allow_refund = models.BooleanField(default=True)
    allow_change = models.BooleanField(default=False)
    allow_split_payment = models.BooleanField(default=True)
    gl_account = models.ForeignKey('GLAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='store_tenders')
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ('sequence', 'tender__name')
        constraints = [
            models.UniqueConstraint(fields=('store', 'tender'), name='unique_store_tender'),
            models.UniqueConstraint(fields=('store',), condition=models.Q(is_default=True), name='one_default_tender_per_store'),
        ]

    def __str__(self):
        return f'{self.store.code} / {self.tender.name}'

    def is_effective(self, on=None):
        on = on or dj_timezone.localdate()
        return (self.active and self.tender.status == 'active'
                and (not self.effective_from or self.effective_from <= on)
                and (not self.effective_to or self.effective_to >= on))

    # Effective rules: the store setting can only tighten what the tender master allows.
    @property
    def effective_min(self):
        values = [v for v in (self.minimum_amount, self.tender.minimum_amount) if v is not None]
        return max(values) if values else None

    @property
    def effective_max(self):
        values = [v for v in (self.maximum_amount, self.tender.maximum_amount) if v is not None]
        return min(values) if values else None

    @property
    def needs_reference(self):
        return self.requires_reference or self.tender.requires_reference

    @property
    def can_give_change(self):
        return self.allow_change and self.tender.allow_change

    @property
    def can_split(self):
        return self.allow_split_payment and self.tender.allow_split_payment

    @property
    def can_refund(self):
        return self.allow_refund and self.tender.allow_refund

    def clean(self):
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({'effective_to': 'Effective-to date cannot be before effective-from date.'})
        if self.minimum_amount is not None and self.maximum_amount is not None and self.maximum_amount < self.minimum_amount:
            raise ValidationError({'maximum_amount': 'Maximum amount cannot be below the minimum amount.'})

    def save(self, *args, **kwargs):
        if not self.pk and self.tender.status != 'active':
            raise ValidationError('Only active tenders can be assigned to a store.')
        self.tender_name_snapshot = self.tender_name_snapshot or self.tender.name
        super().save(*args, **kwargs)


class POSStaffAssignment(models.Model):
    """Optional staff -> POS terminal access list. When a staff member has any active assignment, they may only log in
    to those terminals; otherwise to any terminal of their own store. Never to another store's terminal."""
    staff = models.ForeignKey(POSStaff, on_delete=models.CASCADE, related_name='assignments')
    terminal = models.ForeignKey(POSTerminal, on_delete=models.CASCADE, related_name='staff_assignments')
    role = models.CharField(max_length=20, choices=POSStaff.ROLE_CHOICES)
    effective_from = models.DateField(default=dj_timezone.now)
    effective_to = models.DateField(null=True, blank=True)
    primary_terminal = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('staff', 'terminal', 'role'), name='unique_pos_staff_terminal_role')]

    @property
    def store(self):
        return self.terminal.store

    def is_effective(self, on=None):
        on = on or dj_timezone.localdate()
        return self.active and self.effective_from <= on and (not self.effective_to or self.effective_to >= on)

    def clean(self):
        if self.staff_id and self.terminal_id and self.staff.store_id != self.terminal.store_id:
            raise ValidationError('Staff can only be assigned to POS terminals of their own store.')
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({'effective_to': 'Effective-to date cannot be before effective-from date.'})

    def save(self, *args, **kwargs):
        if hasattr(self.effective_from, 'date'):
            self.effective_from = self.effective_from.date()
        if self.active and self.staff.store_id != self.terminal.store_id:
            raise ValidationError('Staff can only be assigned to POS terminals of their own store.')
        super().save(*args, **kwargs)


class POSShift(models.Model):
    STATUS_CHOICES = [('open', 'Open'), ('closed', 'Closed'), ('suspended', 'Suspended')]
    shift_code = models.CharField(max_length=50, unique=True)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='pos_shifts')
    terminal = models.ForeignKey(POSTerminal, on_delete=models.PROTECT, related_name='shifts')
    opening_staff = models.ForeignKey(POSStaff, on_delete=models.PROTECT, related_name='opened_shifts')
    closing_staff = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True, related_name='closed_shifts')
    opening_time = models.DateTimeField(default=dj_timezone.now)
    closing_time = models.DateTimeField(null=True, blank=True)
    opening_cash = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    expected_cash = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    actual_cash = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cash_difference = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    location_code_snapshot = models.CharField(max_length=30, blank=True)
    closing_notes = models.TextField(blank=True)

    def __str__(self):
        return self.shift_code


class POSSession(models.Model):
    # 'active' is an open session; 'logged_out' is a normally closed one.
    STATUS_CHOICES = [('active', 'Open'), ('logged_out', 'Closed'), ('expired', 'Expired'), ('locked', 'Locked'),
                      ('force_closed', 'Force closed')]
    session_key = models.CharField(max_length=100)
    session_no = models.CharField(max_length=30, blank=True)
    staff = models.ForeignKey(POSStaff, on_delete=models.PROTECT, related_name='pos_sessions')
    terminal = models.ForeignKey(POSTerminal, on_delete=models.PROTECT, related_name='pos_sessions')
    shift = models.ForeignKey(POSShift, on_delete=models.PROTECT, related_name='sessions')
    login_time = models.DateTimeField(default=dj_timezone.now)
    logout_time = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(default=dj_timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_id = models.CharField(max_length=100, blank=True)
    # Snapshots: history must not change when master data does.
    staff_code_snapshot = models.CharField(max_length=50, blank=True)
    staff_name_snapshot = models.CharField(max_length=200, blank=True)
    role_snapshot = models.CharField(max_length=100, blank=True)
    store_code_snapshot = models.CharField(max_length=30, blank=True)
    store_name_snapshot = models.CharField(max_length=200, blank=True)
    location_code_snapshot = models.CharField(max_length=30, blank=True)
    location_name_snapshot = models.CharField(max_length=200, blank=True)
    terminal_code_snapshot = models.CharField(max_length=30, blank=True)
    terminal_name_snapshot = models.CharField(max_length=100, blank=True)
    # Closing summary, filled at logout.
    sales_count = models.PositiveIntegerField(default=0)
    sales_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    refund_count = models.PositiveIntegerField(default=0)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cash_collected = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('terminal',), condition=models.Q(status='active'),
                                               name='one_open_session_per_terminal')]

    def __str__(self):
        return self.session_no or f'POSSES-{self.pk}'

    @property
    def duration(self):
        end = self.logout_time or dj_timezone.now()
        return end - self.login_time


class POSPayment(models.Model):
    """One tender line of a POS bill, with snapshots of the tender as it was when taken."""
    invoice = models.ForeignKey('SalesInvoice', on_delete=models.PROTECT, related_name='pos_payments')
    session = models.ForeignKey(POSSession, on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    store_tender = models.ForeignKey(StoreTender, on_delete=models.PROTECT, related_name='payments')
    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name='payments')
    tender_code_snapshot = models.CharField(max_length=30)
    tender_name_snapshot = models.CharField(max_length=100)
    tender_type_snapshot = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    change_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reference = models.CharField(max_length=100, blank=True)
    is_refund = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='pos_payment_amount_positive')]

    @property
    def net_amount(self):
        return self.amount - self.change_amount


class RetailImportBatch(models.Model):
    """Staged Excel import for the store/POS masters: upload -> validate -> preview/errors -> import."""
    TYPES = [('locations', 'Locations'), ('stores', 'Stores'), ('staff', 'Staff'), ('terminals', 'POS terminals'),
             ('tenders', 'Tenders'), ('store-tenders', 'Store tenders'), ('staff-terminals', 'Staff POS assignments')]
    STATUS_CHOICES = [('validated', 'Validated'), ('failed', 'Has errors'), ('imported', 'Imported')]
    import_type = models.CharField(max_length=30, choices=TYPES)
    file_name = models.CharField(max_length=200, blank=True)
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='validated')
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=dj_timezone.now)
    imported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)


class Brand(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='brands')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class Channel(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='channels')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class Division(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='merchandise_divisions')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class SpecialGroup(models.Model):
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name='special_groups')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('division', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class Category(models.Model):
    special_group = models.ForeignKey(SpecialGroup, on_delete=models.PROTECT, related_name='categories')
    parent = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='children')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('special_group', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class SubCategory(models.Model):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='subcategories')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('category', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class UnitOfMeasure(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='units_of_measure')
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'code'), name='unique_uom_scope')]

    def __str__(self):
        return self.code


class Item(models.Model):
    ITEM_TYPES = [('inventory', 'Inventory'), ('service', 'Service'), ('kit', 'Kit'), ('bundle', 'Bundle'), ('consumable', 'Consumable')]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='retail_items')
    item_number = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=200)
    item_type = models.CharField(max_length=30, choices=ITEM_TYPES, default='inventory')
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name='items')
    special_group = models.ForeignKey(SpecialGroup, on_delete=models.PROTECT, related_name='items')
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='items')
    subcategory = models.ForeignKey(SubCategory, on_delete=models.PROTECT, related_name='items')
    brand = models.ForeignKey('Brand', on_delete=models.SET_NULL, null=True, blank=True, related_name='retail_items')
    base_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='base_items')
    standard_cost = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    variant_mandatory = models.BooleanField(default=False)
    lot_tracking = models.BooleanField(default=False)
    serial_tracking = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=[('draft', 'Draft'), ('approved', 'Approved'), ('active', 'Active'), ('inactive', 'Inactive'), ('blocked', 'Blocked')], default='draft')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.subcategory.category_id != self.category_id:
            raise ValidationError({'subcategory': 'SubCategory must belong to the selected Category.'})
        if self.category.special_group_id != self.special_group_id:
            raise ValidationError({'category': 'Category must belong to the selected Special Group.'})
        if self.special_group.division_id != self.division_id:
            raise ValidationError({'special_group': 'Special Group must belong to the selected Division.'})

    def __str__(self):
        return f'{self.item_number} - {self.name}'


class ItemUnitOfMeasure(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='units_of_measure')
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='item_conversions')
    quantity_per_uom = models.DecimalField(max_digits=18, decimal_places=6, default=1)
    is_purchase_uom = models.BooleanField(default=False)
    is_sales_uom = models.BooleanField(default=False)
    is_inventory_uom = models.BooleanField(default=False)

    class Meta:
        unique_together = ('item', 'uom')

    def clean(self):
        if self.quantity_per_uom <= 0:
            raise ValidationError({'quantity_per_uom': 'Quantity per UOM must be greater than zero.'})


class ItemVariant(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='variants')
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=150)
    color = models.CharField(max_length=60, blank=True)
    size = models.CharField(max_length=60, blank=True)
    material = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('item', 'code'), name='unique_item_variant')]

    def __str__(self):
        return f'{self.item.item_number} / {self.code}'


class SKU(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='skus')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='skus')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='retail_skus')
    code = models.CharField(max_length=80, unique=True)
    cost = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('item', 'variant', 'location'), name='unique_item_variant_location_sku')]

    def clean(self):
        if self.variant_id and self.variant.item_id != self.item_id:
            raise ValidationError({'variant': 'Variant must belong to the selected Item.'})

    def __str__(self):
        return self.code


class Barcode(models.Model):
    BARCODE_TYPES = [('ean13', 'EAN-13'), ('upc', 'UPC'), ('gtin', 'GTIN'), ('code128', 'Code 128'), ('internal', 'Internal'), ('gs1', 'GS1')]
    value = models.CharField(max_length=100, unique=True)
    barcode_type = models.CharField(max_length=20, choices=BARCODE_TYPES, default='internal')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name='retail_barcodes')
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='retail_barcodes')
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=1)
    is_primary = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    def clean(self):
        if self.sku.item.base_uom_id != self.uom_id and not self.sku.item.units_of_measure.filter(uom_id=self.uom_id).exists():
            raise ValidationError({'uom': 'Barcode UOM must be configured for the SKU item.'})

    def __str__(self):
        return self.value


class ItemLocation(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='location_setups')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='location_setups')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='item_setups')
    default_bin = models.ForeignKey('BinLocation', on_delete=models.PROTECT, null=True, blank=True, related_name='retail_item_setups')
    reorder_point = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    reorder_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('item', 'variant', 'location'), name='unique_item_location_setup')]

    def clean(self):
        if self.variant_id and self.variant.item_id != self.item_id:
            raise ValidationError({'variant': 'Variant must belong to the selected Item.'})


class Permission(models.Model):
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=80, default='general')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class RoleManager(models.Manager):
    def create(self, **kwargs):
        permission_payload = kwargs.pop('permissions', None)
        permission_config = kwargs.pop('permission_config', None)
        if permission_payload is not None and permission_config is None:
            permission_config = permission_payload
        instance = super().create(**kwargs)
        if permission_config is not None:
            instance.permission_config = permission_config
            instance.save(update_fields=['permission_config'])
        return instance


class Role(models.Model):
    objects = RoleManager()

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    permissions = models.ManyToManyField('Permission', through='RolePermission', related_name='roles', blank=True)
    permission_config = models.JSONField(default=dict, blank=True)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='role_permissions')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name='role_permissions')
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='granted_permissions')
    granted_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        unique_together = ('role', 'permission')

    def __str__(self):
        return f'{self.role.name} -> {self.permission.name}'


class UserRole(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='erp_roles')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='user_roles')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='user_roles', null=True, blank=True)
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_user_roles')
    assigned_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        unique_together = ('user', 'role', 'company')

    def __str__(self):
        return f'{self.user.username} -> {self.role.name}'


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('approve', 'Approve'),
        ('post', 'Post'),
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('export', 'Export'),
        ('import', 'Import'),
        ('assign', 'Assign'),
        ('unassign', 'Unassign'),
        ('activate', 'Activate'),
        ('deactivate', 'Deactivate'),
        ('lock', 'Lock account'),
        ('unlock', 'Unlock account'),
        ('password_reset', 'Password reset'),
        ('login_failed', 'Login failed'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='audit_logs', null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    table_name = models.CharField(max_length=200)
    action = models.CharField(max_length=40, choices=ACTION_CHOICES, default='create')
    record_id = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.table_name} - {self.action}'


class GLAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('revenue', 'Revenue'),
        ('expense', 'Expense'),
        ('contra', 'Contra'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='gl_accounts')
    account_code = models.CharField(max_length=30, unique=True)
    account_name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=30, choices=ACCOUNT_TYPE_CHOICES, default='asset')
    parent_account = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_accounts')
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_code']

    def __str__(self):
        return f'{self.account_code} - {self.account_name}'


class DimensionSet(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='dimension_sets')
    gl_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='dimension_sets')
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    business_unit = models.ForeignKey(BusinessUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    channel = models.ForeignKey(Channel, on_delete=models.PROTECT, null=True, blank=True, related_name='dimension_sets')
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    'company', 'gl_account', 'department', 'cost_center', 'store',
                    'brand', 'business_unit', 'project', 'channel',
                ),
                name='unique_dimension_set',
            ),
        ]

    def clean(self):
        dimensions = {
            'gl_account': self.gl_account,
            'department': self.department,
            'cost_center': self.cost_center,
            'store': self.store,
            'brand': self.brand,
            'business_unit': self.business_unit,
            'project': self.project,
            'channel': self.channel,
        }
        mismatches = [
            name for name, dimension in dimensions.items()
            if dimension is not None and dimension.company_id != self.company_id
        ]
        if mismatches:
            raise ValidationError({'company': f'Dimensions belong to another company: {", ".join(mismatches)}.'})

    def __str__(self):
        return f'{self.company.company_code} / {self.gl_account.account_code}'


class JournalEntry(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('void', 'Void'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='journal_entries')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    entry_no = models.CharField(max_length=50, unique=True)
    entry_date = models.DateTimeField(default=dj_timezone.now)
    reference = models.CharField(max_length=100, blank=True)
    narration = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_debit(self):
        return sum((line.debit_amount for line in self.lines.all()), Decimal('0.00'))

    @property
    def total_credit(self):
        return sum((line.credit_amount for line in self.lines.all()), Decimal('0.00'))

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit

    def post(self):
        if self.status == 'posted':
            return self
        if not self.is_balanced:
            raise ValueError('Journal entry is not balanced.')

        for line in self.lines.all():
            GeneralLedger.objects.create(
                company=self.company,
                entry=self,
                account=line.account,
                description=line.description,
                debit_amount=line.debit_amount,
                credit_amount=line.credit_amount,
                balance_after=line.debit_amount - line.credit_amount,
            )
            account = line.account
            account.current_balance = (account.current_balance or Decimal('0.00')) + (line.debit_amount - line.credit_amount)
            account.save(update_fields=['current_balance', 'updated_at'])

        self.status = 'posted'
        self.posted_at = dj_timezone.now()
        self.save(update_fields=['status', 'posted_at', 'updated_at'])
        return self

    def __str__(self):
        return self.entry_no


class JournalEntryLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='journal_lines')
    description = models.CharField(max_length=200, blank=True)
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.entry.entry_no} - {self.account.account_code}'


class GeneralLedger(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='general_ledger')
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='ledger_entries')
    account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='ledger_posts')
    description = models.CharField(max_length=200, blank=True)
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ['-created_at', 'account__account_code']

    def __str__(self):
        return f'{self.account.account_code} - {self.entry.entry_no}'


class FinanceJournalTemplate(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    operation_type = models.CharField(max_length=40, default='general_journal')
    voucher_type = models.CharField(max_length=40, default='journal_voucher')
    source_code = models.CharField(max_length=40, blank=True)
    reason_code = models.CharField(max_length=40, blank=True)
    approval_required = models.BooleanField(default=True)
    auto_post_allowed = models.BooleanField(default=False)
    recurring_allowed = models.BooleanField(default=False)
    reversal_allowed = models.BooleanField(default=True)
    allow_dimensions = models.BooleanField(default=True)
    allow_gst = models.BooleanField(default=True)
    allow_tds = models.BooleanField(default=True)
    allow_foreign_currency = models.BooleanField(default=True)
    allow_attachment = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.code} - {self.name}'


class FinanceJournalBatch(models.Model):
    STATUS_CHOICES = [('open', 'Open'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('posted', 'Posted'), ('closed', 'Closed')]
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='finance_journal_batches')
    template = models.ForeignKey(FinanceJournalTemplate, on_delete=models.PROTECT, related_name='batches')
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='finance_journal_batches')
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=150)
    source_code = models.CharField(max_length=40, blank=True)
    reason_code = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.company.company_code} - {self.code}'


class FinanceVoucherType(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='finance_voucher_types')
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=40, default='journal')
    template = models.ForeignKey(FinanceJournalTemplate, on_delete=models.PROTECT, related_name='voucher_types')
    approval_required = models.BooleanField(default=True)
    auto_post = models.BooleanField(default=False)
    allow_customer = models.BooleanField(default=True)
    allow_vendor = models.BooleanField(default=True)
    allow_bank = models.BooleanField(default=True)
    allow_cash = models.BooleanField(default=True)
    allow_tax = models.BooleanField(default=True)
    allow_dimensions = models.BooleanField(default=True)
    allow_reversal = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'code'), name='unique_finance_voucher_type')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class FinanceVoucher(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('saved', 'Saved'), ('submitted', 'Submitted'),
        ('under_review', 'Under Review'), ('approved', 'Approved'),
        ('rejected', 'Rejected'), ('posted', 'Posted'), ('reversed', 'Reversed'),
        ('cancelled', 'Cancelled'), ('failed', 'Failed'),
    ]
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='finance_vouchers')
    batch = models.ForeignKey(FinanceJournalBatch, on_delete=models.PROTECT, related_name='vouchers')
    voucher_type = models.ForeignKey(FinanceVoucherType, on_delete=models.PROTECT, related_name='vouchers')
    voucher_no = models.CharField(max_length=60, unique=True)
    posting_no = models.CharField(max_length=60, unique=True, null=True, blank=True)
    document_no = models.CharField(max_length=100, blank=True)
    external_document_no = models.CharField(max_length=100, blank=True)
    voucher_date = models.DateField(default=date.today)
    document_date = models.DateField(default=date.today)
    posting_date = models.DateField(null=True, blank=True)
    currency_code = models.CharField(max_length=10, default='INR')
    exchange_rate = models.DecimalField(max_digits=14, decimal_places=6, default=1)
    narration = models.TextField(blank=True)
    total_debit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    approval_status = models.CharField(max_length=20, default='not_required')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_finance_vouchers')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_finance_vouchers')
    posted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='posted_finance_vouchers')
    created_at = models.DateTimeField(default=dj_timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit and self.total_debit > 0

    def __str__(self):
        return self.voucher_no


class FinanceVoucherLine(models.Model):
    voucher = models.ForeignKey(FinanceVoucher, on_delete=models.PROTECT, related_name='lines')
    line_no = models.PositiveIntegerField()
    account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='finance_voucher_lines')
    description = models.CharField(max_length=250, blank=True)
    debit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    dimension_set = models.ForeignKey(DimensionSet, on_delete=models.PROTECT, null=True, blank=True, related_name='finance_voucher_lines')
    gst_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tds_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('voucher', 'line_no'), name='unique_finance_voucher_line')]
        ordering = ['line_no']

    def clean(self):
        if self.debit_amount and self.credit_amount:
            raise ValidationError('A voucher line cannot contain both debit and credit.')
        if self.debit_amount <= 0 and self.credit_amount <= 0:
            raise ValidationError('A voucher line must contain a debit or credit amount.')


class FinanceVoucherApproval(models.Model):
    voucher = models.ForeignKey(FinanceVoucher, on_delete=models.PROTECT, related_name='approvals')
    approver = models.ForeignKey(User, on_delete=models.PROTECT, related_name='finance_approvals')
    status = models.CharField(max_length=20, choices=[('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='pending')
    comments = models.TextField(blank=True)
    acted_at = models.DateTimeField(null=True, blank=True)


class FinancePostedVoucher(models.Model):
    voucher = models.OneToOneField(FinanceVoucher, on_delete=models.PROTECT, related_name='posted_snapshot')
    company_name = models.CharField(max_length=250)
    voucher_no = models.CharField(max_length=60)
    posting_no = models.CharField(max_length=60, unique=True)
    posting_date = models.DateField()
    total_debit = models.DecimalField(max_digits=18, decimal_places=2)
    total_credit = models.DecimalField(max_digits=18, decimal_places=2)
    narration = models.TextField(blank=True)
    posted_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='posted_voucher_snapshots')
    posted_at = models.DateTimeField(default=dj_timezone.now)


class FinancePostedVoucherLine(models.Model):
    posted_voucher = models.ForeignKey(FinancePostedVoucher, on_delete=models.PROTECT, related_name='lines')
    line_no = models.PositiveIntegerField()
    account_code = models.CharField(max_length=30)
    account_name = models.CharField(max_length=200)
    description = models.CharField(max_length=250, blank=True)
    debit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tds_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

class Business(models.Model):
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    place = models.CharField(max_length=200, blank=True)
    state_code = models.CharField(max_length=2, default='27')
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class Customer(models.Model):
    TYPE_CHOICES = [
        ('retail', 'Retail'),
        ('wholesale', 'Wholesale'),
        ('b2b', 'B2B'),
    ]
    name = models.CharField(max_length=200)
    customer_no = models.CharField(max_length=50, unique=True, null=True, blank=True)
    title = models.CharField(max_length=20, blank=True)
    first_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    gender = models.CharField(max_length=30, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    anniversary_date = models.DateField(null=True, blank=True)
    occupation = models.CharField(max_length=120, blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    designation = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    alternate_phone = models.CharField(max_length=15, blank=True)
    whatsapp_number = models.CharField(max_length=15, blank=True)
    preferred_contact_method = models.CharField(max_length=20, default='phone')
    preferred_language = models.CharField(max_length=50, default='en-IN')
    email = models.EmailField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    gst_customer_type = models.CharField(max_length=30, default='unregistered')
    gst_registration_type = models.CharField(max_length=20, default='GSTIN')
    gst_state_code = models.CharField(max_length=5, blank=True)
    gst_legal_name = models.CharField(max_length=200, blank=True)
    gst_verification_status = models.CharField(max_length=30, default='unverified')
    pan = models.CharField(max_length=10, blank=True)
    customer_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='retail')
    customer_status = models.CharField(max_length=20, default='active')
    customer_posting_group = models.ForeignKey('CustomerPostingGroup', on_delete=models.PROTECT, null=True, blank=True, related_name='customers')
    customer_price_group = models.ForeignKey('CustomerPriceGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='customers')
    customer_discount_group = models.ForeignKey('CustomerDiscountGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='customers')
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.customer_no or self.pk} - {self.name}'


class CustomerPriceGroup(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    calculation_method = models.CharField(max_length=30, default='standard')
    price_includes_tax = models.BooleanField(default=False)
    allow_line_discount = models.BooleanField(default=True)
    allow_invoice_discount = models.BooleanField(default=True)
    currency_code = models.CharField(max_length=10, default='INR')
    is_active = models.BooleanField(default=True)


class CustomerDiscountGroup(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    maximum_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    approval_required = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)


class CustomerDiscountRule(models.Model):
    discount_group = models.ForeignKey(CustomerDiscountGroup, on_delete=models.CASCADE, related_name='rules')
    product = models.ForeignKey('Product', on_delete=models.CASCADE, null=True, blank=True, related_name='customer_discount_rules')
    item_category = models.ForeignKey('ItemCategory', on_delete=models.CASCADE, null=True, blank=True, related_name='customer_discount_rules')
    discount_type = models.CharField(max_length=30, default='total')
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    minimum_transaction_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    priority = models.PositiveIntegerField(default=100)
    effective_from = models.DateField(default=date.today)
    effective_to = models.DateField(null=True, blank=True)
    approval_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)


class CustomerNumberSeries(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='customer_number_series')
    store = models.ForeignKey(Store, on_delete=models.CASCADE, null=True, blank=True, related_name='customer_number_series')
    prefix = models.CharField(max_length=20, default='CUS')
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=6)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'store'), name='unique_customer_number_series_scope')]


class CustomerAddress(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='billing_address')
    address_1 = models.CharField(max_length=200, blank=True)
    address_2 = models.CharField(max_length=200, blank=True)
    address_3 = models.CharField(max_length=200, blank=True)
    landmark = models.CharField(max_length=150, blank=True)
    area = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=5, blank=True)
    country = models.CharField(max_length=100, default='India')
    country_code = models.CharField(max_length=3, default='IN')
    pin_code = models.CharField(max_length=10, blank=True)


class CustomerShippingAddress(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='shipping_addresses')
    address_code = models.CharField(max_length=30)
    address_name = models.CharField(max_length=100)
    recipient_name = models.CharField(max_length=200, blank=True)
    mobile = models.CharField(max_length=15, blank=True)
    address_1 = models.CharField(max_length=200, blank=True)
    address_2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=5, blank=True)
    country = models.CharField(max_length=100, default='India')
    country_code = models.CharField(max_length=3, default='IN')
    pin_code = models.CharField(max_length=10, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('customer', 'address_code'), name='unique_customer_shipping_address_code')]


class CustomerIdentityDocument(models.Model):
    DOCUMENT_TYPES = [('PAN', 'PAN'), ('PASSPORT', 'Passport'), ('AADHAAR', 'Aadhaar'), ('VOTER_ID', 'Voter ID'), ('DRIVING_LICENSE', 'Driving licence'), ('OTHER', 'Other')]
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='identity_documents')
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)
    document_number = models.CharField(max_length=100)
    name_on_document = models.CharField(max_length=200, blank=True)
    verification_status = models.CharField(max_length=30, default='unverified')
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)


class CustomerCommunicationPreference(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='communication_preferences')
    sms_consent = models.BooleanField(default=False)
    email_consent = models.BooleanField(default=False)
    whatsapp_consent = models.BooleanField(default=False)
    phone_consent = models.BooleanField(default=False)
    promotional_consent = models.BooleanField(default=False)
    transactional_consent = models.BooleanField(default=True)
    consent_source = models.CharField(max_length=80, blank=True)
    consent_date = models.DateTimeField(null=True, blank=True)


class CustomerJewelleryPreference(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='jewellery_preferences')
    preferred_metal = models.CharField(max_length=30, blank=True)
    preferred_purity = models.CharField(max_length=30, blank=True)
    preferred_category = models.CharField(max_length=100, blank=True)
    preferred_style = models.CharField(max_length=100, blank=True)
    preferred_stone = models.CharField(max_length=100, blank=True)
    budget_minimum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    budget_maximum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    preferred_occasion = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)


class CustomerMarketingProfile(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='marketing_profile')
    segment = models.CharField(max_length=80, blank=True)
    tier = models.CharField(max_length=80, blank=True)
    loyalty_number = models.CharField(max_length=80, blank=True)
    loyalty_points = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    preferred_store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True, related_name='preferred_customers')
    source = models.CharField(max_length=80, blank=True)
    campaign_name = models.CharField(max_length=120, blank=True)
    first_purchase_date = models.DateField(null=True, blank=True)
    last_purchase_date = models.DateField(null=True, blank=True)
    lifetime_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)


class CustomerKYC(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='kyc')
    required = models.BooleanField(default=False)
    status = models.CharField(max_length=30, default='pending')
    pan_verified = models.BooleanField(default=False)
    address_verified = models.BooleanField(default=False)
    identity_verified = models.BooleanField(default=False)
    risk_category = models.CharField(max_length=30, default='standard')
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='verified_customer_kyc')
    verified_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True)


class Karigar(models.Model):
    KARIGAR_TYPES = [('internal', 'Internal'), ('external', 'External')]
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)
    mobile = models.CharField(max_length=15, blank=True)
    karigar_type = models.CharField(max_length=20, choices=KARIGAR_TYPES, default='internal')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='karigars')
    skills = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.code} - {self.name}'


class RepairService(models.Model):
    PRICING_METHODS = [('fixed', 'Fixed'), ('per_gram', 'Per gram'), ('per_piece', 'Per piece'), ('manual', 'Manual')]
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    pricing_method = models.CharField(max_length=20, choices=PRICING_METHODS, default='fixed')
    default_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate_code = models.CharField(max_length=40, default='GST-3')
    is_active = models.BooleanField(default=True)


class RepairOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('received', 'Received'), ('accepted', 'Accepted'),
        ('assigned', 'Assigned'), ('with_karigar', 'With Karigar'), ('in_repair', 'In Repair'),
        ('returned_by_karigar', 'Returned by Karigar'), ('qc_pending', 'QC Pending'),
        ('qc_failed', 'QC Failed'), ('qc_passed', 'QC Passed'), ('ready_for_customer', 'Ready for Customer'),
        ('billed', 'Billed'), ('payment_pending', 'Payment Pending'), ('paid', 'Paid'),
        ('returned_to_customer', 'Returned to Customer'), ('closed', 'Closed'), ('cancelled', 'Cancelled'),
    ]
    order_no = models.CharField(max_length=60, unique=True)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='repair_orders')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='repair_orders')
    customer_name_snapshot = models.CharField(max_length=200)
    customer_phone_snapshot = models.CharField(max_length=15, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    repair_type = models.CharField(max_length=120, blank=True)
    repair_description = models.TextField(blank=True)
    customer_remarks = models.TextField(blank=True)
    priority = models.CharField(max_length=20, default='normal')
    expected_completion_date = models.DateField(null=True, blank=True)
    estimated_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    final_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    received_by = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True, related_name='received_repairs')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_repairs')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class CustomerOrnament(models.Model):
    STATUS_CHOICES = [('at_store', 'At Store'), ('with_karigar', 'With Karigar'), ('returned', 'Returned'), ('blocked', 'Blocked')]
    ornament_id = models.CharField(max_length=60, unique=True)
    repair_order = models.OneToOneField(RepairOrder, on_delete=models.PROTECT, related_name='ornament')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='custody_ornaments')
    description = models.CharField(max_length=250)
    metal_type = models.CharField(max_length=30, blank=True)
    purity = models.CharField(max_length=30, blank=True)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    stone_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    other_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    huid = models.CharField(max_length=100, blank=True)
    certificate_number = models.CharField(max_length=100, blank=True)
    condition_at_receipt = models.TextField(blank=True)
    customer_declared_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    custody_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='at_store')
    current_location = models.CharField(max_length=120, default='STORE')
    accepted_at = models.DateTimeField(null=True, blank=True)

    @property
    def net_metal_weight(self):
        return max(self.gross_weight - self.stone_weight - self.other_weight, Decimal('0'))


class RepairCustodyEvent(models.Model):
    repair_order = models.ForeignKey(RepairOrder, on_delete=models.PROTECT, related_name='custody_events')
    ornament = models.ForeignKey(CustomerOrnament, on_delete=models.PROTECT, related_name='custody_events')
    from_location = models.CharField(max_length=120, blank=True)
    to_location = models.CharField(max_length=120)
    status = models.CharField(max_length=30)
    performed_by = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)


class RepairKarigarAssignment(models.Model):
    repair_order = models.OneToOneField(RepairOrder, on_delete=models.PROTECT, related_name='karigar_assignment')
    karigar = models.ForeignKey(Karigar, on_delete=models.PROTECT, related_name='repair_assignments')
    karigar_name_snapshot = models.CharField(max_length=200)
    assigned_by = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True)
    assigned_at = models.DateTimeField(default=dj_timezone.now)
    instructions = models.TextField(blank=True)
    status = models.CharField(max_length=30, default='assigned')


class RepairQC(models.Model):
    repair_order = models.OneToOneField(RepairOrder, on_delete=models.PROTECT, related_name='qc')
    passed = models.BooleanField(default=False)
    weight_after = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    notes = models.TextField(blank=True)
    inspected_by = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True)
    inspected_at = models.DateTimeField(default=dj_timezone.now)


class RepairInvoice(models.Model):
    repair_order = models.OneToOneField(RepairOrder, on_delete=models.PROTECT, related_name='repair_invoice')
    sales_invoice = models.OneToOneField('SalesInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='repair_invoice')
    invoice_no = models.CharField(max_length=60, unique=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    posted_at = models.DateTimeField(null=True, blank=True)


class Supplier(models.Model):
    vendor_no = models.CharField(max_length=50, unique=True, null=True, blank=True)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def save(self, *args, **kwargs):
        if not self.vendor_no:
            from .services import get_next_number
            self.vendor_no = get_next_number('vendor')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.vendor_no or self.pk} - {self.name}'


class PaymentTerm(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='payment_terms')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    due_days = models.PositiveIntegerField(default=0)
    discount_days = models.PositiveIntegerField(default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'code'), name='unique_payment_term_scope')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class PaymentMethod(models.Model):
    METHOD_CHOICES = [
        ('cash', 'Cash'), ('bank', 'Bank'), ('cheque', 'Cheque'),
        ('upi', 'UPI'), ('card', 'Card'), ('gateway', 'Payment Gateway'),
        ('other', 'Other'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='payment_methods')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    method_type = models.CharField(max_length=20, choices=METHOD_CHOICES, default='bank')
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'code'), name='unique_payment_method_scope')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class CustomerPostingGroup(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='customer_posting_groups')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    receivable_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='customer_posting_groups')
    advance_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='customer_advance_groups')
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class VendorPostingGroup(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='vendor_posting_groups')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    payable_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='vendor_posting_groups')
    advance_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='vendor_advance_groups')
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return f'{self.code} - {self.name}'


class CustomerFinanceProfile(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='finance_profile')
    payment_term = models.ForeignKey(PaymentTerm, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_profiles')
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_profiles')
    posting_group = models.ForeignKey(CustomerPostingGroup, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_profiles')
    currency_code = models.CharField(max_length=10, default='INR')
    credit_limit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit_hold = models.BooleanField(default=False)
    allow_sales = models.BooleanField(default=True)
    allow_credit = models.BooleanField(default=True)
    allow_refund = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class VendorFinanceProfile(models.Model):
    vendor = models.OneToOneField(Supplier, on_delete=models.CASCADE, related_name='finance_profile')
    payment_term = models.ForeignKey(PaymentTerm, on_delete=models.PROTECT, null=True, blank=True, related_name='vendor_profiles')
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, null=True, blank=True, related_name='vendor_profiles')
    posting_group = models.ForeignKey(VendorPostingGroup, on_delete=models.PROTECT, null=True, blank=True, related_name='vendor_profiles')
    currency_code = models.CharField(max_length=10, default='INR')
    payment_hold = models.BooleanField(default=False)
    purchase_hold = models.BooleanField(default=False)
    approval_required = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class FinancePostingSetup(models.Model):
    """The minimal General Posting Matrix this ERP needs: which default G/L accounts absorb Sales/Purchase document postings."""
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name='posting_setup')
    sales_revenue_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='+')
    gst_output_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='+')
    purchase_expense_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='+')
    gst_input_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='+')
    default_cash_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name='+')
    rounding_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Posting Setup - {self.company.company_code}'


class DocumentRelationship(models.Model):
    source_type = models.CharField(max_length=50)
    source_id = models.PositiveBigIntegerField()
    target_type = models.CharField(max_length=50)
    target_id = models.PositiveBigIntegerField()
    relationship_type = models.CharField(max_length=40, default='derived_from')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='document_relationships')
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('source_type', 'source_id', 'target_type', 'target_id', 'relationship_type'), name='unique_document_relationship')]
        indexes = [
            models.Index(fields=('source_type', 'source_id')),
            models.Index(fields=('target_type', 'target_id')),
        ]


class DocumentStatusHistory(models.Model):
    document_type = models.CharField(max_length=50)
    document_id = models.PositiveBigIntegerField()
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='document_status_changes')
    changed_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        indexes = [models.Index(fields=('document_type', 'document_id', 'changed_at'))]


class Staff(models.Model):
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=100)
    employee_code = models.CharField(max_length=50, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.name} ({self.employee_code})'


class Employee(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='employee_profile')
    employee_code = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    department = models.CharField(max_length=100, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.full_name} ({self.employee_code})'


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='erp_profile')
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='user_profiles')
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.user.username


class ItemCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Product(models.Model):
    METAL_CHOICES = [
        ('gold', 'Gold'),
        ('silver', 'Silver'),
        ('diamond', 'Diamond'),
        ('platinum', 'Platinum'),
    ]
    item_category = models.ForeignKey(ItemCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    subcategory = models.CharField(max_length=100, blank=True)
    variant = models.CharField(max_length=120, blank=True)
    sku = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    metal_type = models.CharField(max_length=20, choices=METAL_CHOICES, default='gold')
    purity = models.CharField(max_length=30, default='22K')
    weight_grams = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    making_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mrp = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    barcode = models.CharField(max_length=100, blank=True)
    hsn_code = models.CharField(max_length=20, blank=True)
    stock_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class JewelleryMetalRate(models.Model):
    SOURCE_CHOICES = [('manual', 'Manual'), ('api', 'API')]

    metal_type = models.CharField(max_length=30)
    purity = models.CharField(max_length=30)
    rate_per_gram = models.DecimalField(max_digits=14, decimal_places=2)
    rate_type = models.CharField(max_length=30, default='selling')
    store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True, related_name='jewellery_metal_rates')
    effective_from = models.DateTimeField(default=dj_timezone.now)
    effective_to = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    source_reference = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='metal_rates_created')

    class Meta:
        ordering = ['-effective_from']


class JewelleryPricingRule(models.Model):
    MAKING_METHODS = [('percent', 'Percentage'), ('per_gram', 'Per gram'), ('fixed', 'Fixed amount')]
    WASTAGE_METHODS = [('weight', 'Weight x rate'), ('percent', 'Metal value percentage')]
    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name='jewellery_pricing_rule')
    making_method = models.CharField(max_length=20, choices=MAKING_METHODS, default='percent')
    making_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wastage_method = models.CharField(max_length=20, choices=WASTAGE_METHODS, default='weight')
    wastage_percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    hallmark_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    certification_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate_code = models.CharField(max_length=40, default='GST-3')
    requires_huid = models.BooleanField(default=False)


class JewelleryItemUnit(models.Model):
    STATUS_CHOICES = [('available', 'Available'), ('reserved', 'Reserved'), ('held', 'Held'), ('sold', 'Sold'), ('returned', 'Returned'), ('blocked', 'Blocked')]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='jewellery_units')
    barcode = models.CharField(max_length=100, unique=True)
    serial_number = models.CharField(max_length=100, unique=True)
    design_id = models.CharField(max_length=100, blank=True)
    huid = models.CharField(max_length=100, blank=True)
    certificate_number = models.CharField(max_length=100, blank=True)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    stone_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    other_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    metal_type = models.CharField(max_length=30, blank=True)
    purity = models.CharField(max_length=30, blank=True)
    current_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    current_store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='jewellery_units')

    @property
    def net_metal_weight(self):
        return max(self.gross_weight - self.stone_weight - self.other_weight, Decimal('0'))


class JewelleryStone(models.Model):
    unit = models.ForeignKey(JewelleryItemUnit, on_delete=models.CASCADE, related_name='stones')
    stone_type = models.CharField(max_length=50)
    stone_name = models.CharField(max_length=100, blank=True)
    shape = models.CharField(max_length=50, blank=True)
    carat = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)
    rate_per_carat = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    fixed_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    certificate_number = models.CharField(max_length=100, blank=True)

    @property
    def net_value(self):
        value = self.fixed_value or (self.carat * self.rate_per_carat)
        return max(value - self.discount, Decimal('0')).quantize(Decimal('0.01'))


class ItemPrice(models.Model):
    PRICE_TYPE_CHOICES = [
        ('purchase', 'Purchase'),
        ('sale', 'Sale'),
        ('wholesale', 'Wholesale'),
        ('mrp', 'MRP'),
    ]
    item = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='prices')
    price_type = models.CharField(max_length=20, choices=PRICE_TYPE_CHOICES, default='sale')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    valid_from = models.DateField(default=dj_timezone.now)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.item.name} - {self.price_type}'


class ItemBarcode(models.Model):
    BARCODE_TYPE_CHOICES = [
        ('EAN13', 'EAN 13'),
        ('CODE128', 'Code 128'),
        ('QR', 'QR'),
        ('UPC', 'UPC'),
    ]
    item = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='barcodes')
    barcode = models.CharField(max_length=100, unique=True)
    barcode_type = models.CharField(max_length=20, choices=BARCODE_TYPE_CHOICES, default='EAN13')
    is_primary = models.BooleanField(default=False)

    def __str__(self):
        return self.barcode


class ItemBatch(models.Model):
    item = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='batches')
    batch_no = models.CharField(max_length=80, unique=True)
    manufacturing_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    status = models.CharField(max_length=20, default='active')
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.item.name} - {self.batch_no}'


class ItemSerial(models.Model):
    item = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='serials')
    batch = models.ForeignKey(ItemBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='serial_numbers')
    serial_no = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, default='available')
    warehouse_code = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.serial_no


class GSTSlab(models.Model):
    name = models.CharField(max_length=100)
    state_code = models.CharField(max_length=5, default='27')
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    hsn_code = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(default=dj_timezone.now)
    description = models.TextField(blank=True)

    def __str__(self):
        return f'{self.name} ({self.gst_rate}%)'


class GSTState(models.Model):
    state_code = models.CharField(max_length=3, unique=True)
    state_name = models.CharField(max_length=100)
    gst_state_code = models.CharField(max_length=3)
    is_union_territory = models.BooleanField(default=False)
    country_code = models.CharField(max_length=3, default='IN')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.state_code} - {self.state_name}'


class GSTRegistration(models.Model):
    REGISTRATION_TYPES = [
        ('regular', 'Regular'), ('composition', 'Composition'), ('sez', 'SEZ'),
        ('isd', 'ISD'), ('tds', 'TDS'), ('tcs', 'TCS'), ('casual', 'Casual'),
        ('non_resident', 'Non-resident'), ('other', 'Other'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='gst_registrations')
    gstin = models.CharField(max_length=15, unique=True)
    legal_name = models.CharField(max_length=250)
    trade_name = models.CharField(max_length=250, blank=True)
    state = models.ForeignKey(GSTState, on_delete=models.PROTECT, related_name='registrations')
    registration_type = models.CharField(max_length=20, choices=REGISTRATION_TYPES, default='regular')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    filing_frequency = models.CharField(max_length=20, default='monthly')
    composition_scheme = models.BooleanField(default=False)
    is_isd = models.BooleanField(default=False)
    is_tds_deductor = models.BooleanField(default=False)
    is_tcs_collector = models.BooleanField(default=False)
    lut_applicable = models.BooleanField(default=False)
    einvoice_applicable = models.BooleanField(default=False)
    ewaybill_applicable = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=[('draft', 'Draft'), ('active', 'Active'), ('blocked', 'Blocked')], default='draft')
    default_registration = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def clean(self):
        if len(self.gstin) != 15:
            from django.core.exceptions import ValidationError
            raise ValidationError({'gstin': 'GSTIN must contain 15 characters.'})

    def __str__(self):
        return f'{self.gstin} - {self.legal_name}'


class GSTGroup(models.Model):
    code = models.CharField(max_length=40, unique=True)
    description = models.CharField(max_length=200)
    taxability = models.CharField(max_length=20, choices=[('taxable', 'Taxable'), ('exempt', 'Exempt'), ('nil', 'Nil-rated'), ('non_gst', 'Non-GST'), ('zero', 'Zero-rated')], default='taxable')
    reverse_charge = models.BooleanField(default=False)
    itc_allowed = models.BooleanField(default=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return self.code


class GSTComponent(models.Model):
    COMPONENTS = [('cgst', 'CGST'), ('sgst', 'SGST'), ('utgst', 'UTGST'), ('igst', 'IGST'), ('cess', 'Cess'), ('tds', 'GST TDS'), ('tcs', 'GST TCS')]
    code = models.CharField(max_length=20, choices=COMPONENTS, unique=True)
    name = models.CharField(max_length=100)
    recoverable = models.BooleanField(default=True)
    payable = models.BooleanField(default=True)
    receivable = models.BooleanField(default=False)
    settlement_priority = models.PositiveIntegerField(default=100)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return self.name


class GSTRate(models.Model):
    code = models.CharField(max_length=40, unique=True)
    description = models.CharField(max_length=200)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    utgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cess_type = models.CharField(max_length=20, default='percentage')
    taxable = models.BooleanField(default=True)
    zero_rated = models.BooleanField(default=False)
    exempt = models.BooleanField(default=False)
    nil_rated = models.BooleanField(default=False)
    non_gst = models.BooleanField(default=False)
    reverse_charge = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return self.code


class HSNCode(models.Model):
    code = models.CharField(max_length=20, unique=True)
    description = models.CharField(max_length=250)
    chapter = models.CharField(max_length=10, blank=True)
    heading = models.CharField(max_length=10, blank=True)
    subheading = models.CharField(max_length=10, blank=True)
    gst_group = models.ForeignKey(GSTGroup, on_delete=models.PROTECT, null=True, blank=True, related_name='hsn_codes')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return f'{self.code} - {self.description}'


class SACCode(models.Model):
    code = models.CharField(max_length=20, unique=True)
    description = models.CharField(max_length=250)
    service_category = models.CharField(max_length=150, blank=True)
    gst_group = models.ForeignKey(GSTGroup, on_delete=models.PROTECT, null=True, blank=True, related_name='sac_codes')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return f'{self.code} - {self.description}'


class SupplyType(models.Model):
    code = models.CharField(max_length=30, unique=True)
    description = models.CharField(max_length=150)
    status = models.CharField(max_length=20, default='active')

    def __str__(self):
        return self.code


class GSTTaxRule(models.Model):
    code = models.CharField(max_length=40, unique=True)
    description = models.CharField(max_length=250)
    priority = models.PositiveIntegerField(default=100)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    supply_type = models.ForeignKey(SupplyType, on_delete=models.PROTECT, null=True, blank=True, related_name='tax_rules')
    rate = models.ForeignKey(GSTRate, on_delete=models.PROTECT, null=True, blank=True, related_name='tax_rules')
    status = models.CharField(max_length=20, default='draft')
    approved = models.BooleanField(default=False)

    def __str__(self):
        return self.code


class GSTTaxSnapshot(models.Model):
    source_document_type = models.CharField(max_length=50)
    source_document_id = models.PositiveBigIntegerField()
    transaction_date = models.DateField()
    place_of_supply = models.CharField(max_length=3, blank=True)
    supply_type = models.CharField(max_length=30)
    tax_rule = models.ForeignKey(GSTTaxRule, on_delete=models.PROTECT, null=True, blank=True, related_name='snapshots')
    rate_code = models.CharField(max_length=40, blank=True)
    taxable_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    explanation = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)


class GSTLedgerEntry(models.Model):
    entry_no = models.CharField(max_length=60, unique=True)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='gst_ledger_entries')
    registration = models.ForeignKey(GSTRegistration, on_delete=models.PROTECT, related_name='ledger_entries')
    posting_date = models.DateField()
    document_type = models.CharField(max_length=50)
    document_no = models.CharField(max_length=100)
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    rcm = models.BooleanField(default=False)
    itc_eligible = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    itc_ineligible = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    place_of_supply = models.CharField(max_length=3, blank=True)
    supply_type = models.CharField(max_length=30)
    source_document_type = models.CharField(max_length=50, blank=True)
    source_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='gst_ledger_entries')
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ['-posting_date', '-id']


class Warehouse(models.Model):
    LOCATION_TYPE_CHOICES = [
        ('warehouse', 'Warehouse'),
        ('store', 'Store'),
        ('ecommerce', 'E-commerce'),
        ('factory', 'Factory'),
        ('3pl', '3PL'),
        ('return_center', 'Return Center'),
        ('quarantine', 'Quarantine'),
        ('transit', 'Transit'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='warehouses')
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    location_type = models.CharField(max_length=30, choices=LOCATION_TYPE_CHOICES, default='warehouse')
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=5, default='27')
    country = models.CharField(max_length=3, default='IN')
    pincode = models.CharField(max_length=10, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    timezone = models.CharField(max_length=100, default='Asia/Kolkata')
    bin_mandatory = models.BooleanField(default=True)
    directed_putaway = models.BooleanField(default=False)
    directed_pick = models.BooleanField(default=False)
    require_receipt = models.BooleanField(default=True)
    require_putaway = models.BooleanField(default=True)
    require_pick = models.BooleanField(default=True)
    require_shipment = models.BooleanField(default=True)
    require_inventory_movement = models.BooleanField(default=True)
    allow_negative_inventory = models.BooleanField(default=False)
    allow_cross_docking = models.BooleanField(default=False)
    allow_replenishment = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=[('draft', 'Draft'), ('active', 'Active'), ('blocked', 'Blocked')], default='draft')
    is_active = models.BooleanField(default=True)
    manager = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='managed_warehouses')
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.name} ({self.code})'


class WarehouseZone(models.Model):
    ZONE_TYPE_CHOICES = [
        ('receiving', 'Receiving'),
        ('putaway', 'Put-away'),
        ('bulk', 'Bulk'),
        ('picking', 'Picking'),
        ('fast_pick', 'Fast Pick'),
        ('reserve', 'Reserve'),
        ('replenishment', 'Replenishment'),
        ('qc', 'QC'),
        ('quarantine', 'Quarantine'),
        ('damage', 'Damage'),
        ('return', 'Return'),
        ('staging', 'Staging'),
        ('shipping', 'Shipping'),
        ('cross_dock', 'Cross Dock'),
        ('packing', 'Packing'),
        ('scrap', 'Scrap'),
    ]

    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='zones')
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    zone_type = models.CharField(max_length=30, choices=ZONE_TYPE_CHOICES, default='bulk')
    priority = models.PositiveIntegerField(default=100)
    temperature_controlled = models.BooleanField(default=False)
    security_level = models.PositiveSmallIntegerField(default=0)
    allow_pick = models.BooleanField(default=True)
    allow_putaway = models.BooleanField(default=True)
    allow_movement = models.BooleanField(default=True)
    allow_replenishment = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=[('draft', 'Draft'), ('active', 'Active'), ('blocked', 'Blocked')], default='draft')

    class Meta:
        unique_together = ('warehouse', 'code')
        ordering = ['priority', 'code']

    def __str__(self):
        return f'{self.warehouse.code}-{self.code}'


class BinLocation(models.Model):
    BIN_TYPE_CHOICES = [
        ('bulk', 'Bulk'),
        ('pick', 'Pick'),
        ('putaway', 'Putaway'),
        ('replenishment', 'Replenishment'),
        ('damaged', 'Damaged'),
        ('receiving', 'Receiving'),
        ('shipping', 'Shipping'),
        ('quarantine', 'Quarantine'),
        ('return', 'Return'),
        ('staging', 'Staging'),
        ('scrap', 'Scrap'),
    ]
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='bins')
    zone = models.ForeignKey(WarehouseZone, on_delete=models.SET_NULL, null=True, blank=True, related_name='bins')
    code = models.CharField(max_length=50)
    bin_type = models.CharField(max_length=30, choices=BIN_TYPE_CHOICES, default='bulk')
    aisle = models.CharField(max_length=30, blank=True)
    rack = models.CharField(max_length=30, blank=True)
    level = models.CharField(max_length=30, blank=True)
    position = models.CharField(max_length=30, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    rank = models.PositiveIntegerField(default=100)
    capacity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    capacity_unit = models.CharField(max_length=20, default='piece')
    max_weight = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    max_volume = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    current_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    current_weight = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    current_volume = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    allow_mixed_items = models.BooleanField(default=True)
    allow_mixed_lots = models.BooleanField(default=True)
    allow_mixed_serials = models.BooleanField(default=False)
    allow_putaway = models.BooleanField(default=True)
    allow_pick = models.BooleanField(default=True)
    allow_count = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('warehouse', 'code')

    def __str__(self):
        return f'{self.warehouse.code}-{self.code}'


class WarehouseUserAssignment(models.Model):
    OPERATION_CHOICES = [
        ('receive', 'Receive'),
        ('putaway', 'Put-away'),
        ('pick', 'Pick'),
        ('move', 'Move'),
        ('count', 'Count'),
        ('qc', 'QC'),
        ('ship', 'Ship'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='warehouse_assignments')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='user_assignments')
    zone = models.ForeignKey(WarehouseZone, on_delete=models.CASCADE, null=True, blank=True, related_name='user_assignments')
    operation = models.CharField(max_length=20, choices=OPERATION_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        unique_together = ('user', 'warehouse', 'zone', 'operation')


class WarehouseMovement(models.Model):
    MOVEMENT_TYPE_CHOICES = [
        ('pick', 'Pick'),
        ('put', 'Put'),
        ('transfer', 'Transfer'),
        ('adjustment', 'Adjustment'),
    ]
    item = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='warehouse_movements')
    batch = models.ForeignKey(ItemBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='warehouse_movements')
    serial = models.ForeignKey(ItemSerial, on_delete=models.SET_NULL, null=True, blank=True, related_name='warehouse_movements')
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPE_CHOICES)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    from_bin = models.ForeignKey(BinLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name='movements_from')
    to_bin = models.ForeignKey(BinLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name='movements_to')
    reference = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.movement_type.upper()} - {self.item.name}'


class InventoryMovement(models.Model):
    MOVEMENT_TYPE_CHOICES = [
        ('inward', 'Inward'),
        ('outward', 'Outward'),
        ('exchange', 'Exchange'),
        ('adjustment', 'Adjustment'),
    ]
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='movements')
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPE_CHOICES)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def post(self):
        if self.quantity <= 0:
            raise ValueError('Movement quantity must be greater than zero.')

        current_stock = self.product.stock_quantity or Decimal('0.00')
        if self.movement_type == 'outward':
            if current_stock < self.quantity:
                raise ValueError('Insufficient stock for outward movement.')
            new_stock = current_stock - self.quantity
            direction = 'outward'
        elif self.movement_type in {'inward', 'adjustment'}:
            new_stock = current_stock + self.quantity
            direction = 'inward'
        else:
            new_stock = current_stock
            direction = self.movement_type

        self.product.stock_quantity = new_stock
        self.product.save(update_fields=['stock_quantity'])

        StockLedger.objects.create(
            product=self.product,
            movement_type=direction,
            quantity=self.quantity,
            rate=self.product.purchase_price or Decimal('0.00'),
            balance=self.product.stock_quantity,
            reference=self.reference,
        )
        return self


class SalesInvoice(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('completed', 'Completed'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
        ('overpaid', 'Overpaid'),
    ]
    invoice_no = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    sales_order = models.ForeignKey('SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    quotation = models.ForeignKey('Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid')
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sales_date = models.DateTimeField(default=dj_timezone.now)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    notes = models.TextField(blank=True)
    terms_conditions = models.TextField(blank=True, default='1. Goods once sold will not be taken back.\n2. All disputes are subject to jurisdiction of the applicable court.\n3. Payment due immediately upon invoice issue.')
    place_of_supply = models.CharField(max_length=100, blank=True)
    customer_phone = models.CharField(max_length=15, blank=True)
    customer_gstin = models.CharField(max_length=15, blank=True)
    invoice_type = models.CharField(max_length=20, default='sales')
    cashier_staff = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True, related_name='cashier_invoices')
    sales_staff = models.ForeignKey(POSStaff, on_delete=models.PROTECT, null=True, blank=True, related_name='sales_staff_invoices')
    pos_terminal = models.ForeignKey(POSTerminal, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    pos_session = models.ForeignKey(POSSession, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    cashier_name_snapshot = models.CharField(max_length=200, blank=True)
    sales_staff_name_snapshot = models.CharField(max_length=200, blank=True)
    cashier_role_snapshot = models.CharField(max_length=50, blank=True)
    store_code_snapshot = models.CharField(max_length=30, blank=True)
    terminal_code_snapshot = models.CharField(max_length=30, blank=True)
    cashier_code_snapshot = models.CharField(max_length=50, blank=True)
    store_name_snapshot = models.CharField(max_length=200, blank=True)
    location_code_snapshot = models.CharField(max_length=30, blank=True)
    location_name_snapshot = models.CharField(max_length=200, blank=True)
    terminal_name_snapshot = models.CharField(max_length=100, blank=True)
    print_type_snapshot = models.CharField(max_length=40, default='A4_INVOICE')
    print_layout_snapshot = models.CharField(max_length=40, blank=True)
    print_layout_version_snapshot = models.PositiveIntegerField(default=1)

    @property
    def roundoff_total(self):
        return self.total_amount.quantize(Decimal('0.01'))

    @property
    def balance_amount(self):
        return max(self.total_amount - self.paid_amount, Decimal('0')).quantize(Decimal('0.01'))

    @property
    def cgst_amount(self):
        return (self.gst_amount / Decimal('2')).quantize(Decimal('0.01'))

    @property
    def sgst_amount(self):
        return (self.gst_amount / Decimal('2')).quantize(Decimal('0.01'))

    def __str__(self):
        return self.invoice_no


class SalesInvoiceItem(models.Model):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    barcode = models.CharField(max_length=100, blank=True)
    serial_no = models.CharField(max_length=100, blank=True)
    batch_no = models.CharField(max_length=100, blank=True)
    stone_name = models.CharField(max_length=100, blank=True)
    stone_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    making_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    jewellery_unit = models.ForeignKey(JewelleryItemUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='sold_invoice_items')
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    stone_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    net_metal_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    metal_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    metal_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wastage_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    diamond_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gemstone_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    hallmark_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    certification_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    pricing_snapshot = models.JSONField(default=dict, blank=True)

    @property
    def gst_amount(self):
        return (self.cgst_amount + self.sgst_amount + self.igst_amount).quantize(Decimal('0.01'))


class SalesReturn(models.Model):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name='returns')
    return_date = models.DateTimeField(default=dj_timezone.now)
    reason = models.TextField(blank=True)
    total_return_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, default='requested')
    notes = models.TextField(blank=True)

    def __str__(self):
        return f'{self.invoice.invoice_no} return'


class InvoiceSetting(models.Model):
    company_name = models.CharField(max_length=200, default='Goldi ERP')
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=5, default='27')
    phone = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    terms_and_conditions = models.TextField(blank=True, default='1. Goods once sold will not be taken back.\n2. Subject to the jurisdiction of the local court.\n3. Payment is due immediately upon invoice issue.')
    is_default = models.BooleanField(default=True)

    def __str__(self):
        return self.company_name


class SalesApproval(models.Model):
    APPROVAL_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    invoice = models.OneToOneField(SalesInvoice, on_delete=models.CASCADE, related_name='approval')
    status = models.CharField(max_length=20, choices=APPROVAL_STATUS_CHOICES, default='pending')
    approved_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_sales')
    approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.invoice.invoice_no} - {self.status}'


class JewelleryLineMixin(models.Model):
    """Shared pricing/jewellery fields for Quotation and Sales Order lines, mirroring SalesInvoiceItem."""
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    stone_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    net_metal_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    metal_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    metal_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wastage_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    making_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    stone_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    hallmark_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    certification_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    pricing_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True

    @property
    def gst_amount(self):
        return (self.cgst_amount + self.sgst_amount + self.igst_amount).quantize(Decimal('0.01'))


class Quotation(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('sent', 'Sent'),
        ('customer_accepted', 'Customer Accepted'),
        ('expired', 'Expired'),
        ('converted', 'Converted'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    quotation_no = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='quotations')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='quotations')
    salesperson = models.ForeignKey(POSStaff, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations')
    quotation_date = models.DateTimeField(default=dj_timezone.now)
    valid_until = models.DateField(null=True, blank=True)
    payment_terms = models.ForeignKey(PaymentTerm, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations')
    delivery_terms = models.TextField(blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations_created')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.quotation_no


class QuotationLine(JewelleryLineMixin):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='quotation_lines')
    jewellery_unit = models.ForeignKey(JewelleryItemUnit, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    converted_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    @property
    def remaining_quantity(self):
        return max(self.quantity - self.converted_quantity, Decimal('0'))


class SalesOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('released', 'Released'),
        ('partially_fulfilled', 'Partially Fulfilled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    order_no = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='sales_orders')
    quotation = models.ForeignKey(Quotation, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='sales_orders')
    salesperson = models.ForeignKey(POSStaff, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders')
    order_date = models.DateTimeField(default=dj_timezone.now)
    expected_delivery_date = models.DateField(null=True, blank=True)
    payment_terms = models.ForeignKey(PaymentTerm, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders')
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders_created')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.order_no


class SalesOrderLine(JewelleryLineMixin):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='lines')
    quotation_line = models.ForeignKey(QuotationLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_order_lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales_order_lines')
    jewellery_unit = models.ForeignKey(JewelleryItemUnit, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reserved_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    invoiced_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    cancelled_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    @property
    def remaining_quantity(self):
        return max(self.quantity - self.invoiced_quantity - self.cancelled_quantity, Decimal('0'))


class PaymentReceipt(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]
    receipt_no = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='payment_receipts')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='payment_receipts')
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_receipts')
    bank_account = models.ForeignKey('BankAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_receipts')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    receipt_date = models.DateTimeField(default=dj_timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    reference_no = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_receipts_created')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def allocated_amount(self):
        return self.allocations.aggregate(total=models.Sum('allocated_amount'))['total'] or Decimal('0')

    @property
    def unapplied_amount(self):
        return max(self.amount - self.allocated_amount, Decimal('0'))

    def __str__(self):
        return self.receipt_no


class PaymentAllocation(models.Model):
    receipt = models.ForeignKey(PaymentReceipt, on_delete=models.CASCADE, related_name='allocations')
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name='receipt_allocations')
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('receipt', 'invoice'), name='unique_payment_allocation_per_invoice')]

    def __str__(self):
        return f'{self.receipt.receipt_no} -> {self.invoice.invoice_no}: {self.allocated_amount}'


class ExchangeTransaction(models.Model):
    METAL_CHOICES = [
        ('gold', 'Gold'),
        ('silver', 'Silver'),
    ]
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    metal_type = models.CharField(max_length=20, choices=METAL_CHOICES)
    weight_grams = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    old_product_name = models.CharField(max_length=200)
    market_rate_per_gram = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    exchange_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    transaction_date = models.DateTimeField(default=dj_timezone.now)
    notes = models.TextField(blank=True)
    is_approved = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.customer.name} - {self.metal_type}'


class BankAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('saving', 'Saving'),
        ('current', 'Current'),
        ('cash', 'Cash'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='bank_accounts')
    bank_name = models.CharField(max_length=200)
    branch_name = models.CharField(max_length=200, blank=True)
    account_name = models.CharField(max_length=200)
    account_number = models.CharField(max_length=50, unique=True)
    ifsc_code = models.CharField(max_length=20, blank=True)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='current')
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    gl_account = models.ForeignKey('GLAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_accounts')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.bank_name} - {self.account_name}'


class Payment(models.Model):
    MODE_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Card'),
        ('upi', 'UPI'),
        ('bank_transfer', 'Bank Transfer'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('posted', 'Posted'),
        ('reversed', 'Reversed'),
    ]

    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='payments')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    payment_method = models.CharField(max_length=30, choices=MODE_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payment_date = models.DateTimeField(default=dj_timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def post(self):
        if self.status == 'posted':
            return self

        if self.bank_account is not None:
            self.bank_account.current_balance = (self.bank_account.current_balance or Decimal('0.00')) + self.amount
            self.bank_account.save(update_fields=['current_balance', 'updated_at'])
            BankTransaction.objects.create(
                bank_account=self.bank_account,
                payment=self,
                transaction_type='credit',
                amount=self.amount,
                description=f'Payment received against invoice {self.invoice.invoice_no}',
                reference=self.reference,
            )

        self.status = 'posted'
        self.payment_date = dj_timezone.now()
        self.save(update_fields=['status', 'payment_date', 'updated_at'])
        return self

    def __str__(self):
        return f'{self.invoice.invoice_no} - {self.amount}'


class BankTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
    ]

    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='transactions')
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES, default='credit')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    description = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return f'{self.bank_account.account_name} - {self.transaction_type}'


class TaxLedger(models.Model):
    tax_type = models.CharField(max_length=50)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    base_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)


class SupplierInvoice(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('partial', 'Partial'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
        ('cancelled', 'Cancelled'),
    ]

    WORKFLOW_STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='supplier_invoices')
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='invoices')
    document_no = models.CharField(max_length=50, unique=True, null=True, blank=True)
    purchase_order = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    goods_receipt = models.ForeignKey('GoodsReceipt', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    workflow_status = models.CharField(max_length=30, choices=WORKFLOW_STATUS_CHOICES, default='draft')
    invoice_no = models.CharField(max_length=100, unique=True)
    invoice_date = models.DateField(default=date.today)
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def outstanding_amount(self):
        return self.net_amount - self.paid_amount

    def apply_payment(self, amount):
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError('Payment amount must be greater than zero.')

        remaining = self.outstanding_amount
        if amount > remaining:
            raise ValueError('Payment exceeds outstanding amount.')

        self.paid_amount += amount
        if self.outstanding_amount <= 0:
            self.status = 'paid'
        elif self.paid_amount > 0:
            self.status = 'partial'
        self.save(update_fields=['paid_amount', 'status', 'updated_at'])
        return self

    def __str__(self):
        return f'{self.invoice_no} - {self.supplier.name}'


class SupplierInvoiceLine(models.Model):
    supplier_invoice = models.ForeignKey(SupplierInvoice, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_invoice_lines')
    description = models.CharField(max_length=250, blank=True)
    purchase_order_line = models.ForeignKey('PurchaseOrderLine', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_lines')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18.00'))
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    @property
    def gst_amount(self):
        return (self.cgst_amount + self.sgst_amount + self.igst_amount).quantize(Decimal('0.01'))


class VendorPayment(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]
    payment_no = models.CharField(max_length=50, unique=True)
    vendor = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='payments_made')
    bank_account = models.ForeignKey('BankAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='vendor_payments')
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True, related_name='vendor_payments')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payment_date = models.DateTimeField(default=dj_timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    reference_no = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='vendor_payments_created')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def allocated_amount(self):
        return self.allocations.aggregate(total=models.Sum('allocated_amount'))['total'] or Decimal('0')

    @property
    def unapplied_amount(self):
        return max(self.amount - self.allocated_amount, Decimal('0'))

    def __str__(self):
        return self.payment_no


class VendorPaymentAllocation(models.Model):
    payment = models.ForeignKey(VendorPayment, on_delete=models.CASCADE, related_name='allocations')
    supplier_invoice = models.ForeignKey(SupplierInvoice, on_delete=models.PROTECT, related_name='payment_allocations')
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('payment', 'supplier_invoice'), name='unique_vendor_payment_allocation_per_invoice')]

    def __str__(self):
        return f'{self.payment.payment_no} -> {self.supplier_invoice.invoice_no}: {self.allocated_amount}'


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('sent_to_vendor', 'Sent to Vendor'),
        ('partially_received', 'Partially Received'),
        ('fully_received', 'Fully Received'),
        ('cancelled', 'Cancelled'),
    ]
    order_no = models.CharField(max_length=50, unique=True)
    vendor = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    warehouse = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    buyer = models.ForeignKey('Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    order_date = models.DateTimeField(default=dj_timezone.now)
    expected_delivery_date = models.DateField(null=True, blank=True)
    payment_terms = models.ForeignKey(PaymentTerm, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders_created')
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.order_no


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='purchase_order_lines')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18.00'))
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    received_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    invoiced_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    cancelled_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    @property
    def gst_amount(self):
        return (self.cgst_amount + self.sgst_amount + self.igst_amount).quantize(Decimal('0.01'))

    @property
    def remaining_quantity(self):
        return max(self.quantity - self.received_quantity - self.cancelled_quantity, Decimal('0'))

    @property
    def remaining_to_invoice(self):
        return max(self.received_quantity - self.invoiced_quantity, Decimal('0'))


class GoodsReceipt(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]
    receipt_no = models.CharField(max_length=50, unique=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name='goods_receipts')
    vendor = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='goods_receipts')
    warehouse = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='goods_receipts')
    receipt_date = models.DateTimeField(default=dj_timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='goods_receipts_received')
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.receipt_no


class GoodsReceiptLine(models.Model):
    goods_receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name='lines')
    purchase_order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name='receipt_lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='goods_receipt_lines')
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantity_rejected = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    rejection_reason = models.CharField(max_length=200, blank=True)

    @property
    def accepted_quantity(self):
        return max(self.quantity_received - self.quantity_rejected, Decimal('0'))


class StockLedger(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='stock_ledger')
    movement_type = models.CharField(max_length=20, default='inward')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    reference = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)


class Expense(models.Model):
    category = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    expense_date = models.DateTimeField(default=dj_timezone.now)
    notes = models.TextField(blank=True)


class LedgerEntry(models.Model):
    account = models.CharField(max_length=100)
    entry_type = models.CharField(max_length=20, default='debit')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)
    notes = models.TextField(blank=True)
