"""Goldio Job Work & Subcontracting.

Job work (goods of the principal processed by an outside job worker) and subcontracting (a routing operation of a
production order performed by a vendor) share one document model, kept distinguishable by ``order_kind`` and
``transaction_type``. Nothing here moves stock or money directly:

* stock moves through ``inventory.engine.InventoryPostingEngine`` into a real inventory Location of type JOB_WORKER,
  so the physical position changes while ownership and historical cost do not;
* production-linked consumption, loss, output and subcontract cost go through
  ``manufacturing.engine.ManufacturingPostingEngine``;
* G/L vouchers go through the ERP finance engine.

``JobWorkerStockEntry`` is the job-worker stock ledger (owner, custodian, weights, value per order line). Posted
ledgers are immutable - corrections are reversals. GST rates, statutory periods, e-way bill thresholds and ITC-04
frequency are effective-dated, approval-gated master data (``TaxRate``, ``ComplianceRule``), never code.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone as dj_timezone

from inventory.models import METALS, MONEY, QTY, WEIGHT, ZERO, TenantModel, TenantQuerySet

USER = settings.AUTH_USER_MODEL
RATE = dict(max_digits=14, decimal_places=4, default=0)
PERCENT = dict(max_digits=7, decimal_places=3, default=0)
TAX_PERCENT = dict(max_digits=6, decimal_places=3, default=0)


class JWModel(TenantModel):
    """Tenant-scoped row with the owning company (the ERP legal entity) alongside the audit columns."""
    company = models.ForeignKey('erp.Company', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        abstract = True


class PostedQuerySet(TenantQuerySet):
    def delete(self):
        raise ValidationError('Posted job work entries are immutable; post a reversal instead.')


class PostedModel(JWModel):
    """Posted ledger rows: never updated (except the `reversed` marker) and never deleted."""
    MUTABLE = {'reversed', 'updated_at', 'updated_by'}
    posting_date = models.DateField()
    reversed = models.BooleanField(default=False)
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversals')

    objects = PostedQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if self.pk and not (update_fields and set(update_fields) <= self.MUTABLE):
            raise ValidationError('Posted job work entries are immutable; post a reversal instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Posted job work entries are immutable; post a reversal instead.')


RATE_BASES = [('PER_PIECE', 'Per piece'), ('PER_GRAM', 'Per gram'), ('PER_CARAT', 'Per carat'), ('PER_OPERATION', 'Per operation'),
              ('HOURLY', 'Hourly'), ('FIXED', 'Fixed charge per job'), ('PERCENT', 'Percentage of base value')]
TRANSACTION_TYPES = [('JOB_WORK', 'Job work'), ('SUBCONTRACTING', 'Subcontracting'), ('REPAIR_JOB_WORK', 'Repair job work'),
                     ('REWORK_SUBCONTRACT', 'Rework subcontract'), ('PROCESSING', 'Processing'), ('STONE_SETTING', 'Stone setting'),
                     ('CASTING', 'Casting'), ('POLISHING', 'Polishing'), ('PLATING', 'Plating'), ('MELTING', 'Melting'), ('ASSAY', 'Assay'),
                     ('HALLMARKING', 'Hallmarking'), ('OTHER', 'Other')]
GOODS_CATEGORIES = [('INPUTS', 'Inputs'), ('CAPITAL_GOODS', 'Capital goods (moulds, dies, jigs, fixtures, tools)')]
LOSS_CLASSES = [('PROCESS', 'Process loss'), ('RECOVERABLE', 'Recoverable loss'), ('NON_RECOVERABLE', 'Non-recoverable loss'),
                ('UNEXPLAINED', 'Unexplained shortage'), ('PRINCIPAL', 'Customer / principal loss'),
                ('JOB_WORKER_LIABILITY', 'Job worker liability')]
SUPPLY_METHODS = [('PRINCIPAL', 'Principal supplied'), ('VENDOR', 'Vendor supplied'), ('CONSIGNMENT', 'Consignment (principal-owned at JW)'),
                  ('DIRECT_PURCHASE', 'Direct purchase to job worker'), ('CUSTOMER', 'Customer supplied')]
OWNERS = [('PRINCIPAL', 'Principal (Goldio)'), ('VENDOR', 'Job worker / vendor'), ('CUSTOMER', 'Customer')]
DOC_STATUSES = [('DRAFT', 'Draft'), ('POSTED', 'Posted'), ('REVERSED', 'Reversed'), ('CANCELLED', 'Cancelled')]


# ---------------------------------------------------------------------------
# Setup, security, approval matrix
# ---------------------------------------------------------------------------

class JobWorkSetup(JWModel):
    enabled = models.BooleanField(default=True)
    principal_gstin = models.CharField(max_length=15, blank=True, help_text='Blank = the company GSTIN.')
    principal_state_code = models.CharField(max_length=2, blank=True, help_text='Blank = first two digits of the principal GSTIN.')
    aggregate_turnover_previous_fy = models.DecimalField(**MONEY, help_text='Drives the ITC-04 frequency rule.')
    transit_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                         help_text='Blank = the tenant IN-TRANSIT location.')
    default_source_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_return_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_return_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    require_order_approval = models.BooleanField(default=True)
    allow_self_approval = models.BooleanField(default=False, help_text='Maker-checker: when off, the approver must differ from the maker.')
    require_qc_on_receipt = models.BooleanField(default=True)
    weight_tolerance = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.001'),
                                           help_text='Grams of rounding tolerated before a reconciliation difference is raised.')
    invoice_tolerance_amount = models.DecimalField(**MONEY)
    allow_close_without_invoice = models.BooleanField(default=False)
    require_scale_weight = models.BooleanField(default=False, help_text='Dispatch/receipt weights must come from a scale capture.')
    cost_allocation_method = models.CharField(max_length=20, default='WEIGHT', choices=[
        ('QUANTITY', 'Quantity'), ('WEIGHT', 'Weight'), ('VALUE', 'Value'), ('STANDARD', 'Standard cost')])
    selection_weights = models.JSONField(default=dict, blank=True,
                                         help_text='Job worker ranking weights, e.g. {"price": 0.4, "quality": 0.3, "on_time": 0.2, "lead_time": 0.1}.')
    auto_select_lowest_price = models.BooleanField(default=False)
    # Financial posting (through the ERP finance engine)
    gl_posting_enabled = models.BooleanField(default=False)
    job_work_cost_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                              help_text='Dr for job work charges of non-production orders.')
    gst_input_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gst_rcm_payable_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    vendor_payable_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                               help_text='Fallback when the vendor has no posting group.')
    recovery_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                         help_text='Cr for debit notes (job worker recoveries).')
    freight_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'job_work_setup'
        constraints = [models.UniqueConstraint(fields=('tenant',), name='jw_unique_setup_per_tenant')]


class JobWorkRole(JWModel):
    ROLES = [('OPERATOR', 'Job work operator'), ('SUPERVISOR', 'Supervisor'), ('MANAGER', 'Manager'),
             ('HEAD_OF_MANUFACTURING', 'Head of manufacturing'), ('QUALITY', 'Quality inspector'), ('FINANCE', 'Finance'),
             ('TAX_ADMIN', 'Tax administrator'), ('AUDITOR', 'Auditor (read-only)')]
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='job_work_roles')
    role = models.CharField(max_length=30, choices=ROLES)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'job_work_roles'
        constraints = [models.UniqueConstraint(fields=('tenant', 'user', 'role'), name='jw_unique_user_role')]


class ApprovalRule(JWModel):
    """Approval matrix: who must approve, by metric band. Bands are examples only - fully configurable."""
    METRICS = [('ORDER_VALUE', 'Job work order value'), ('MATERIAL_VALUE', 'Material value'), ('METAL_WEIGHT', 'Metal weight (g)'),
               ('LOSS_PERCENT', 'Loss %'), ('VENDOR_INVOICE', 'Vendor invoice amount'), ('SCRAP_VALUE', 'Scrap value'),
               ('DEBIT_NOTE', 'Debit note amount'), ('ADJUSTMENT', 'Manual adjustment value'), ('EXCEPTION', 'Any exception')]
    metric = models.CharField(max_length=20, choices=METRICS)
    min_value = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    max_value = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text='Blank = no upper limit.')
    required_role = models.CharField(max_length=30, choices=JobWorkRole.ROLES)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'job_work_approval_rules'
        ordering = ['metric', 'min_value']


# ---------------------------------------------------------------------------
# GST tax master and compliance rule engine (effective-dated, approval-gated)
# ---------------------------------------------------------------------------

MASTER_STATUSES = [('DRAFT', 'Draft - awaiting tax administrator'), ('APPROVED', 'Approved'), ('RETIRED', 'Retired')]


class TaxRate(JWModel):
    """Effective-dated GST rate for one HSN/SAC. Historical documents keep their JobWorkTaxSnapshot; a new rate is a new row."""
    code = models.CharField(max_length=20, help_text='HSN or SAC code, e.g. 9988.')
    code_type = models.CharField(max_length=3, choices=[('HSN', 'HSN'), ('SAC', 'SAC')], default='SAC')
    description = models.CharField(max_length=250)
    taxability = models.CharField(max_length=20, default='TAXABLE', choices=[
        ('TAXABLE', 'Taxable'), ('EXEMPT', 'Exempt'), ('NIL', 'Nil-rated'), ('NON_GST', 'Non-GST'), ('ZERO', 'Zero-rated')])
    cgst_rate = models.DecimalField(**TAX_PERCENT)
    sgst_rate = models.DecimalField(**TAX_PERCENT)
    utgst_rate = models.DecimalField(**TAX_PERCENT)
    igst_rate = models.DecimalField(**TAX_PERCENT)
    cess_rate = models.DecimalField(**TAX_PERCENT)
    reverse_charge = models.BooleanField(default=False)
    applies_to = models.CharField(max_length=30, blank=True, help_text='Optional transaction type filter (e.g. JOB_WORK); blank = all.')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    notification = models.CharField(max_length=200, blank=True)
    circular = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=MASTER_STATUSES, default='DRAFT')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'job_work_tax_rates'
        ordering = ['code', '-effective_from']
        indexes = [models.Index(fields=('tenant', 'code', 'effective_from'))]

    def __str__(self):
        return f'{self.code} {self.igst_rate}% from {self.effective_from}'

    def clean(self):
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({'effective_to': 'Effective-to cannot precede effective-from.'})


class ComplianceRule(JWModel):
    """GST compliance rule engine. Each rule type reads its `parameters`:

    RETURN_PERIOD         {"goods_category": "INPUTS", "period_months": 12}
    EWAY_BILL             {"threshold_value": "50000", "movement_types": ["PRINCIPAL_TO_JW", ...]}
    ITC04_FREQUENCY       {"turnover_threshold": "50000000", "above": "HALF_YEARLY", "at_or_below": "ANNUAL"}
    ALERT_THRESHOLDS      {"review_days": 90, "warning_days": 180, "critical_days": 270, "due_soon_days": 30}
    EINVOICE              {"turnover_threshold": "50000000"}
    DELIVERY_CHALLAN      {"required": true}
    """
    TYPES = [('RETURN_PERIOD', 'Job work return period'), ('EWAY_BILL', 'E-way bill applicability'),
             ('ITC04_FREQUENCY', 'ITC-04 filing frequency'), ('ALERT_THRESHOLDS', 'Compliance alert thresholds'),
             ('EINVOICE', 'E-invoice applicability'), ('DELIVERY_CHALLAN', 'Delivery challan requirement')]
    rule_code = models.CharField(max_length=40)
    rule_type = models.CharField(max_length=20, choices=TYPES)
    description = models.CharField(max_length=250)
    parameters = models.JSONField(default=dict)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    notification = models.CharField(max_length=200, blank=True)
    circular = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=MASTER_STATUSES, default='DRAFT')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'job_work_compliance_rules'
        ordering = ['rule_type', '-effective_from']
        constraints = [models.UniqueConstraint(fields=('tenant', 'rule_code'), name='jw_unique_rule_code')]

    def __str__(self):
        return f'{self.rule_code} ({self.get_rule_type_display()})'


# ---------------------------------------------------------------------------
# Job worker masters
# ---------------------------------------------------------------------------

SPECIALIZATIONS = ['GOLD', 'SILVER', 'DIAMOND', 'STONE_SETTING', 'CASTING', 'POLISHING', 'PLATING', 'RHODIUM', 'MELTING', 'REPAIR',
                   'MANUFACTURING', 'ASSAY', 'HALLMARKING']


class JobWorker(JWModel):
    REGISTRATION_TYPES = [('REGULAR', 'Regular'), ('COMPOSITION', 'Composition'), ('UNREGISTERED', 'Unregistered'), ('SEZ', 'SEZ'),
                          ('OTHER', 'Other')]
    code = models.CharField(max_length=20)
    supplier = models.ForeignKey('erp.Supplier', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 help_text='The vendor record for payables. One vendor may be supplier, job worker and subcontractor.')
    subcontractor = models.ForeignKey('manufacturing.Subcontractor', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                      help_text='Routing subcontractor this job worker performs for.')
    legal_name = models.CharField(max_length=200)
    trade_name = models.CharField(max_length=200, blank=True)
    business_type = models.CharField(max_length=60, blank=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    # GST
    gst_registered = models.BooleanField(default=True)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    registration_type = models.CharField(max_length=20, choices=REGISTRATION_TYPES, default='REGULAR')
    state = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(max_length=2, blank=True)
    default_sac = models.CharField(max_length=20, blank=True)
    # Compliance
    job_worker_eligible = models.BooleanField(default=True)
    compliance_status = models.CharField(max_length=20, default='COMPLIANT', choices=[
        ('COMPLIANT', 'Compliant'), ('UNDER_REVIEW', 'Under review'), ('NON_COMPLIANT', 'Non-compliant')])
    registration_valid_until = models.DateField(null=True, blank=True)
    kyc_status = models.CharField(max_length=20, default='PENDING', choices=[('PENDING', 'Pending'), ('VERIFIED', 'Verified'),
                                                                            ('REJECTED', 'Rejected')])
    # Operational
    specializations = models.JSONField(default=list, blank=True)
    daily_capacity_qty = models.DecimalField(**QTY)
    metal_capacity_grams = models.DecimalField(**WEIGHT, help_text='Maximum metal weight held at any time; 0 = unlimited.')
    lead_time_days = models.PositiveSmallIntegerField(default=7)
    processing_days = models.PositiveSmallIntegerField(default=3)
    default_loss_percent = models.DecimalField(**PERCENT)
    max_loss_percent = models.DecimalField(**PERCENT)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'job_workers'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='jw_unique_job_worker_code')]

    def __str__(self):
        return f'{self.code} - {self.trade_name or self.legal_name}'

    def clean(self):
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
            if len(self.gstin) != 15:
                raise ValidationError({'gstin': 'GSTIN must contain 15 characters.'})
            if not self.state_code:
                self.state_code = self.gstin[:2]
            elif self.state_code != self.gstin[:2]:
                raise ValidationError({'state_code': 'State code does not match the GSTIN.'})
        if self.gst_registered and not self.gstin:
            raise ValidationError({'gstin': 'A GST-registered job worker needs a GSTIN.'})

    @property
    def usable(self):
        return self.active and not self.blocked and self.job_worker_eligible

    @property
    def default_location(self):
        link = self.locations.filter(active=True).order_by('-is_default', 'id').select_related('location').first()
        return link.location if link else None


class JobWorkerLocation(JWModel):
    """The controlled inventory location holding material at a job worker's premises (address/GSTIN live on the Location)."""
    job_worker = models.ForeignKey(JobWorker, on_delete=models.CASCADE, related_name='locations')
    location = models.OneToOneField('inventory.Location', on_delete=models.PROTECT, related_name='job_worker_link')
    state_code = models.CharField(max_length=2, blank=True)
    is_default = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'job_worker_locations'

    def __str__(self):
        return f'{self.job_worker.code} @ {self.location.code}'


