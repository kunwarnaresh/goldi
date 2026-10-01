"""Goldio Jewellery Savings & Advance Purchase Scheme.

A customer commits to a monthly installment for a fixed number of months; at maturity the jeweller grants a
configured benefit and the customer redeems contribution + benefit against jewellery purchases.

Five balances are kept apart everywhere - customer contribution, jeweller benefit, redemption, refund and
forfeiture - and are never merged into one "balance". Every money event is an immutable ``SchemeLedgerEntry``
written by ``savings.engine.SchemePostingEngine``; corrections are reversals, never edits.

Rules live on a ``SchemeVersion``. An enrolment points at the version it signed and also stores a JSON snapshot
of those rules, so a later version never changes an existing customer agreement.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone as dj_timezone

from inventory.models import MONEY, ZERO, TenantModel, TenantQuerySet

PERCENT = dict(max_digits=7, decimal_places=3, default=0)
USER = settings.AUTH_USER_MODEL


class SavingsModel(TenantModel):
    """Tenant-scoped row with the owning company (the ERP legal entity) alongside the audit columns."""
    company = models.ForeignKey('erp.Company', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        abstract = True


class ImmutableQuerySet(TenantQuerySet):
    def delete(self):
        raise ValidationError('Posted scheme entries are immutable; post a reversal instead.')


class ImmutableModel(SavingsModel):
    """Posted money documents: only the listed markers may change after creation, and rows are never deleted."""
    MUTABLE = {'status', 'reversed_by', 'updated_at', 'updated_by', 'finance_voucher'}

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if self.pk and not (update_fields and set(update_fields) <= self.MUTABLE):
            raise ValidationError('Posted scheme entries are immutable; post a reversal instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Posted scheme entries are immutable; post a reversal instead.')


# ---------------------------------------------------------------------------
# Setup, security
# ---------------------------------------------------------------------------

BENEFIT_TREATMENTS = [('ACCRUE_ON_APPROVAL', 'Accrue benefit liability when approved'),
                      ('RECOGNISE_ON_REDEMPTION', 'Recognise benefit cost only when redeemed')]


class SavingsSetup(SavingsModel):
    display_name = models.CharField(max_length=80, default='Jewellery Savings Plan', help_text='Customer-facing module name.')
    reminder_days = models.PositiveSmallIntegerField(default=5, help_text='Installments become "Due" this many days before the due date.')
    require_enrollment_approval = models.BooleanField(default=True)
    allow_self_approval = models.BooleanField(default=False, help_text='Maker-checker: when off, the approver must differ from the maker.')
    allow_multiple_active_schemes = models.BooleanField(default=True)
    max_active_schemes_per_customer = models.PositiveSmallIntegerField(default=3)
    max_monthly_contribution = models.DecimalField(**MONEY, help_text='0 = no limit (all active schemes of a customer).')
    same_day_reversal_alert = models.BooleanField(default=True)
    # Financial posting through the ERP finance engine - the chart of accounts is the jeweller's policy.
    gl_posting_enabled = models.BooleanField(default=False)
    benefit_treatment = models.CharField(max_length=30, choices=BENEFIT_TREATMENTS, default='ACCRUE_ON_APPROVAL')
    cash_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                     help_text='Cash collections. Blank = finance posting setup default cash account.')
    bank_clearing_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                              help_text='Card / UPI / bank / cheque collections without a bank account.')
    contribution_liability_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                                       help_text='Customer scheme advance / contribution liability.')
    benefit_liability_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    benefit_expense_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                                help_text='Scheme benefit / promotional cost.')
    redemption_settlement_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                                      help_text='Credited on redemption. Blank = customer receivable of the linked invoice.')
    penalty_income_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    forfeiture_income_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                                  help_text='Forfeited contribution and cancellation charges.')
    customer_credit_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                                help_text='Remaining balance converted to customer purchase credit.')

    class Meta:
        db_table = 'savings_setup'
        constraints = [models.UniqueConstraint(fields=('tenant',), name='sav_unique_setup_per_tenant')]


class SavingsRole(SavingsModel):
    ROLES = [('OPERATOR', 'Scheme operator'), ('MANAGER', 'Store manager'), ('FINANCE', 'Finance'),
             ('ADMINISTRATOR', 'Scheme administrator'), ('AUDITOR', 'Auditor')]
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')
    role = models.CharField(max_length=20, choices=ROLES)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'scheme_roles'
        constraints = [models.UniqueConstraint(fields=('tenant', 'user', 'role'), name='sav_unique_role')]


# ---------------------------------------------------------------------------
# Scheme master and versions
# ---------------------------------------------------------------------------

class JewellerySavingsScheme(SavingsModel):
    STATUSES = [('DRAFT', 'Draft'), ('UNDER_REVIEW', 'Under review'), ('APPROVED', 'Approved'), ('ACTIVE', 'Active'),
                ('SUSPENDED', 'Suspended'), ('EXPIRED', 'Expired'), ('CLOSED', 'Closed'), ('ARCHIVED', 'Archived')]
    CALCULATION_MODELS = [('MONETARY', 'Money-based (₹ contribution)'), ('GOLD_WEIGHT', 'Gold-weight accumulation (future)')]
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    calculation_model = models.CharField(max_length=20, choices=CALCULATION_MODELS, default='MONETARY')
    account_prefix = models.CharField(max_length=10, default='GSP', help_text='Scheme account numbers look like GSP-000001.')
    currency = models.CharField(max_length=10, default='INR')
    locations = models.ManyToManyField('inventory.Location', blank=True, related_name='+', help_text='Blank = all locations.')
    collect_at_any_location = models.BooleanField(default=True)
    redeem_at_any_location = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    active_from = models.DateField(default=dj_timezone.localdate)
    active_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'jewellery_schemes'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='sav_unique_scheme_code')]

    def __str__(self):
        return f'{self.code} - {self.name}'

    def current_version(self):
        return self.versions.filter(status='ACTIVE').order_by('-version_no').first()


INSTALLMENT_MODES = [('FIXED', 'Fixed monthly amount'), ('VARIABLE', 'Variable within limits')]
ADVANCE_MODES = [('ALLOCATE_FUTURE', 'Allocate to future installments'), ('UNALLOCATED', 'Hold as unallocated advance'),
                 ('NOT_ALLOWED', 'Not allowed')]
BENEFIT_TYPES = [('NONE', 'No benefit'), ('FIXED', 'Fixed amount'), ('ONE_INSTALLMENT', 'One installment'),
                 ('PERCENT_CONTRIBUTION', '% of contribution'), ('PERCENT_ELIGIBLE', '% of eligible contribution'),
                 ('TIERED', 'Tiered % of eligible contribution')]
BENEFIT_ELIGIBILITY = [('ALL_PAID', 'All installments paid (late allowed)'), ('ON_TIME', 'All installments paid within grace'),
                       ('PRO_RATA', 'Pro-rata to eligible installments')]
MISSED_RULES = [('MUST_PAY', 'Missed installments must be paid before maturity'), ('EXTEND_MATURITY', 'Maturity extends by missed months'),
                ('REDUCE_BENEFIT', 'Benefit reduces per missed installment'), ('INELIGIBLE', 'Any missed installment forfeits the benefit')]
LATE_RULES = [('NONE', 'No penalty'), ('FIXED', 'Fixed penalty'), ('PERCENT', '% of installment penalty'),
              ('BENEFIT_REDUCTION', 'Benefit reduces per late installment')]
BENEFIT_APPLICATIONS = [('MONETARY', 'Monetary entitlement against eligible jewellery'), ('MAKING_CHARGE', 'Benefit only against making charges'),
                        ('DISCOUNT', 'Benefit as discount (capped % of invoice)'), ('CREDIT', 'Scheme credit')]
REMAINING_RULES = [('KEEP', 'Keep balance for later purchases'), ('REFUND', 'Refund contribution balance'), ('FORFEIT', 'Forfeit'),
                   ('CREDIT', 'Convert to customer credit')]
CANCELLATION_BENEFIT = [('FORFEIT', 'Benefit forfeited (never refunded as cash)')]
CHARGE_TYPES = [('NONE', 'None'), ('FIXED', 'Fixed amount'), ('PERCENT', '% of contribution')]


class SchemeVersion(SavingsModel):
    """The contractual rule set customers sign. Once active it is frozen; changes go into a new version."""
    STATUSES = JewellerySavingsScheme.STATUSES
    FROZEN_STATUSES = ('APPROVED', 'ACTIVE', 'SUSPENDED', 'EXPIRED', 'CLOSED', 'ARCHIVED')
    scheme = models.ForeignKey(JewellerySavingsScheme, on_delete=models.CASCADE, related_name='versions')
    version_no = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    change_note = models.CharField(max_length=250, blank=True)
    effective_from = models.DateField(default=dj_timezone.localdate)
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    # Installment rules
    installment_mode = models.CharField(max_length=10, choices=INSTALLMENT_MODES, default='FIXED')
    installment_amount = models.DecimalField(**MONEY, help_text='Fixed schemes: default monthly amount (enrolment may choose within limits).')
    min_installment = models.DecimalField(**MONEY)
    max_installment = models.DecimalField(**MONEY, help_text='0 = no maximum.')
    installment_step = models.DecimalField(**MONEY, help_text='Installment must be a multiple of this (0 = any amount).')
    number_of_installments = models.PositiveSmallIntegerField(default=11)
    due_day = models.PositiveSmallIntegerField(default=5, help_text='Day of month installments fall due.')
    maturity_months_after_last = models.PositiveSmallIntegerField(default=1, help_text='11+1: maturity one month after the 11th due date.')
    allow_early_maturity = models.BooleanField(default=False, help_text='Mature as soon as every installment is paid.')
    grace_days = models.PositiveSmallIntegerField(default=10)
    partial_payment_allowed = models.BooleanField(default=True)
    advance_mode = models.CharField(max_length=20, choices=ADVANCE_MODES, default='ALLOCATE_FUTURE')
    max_advance_installments = models.PositiveSmallIntegerField(default=0, help_text='0 = no limit.')
    late_payment_rule = models.CharField(max_length=20, choices=LATE_RULES, default='NONE')
    late_payment_value = models.DecimalField(max_digits=14, decimal_places=3, default=0, help_text='₹ or % depending on the rule.')
    missed_installment_rule = models.CharField(max_length=20, choices=MISSED_RULES, default='MUST_PAY')
    missed_reduction_percent = models.DecimalField(**PERCENT, help_text='Benefit reduction % per missed installment (REDUCE_BENEFIT).')
    max_missed_installments = models.PositiveSmallIntegerField(default=0, help_text='More missed than this = benefit not eligible (0 = no limit).')
    # Benefit rules
    benefit_type = models.CharField(max_length=30, choices=BENEFIT_TYPES, default='ONE_INSTALLMENT')
    benefit_value = models.DecimalField(max_digits=14, decimal_places=3, default=0, help_text='₹ for FIXED, % for percentage types.')
    benefit_tiers = models.JSONField(default=list, blank=True, help_text='[{"from": 0, "to": 50000, "percent": 5}, ...] - "to" null = no upper limit.')
    min_benefit = models.DecimalField(**MONEY)
    max_benefit = models.DecimalField(**MONEY, help_text='0 = no cap.')
    benefit_eligibility = models.CharField(max_length=20, choices=BENEFIT_ELIGIBILITY, default='ALL_PAID')
    benefit_override_allowed = models.BooleanField(default=True)
    max_override_percent = models.DecimalField(**PERCENT, help_text='Override may move the benefit at most this % from calculated (0 = any).')
    benefit_application = models.CharField(max_length=20, choices=BENEFIT_APPLICATIONS, default='MONETARY')
    discount_cap_percent = models.DecimalField(**PERCENT, help_text='DISCOUNT application: benefit capped at this % of eligible invoice value.')
    # Redemption rules
    redemption_window_days = models.PositiveIntegerField(default=365, help_text='Days after maturity the entitlement can be redeemed.')
    redeem_before_maturity = models.BooleanField(default=False, help_text='Contribution (never benefit) may be redeemed before maturity.')
    min_redemption_amount = models.DecimalField(**MONEY)
    max_redemption_amount = models.DecimalField(**MONEY, help_text='Per redemption; 0 = no maximum.')
    partial_redemption_allowed = models.BooleanField(default=True)
    max_redemptions = models.PositiveSmallIntegerField(default=0, help_text='0 = no limit.')
    remaining_balance_rule = models.CharField(max_length=10, choices=REMAINING_RULES, default='KEEP')
    making_charge_eligible = models.BooleanField(default=True)
    # Cancellation / refund rules
    cancellation_allowed = models.BooleanField(default=True)
    cancellation_benefit_rule = models.CharField(max_length=20, choices=CANCELLATION_BENEFIT, default='FORFEIT')
    cancellation_charge_type = models.CharField(max_length=10, choices=CHARGE_TYPES, default='NONE')
    cancellation_charge_value = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    refund_allowed = models.BooleanField(default=True)
    refund_timeline_days = models.PositiveSmallIntegerField(default=7)
    # Limits and KYC
    nominee_required = models.BooleanField(default=False)
    kyc_required = models.BooleanField(default=False, help_text='Customer PAN must be on file.')
    max_entitlement = models.DecimalField(**MONEY, help_text='0 = no cap.')
    # Tax configuration - never inferred; reviewed with the jeweller's tax advisor before use.
    tax_treatment = models.CharField(max_length=120, blank=True, help_text='e.g. "Advance - GST on invoice at redemption".')
    advance_treatment = models.CharField(max_length=120, blank=True)
    redemption_treatment = models.CharField(max_length=120, blank=True)
    benefit_treatment_note = models.CharField(max_length=120, blank=True)
    tax_code = models.CharField(max_length=30, blank=True)
    terms = models.TextField(blank=True, help_text='Terms & conditions printed on the agreement.')

    class Meta:
        db_table = 'jewellery_scheme_versions'
        ordering = ['scheme', '-version_no']
        constraints = [models.UniqueConstraint(fields=('scheme', 'version_no'), name='sav_unique_scheme_version')]

    RULE_FIELDS = (
        'installment_mode', 'installment_amount', 'min_installment', 'max_installment', 'installment_step', 'number_of_installments',
        'due_day', 'maturity_months_after_last', 'allow_early_maturity', 'grace_days', 'partial_payment_allowed', 'advance_mode',
        'max_advance_installments', 'late_payment_rule', 'late_payment_value', 'missed_installment_rule', 'missed_reduction_percent',
        'max_missed_installments', 'benefit_type', 'benefit_value', 'benefit_tiers', 'min_benefit', 'max_benefit',
        'benefit_eligibility', 'benefit_override_allowed', 'max_override_percent', 'benefit_application', 'discount_cap_percent',
        'redemption_window_days', 'redeem_before_maturity', 'min_redemption_amount', 'max_redemption_amount',
        'partial_redemption_allowed', 'max_redemptions', 'remaining_balance_rule', 'making_charge_eligible', 'cancellation_allowed',
        'cancellation_benefit_rule', 'cancellation_charge_type', 'cancellation_charge_value', 'refund_allowed', 'refund_timeline_days',
        'nominee_required', 'kyc_required', 'max_entitlement', 'tax_treatment', 'advance_treatment', 'redemption_treatment',
        'benefit_treatment_note', 'tax_code', 'terms',
    )

    def __str__(self):
        return f'{self.scheme.code} V{self.version_no}'

    @property
    def code(self):
        return f'V{self.version_no}'

    @property
    def editable(self):
        return self.status in ('DRAFT', 'UNDER_REVIEW')

    def snapshot(self):
        data = {name: getattr(self, name) for name in self.RULE_FIELDS}
        data = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()}
        data['product_rules'] = [r.snapshot() for r in self.product_rules.all()]
        data['scheme_version'] = str(self)
        return data

    def clean(self):
        if self.number_of_installments < 1:
            raise ValidationError({'number_of_installments': 'At least one installment is required.'})
        if not 1 <= self.due_day <= 28:
            raise ValidationError({'due_day': 'Use a due day between 1 and 28 so every month has it.'})
        if self.max_installment and self.min_installment > self.max_installment:
            raise ValidationError({'max_installment': 'Maximum installment is below the minimum.'})
        if self.benefit_type == 'TIERED' and not self.benefit_tiers:
            raise ValidationError({'benefit_tiers': 'Enter at least one tier.'})

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values_list('status', flat=True).first()
            update_fields = set(kwargs.get('update_fields') or ())
            workflow_only = update_fields and update_fields <= {'status', 'approved_by', 'approved_at', 'submitted_by', 'updated_at', 'updated_by'}
            if stored in self.FROZEN_STATUSES and not workflow_only:
                raise ValidationError('This scheme version is approved and frozen; create a new version to change its rules.')
        super().save(*args, **kwargs)


class SchemeProductRule(SavingsModel):
    """What jewellery the entitlement may buy. Any INCLUDE rule restricts to matches; EXCLUDE always wins."""
    RULE_TYPES = [('INCLUDE', 'Include'), ('EXCLUDE', 'Exclude')]
    SCOPES = [('ALL', 'All jewellery'), ('METAL', 'Metal'), ('CATEGORY', 'Category'), ('COLLECTION', 'Collection'), ('SKU', 'SKU / item code'),
              ('LOCATION', 'Location code'), ('TAG', 'Tag (coin, bar, clearance, discounted, special)')]
    version = models.ForeignKey(SchemeVersion, on_delete=models.CASCADE, related_name='product_rules')
    rule_type = models.CharField(max_length=10, choices=RULE_TYPES, default='INCLUDE')
    scope = models.CharField(max_length=20, choices=SCOPES, default='ALL')
    value = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = 'scheme_product_rules'

    def snapshot(self):
        return {'rule_type': self.rule_type, 'scope': self.scope, 'value': self.value}


# ---------------------------------------------------------------------------
# Enrolment, agreement, nominee and schedule
# ---------------------------------------------------------------------------

class SchemeEnrollment(SavingsModel):
    STATUSES = [
        ('DRAFT', 'Draft'), ('PENDING_APPROVAL', 'Pending approval'), ('ACTIVE', 'Active'), ('PAYMENT_DUE', 'Payment due'),
        ('PARTIALLY_PAID', 'Partially paid'), ('OVERDUE', 'Overdue'), ('COMPLETED', 'Completed'), ('MATURED', 'Matured'),
        ('BENEFIT_APPROVED', 'Benefit approved'), ('PARTIALLY_REDEEMED', 'Partially redeemed'), ('REDEEMED', 'Redeemed'),
        ('CANCELLED', 'Cancelled'), ('REFUNDED', 'Refunded'), ('CLOSED', 'Closed'),
    ]
    CONTRIBUTING = ('ACTIVE', 'PAYMENT_DUE', 'PARTIALLY_PAID', 'OVERDUE', 'COMPLETED')
    REDEEMABLE = ('BENEFIT_APPROVED', 'PARTIALLY_REDEEMED')
    OPEN = CONTRIBUTING + ('PENDING_APPROVAL', 'MATURED') + REDEEMABLE
    FINAL = ('REDEEMED', 'REFUNDED', 'CLOSED')
    ACCEPTANCE_METHODS = [('PHYSICAL', 'Physical signature'), ('OTP', 'OTP / digital'), ('ESIGN', 'e-Sign'), ('PORTAL', 'Customer portal')]

    account_no = models.CharField(max_length=30)
    scheme = models.ForeignKey(JewellerySavingsScheme, on_delete=models.PROTECT, related_name='enrollments')
    version = models.ForeignKey(SchemeVersion, on_delete=models.PROTECT, related_name='enrollments')
    rules = models.JSONField(default=dict, help_text='Snapshot of the version rules the customer signed.')
    customer = models.ForeignKey('erp.Customer', on_delete=models.PROTECT, related_name='scheme_enrollments')
    customer_name = models.CharField(max_length=200)
    mobile = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    kyc_reference = models.CharField(max_length=60, blank=True, help_text='Masked PAN / KYC document reference.')
    branch = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sales_staff = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    sales_staff_name = models.CharField(max_length=150, blank=True)
    relationship_manager = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    enrollment_date = models.DateField(default=dj_timezone.localdate)
    start_date = models.DateField()
    last_due_date = models.DateField()
    expected_maturity_date = models.DateField()
    installment_amount = models.DecimalField(**MONEY)
    number_of_installments = models.PositiveSmallIntegerField()
    planned_contribution = models.DecimalField(**MONEY)
    expected_benefit = models.DecimalField(**MONEY)
    expected_entitlement = models.DecimalField(**MONEY)
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    # Agreement
    agreement_version = models.CharField(max_length=40, blank=True)
    agreement_accepted_at = models.DateTimeField(null=True, blank=True)
    acceptance_method = models.CharField(max_length=10, choices=ACCEPTANCE_METHODS, blank=True)
    signature_reference = models.CharField(max_length=120, blank=True)
    agreement_staff = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    # Maturity
    matured_on = models.DateField(null=True, blank=True)
    redemption_valid_until = models.DateField(null=True, blank=True)
    benefit_eligibility = models.CharField(max_length=20, blank=True, help_text='ELIGIBLE / PENDING / REDUCED / NOT_ELIGIBLE')
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=250, blank=True)
    # Running balances - maintained only by the posting engine, always reproducible from the ledger.
    contribution_paid = models.DecimalField(**MONEY, help_text='Customer money received (net of reversals), incl. unallocated advance.')
    unallocated_advance = models.DecimalField(**MONEY)
    penalty_paid = models.DecimalField(**MONEY)
    benefit_approved = models.DecimalField(**MONEY, help_text='Jeweller benefit granted - never customer cash.')
    contribution_redeemed = models.DecimalField(**MONEY)
    benefit_redeemed = models.DecimalField(**MONEY)
    contribution_refunded = models.DecimalField(**MONEY)
    contribution_forfeited = models.DecimalField(**MONEY)
    benefit_forfeited = models.DecimalField(**MONEY)
    contribution_credited = models.DecimalField(**MONEY, help_text='Converted to customer purchase credit.')
    redemption_count = models.PositiveSmallIntegerField(default=0)
    qr_token = models.UUIDField(default=uuid.uuid4, editable=False, help_text='Opaque identifier printed on the scheme card.')

    class Meta:
        db_table = 'scheme_enrollments'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'account_no'), name='sav_unique_account_no'),
                       models.UniqueConstraint(fields=('qr_token',), name='sav_unique_qr_token')]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'customer')),
                   models.Index(fields=('tenant', 'mobile'))]

    def __str__(self):
        return self.account_no

    @property
    def contribution_balance(self):
        """Customer money still held in the scheme."""
        return (self.contribution_paid - self.contribution_redeemed - self.contribution_refunded - self.contribution_forfeited
                - self.contribution_credited)

    @property
    def benefit_balance(self):
        return self.benefit_approved - self.benefit_redeemed - self.benefit_forfeited

    @property
    def total_entitlement(self):
        return self.contribution_paid + self.benefit_approved

    @property
    def total_redeemed(self):
        return self.contribution_redeemed + self.benefit_redeemed

    @property
    def available_entitlement(self):
        return max(self.contribution_balance + self.benefit_balance, ZERO)

    @property
    def is_open(self):
        return self.status in self.OPEN

    def rule(self, name):
        return self.rules.get(name)

    def dec(self, name):
        return Decimal(str(self.rules.get(name) or 0))


class SchemeNominee(SavingsModel):
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.CASCADE, related_name='nominees')
    name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=60)
    date_of_birth = models.DateField(null=True, blank=True)
    mobile = models.CharField(max_length=15, blank=True)
    address = models.TextField(blank=True)
    id_reference = models.CharField(max_length=60, blank=True, help_text='Masked ID reference only.')
    percentage = models.DecimalField(max_digits=6, decimal_places=2, default=100)
    effective_from = models.DateField(default=dj_timezone.localdate)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'scheme_nominees'


class SchemeInstallment(SavingsModel):
    STATUSES = [('UPCOMING', 'Upcoming'), ('DUE', 'Due'), ('GRACE', 'Grace period'), ('OVERDUE', 'Overdue'), ('PARTIALLY_PAID', 'Partially paid'),
                ('PAID', 'Paid'), ('WAIVED', 'Waived'), ('ADJUSTED', 'Adjusted'), ('CANCELLED', 'Cancelled')]
    SETTLED = ('PAID', 'WAIVED', 'ADJUSTED', 'CANCELLED')
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.CASCADE, related_name='installments')
    installment_no = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    scheduled_amount = models.DecimalField(**MONEY, help_text='Amount that settles the installment.')
    cap_amount = models.DecimalField(**MONEY, help_text='Most this installment may receive (variable schemes).')
    paid_amount = models.DecimalField(**MONEY)
    eligible_amount = models.DecimalField(**MONEY, help_text='Contribution counted for the benefit.')
    penalty_amount = models.DecimalField(**MONEY)
    first_payment_date = models.DateField(null=True, blank=True)
    settled_date = models.DateField(null=True, blank=True)
    late_days = models.PositiveIntegerField(default=0, help_text='Days past due date when settled (or today, if open).')
    paid_within_grace = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUSES, default='UPCOMING')

    class Meta:
        db_table = 'scheme_installment_schedule'
        ordering = ['enrollment', 'installment_no']
        constraints = [models.UniqueConstraint(fields=('enrollment', 'installment_no'), name='sav_unique_installment')]
        indexes = [models.Index(fields=('tenant', 'due_date', 'status'))]

    @property
    def outstanding(self):
        if self.status in ('WAIVED', 'CANCELLED'):
            return ZERO
        return max(self.scheduled_amount - self.paid_amount, ZERO)

    @property
    def room(self):
        """How much more this installment can accept."""
        if self.status in ('WAIVED', 'CANCELLED'):
            return ZERO
        return max((self.cap_amount or self.scheduled_amount) - self.paid_amount, ZERO)

    @property
    def grace_end(self):
        from datetime import timedelta
        return self.due_date + timedelta(days=int(self.enrollment.rules.get('grace_days') or 0))


# ---------------------------------------------------------------------------
# Money documents (immutable) and the customer scheme ledger
# ---------------------------------------------------------------------------

class SchemePayment(ImmutableModel):
    STATUSES = [('POSTED', 'Posted'), ('REVERSED', 'Reversed'), ('REVERSAL', 'Reversal')]
    receipt_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='payments')
    payment_date = models.DateField(default=dj_timezone.localdate)
    amount = models.DecimalField(**MONEY, help_text='Total received (negative on a reversal).')
    contribution_amount = models.DecimalField(**MONEY)
    penalty_amount = models.DecimalField(**MONEY)
    advance_amount = models.DecimalField(**MONEY, help_text='Left unallocated as scheme advance.')
    payment_method = models.ForeignKey('erp.PaymentMethod', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    method_type = models.CharField(max_length=20, blank=True, help_text='cash / card / upi / bank / cheque / wallet / other')
    bank_account = models.ForeignKey('erp.BankAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reference_no = models.CharField(max_length=80, blank=True)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    collected_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    collected_by_name = models.CharField(max_length=150, blank=True)
    idempotency_key = models.CharField(max_length=80, blank=True)
    remarks = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='POSTED')
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reversed_by = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'scheme_payments'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'receipt_no'), name='sav_unique_receipt_no'),
                       models.UniqueConstraint(fields=('tenant', 'idempotency_key'), condition=~models.Q(idempotency_key=''),
                                               name='sav_unique_payment_idempotency')]
        indexes = [models.Index(fields=('tenant', 'payment_date')), models.Index(fields=('tenant', 'reference_no'))]

    def __str__(self):
        return self.receipt_no


class SchemePaymentAllocation(ImmutableModel):
    TYPES = [('ARREAR', 'Overdue installment'), ('CURRENT', 'Current installment'), ('FUTURE', 'Future installment'),
             ('ADVANCE_APPLIED', 'Applied from unallocated advance')]
    payment = models.ForeignKey(SchemePayment, on_delete=models.PROTECT, related_name='allocations')
    installment = models.ForeignKey(SchemeInstallment, on_delete=models.PROTECT, related_name='allocations')
    allocation_type = models.CharField(max_length=20, choices=TYPES)
    amount = models.DecimalField(**MONEY)
    penalty_amount = models.DecimalField(**MONEY)
    within_grace = models.BooleanField(default=True)

    class Meta:
        db_table = 'scheme_payment_allocations'


class BenefitCalculation(SavingsModel):
    """One run of the benefit engine. Calculated values never change; approval is recorded alongside, and a wrong
    approval is reversed with a ledger reversal plus a fresh calculation."""
    STATUSES = [('CALCULATED', 'Calculated'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'), ('SUPERSEDED', 'Superseded'),
                ('REVERSED', 'Reversed')]
    ELIGIBILITY = [('ELIGIBLE', 'Eligible'), ('PENDING', 'Pending'), ('REDUCED', 'Reduced'), ('NOT_ELIGIBLE', 'Not eligible')]
    calculation_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='benefit_calculations')
    as_of = models.DateField()
    scheduled_contribution = models.DecimalField(**MONEY)
    paid_contribution = models.DecimalField(**MONEY)
    eligible_contribution = models.DecimalField(**MONEY)
    outstanding_contribution = models.DecimalField(**MONEY)
    installments_paid = models.PositiveSmallIntegerField(default=0)
    installments_missed = models.PositiveSmallIntegerField(default=0)
    installments_late = models.PositiveSmallIntegerField(default=0)
    installments_partial = models.PositiveSmallIntegerField(default=0)
    gross_benefit = models.DecimalField(**MONEY, help_text='Before eligibility reductions.')
    calculated_benefit = models.DecimalField(**MONEY)
    eligibility = models.CharField(max_length=20, choices=ELIGIBILITY)
    explanation = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=STATUSES, default='CALCULATED')
    approved_benefit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    override_reason = models.CharField(max_length=250, blank=True)
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'scheme_benefit_calculations'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'calculation_no'), name='sav_unique_benefit_calc_no')]

    def save(self, *args, **kwargs):
        if self.pk:
            frozen = {'calculated_benefit', 'gross_benefit', 'eligible_contribution', 'paid_contribution', 'eligibility'}
            stored = type(self).objects.filter(pk=self.pk).values(*frozen).first()
            if stored and any(stored[f] != getattr(self, f) for f in frozen):
                raise ValidationError('A benefit calculation is never edited; recalculate instead.')
        super().save(*args, **kwargs)


class SchemeRedemption(ImmutableModel):
    STATUSES = [('POSTED', 'Posted'), ('REVERSED', 'Reversed'), ('REVERSAL', 'Reversal')]
    redemption_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='redemptions')
    redemption_date = models.DateField(default=dj_timezone.localdate)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sales_invoice = models.ForeignKey('erp.SalesInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='scheme_redemptions')
    invoice_reference = models.CharField(max_length=60, blank=True)
    invoice_value = models.DecimalField(**MONEY, help_text='Invoice value from the normal pricing / invoice engine.')
    eligible_value = models.DecimalField(**MONEY, help_text='Part of the invoice the scheme may settle.')
    making_charge_value = models.DecimalField(**MONEY)
    contribution_applied = models.DecimalField(**MONEY)
    benefit_applied = models.DecimalField(**MONEY)
    amount = models.DecimalField(**MONEY, help_text='Total scheme settlement (contribution + benefit).')
    balance_payable = models.DecimalField(**MONEY, help_text='Customer pays this through normal payment processing.')
    staff = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    staff_name = models.CharField(max_length=150, blank=True)
    idempotency_key = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='POSTED')
    reason = models.CharField(max_length=250, blank=True)
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reversed_by = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'scheme_redemptions'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'redemption_no'), name='sav_unique_redemption_no'),
                       models.UniqueConstraint(fields=('tenant', 'idempotency_key'), condition=~models.Q(idempotency_key=''),
                                               name='sav_unique_redemption_idempotency')]

    def __str__(self):
        return self.redemption_no


class SchemeRedemptionLine(ImmutableModel):
    redemption = models.ForeignKey(SchemeRedemption, on_delete=models.PROTECT, related_name='lines')
    description = models.CharField(max_length=200)
    item_code = models.CharField(max_length=60, blank=True)
    metal = models.CharField(max_length=20, blank=True)
    category = models.CharField(max_length=100, blank=True)
    collection = models.CharField(max_length=100, blank=True)
    tags = models.CharField(max_length=200, blank=True, help_text='Comma separated, e.g. coin, clearance.')
    line_value = models.DecimalField(**MONEY)
    making_charge = models.DecimalField(**MONEY)
    eligible = models.BooleanField(default=True)
    ineligible_reason = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = 'scheme_redemption_lines'


class SchemeCancellation(SavingsModel):
    TYPES = [('CUSTOMER', 'Customer cancellation'), ('JEWELLER', 'Jeweller cancellation'), ('SYSTEM', 'System cancellation'),
             ('COMPLIANCE', 'Fraud / compliance hold'), ('EXPIRED', 'Expired scheme')]
    STATUSES = [('REQUESTED', 'Requested'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')]
    cancellation_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='cancellations')
    cancellation_type = models.CharField(max_length=20, choices=TYPES, default='CUSTOMER')
    reason = models.CharField(max_length=250)
    contribution_balance = models.DecimalField(**MONEY)
    benefit_balance = models.DecimalField(**MONEY)
    cancellation_charge = models.DecimalField(**MONEY)
    benefit_forfeited = models.DecimalField(**MONEY)
    refundable_amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=20, choices=STATUSES, default='REQUESTED')
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'scheme_cancellations'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'cancellation_no'), name='sav_unique_cancellation_no')]


class SchemeRefund(SavingsModel):
    STATUSES = [('REQUESTED', 'Requested'), ('APPROVED', 'Approved'), ('PAID', 'Paid'), ('REJECTED', 'Rejected')]
    refund_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='refunds')
    cancellation = models.ForeignKey(SchemeCancellation, on_delete=models.PROTECT, null=True, blank=True, related_name='refunds')
    contribution_refund = models.DecimalField(**MONEY, help_text='Customer money returned.')
    deduction = models.DecimalField(**MONEY, help_text='Cancellation / administrative charge kept.')
    benefit_reversal = models.DecimalField(**MONEY, help_text='Benefit forfeited - never paid out as cash.')
    refund_amount = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=250)
    payment_method = models.ForeignKey('erp.PaymentMethod', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    method_type = models.CharField(max_length=20, blank=True)
    bank_account = models.ForeignKey('erp.BankAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bank_details_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='REQUESTED')
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    refund_date = models.DateField(null=True, blank=True)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'scheme_refunds'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'refund_no'), name='sav_unique_refund_no')]


class SchemeAdjustment(SavingsModel):
    """Scheme adjustment journal - maker-checker; posts to the ledger only once approved."""
    TYPES = [('CONTRIBUTION', 'Contribution adjustment'), ('BENEFIT', 'Benefit adjustment'), ('PENALTY', 'Penalty adjustment'),
             ('WAIVER', 'Installment waiver'), ('CORRECTION', 'Correction')]
    STATUSES = [('PENDING_APPROVAL', 'Pending approval'), ('POSTED', 'Posted'), ('REJECTED', 'Rejected')]
    adjustment_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='adjustments')
    adjustment_type = models.CharField(max_length=20, choices=TYPES)
    amount = models.DecimalField(**MONEY, help_text='Signed: + increases the balance, - decreases it.')
    installment = models.ForeignKey(SchemeInstallment, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reason = models.CharField(max_length=250)
    reference = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING_APPROVAL')
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'scheme_adjustments'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'adjustment_no'), name='sav_unique_adjustment_no')]


class SchemeLedgerEntry(ImmutableModel):
    """The customer scheme ledger. Each bucket column is a signed movement; the balances on the enrolment are
    exactly the column sums, so every figure a customer sees can be traced to entries here."""
    TYPES = [('ENROLLMENT', 'Enrollment'), ('PAYMENT', 'Customer payment'), ('ADVANCE', 'Advance payment'),
             ('ALLOCATION', 'Advance allocated to installment'), ('PAYMENT_REVERSAL', 'Payment reversal'), ('PENALTY', 'Late payment penalty'),
             ('BENEFIT_ACCRUAL', 'Benefit accrual'), ('BENEFIT_ADJUSTMENT', 'Benefit adjustment'), ('BENEFIT_REVERSAL', 'Benefit reversal'),
             ('REDEMPTION', 'Redemption'), ('REDEMPTION_REVERSAL', 'Redemption reversal'), ('REFUND', 'Refund'),
             ('CANCELLATION', 'Cancellation'), ('FORFEITURE', 'Forfeiture'), ('CREDIT_TRANSFER', 'Transfer to customer credit'),
             ('ADJUSTMENT', 'Adjustment')]
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='ledger')
    entry_date = models.DateField(default=dj_timezone.localdate)
    entry_type = models.CharField(max_length=30, choices=TYPES)
    document_type = models.CharField(max_length=30)
    document_no = models.CharField(max_length=40)
    description = models.CharField(max_length=250)
    contribution = models.DecimalField(**MONEY)
    advance = models.DecimalField(**MONEY, help_text='Movement in the unallocated part of the contribution.')
    penalty = models.DecimalField(**MONEY)
    benefit = models.DecimalField(**MONEY)
    contribution_redeemed = models.DecimalField(**MONEY)
    benefit_redeemed = models.DecimalField(**MONEY)
    refund = models.DecimalField(**MONEY)
    contribution_forfeited = models.DecimalField(**MONEY)
    benefit_forfeited = models.DecimalField(**MONEY)
    credit_transfer = models.DecimalField(**MONEY)
    entitlement_balance = models.DecimalField(**MONEY, help_text='Available entitlement after this entry.')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    staff = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    staff_name = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=10, default='POSTED')
    reversed_by = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'scheme_customer_ledger'
        ordering = ['id']
        indexes = [models.Index(fields=('tenant', 'entry_date')), models.Index(fields=('enrollment', 'entry_type'))]


class SchemePostingEntry(ImmutableModel):
    """Accounting events generated for each document, whether or not G/L posting is switched on."""
    document_type = models.CharField(max_length=30)
    document_no = models.CharField(max_length=40)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.PROTECT, related_name='postings')
    posting_date = models.DateField()
    account_role = models.CharField(max_length=40, help_text='e.g. contribution_liability, benefit_expense.')
    account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    debit = models.DecimalField(**MONEY)
    credit = models.DecimalField(**MONEY)
    status = models.CharField(max_length=20, default='RECORDED', help_text='RECORDED (G/L off) or POSTED.')
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'scheme_posting_entries'
        ordering = ['id']
        indexes = [models.Index(fields=('tenant', 'document_no'))]


class SchemeNotification(SavingsModel):
    EVENTS = [('ENROLLMENT', 'Enrollment confirmation'), ('DUE', 'Due reminder'), ('PAYMENT', 'Payment confirmation'),
              ('OVERDUE', 'Missed payment reminder'), ('MATURITY', 'Maturity notification'), ('BENEFIT', 'Benefit confirmation'),
              ('REDEMPTION', 'Redemption confirmation'), ('REFUND', 'Refund notification')]
    CHANNELS = [('SMS', 'SMS'), ('WHATSAPP', 'WhatsApp'), ('EMAIL', 'Email'), ('PUSH', 'Push')]
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.CASCADE, related_name='notifications')
    event = models.CharField(max_length=20, choices=EVENTS)
    channel = models.CharField(max_length=10, choices=CHANNELS, default='SMS')
    recipient = models.CharField(max_length=150, blank=True)
    message = models.TextField()
    dedupe_key = models.CharField(max_length=120, help_text='Makes scheduled reminders idempotent.')
    status = models.CharField(max_length=10, default='QUEUED', help_text='QUEUED until the notification engine sends it.')

    class Meta:
        db_table = 'scheme_notifications'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'dedupe_key', 'channel'), name='sav_unique_notification')]


class SchemeAuditLog(models.Model):
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.PROTECT, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=40)
    document_type = models.CharField(max_length=40)
    document_no = models.CharField(max_length=60, blank=True)
    enrollment = models.ForeignKey(SchemeEnrollment, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = 'scheme_audit_logs'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'document_type', 'document_no')), models.Index(fields=('tenant', 'created_at'))]