class JobWorkerWorkCenter(JWModel):
    """Business Central's subcontract work centre: an operation a vendor performs, with its costing."""
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='work_centers')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    manufacturing_work_center = models.ForeignKey('manufacturing.WorkCenter', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    operation_code = models.CharField(max_length=30, blank=True, help_text='CASTING, POLISHING, SETTING...')
    capacity_per_day = models.DecimalField(**QTY)
    costing_method = models.CharField(max_length=20, choices=RATE_BASES, default='PER_PIECE')
    unit_cost = models.DecimalField(**RATE)
    hourly_cost = models.DecimalField(**RATE)
    minimum_charge = models.DecimalField(**MONEY)
    indirect_cost_percent = models.DecimalField(**PERCENT)
    overhead_rate = models.DecimalField(**RATE)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'job_worker_work_centers'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='jw_unique_work_center_code')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class JobWorkerPrice(JWModel):
    """Subcontractor price. Blank dimensions are wildcards; the most specific applicable row wins."""
    job_worker = models.ForeignKey(JobWorker, on_delete=models.CASCADE, related_name='prices')
    work_center = models.ForeignKey(JobWorkerWorkCenter, on_delete=models.CASCADE, null=True, blank=True, related_name='prices')
    item = models.ForeignKey('inventory.Item', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    operation_code = models.CharField(max_length=30, blank=True)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    currency = models.CharField(max_length=10, default='INR')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    minimum_quantity = models.DecimalField(**QTY)
    rate_basis = models.CharField(max_length=20, choices=RATE_BASES, default='PER_PIECE')
    rate = models.DecimalField(**RATE)
    minimum_amount = models.DecimalField(**MONEY)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'job_worker_prices'
        ordering = ['job_worker', 'operation_code', '-effective_from']

    def __str__(self):
        return f'{self.job_worker.code} {self.operation_code or "*"} {self.rate} {self.get_rate_basis_display()}'


class JobWorkerAgreement(JWModel):
    agreement_no = models.CharField(max_length=40)
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='agreements')
    effective_from = models.DateField()
    expires_on = models.DateField(null=True, blank=True)
    operations = models.JSONField(default=list, blank=True)
    rate_terms = models.TextField(blank=True)
    minimum_charge = models.DecimalField(**MONEY)
    expected_loss_percent = models.DecimalField(**PERCENT)
    max_loss_percent = models.DecimalField(**PERCENT)
    weight_tolerance = models.DecimalField(**WEIGHT)
    payment_terms = models.CharField(max_length=120, blank=True)
    quality_terms = models.TextField(blank=True)
    return_period_days = models.PositiveIntegerField(default=0)
    liability_terms = models.TextField(blank=True)
    insurance_terms = models.TextField(blank=True)
    scrap_ownership = models.CharField(max_length=20, choices=OWNERS, default='PRINCIPAL')
    metal_loss_policy = models.TextField(blank=True)
    confidentiality = models.BooleanField(default=True)
    status = models.CharField(max_length=10, default='DRAFT', choices=[('DRAFT', 'Draft'), ('APPROVED', 'Approved'), ('EXPIRED', 'Expired')])
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'job_worker_agreements'
        ordering = ['-effective_from']
        constraints = [models.UniqueConstraint(fields=('tenant', 'agreement_no'), name='jw_unique_agreement_no')]

    def __str__(self):
        return self.agreement_no


# ---------------------------------------------------------------------------
# Job work / subcontract orders
# ---------------------------------------------------------------------------

class JobWorkOrder(JWModel):
    STATUSES = [
        ('DRAFT', 'Draft'), ('PENDING_APPROVAL', 'Pending approval'), ('APPROVED', 'Approved'), ('MATERIAL_PENDING', 'Material pending'),
        ('READY_TO_DISPATCH', 'Ready to dispatch'), ('DISPATCH_CREATED', 'Dispatch created'), ('IN_TRANSIT', 'In transit'),
        ('RECEIVED_BY_JOB_WORKER', 'Received by job worker'), ('PROCESSING', 'Processing'), ('PARTIALLY_COMPLETED', 'Partially completed'),
        ('READY_FOR_RETURN', 'Ready for return'), ('RETURN_IN_TRANSIT', 'Return in transit'), ('PARTIALLY_RETURNED', 'Partially returned'),
        ('RECEIVED', 'Received'), ('QC_PENDING', 'QC pending'), ('QC_PASSED', 'QC passed'), ('QC_FAILED', 'QC failed'), ('REWORK', 'Rework'),
        ('TRANSFERRED', 'Transferred to next job worker'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('CLOSED', 'Closed'),
    ]
    OPEN_STATUSES = ('APPROVED', 'MATERIAL_PENDING', 'READY_TO_DISPATCH', 'DISPATCH_CREATED', 'IN_TRANSIT', 'RECEIVED_BY_JOB_WORKER',
                     'PROCESSING', 'PARTIALLY_COMPLETED', 'READY_FOR_RETURN', 'RETURN_IN_TRANSIT', 'PARTIALLY_RETURNED', 'RECEIVED',
                     'QC_PENDING', 'QC_PASSED', 'QC_FAILED', 'REWORK', 'TRANSFERRED')
    EDITABLE_STATUSES = ('DRAFT',)
    TERMINAL_STATUSES = ('CANCELLED', 'CLOSED')
    KINDS = [('JOB_WORK', 'Job work order'), ('SUBCONTRACT', 'Subcontract order')]
    SOURCES = [('MANUAL', 'Manual requirement'), ('PRODUCTION_ORDER', 'Production order'), ('SALES_ORDER', 'Sales order'),
               ('REPAIR_ORDER', 'Repair order'), ('REWORK', 'Rework order')]
    PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent')]

    order_no = models.CharField(max_length=40)
    order_kind = models.CharField(max_length=20, choices=KINDS, default='JOB_WORK')
    transaction_type = models.CharField(max_length=24, choices=TRANSACTION_TYPES, default='JOB_WORK')
    source_type = models.CharField(max_length=20, choices=SOURCES, default='MANUAL')
    source_no = models.CharField(max_length=60, blank=True)
    production_order = models.ForeignKey('manufacturing.ProductionOrder', on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='job_work_orders')
    operation = models.ForeignKey('manufacturing.ProductionOrderRoutingLine', on_delete=models.PROTECT, null=True, blank=True,
                                  related_name='job_work_orders')
    sales_order = models.ForeignKey('erp.SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    parent = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='children',
                               help_text='Previous stage in multi-level job work.')
    rework_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='rework_orders')
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='orders')
    work_center = models.ForeignKey(JobWorkerWorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    job_worker_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    source_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    source_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    return_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    return_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    operation_code = models.CharField(max_length=30, blank=True)
    description = models.CharField(max_length=200, blank=True)
    goods_category = models.CharField(max_length=20, choices=GOODS_CATEGORIES, default='INPUTS')
    service_sac = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=24, choices=STATUSES, default='DRAFT')
    priority = models.CharField(max_length=10, choices=PRIORITIES, default='NORMAL')
    selection_mode = models.CharField(max_length=20, default='MANUAL', choices=[
        ('FIXED', 'Fixed (routing)'), ('PREFERRED', 'Preferred (suggested)'), ('COMPETITIVE', 'Competitive'), ('MANUAL', 'Manual')])
    # Dates
    order_date = models.DateField(default=dj_timezone.localdate)
    required_date = models.DateField(null=True, blank=True)
    expected_dispatch_date = models.DateField(null=True, blank=True)
    expected_return_date = models.DateField(null=True, blank=True)
    first_dispatch_date = models.DateField(null=True, blank=True)
    actual_return_date = models.DateField(null=True, blank=True)
    compliance_due_date = models.DateField(null=True, blank=True, help_text='Statutory return date from the approved RETURN_PERIOD rule.')
    # Pricing snapshot (from the resolved JobWorkerPrice - never re-priced)
    price = models.ForeignKey(JobWorkerPrice, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    rate_basis = models.CharField(max_length=20, choices=RATE_BASES, default='PER_PIECE')
    rate = models.DecimalField(**RATE)
    minimum_charge = models.DecimalField(**MONEY)
    planned_quantity = models.DecimalField(**QTY, help_text='Pieces / units of work ordered.')
    expected_charge = models.DecimalField(**MONEY)
    material_value = models.DecimalField(**MONEY)
    # Loss tolerance snapshot
    expected_loss_percent = models.DecimalField(**PERCENT)
    max_loss_percent = models.DecimalField(**PERCENT)
    # Output control (Business Central warns about blind final-output posting on subcontract receipt)
    output_confirmation_required = models.BooleanField(default=True)
    qc_required = models.BooleanField(default=True)
    # Progress (maintained by services)
    output_accepted_qty = models.DecimalField(**QTY)
    output_rejected_qty = models.DecimalField(**QTY)
    output_posted_qty = models.DecimalField(**QTY, help_text='Accepted output posted to the production operation.')
    invoiced_amount = models.DecimalField(**MONEY)
    invoiced_quantity = models.DecimalField(**QTY)
    # Control
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    required_approval_role = models.CharField(max_length=30, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=250, blank=True)
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'job_work_orders'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'order_no'), name='jw_unique_order_no')]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'job_worker', 'status')),
                   models.Index(fields=('tenant', 'compliance_due_date'))]

    def __str__(self):
        return self.order_no

    @property
    def is_production(self):
        return self.production_order_id is not None

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self):
        today = dj_timezone.localdate()
        return bool(self.expected_return_date and self.status not in ('COMPLETED',) + self.TERMINAL_STATUSES
                    and self.expected_return_date < today)


class JobWorkOrderLine(JWModel):
    TYPES = [('INPUT', 'Input material'), ('WIP', 'WIP'), ('COMPONENT', 'Component'), ('OUTPUT', 'Finished / processed output'),
             ('SERVICE', 'Service'), ('SCRAP', 'Scrap'), ('BY_PRODUCT', 'By-product')]
    INBOUND_TYPES = ('INPUT', 'WIP', 'COMPONENT')     # sent to the job worker
    RESULT_TYPES = ('OUTPUT', 'SCRAP', 'BY_PRODUCT')  # produced by the job worker
    order = models.ForeignKey(JobWorkOrder, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    line_type = models.CharField(max_length=12, choices=TYPES, default='INPUT')
    supply_method = models.CharField(max_length=20, choices=SUPPLY_METHODS, default='PRINCIPAL')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    production_component = models.ForeignKey('manufacturing.ProductionOrderComponent', on_delete=models.PROTECT, null=True, blank=True,
                                             related_name='job_work_lines')
    routing_link_code = models.CharField(max_length=20, blank=True)
    description = models.CharField(max_length=200, blank=True)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    hsn_code = models.CharField(max_length=20, blank=True)
    metal = models.CharField(max_length=20, choices=METALS, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    lot_no = models.CharField(max_length=60, blank=True)
    certificate_no = models.CharField(max_length=100, blank=True)
    # Requirement / expectation
    quantity = models.DecimalField(**QTY, help_text='Required (inputs) or expected (outputs) quantity.')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    expected_loss_weight = models.DecimalField(**WEIGHT)
    unit_value = models.DecimalField(**RATE, help_text='Value per unit for documentation (challan).')
    # Progress - inputs
    reserved_qty = models.DecimalField(**QTY)
    dispatched_qty = models.DecimalField(**QTY)
    dispatched_gross = models.DecimalField(**WEIGHT)
    dispatched_net = models.DecimalField(**WEIGHT)
    dispatched_fine = models.DecimalField(**WEIGHT)
    dispatched_value = models.DecimalField(**MONEY)
    consumed_qty = models.DecimalField(**QTY)
    consumed_net = models.DecimalField(**WEIGHT)
    scrap_qty = models.DecimalField(**QTY)
    loss_qty = models.DecimalField(**QTY)
    transferred_qty = models.DecimalField(**QTY, help_text='Sent on to the next job worker.')
    # Progress - both directions
    returned_qty = models.DecimalField(**QTY, help_text='Dispatched back by the job worker.')
    received_qty = models.DecimalField(**QTY, help_text='Received at the principal.')
    received_gross = models.DecimalField(**WEIGHT)
    received_net = models.DecimalField(**WEIGHT)
    # Progress - outputs
    produced_qty = models.DecimalField(**QTY)
    produced_gross = models.DecimalField(**WEIGHT)
    produced_net = models.DecimalField(**WEIGHT)
    accepted_qty = models.DecimalField(**QTY)
    rejected_qty = models.DecimalField(**QTY)
    cost_value = models.DecimalField(**MONEY, help_text='Output: material value allocated; input: value consumed.')

    class Meta:
        db_table = 'job_work_order_lines'
        ordering = ['line_no']
        constraints = [models.UniqueConstraint(fields=('order', 'line_no'), name='jw_unique_order_line')]

    def __str__(self):
        return f'{self.order.order_no}/{self.line_no} {self.item.item_no if self.item_id else self.description}'

    @property
    def is_inbound(self):
        return self.line_type in self.INBOUND_TYPES

    @property
    def is_memo(self):
        """Not company inventory: vendor/customer-owned material is tracked only in the job worker stock ledger."""
        return self.supply_method in ('VENDOR', 'CUSTOMER')

    @property
    def to_dispatch_qty(self):
        return max(self.quantity - self.dispatched_qty, ZERO)


# ---------------------------------------------------------------------------
# Dispatch, delivery challan, e-way bill
# ---------------------------------------------------------------------------

class JobWorkDispatch(JWModel):
    MOVEMENTS = [('PRINCIPAL_TO_JW', 'Principal -> job worker'), ('JW_TO_JW', 'Job worker -> job worker (WIP transfer)'),
                 ('JW_TO_PRINCIPAL', 'Job worker -> principal (return)')]
    STATUSES = [('DRAFT', 'Draft'), ('IN_TRANSIT', 'In transit'), ('DELIVERED', 'Delivered'), ('PARTIALLY_RECEIVED', 'Partially received'),
                ('RECEIVED', 'Received'), ('REVERSED', 'Reversed'), ('CANCELLED', 'Cancelled')]
    dispatch_no = models.CharField(max_length=40)
    movement_type = models.CharField(max_length=20, choices=MOVEMENTS)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='dispatches',
                              help_text='The order whose stock arrives (principal->JW, JW->JW) or returns (JW->principal).')
    source_order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, null=True, blank=True, related_name='outgoing_transfers',
                                     help_text='JW->JW: the previous-stage order whose stock leaves.')
    from_job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    from_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    transit_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    dispatch_date = models.DateField(default=dj_timezone.localdate)
    expected_return_date = models.DateField(null=True, blank=True)
    compliance_due_date = models.DateField(null=True, blank=True)
    vehicle_no = models.CharField(max_length=20, blank=True)
    transporter = models.CharField(max_length=150, blank=True)
    transporter_id = models.CharField(max_length=20, blank=True)
    transport_mode = models.CharField(max_length=10, default='ROAD', choices=[('ROAD', 'Road'), ('RAIL', 'Rail'), ('AIR', 'Air'),
                                                                           ('SHIP', 'Ship'), ('HAND', 'Hand delivery')])
    distance_km = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    total_qty = models.DecimalField(**QTY)
    total_gross = models.DecimalField(**WEIGHT)
    total_net = models.DecimalField(**WEIGHT)
    total_fine = models.DecimalField(**WEIGHT)
    total_value = models.DecimalField(**MONEY)
    eway_bill_required = models.BooleanField(default=False)
    eway_bill_reason = models.CharField(max_length=250, blank=True)
    posted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    delivered_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=250, blank=True)
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'job_work_dispatches'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'dispatch_no'), name='jw_unique_dispatch_no')]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'dispatch_date'))]

    def __str__(self):
        return self.dispatch_no


class JobWorkDispatchLine(JWModel):
    dispatch = models.ForeignKey(JobWorkDispatch, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    order_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, related_name='dispatch_lines',
                                   help_text='Line of dispatch.order this movement belongs to.')
    source_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer_out_lines',
                                    help_text='JW->JW: line of the previous-stage order that is depleted.')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    hsn_code = models.CharField(max_length=20, blank=True)
    memo = models.BooleanField(default=False, help_text='Not company inventory (vendor/customer owned) or production WIP.')
    owner = models.CharField(max_length=10, choices=OWNERS, default='PRINCIPAL')
    quantity = models.DecimalField(**QTY)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    fine_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    metal = models.CharField(max_length=20, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    value = models.DecimalField(**MONEY)
    received_qty = models.DecimalField(**QTY)
    received_gross = models.DecimalField(**WEIGHT)
    weight_capture = models.ForeignKey('WeightCapture', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'job_work_dispatch_lines'
        ordering = ['line_no']

    @property
    def open_qty(self):
        return max(self.quantity - self.received_qty, ZERO)


class DeliveryChallan(JWModel):
    """Rule 55 delivery challan for a non-supply movement. It is never a tax invoice."""
    REASONS = [('JOB_WORK', 'Goods sent for job work'), ('JOB_WORK_RETURN', 'Return of goods after job work'),
               ('JW_TO_JW', 'Goods sent from one job worker to another')]
    challan_no = models.CharField(max_length=40)
    dispatch = models.OneToOneField(JobWorkDispatch, on_delete=models.PROTECT, related_name='challan')
    challan_date = models.DateField()
    document_kind = models.CharField(max_length=20, default='DELIVERY_CHALLAN', editable=False)
    reason = models.CharField(max_length=20, choices=REASONS)
    job_work_type = models.CharField(max_length=24, choices=TRANSACTION_TYPES)
    principal_gstin = models.CharField(max_length=15, blank=True)
    principal_name = models.CharField(max_length=200, blank=True)
    consignor_gstin = models.CharField(max_length=15, blank=True)
    consignor_name = models.CharField(max_length=200, blank=True)
    consignor_address = models.TextField(blank=True)
    consignor_state_code = models.CharField(max_length=2, blank=True)
    consignee_gstin = models.CharField(max_length=15, blank=True)
    consignee_name = models.CharField(max_length=200, blank=True)
    consignee_address = models.TextField(blank=True)
    consignee_state_code = models.CharField(max_length=2, blank=True)
    order_no = models.CharField(max_length=40)
    production_order_no = models.CharField(max_length=40, blank=True)
    goods_category = models.CharField(max_length=20, choices=GOODS_CATEGORIES, default='INPUTS')
    total_value = models.DecimalField(**MONEY)
    return_due_date = models.DateField(null=True, blank=True)
    vehicle_no = models.CharField(max_length=20, blank=True)
    transporter = models.CharField(max_length=150, blank=True)
    eway_bill_no = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=12, default='ISSUED', choices=[('ISSUED', 'Issued'), ('CANCELLED', 'Cancelled'),
                                                                       ('CORRECTED', 'Superseded by correction')])
    corrected_by = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='corrects')
    cancellation_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'job_work_delivery_challans'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'challan_no'), name='jw_unique_challan_no')]

    def __str__(self):
        return self.challan_no


class DeliveryChallanLine(JWModel):
    challan = models.ForeignKey(DeliveryChallan, on_delete=models.CASCADE, related_name='lines')
    dispatch_line = models.ForeignKey(JobWorkDispatchLine, on_delete=models.PROTECT, related_name='+')
    line_no = models.PositiveIntegerField()
    item_no = models.CharField(max_length=40)
    description = models.CharField(max_length=200)
    hsn_code = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(**QTY)
    uom = models.CharField(max_length=10, blank=True)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    fine_weight = models.DecimalField(**WEIGHT)
    purity = models.CharField(max_length=20, blank=True)
    value = models.DecimalField(**MONEY, help_text='Value for documentation only.')
    serial_no = models.CharField(max_length=100, blank=True)
    lot_no = models.CharField(max_length=60, blank=True)
    huid = models.CharField(max_length=20, blank=True)

    class Meta:
        db_table = 'job_work_delivery_challan_lines'
        ordering = ['line_no']


class EWayBill(JWModel):
    STATUSES = [('PENDING', 'Pending generation'), ('GENERATED', 'Generated'), ('CANCELLED', 'Cancelled'), ('EXPIRED', 'Expired')]
    dispatch = models.ForeignKey(JobWorkDispatch, on_delete=models.PROTECT, related_name='eway_bills')
    ewb_no = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='PENDING')
    document_type = models.CharField(max_length=20, default='DELIVERY_CHALLAN')
    document_no = models.CharField(max_length=40)
    document_date = models.DateField()
    generated_at = models.DateTimeField(null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    vehicle_no = models.CharField(max_length=20, blank=True)
    transporter = models.CharField(max_length=150, blank=True)
    transporter_id = models.CharField(max_length=20, blank=True)
    provider = models.CharField(max_length=40, default='MANUAL')
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True, help_text='Every generate/cancel/extend/vehicle update response.')
    cancel_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'job_work_eway_bills'
        ordering = ['-id']


# ---------------------------------------------------------------------------
# Job worker stock ledger (immutable)
# ---------------------------------------------------------------------------

class JobWorkerStockEntry(PostedModel):
    """Stock held at a job worker. `quantity`/weights are signed changes at the job worker location.

    Inventory-backed rows mirror an InventoryLedgerEntry at the job worker location; memo rows (vendor/customer-owned
    material, production WIP) have no inventory entry. Ownership never changes here - only custody."""
    TYPES = [('RECEIVED_BY_JW', 'Received by job worker'), ('TRANSFER_IN', 'Transfer in (from job worker)'),
             ('DIRECT_PURCHASE', 'Direct purchase to job worker'), ('CONSUMED', 'Consumed'), ('OUTPUT', 'Output'),
             ('SCRAP', 'Scrap generated'), ('LOSS', 'Loss'), ('RETURN_DISPATCHED', 'Returned (dispatched to principal)'),
             ('TRANSFER_OUT', 'Transfer out (to job worker)'), ('ADJUSTMENT', 'Adjustment'), ('REVERSAL', 'Reversal')]
    entry_no = models.CharField(max_length=40)
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='stock_entries')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='stock_entries')
    order_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, related_name='stock_entries')
    entry_type = models.CharField(max_length=20, choices=TYPES)
    document_type = models.CharField(max_length=30)
    document_no = models.CharField(max_length=40)
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    metal = models.CharField(max_length=20, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    fine_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    value = models.DecimalField(**MONEY)
    owner = models.CharField(max_length=10, choices=OWNERS, default='PRINCIPAL')
    custodian = models.CharField(max_length=200, blank=True)
    memo = models.BooleanField(default=False)
    loss_class = models.CharField(max_length=24, choices=LOSS_CLASSES, blank=True)
    dispatch = models.ForeignKey(JobWorkDispatch, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    inventory_entry = models.ForeignKey('inventory.InventoryLedgerEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'job_worker_inventory_ledger'
        ordering = ['posting_date', 'id']
        indexes = [models.Index(fields=('tenant', 'job_worker', 'item')), models.Index(fields=('tenant', 'order')),
                   models.Index(fields=('tenant', 'jewellery_unit')), models.Index(fields=('tenant', 'lot_no'))]


# ---------------------------------------------------------------------------
# Processing report (consumption, output, scrap, loss) and direct purchase
# ---------------------------------------------------------------------------

class JobWorkProcessReport(JWModel):
    report_no = models.CharField(max_length=40)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='process_reports')
    report_date = models.DateField(default=dj_timezone.localdate)
    status = models.CharField(max_length=10, choices=DOC_STATUSES, default='DRAFT')
    reference = models.CharField(max_length=60, blank=True, help_text="Job worker's own report / docket number.")
    posted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    reversal_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'job_work_consumption'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'report_no'), name='jw_unique_report_no')]

    def __str__(self):
        return self.report_no


class JobWorkProcessLine(JWModel):
    KINDS = [('OUTPUT', 'Output (consumes input)'), ('CONSUMPTION', 'Consumption without output'), ('SCRAP', 'Scrap'), ('LOSS', 'Loss')]
    SCRAP_DISPOSITIONS = [('RETURNED', 'Returned to principal'), ('RETAINED', 'Retained by job worker'), ('DISPOSED', 'Disposed by principal'),
                          ('SOLD', 'Sold'), ('RECYCLED', 'Recycled')]
    report = models.ForeignKey(JobWorkProcessReport, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    kind = models.CharField(max_length=12, choices=KINDS)
    input_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                   help_text='The input consumed.')
    input_qty = models.DecimalField(**QTY, help_text='Input quantity (grams for metal) used by this line.')
    input_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    result_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                    help_text='Output / scrap line produced.')
    quantity = models.DecimalField(**QTY, help_text='Output / scrap quantity produced.')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    purity = models.CharField(max_length=20, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    serial_no = models.CharField(max_length=100, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    output_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    loss_class = models.CharField(max_length=24, choices=LOSS_CLASSES, blank=True)
    scrap_disposition = models.CharField(max_length=12, choices=SCRAP_DISPOSITIONS, blank=True)
    recoverable_value = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=200, blank=True)
    value = models.DecimalField(**MONEY, help_text='Material value moved by this line (set at posting).')

    class Meta:
        db_table = 'job_work_output'
        ordering = ['line_no']


# ---------------------------------------------------------------------------
# Receipt at principal and QC
# ---------------------------------------------------------------------------

class JobWorkReceipt(JWModel):
    receipt_no = models.CharField(max_length=40)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='receipts')
    dispatch = models.ForeignKey(JobWorkDispatch, on_delete=models.PROTECT, related_name='receipts')
    receipt_date = models.DateField(default=dj_timezone.localdate)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=10, choices=DOC_STATUSES, default='POSTED')
    qc_status = models.CharField(max_length=12, default='PENDING', choices=[('NOT_REQUIRED', 'Not required'), ('PENDING', 'Pending'),
                                                                          ('DONE', 'Done')])
    received_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reversal_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'job_work_receipts'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'receipt_no'), name='jw_unique_receipt_no')]

    def __str__(self):
        return self.receipt_no


class JobWorkReceiptLine(JWModel):
    receipt = models.ForeignKey(JobWorkReceipt, on_delete=models.CASCADE, related_name='lines')
    dispatch_line = models.ForeignKey(JobWorkDispatchLine, on_delete=models.PROTECT, related_name='receipt_lines')
    order_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, related_name='receipt_lines')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    dispatched_gross = models.DecimalField(**WEIGHT)
    weight_variance = models.DecimalField(**WEIGHT)
    value = models.DecimalField(**MONEY)
    qc_pending_qty = models.DecimalField(**QTY)
    inventory_entry = models.ForeignKey('inventory.InventoryLedgerEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'job_work_return_lines'


class JobWorkQC(JWModel):
    RESULTS = [('ACCEPTED', 'Accepted'), ('REJECTED', 'Rejected'), ('REWORK', 'Rework'), ('HOLD', 'Hold'), ('PARTIAL', 'Partially accepted')]
    qc_no = models.CharField(max_length=40)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='qcs')
    receipt = models.ForeignKey(JobWorkReceipt, on_delete=models.PROTECT, related_name='qcs')
    qc_date = models.DateField(default=dj_timezone.localdate)
    result = models.CharField(max_length=10, choices=RESULTS)
    checks = models.JSONField(default=dict, blank=True, help_text='quantity/weight/purity/design/stone/HUID/hallmark/finish check results.')
    inspector = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    rework_order = models.ForeignKey(JobWorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=DOC_STATUSES, default='POSTED')

    class Meta:
        db_table = 'job_work_qc'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'qc_no'), name='jw_unique_qc_no')]


class JobWorkQCLine(JWModel):
    qc = models.ForeignKey(JobWorkQC, on_delete=models.CASCADE, related_name='lines')
    receipt_line = models.ForeignKey(JobWorkReceiptLine, on_delete=models.PROTECT, related_name='qc_lines')
    accepted_qty = models.DecimalField(**QTY)
    rejected_qty = models.DecimalField(**QTY)
    rework_qty = models.DecimalField(**QTY)
    hold_qty = models.DecimalField(**QTY)
    measured_gross = models.DecimalField(**WEIGHT)
    purity_result = models.CharField(max_length=20, blank=True)
    huid_ok = models.BooleanField(default=True)
    defect = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'job_work_qc_lines'


# ---------------------------------------------------------------------------
# Cost ledger, GST snapshot, vendor invoice, debit / credit notes
# ---------------------------------------------------------------------------

class JobWorkCostEntry(PostedModel):
    """Value ledger of one job work order (signed: + into the order, - out to its output)."""
    TYPES = [('MATERIAL', 'Material consumed'), ('OUTPUT', 'Allocated to output'), ('JOB_CHARGE', 'Job work charge'),
             ('FREIGHT', 'Freight'), ('INSURANCE', 'Insurance'), ('OTHER', 'Other direct charge'), ('LOSS', 'Metal / material loss'),
             ('SCRAP', 'Scrap value'), ('RECOVERY', 'Recovery from job worker'), ('VARIANCE', 'Variance')]
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='cost_entries')
    cost_type = models.CharField(max_length=12, choices=TYPES)
    amount = models.DecimalField(**MONEY)
    order_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    document_type = models.CharField(max_length=30, blank=True)
    document_no = models.CharField(max_length=40, blank=True)
    description = models.CharField(max_length=200, blank=True)
    absorbed = models.BooleanField(default=True, help_text='Part of the material cost allocated to output (normal process loss is; '
                                                           'job worker liability / unexplained shortage is not).')

    class Meta:
        db_table = 'job_work_posting_entries'
        ordering = ['id']
        indexes = [models.Index(fields=('tenant', 'order', 'cost_type'))]


class JobWorkTaxSnapshot(PostedModel):
    """The tax result frozen at posting. Later rate changes never touch it."""
    document_type = models.CharField(max_length=30)
    document_no = models.CharField(max_length=40)
    tax_rate = models.ForeignKey(TaxRate, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    code = models.CharField(max_length=20)
    supply_type = models.CharField(max_length=10, choices=[('INTRA', 'Intra-state'), ('INTER', 'Inter-state')])
    place_of_supply = models.CharField(max_length=2, blank=True)
    supplier_state_code = models.CharField(max_length=2, blank=True)
    recipient_state_code = models.CharField(max_length=2, blank=True)
    taxable_value = models.DecimalField(**MONEY)
    cgst_rate = models.DecimalField(**TAX_PERCENT)
    sgst_rate = models.DecimalField(**TAX_PERCENT)
    igst_rate = models.DecimalField(**TAX_PERCENT)
    cess_rate = models.DecimalField(**TAX_PERCENT)
    cgst = models.DecimalField(**MONEY)
    sgst = models.DecimalField(**MONEY)
    utgst = models.DecimalField(**MONEY)
    igst = models.DecimalField(**MONEY)
    cess = models.DecimalField(**MONEY)
    reverse_charge = models.BooleanField(default=False)
    explanation = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'job_work_tax_snapshots'

    @property
    def total_tax(self):
        return self.cgst + self.sgst + self.utgst + self.igst + self.cess


class JobWorkVendorInvoice(JWModel):
    STATUSES = [('DRAFT', 'Draft'), ('MATCHED', 'Matched'), ('EXCEPTION', 'Exception'), ('APPROVED', 'Approved'), ('POSTED', 'Posted'),
                ('CANCELLED', 'Cancelled')]
    document_no = models.CharField(max_length=40)
    vendor_invoice_no = models.CharField(max_length=60)
    invoice_date = models.DateField()
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='invoices')
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='invoices')
    vendor_gstin = models.CharField(max_length=15, blank=True)
    sac_code = models.CharField(max_length=20)
    billed_quantity = models.DecimalField(**QTY, help_text='Pieces / grams / carats / hours per the rate basis.')
    rate = models.DecimalField(**RATE)
    taxable_value = models.DecimalField(**MONEY)
    freight = models.DecimalField(**MONEY)
    other_charges = models.DecimalField(**MONEY)
    vendor_cgst = models.DecimalField(**MONEY)
    vendor_sgst = models.DecimalField(**MONEY)
    vendor_igst = models.DecimalField(**MONEY)
    vendor_cess = models.DecimalField(**MONEY)
    tds_amount = models.DecimalField(**MONEY)
    total_amount = models.DecimalField(**MONEY)
    payment_terms = models.CharField(max_length=120, blank=True)
    # E-invoice reference issued by the job worker (stored, never assumed)
    irn = models.CharField(max_length=64, blank=True)
    ack_no = models.CharField(max_length=30, blank=True)
    ack_date = models.DateTimeField(null=True, blank=True)
    signed_qr = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='DRAFT')
    match_result = models.JSONField(default=dict, blank=True)
    tax_snapshot = models.OneToOneField(JobWorkTaxSnapshot, on_delete=models.PROTECT, null=True, blank=True, related_name='invoice')
    supplier_invoice = models.ForeignKey('erp.SupplierInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gl_status = models.CharField(max_length=20, default='NOT_POSTED')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'job_work_vendor_invoices'
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=('tenant', 'document_no'), name='jw_unique_invoice_doc_no'),
            models.UniqueConstraint(fields=('tenant', 'job_worker', 'vendor_invoice_no'), condition=~Q(status='CANCELLED'),
                                    name='jw_unique_vendor_invoice_no'),
        ]

    def __str__(self):
        return f'{self.document_no} ({self.vendor_invoice_no})'

    @property
    def vendor_tax(self):
        return self.vendor_cgst + self.vendor_sgst + self.vendor_igst + self.vendor_cess


class JobWorkAdjustmentNote(JWModel):
    """Debit note (recovery from the job worker) or vendor credit note (overbilling, rate correction...). Never automatic."""
    TYPES = [('DEBIT', 'Debit note (recovery from job worker)'), ('CREDIT', 'Vendor credit note')]
    STATUSES = [('DRAFT', 'Draft'), ('APPROVED', 'Approved'), ('POSTED', 'Posted'), ('CANCELLED', 'Cancelled')]
    note_no = models.CharField(max_length=40)
    note_type = models.CharField(max_length=6, choices=TYPES)
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, related_name='notes')
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, related_name='notes')
    exception = models.ForeignKey('JobWorkException', on_delete=models.PROTECT, null=True, blank=True, related_name='notes')
    invoice = models.ForeignKey(JobWorkVendorInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name='notes')
    note_date = models.DateField(default=dj_timezone.localdate)
    reason = models.CharField(max_length=250)
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=10, choices=STATUSES, default='DRAFT')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gl_status = models.CharField(max_length=20, default='NOT_POSTED')

    class Meta:
        db_table = 'job_work_debit_notes'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'note_no'), name='jw_unique_note_no')]

    def __str__(self):
        return self.note_no


# ---------------------------------------------------------------------------
# Exceptions (loss approvals, reconciliation differences, mismatches)
# ---------------------------------------------------------------------------

class JobWorkException(JWModel):
    TYPES = [('MATERIAL_OVERDUE', 'Material overdue'), ('EXCESS_LOSS', 'Loss above tolerance'), ('WEIGHT_DIFFERENCE', 'Unexplained weight difference'),
             ('EXCESS_CONSUMPTION', 'Excess consumption'), ('SHORT_RETURN', 'Short return'), ('EXCESS_RETURN', 'Excess return'),
             ('RECEIPT_WEIGHT_VARIANCE', 'Receipt weight variance'), ('WRONG_HUID', 'Wrong HUID'), ('WRONG_PURITY', 'Wrong purity'),
             ('FINE_WEIGHT_VARIANCE', 'Fine weight variance'), ('MISSING_DC', 'Missing delivery challan'),
             ('MISSING_EWAY_BILL', 'Missing e-way bill'), ('GSTIN_MISMATCH', 'GSTIN mismatch'), ('HSN_MISMATCH', 'HSN / SAC mismatch'),
             ('INVOICE_MISMATCH', 'Vendor invoice mismatch'), ('GST_MISMATCH', 'GST amount mismatch'), ('QC_FAILURE', 'QC failure'),
             ('DUPLICATE_RECEIPT', 'Duplicate receipt'), ('DUPLICATE_INVOICE', 'Duplicate invoice'),
             ('COMPLIANCE_RULE_MISSING', 'No approved compliance rule'), ('CAPACITY', 'Job worker capacity exceeded'), ('OTHER', 'Other')]
    STATUSES = [('OPEN', 'Open'), ('UNDER_REVIEW', 'Under review'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'),
                ('RESOLVED', 'Resolved'), ('CLOSED', 'Closed')]
    OPEN_STATUSES = ('OPEN', 'UNDER_REVIEW', 'REJECTED')
    RESOLUTIONS = [('NO_CHARGE', 'No charge'), ('VENDOR_RECOVERY', 'Vendor recovery'), ('DEBIT_NOTE', 'Debit note'),
                   ('CREDIT_ADJUSTMENT', 'Credit adjustment'), ('INSURANCE_CLAIM', 'Insurance claim'), ('MANAGEMENT_WAIVER', 'Management waiver'),
                   ('RETURNED', 'Material returned'), ('CORRECTED', 'Data corrected'), ('RECLASSIFIED', 'Reclassified')]
    exception_no = models.CharField(max_length=40)
    exception_type = models.CharField(max_length=24, choices=TYPES)
    severity = models.CharField(max_length=10, default='HIGH', choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'),
                                                                       ('CRITICAL', 'Critical')])
    blocking = models.BooleanField(default=True, help_text='Blocks completion/closure of the order until approved or resolved.')
    order = models.ForeignKey(JobWorkOrder, on_delete=models.PROTECT, null=True, blank=True, related_name='exceptions')
    order_line = models.ForeignKey(JobWorkOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    job_worker = models.ForeignKey(JobWorker, on_delete=models.PROTECT, null=True, blank=True, related_name='exceptions')
    document_type = models.CharField(max_length=30, blank=True)
    document_no = models.CharField(max_length=40, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, help_text='Stops scans from raising the same exception twice.')
    description = models.CharField(max_length=300)
    expected_value = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    actual_value = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    variance = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    weight = models.DecimalField(**WEIGHT)
    value = models.DecimalField(**MONEY)
    status = models.CharField(max_length=12, choices=STATUSES, default='OPEN')
    required_role = models.CharField(max_length=30, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    resolution = models.CharField(max_length=20, choices=RESOLUTIONS, blank=True)
    recovery_amount = models.DecimalField(**MONEY)
    loss_class = models.CharField(max_length=24, choices=LOSS_CLASSES, blank=True)
    raised_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reviewed_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'job_work_compliance_alerts'
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=('tenant', 'exception_no'), name='jw_unique_exception_no'),
            models.UniqueConstraint(fields=('tenant', 'dedupe_key'), condition=~Q(dedupe_key='') & Q(status__in=('OPEN', 'UNDER_REVIEW')),
                                    name='jw_unique_open_exception'),
        ]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'order', 'status'))]

    def __str__(self):
        return self.exception_no

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES


# ---------------------------------------------------------------------------
# ITC-04
# ---------------------------------------------------------------------------

class ITC04Return(JWModel):
    STATUSES = [('DRAFT', 'Draft'), ('EXCEPTIONS', 'Has exceptions'), ('RECONCILED', 'Reconciled'), ('PREPARED', 'Prepared'),
                ('FILED', 'Filed')]
    reference_no = models.CharField(max_length=40)
    period_code = models.CharField(max_length=20, help_text='e.g. 2026-27-H1 or 2026-27')
    frequency = models.CharField(max_length=12, choices=[('HALF_YEARLY', 'Half-yearly'), ('ANNUAL', 'Annual'), ('QUARTERLY', 'Quarterly')])
    period_start = models.DateField()
    period_end = models.DateField()
    principal_gstin = models.CharField(max_length=15, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='DRAFT')
    rule = models.ForeignKey(ComplianceRule, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    summary = models.JSONField(default=dict, blank=True)
    generated_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    generated_at = models.DateTimeField(null=True, blank=True)
    prepared_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    filed_reference = models.CharField(max_length=60, blank=True)

    class Meta:
        db_table = 'job_work_itc04_returns'
        ordering = ['-period_start']
        constraints = [models.UniqueConstraint(fields=('tenant', 'reference_no'), name='jw_unique_itc04_ref')]

    def __str__(self):
        return f'{self.reference_no} {self.period_code}'


class ITC04Line(JWModel):
    TABLES = [('4', 'Table 4 - goods sent to job worker'), ('5A', 'Table 5A - received back from job worker'),
              ('5B', 'Table 5B - sent from one job worker to another'), ('5C', 'Table 5C - supplied from job worker premises')]
    MATCH = [('MATCHED', 'Matched'), ('PENDING', 'Pending return'), ('MISSING_RECEIPT', 'Missing receipt'), ('EXCESS_RECEIPT', 'Excess receipt'),
             ('SHORT_RECEIPT', 'Short receipt'), ('WRONG_GSTIN', 'Wrong / missing GSTIN'), ('WRONG_HSN', 'Missing HSN'),
             ('WRONG_QUANTITY', 'Challan quantity differs from movement'), ('WRONG_CHALLAN', 'Original challan not found')]
    ERROR_STATUSES = ('EXCESS_RECEIPT', 'WRONG_GSTIN', 'WRONG_HSN', 'WRONG_QUANTITY', 'WRONG_CHALLAN', 'MISSING_RECEIPT')
    itc04 = models.ForeignKey(ITC04Return, on_delete=models.CASCADE, related_name='lines')
    table = models.CharField(max_length=3, choices=TABLES)
    job_worker_gstin = models.CharField(max_length=15, blank=True)
    job_worker_state_code = models.CharField(max_length=2, blank=True)
    job_worker_name = models.CharField(max_length=200, blank=True)
    challan_no = models.CharField(max_length=40)
    challan_date = models.DateField()
    original_challan_no = models.CharField(max_length=40, blank=True)
    original_challan_date = models.DateField(null=True, blank=True)
    order_no = models.CharField(max_length=40, blank=True)
    item_no = models.CharField(max_length=40, blank=True)
    description = models.CharField(max_length=200, blank=True)
    hsn_code = models.CharField(max_length=20, blank=True)
    uom = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(**QTY)
    loss_quantity = models.DecimalField(**QTY)
    taxable_value = models.DecimalField(**MONEY)
    goods_category = models.CharField(max_length=20, choices=GOODS_CATEGORIES, default='INPUTS')
    nature_of_job_work = models.CharField(max_length=60, blank=True)
    match_status = models.CharField(max_length=16, choices=MATCH, default='MATCHED')
    source_type = models.CharField(max_length=30, blank=True)
    source_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'job_work_itc04_records'
        ordering = ['table', 'challan_date', 'id']


# ---------------------------------------------------------------------------
# Weighing scale, attachments, idempotency, audit
# ---------------------------------------------------------------------------

class WeightCapture(JWModel):
    """A reading pushed by a digital weighing scale. Dispatch/receipt lines can reference it instead of a typed weight."""
    device_id = models.CharField(max_length=60)
    weight = models.DecimalField(**WEIGHT)
    unit = models.CharField(max_length=5, default='GM')
    captured_at = models.DateTimeField(default=dj_timezone.now)
    operator = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    transaction_ref = models.CharField(max_length=60, blank=True)
    used = models.BooleanField(default=False)

    class Meta:
        db_table = 'job_work_weight_captures'
        ordering = ['-id']


class JobWorkAttachment(JWModel):
    KINDS = [('BEFORE_DISPATCH', 'Before dispatch photo'), ('AFTER_JOB_WORK', 'After job work photo'), ('DAMAGE', 'Damage photo'),
             ('QC', 'QC photo'), ('CERTIFICATE', 'Certificate'), ('VENDOR_DOCUMENT', 'Vendor document'), ('OTHER', 'Other')]
    document_type = models.CharField(max_length=30)
    document_id = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=20, choices=KINDS, default='OTHER')
    file = models.FileField(upload_to='jobwork/%Y/%m/')
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'job_work_attachments'
        indexes = [models.Index(fields=('tenant', 'document_type', 'document_id'))]


class JobWorkIdempotencyKey(models.Model):
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=80)
    endpoint = models.CharField(max_length=120)
    response_status = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        db_table = 'job_work_idempotency_keys'
        constraints = [models.UniqueConstraint(fields=('tenant', 'key'), name='jw_unique_idempotency_key')]


class JobWorkAuditLog(models.Model):
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.PROTECT, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=40)
    document_type = models.CharField(max_length=40)
    document_no = models.CharField(max_length=60, blank=True)
    order = models.ForeignKey(JobWorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    source = models.CharField(max_length=20, default='WEB')
    created_at = models.DateTimeField(default=dj_timezone.now)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = 'job_work_audit_logs'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'document_type', 'document_no')), models.Index(fields=('tenant', 'created_at'))]
