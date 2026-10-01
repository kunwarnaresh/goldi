"""Goldio manufacturing & production.

Three master structures drive production: the Production BOM (WHAT materials), the Routing (HOW it
is made) and Work/Machine Centers (WHERE / BY WHOM). A Production Order snapshots one certified BOM
version and one certified routing version, so later master changes never rewrite history.

Nothing here moves stock or money directly. Every consumption, output, runtime and scrap posting goes
through ``manufacturing.engine.ManufacturingPostingEngine``, which calls the central
``inventory.engine.InventoryPostingEngine`` for stock and the ERP finance engine for the G/L, and
writes the immutable production ledgers below in the same database transaction.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone as dj_timezone

from inventory.models import METALS, MONEY, QTY, WEIGHT, ZERO, TenantModel, TenantQuerySet

MINUTES = dict(max_digits=12, decimal_places=2, default=0)
RATE = dict(max_digits=14, decimal_places=2, default=0)
PERCENT = dict(max_digits=7, decimal_places=3, default=0)
COST = dict(max_digits=18, decimal_places=4, default=0)
USER = settings.AUTH_USER_MODEL


class MfgModel(TenantModel):
    """Tenant-scoped row with the owning company (the ERP legal entity) alongside the audit columns."""
    company = models.ForeignKey('erp.Company', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        abstract = True


class LedgerQuerySet(TenantQuerySet):
    def delete(self):
        raise ValidationError('Posted production entries are immutable; post a reversal instead.')


class LedgerModel(MfgModel):
    """Posted production entries: never updated (except the `reversed` marker) and never deleted."""
    MUTABLE = {'reversed', 'updated_at', 'updated_by'}
    posting_date = models.DateField()
    batch = models.ForeignKey('PostingBatch', on_delete=models.PROTECT, related_name='+')
    reversed = models.BooleanField(default=False)
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversals')

    objects = LedgerQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, _engine=False, **kwargs):
        # `_engine` lets the posting engine complete a row it is still writing inside the same posting.
        update_fields = kwargs.get('update_fields')
        if self.pk and not _engine and not (update_fields and set(update_fields) <= self.MUTABLE):
            raise ValidationError('Posted production entries are immutable; post a reversal instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Posted production entries are immutable; post a reversal instead.')

    @property
    def status(self):
        return 'REVERSAL' if self.reversal_of_id else ('REVERSED' if self.reversed else 'POSTED')


# ---------------------------------------------------------------------------
# Setup, security and calendars
# ---------------------------------------------------------------------------

COSTING_METHODS = [('STANDARD', 'Standard (variance at finish)'), ('ACTUAL', 'Actual (output revalued at finish)')]
FLUSHING = [('MANUAL', 'Manual'), ('FORWARD', 'Forward flush'), ('BACKWARD', 'Backward flush'), ('PICK_FORWARD', 'Pick + forward'),
            ('PICK_BACKWARD', 'Pick + backward')]
OUTPUT_METHODS = [('MANUAL', 'Manual'), ('LAST_OPERATION', 'Automatic on last operation')]


class ManufacturingSetup(MfgModel):
    manufacturing_enabled = models.BooleanField(default=True)
    # Defaults
    default_production_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_wip_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_production_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                               help_text='To-production bin: issued material waits here in PICKED state.')
    default_material_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_finished_goods_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_finished_goods_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_scrap_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_qc_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_rework_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    default_work_center = models.ForeignKey('WorkCenter', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_calendar = models.ForeignKey('ShopCalendar', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_shift = models.ForeignKey('Shift', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_costing_method = models.CharField(max_length=20, choices=COSTING_METHODS, default='STANDARD')
    default_consumption_method = models.CharField(max_length=20, choices=FLUSHING, default='MANUAL')
    default_output_method = models.CharField(max_length=20, choices=OUTPUT_METHODS, default='MANUAL')
    # Execution rules
    allow_over_consumption = models.BooleanField(default=False)
    over_consumption_tolerance_percent = models.DecimalField(**PERCENT)
    allow_over_output = models.BooleanField(default=False)
    allow_partial_output = models.BooleanField(default=True)
    allow_finish_with_remaining = models.BooleanField(default=False, help_text='Finish even if output is below planned quantity.')
    allow_release_with_shortage = models.BooleanField(default=False, help_text='Managers may always override.')
    require_material_issue = models.BooleanField(default=True, help_text='Warehouse-controlled: consume only picked material.')
    require_quality_check = models.BooleanField(default=True)
    require_manager_approval = models.BooleanField(default=True, help_text='Order approval before release; reversals by managers.')
    require_huid_on_final_qc = models.BooleanField(default=False)
    require_hallmark_on_final_qc = models.BooleanField(default=False)
    scrap_requires_approval = models.BooleanField(default=True)
    allow_self_approval = models.BooleanField(default=False, help_text='Maker-checker: when off, the approver must differ from the maker.')
    enforce_operation_sequence = models.BooleanField(default=True, help_text="An operation's output cannot exceed the previous operation's.")
    auto_reserve_material = models.BooleanField(default=False)
    auto_calculate_consumption = models.BooleanField(default=True)
    auto_post_consumption = models.BooleanField(default=True, help_text='Flushed components post automatically.')
    auto_post_output = models.BooleanField(default=False)
    auto_post_runtime = models.BooleanField(default=True, help_text='Completing an operation posts its clocked runtime.')
    wip_enabled = models.BooleanField(default=True)
    overhead_enabled = models.BooleanField(default=True)
    material_overhead_percent = models.DecimalField(**PERCENT)
    subcontracting_enabled = models.BooleanField(default=True)
    # Financial posting (through the ERP finance engine)
    gl_posting_enabled = models.BooleanField(default=False)
    raw_material_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finished_goods_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    wip_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    labour_applied_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    machine_applied_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    overhead_applied_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    subcontract_applied_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variance_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_inventory_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_loss_account = models.ForeignKey('erp.GLAccount', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    cost_centre = models.CharField(max_length=40, blank=True)

    class Meta:
        db_table = 'manufacturing_setup'
        constraints = [models.UniqueConstraint(fields=('tenant',), name='mfg_unique_setup_per_tenant')]


class ManufacturingRole(MfgModel):
    ROLES = [
        ('OPERATOR', 'Manufacturing operator'), ('SUPERVISOR', 'Production supervisor'), ('MANAGER', 'Manufacturing manager'),
        ('QUALITY', 'Quality inspector'), ('COSTING', 'Costing manager'), ('FINANCE', 'Finance'), ('AUDITOR', 'Auditor (read-only)'),
        ('BOM_MAKER', 'BOM / routing maker'), ('BOM_REVIEWER', 'BOM / routing reviewer'), ('BOM_APPROVER', 'BOM / routing approver'),
    ]
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='manufacturing_roles')
    role = models.CharField(max_length=20, choices=ROLES)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'manufacturing_roles'
        constraints = [models.UniqueConstraint(fields=('tenant', 'user', 'role'), name='mfg_unique_user_role')]

    def __str__(self):
        return f'{self.user} - {self.get_role_display()}'


class ShopCalendar(MfgModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    monday = models.BooleanField(default=True)
    tuesday = models.BooleanField(default=True)
    wednesday = models.BooleanField(default=True)
    thursday = models.BooleanField(default=True)
    friday = models.BooleanField(default=True)
    saturday = models.BooleanField(default=True)
    sunday = models.BooleanField(default=False)
    start_time = models.TimeField(default='10:00')
    end_time = models.TimeField(default='19:00')
    break_minutes = models.PositiveIntegerField(default=60)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'shop_calendars'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_calendar')]

    def __str__(self):
        return f'{self.code} - {self.name}'

    WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')

    def works_on(self, day):
        return getattr(self, self.WEEKDAYS[day.weekday()])

    @property
    def daily_minutes(self):
        start = self.start_time.hour * 60 + self.start_time.minute
        end = self.end_time.hour * 60 + self.end_time.minute
        return max(end - start - self.break_minutes, 0)


class ShopCalendarLine(MfgModel):
    """Exceptions to the weekly pattern: holidays, shutdowns, overtime and machine / staff unavailability."""
    TYPES = [('HOLIDAY', 'Holiday'), ('SHUTDOWN', 'Shutdown'), ('OVERTIME', 'Overtime'), ('REDUCED', 'Reduced hours')]
    calendar = models.ForeignKey(ShopCalendar, on_delete=models.CASCADE, related_name='lines')
    date = models.DateField()
    line_type = models.CharField(max_length=20, choices=TYPES, default='HOLIDAY')
    minutes = models.PositiveIntegerField(default=0, help_text='Extra (overtime) or remaining (reduced) working minutes.')
    description = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = 'shop_calendar_lines'
        ordering = ['date']
        constraints = [models.UniqueConstraint(fields=('calendar', 'date'), name='mfg_unique_calendar_date')]


class Shift(MfgModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    calendar = models.ForeignKey(ShopCalendar, on_delete=models.SET_NULL, null=True, blank=True, related_name='shifts')
    start_time = models.TimeField(default='10:00')
    end_time = models.TimeField(default='19:00')
    break_minutes = models.PositiveIntegerField(default=60)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'shifts'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_shift')]

    def __str__(self):
        return f'{self.code} - {self.name}'


# ---------------------------------------------------------------------------
# Capacity: work centre groups, work centres, machine centres, resources, tools
# ---------------------------------------------------------------------------

class WorkCenterGroup(MfgModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)

    class Meta:
        db_table = 'work_center_groups'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_wc_group')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class WorkCenter(MfgModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    group = models.ForeignKey(WorkCenterGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='work_centers')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    department = models.CharField(max_length=60, blank=True)
    capacity = models.DecimalField(max_digits=8, decimal_places=2, default=1, help_text='Parallel resources (karigars / benches).')
    efficiency = models.DecimalField(max_digits=7, decimal_places=2, default=100)
    direct_labour_rate = models.DecimalField(**RATE, help_text='Per hour.')
    indirect_labour_rate = models.DecimalField(**RATE, help_text='Per hour.')
    overhead_rate = models.DecimalField(**RATE, help_text='Per hour.')
    calendar = models.ForeignKey(ShopCalendar, on_delete=models.SET_NULL, null=True, blank=True, related_name='work_centers')
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    queue_time = models.DecimalField(**MINUTES)
    working_minutes_per_day = models.PositiveIntegerField(default=0, help_text='0 = from the calendar.')
    subcontractor = models.ForeignKey('Subcontractor', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'work_centers'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_work_center')]

    def __str__(self):
        return f'{self.code} - {self.name}'

    @property
    def unit_cost(self):
        """Cost per hour of this centre's capacity."""
        return self.direct_labour_rate + self.indirect_labour_rate + self.overhead_rate

    @property
    def usable(self):
        return self.active and not self.blocked


class MachineCenter(MfgModel):
    STATUSES = [('AVAILABLE', 'Available'), ('RUNNING', 'Running'), ('IDLE', 'Idle'), ('MAINTENANCE', 'Maintenance'),
                ('BREAKDOWN', 'Breakdown'), ('BLOCKED', 'Blocked')]
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name='machines')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    capacity = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    machine_rate = models.DecimalField(**RATE, help_text='Per hour.')
    operating_cost = models.DecimalField(**RATE, help_text='Per hour (power, consumables).')
    efficiency = models.DecimalField(max_digits=7, decimal_places=2, default=100)
    availability = models.DecimalField(max_digits=7, decimal_places=2, default=100, help_text='% of calendar time available.')
    maintenance_schedule = models.CharField(max_length=150, blank=True)
    next_maintenance_date = models.DateField(null=True, blank=True)
    calendar = models.ForeignKey(ShopCalendar, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=20, choices=STATUSES, default='AVAILABLE')

    class Meta:
        db_table = 'machine_centers'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_machine')]

    def __str__(self):
        return f'{self.code} - {self.name}'

    @property
    def hourly_cost(self):
        return self.machine_rate + self.operating_cost

    def clean(self):
        self.check_same_tenant('work_center', 'location')


class ProductionResource(MfgModel):
    """A karigar / operator / employee who can be assigned to operations."""
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='resources')
    skill = models.CharField(max_length=100, blank=True)
    labour_rate = models.DecimalField(**RATE, help_text='Per hour; 0 = use the work centre rate.')
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'production_resources'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_resource')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class Tool(MfgModel):
    STATUSES = [('AVAILABLE', 'Available'), ('IN_USE', 'In use'), ('MAINTENANCE', 'Maintenance'), ('RETIRED', 'Retired')]
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='tools')
    tool_type = models.CharField(max_length=60, blank=True, help_text='Die, mould, wax tree flask...')
    life_cycles = models.PositiveIntegerField(default=0)
    used_cycles = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='AVAILABLE')

    class Meta:
        db_table = 'production_tools'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_tool')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class Subcontractor(MfgModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    vendor = models.ForeignKey('erp.Supplier', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 help_text="Inventory location representing material held at the vendor's premises.")
    process = models.CharField(max_length=100, blank=True)
    rate_per_unit = models.DecimalField(**RATE)
    rate_per_gram = models.DecimalField(**RATE)
    allowed_loss_percent = models.DecimalField(**PERCENT)
    lead_time_days = models.PositiveSmallIntegerField(default=3)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'subcontractors'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_subcontractor')]

    def __str__(self):
        return f'{self.code} - {self.name}'


# ---------------------------------------------------------------------------
# Reasons, quality parameters, standard operations
# ---------------------------------------------------------------------------

SCRAP_TYPES = [('PROCESS_LOSS', 'Process loss'), ('METAL_LOSS', 'Metal loss'), ('STONE_DAMAGE', 'Stone damage'),
               ('CASTING_DEFECT', 'Casting defect'), ('POLISHING_LOSS', 'Polishing loss'), ('MANUFACTURING_DEFECT', 'Manufacturing defect'),
               ('BREAKAGE', 'Breakage'), ('REJECTED_MATERIAL', 'Rejected material'), ('OTHER', 'Other')]


class ScrapReason(MfgModel):
    code = models.CharField(max_length=20)
    description = models.CharField(max_length=150)
    scrap_type = models.CharField(max_length=30, choices=SCRAP_TYPES, default='PROCESS_LOSS')
    recoverable = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'scrap_reasons'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_scrap_reason')]

    def __str__(self):
        return f'{self.code} - {self.description}'


class DowntimeReason(MfgModel):
    code = models.CharField(max_length=20)
    description = models.CharField(max_length=150)
    planned = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'downtime_reasons'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_downtime_reason')]

    def __str__(self):
        return f'{self.code} - {self.description}'


class QualityParameter(MfgModel):
    TYPES = [('WEIGHT', 'Weight'), ('PURITY', 'Purity'), ('DIMENSION', 'Dimensions'), ('STONE_COUNT', 'Stone count'),
             ('STONE_WEIGHT', 'Stone weight'), ('FINISH', 'Finish'), ('DESIGN', 'Design'), ('HALLMARK', 'Hallmark'), ('HUID', 'HUID'),
             ('CERTIFICATE', 'Certificate'), ('VISUAL', 'Visual quality')]
    STAGES = [('ALL', 'All stages'), ('INCOMING', 'Incoming material'), ('OPERATION', 'Operation'), ('FINAL', 'Final production'),
              ('JEWELLERY', 'Jewellery inspection')]
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    parameter_type = models.CharField(max_length=20, choices=TYPES, default='VISUAL')
    stage = models.CharField(max_length=20, choices=STAGES, default='ALL')
    uom = models.CharField(max_length=10, blank=True)
    min_value = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    mandatory = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'quality_parameters'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_quality_parameter')]

    def __str__(self):
        return f'{self.code} - {self.name}'


class StandardOperation(MfgModel):
    """Reusable operation standard (Melting, Casting...) that routing lines start from."""
    code = models.CharField(max_length=20)
    description = models.CharField(max_length=150)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    setup_time = models.DecimalField(**MINUTES)
    run_time = models.DecimalField(**MINUTES, help_text='Minutes per unit.')
    wait_time = models.DecimalField(**MINUTES)
    move_time = models.DecimalField(**MINUTES)
    queue_time = models.DecimalField(**MINUTES)
    quality_check_required = models.BooleanField(default=False)
    expected_loss_percent = models.DecimalField(**PERCENT)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'production_operations'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='mfg_unique_operation')]

    def __str__(self):
        return f'{self.code} - {self.description}'


# ---------------------------------------------------------------------------
# Item manufacturing profile
# ---------------------------------------------------------------------------

MANUFACTURING_TYPES = [('PURCHASED', 'Purchased'), ('MANUFACTURED', 'Manufactured'), ('SUBCONTRACTED', 'Subcontracted'),
                       ('ASSEMBLY', 'Assembly'), ('SERVICE', 'Service'), ('NON_STOCK', 'Non-stock')]


class ItemManufacturingProfile(MfgModel):
    """Manufacturing and jewellery attributes of an inventory item (the inventory Item stays the one item master)."""
    item = models.OneToOneField('inventory.Item', on_delete=models.CASCADE, related_name='manufacturing')
    manufacturing_type = models.CharField(max_length=20, choices=MANUFACTURING_TYPES, default='PURCHASED')
    jewellery_type = models.CharField(max_length=60, blank=True, help_text='Ring, chain, bangle...')
    design_code = models.CharField(max_length=60, blank=True)
    collection = models.CharField(max_length=100, blank=True)
    style = models.CharField(max_length=60, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    size = models.CharField(max_length=30, blank=True)
    colour = models.CharField(max_length=30, blank=True)
    finish = models.CharField(max_length=60, blank=True)
    gross_weight = models.DecimalField(**WEIGHT)
    net_metal_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    other_weight = models.DecimalField(**WEIGHT)
    making_charge = models.DecimalField(**MONEY)
    wastage_percent = models.DecimalField(**PERCENT)
    hallmark_required = models.BooleanField(default=False)
    huid_required = models.BooleanField(default=False)
    certificate_required = models.BooleanField(default=False)
    default_bom = models.ForeignKey('ProductionBOM', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_routing = models.ForeignKey('Routing', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    lot_size = models.DecimalField(**QTY)
    production_lead_days = models.PositiveSmallIntegerField(default=7)
    standard_cost = models.DecimalField(**MONEY, help_text='Standard unit cost (rolled up from the certified BOM + routing).')

    class Meta:
        db_table = 'item_manufacturing_profiles'

    def __str__(self):
        return f'{self.item.item_no} ({self.get_manufacturing_type_display()})'


# ---------------------------------------------------------------------------
# Production BOM (versioned, maker-checker)
# ---------------------------------------------------------------------------

VERSION_STATUSES = [('DRAFT', 'Draft'), ('SUBMITTED', 'Under review'), ('REVIEWED', 'Reviewed'), ('APPROVED', 'Approved'),
                    ('CERTIFIED', 'Certified'), ('BLOCKED', 'Blocked'), ('EXPIRED', 'Expired'), ('ARCHIVED', 'Archived')]


class VersionMixin(models.Model):
    version_no = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=VERSION_STATUSES, default='DRAFT')
    effective_from = models.DateField(default=dj_timezone.localdate)
    effective_to = models.DateField(null=True, blank=True)
    change_note = models.CharField(max_length=250, blank=True)
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    certified_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    certified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def editable(self):
        return self.status == 'DRAFT'

    def is_active_on(self, day):
        return self.status == 'CERTIFIED' and self.effective_from <= day and (self.effective_to is None or self.effective_to >= day)

    @property
    def code(self):
        return f'V{self.version_no}'


class ProductionBOM(MfgModel):
    bom_no = models.CharField(max_length=40)
    bom_name = models.CharField(max_length=150)
    description = models.CharField(max_length=250, blank=True)
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='production_boms')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    manufacturing_type = models.CharField(max_length=20, choices=MANUFACTURING_TYPES, default='MANUFACTURED')
    costing_method = models.CharField(max_length=20, choices=COSTING_METHODS, blank=True, help_text='Blank = setup default.')
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'production_boms'
        ordering = ['bom_no']
        constraints = [models.UniqueConstraint(fields=('tenant', 'bom_no'), name='mfg_unique_bom_no')]
        indexes = [models.Index(fields=('tenant', 'item'))]

    def __str__(self):
        return f'{self.bom_no} - {self.bom_name}'

    def clean(self):
        self.check_same_tenant('item', 'sku', 'variant', 'location')

    def active_version(self, day=None):
        day = day or dj_timezone.localdate()
        return self.versions.filter(status='CERTIFIED', effective_from__lte=day).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=day)).order_by('-effective_from', '-version_no').first()

    @property
    def status(self):
        if self.blocked:
            return 'BLOCKED'
        active = self.active_version()
        return 'ACTIVE' if active else (self.versions.order_by('-version_no').values_list('status', flat=True).first() or 'DRAFT')


class BOMVersion(VersionMixin, MfgModel):
    bom = models.ForeignKey(ProductionBOM, on_delete=models.CASCADE, related_name='versions')
    base_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=1)
    base_uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    expected_net_weight = models.DecimalField(**WEIGHT, help_text='Expected net metal weight per piece (formula variable).')
    expected_gross_weight = models.DecimalField(**WEIGHT)
    expected_stone_weight = models.DecimalField(**WEIGHT)

    class Meta:
        db_table = 'production_bom_versions'
        ordering = ['bom', '-version_no']
        constraints = [models.UniqueConstraint(fields=('bom', 'version_no'), name='mfg_unique_bom_version')]

    def __str__(self):
        return f'{self.bom.bom_no} {self.code}'


COMPONENT_TYPES = [('RAW_MATERIAL', 'Raw material'), ('SEMI_FINISHED', 'Semi-finished'), ('COMPONENT', 'Component'), ('STONE', 'Stone'),
                   ('DIAMOND', 'Diamond'), ('CONSUMABLE', 'Consumable'), ('PACKAGING', 'Packaging'), ('SUBASSEMBLY', 'Subassembly'),
                   ('TOOL', 'Tool'), ('BY_PRODUCT', 'By-product'), ('CO_PRODUCT', 'Co-product'), ('SCRAP', 'Scrap')]
CONSUMPTION_BASIS = [('QUANTITY', 'Quantity per piece'), ('WEIGHT', 'Weight per piece'), ('PERCENT', '% of net metal weight'),
                     ('FORMULA', 'Formula')]
SUPPLY_METHODS = [('INVENTORY', 'From inventory'), ('PURCHASE', 'Purchase'), ('PRODUCTION', 'Produce (sub-BOM)'),
                  ('SUBCONTRACT', 'Subcontract'), ('CUSTOMER', 'Customer supplied')]


class BOMLine(MfgModel):
    version = models.ForeignKey(BOMVersion, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    component_item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='bom_usages')
    component_sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=200, blank=True)
    component_type = models.CharField(max_length=20, choices=COMPONENT_TYPES, default='RAW_MATERIAL')
    consumption_basis = models.CharField(max_length=20, choices=CONSUMPTION_BASIS, default='QUANTITY')
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0, help_text='Per base quantity of the parent (or % for PERCENT).')
    formula = models.CharField(max_length=250, blank=True, help_text='Per-piece requirement, e.g. net_weight * (1 + wastage_percent / 100).')
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_percent = models.DecimalField(**PERCENT)
    fixed_scrap_qty = models.DecimalField(**QTY)
    expected_loss_percent = models.DecimalField(**PERCENT)
    expected_loss_qty = models.DecimalField(**QTY)
    position = models.CharField(max_length=40, blank=True)
    operation_no = models.CharField(max_length=10, blank=True)
    routing_link_code = models.CharField(max_length=20, blank=True, help_text='Consumed at the routing operation with the same link code.')
    supply_method = models.CharField(max_length=20, choices=SUPPLY_METHODS, default='INVENTORY')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    flushing_method = models.CharField(max_length=20, choices=FLUSHING, blank=True, help_text='Blank = setup default.')
    substitute_item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    batch_required = models.BooleanField(default=False)
    serial_required = models.BooleanField(default=False)
    lot_required = models.BooleanField(default=False)
    metal = models.CharField(max_length=20, choices=METALS, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    component_cost = models.DecimalField(**COST, help_text='Unit cost used for standard costing (0 = current inventory cost).')
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    optional = models.BooleanField(default=False)
    backflush_enabled = models.BooleanField(default=False)

    class Meta:
        db_table = 'production_bom_lines'
        ordering = ['line_no']
        constraints = [models.UniqueConstraint(fields=('version', 'line_no'), name='mfg_unique_bom_line')]

    def clean(self):
        self.check_same_tenant('component_item', 'component_sku', 'variant', 'location', 'bin', 'substitute_item')
        if self.consumption_basis == 'FORMULA' and not self.formula:
            raise ValidationError({'formula': 'Enter the formula for a formula-based component.'})
        if self.bin_id and self.location_id and self.bin.location_id != self.location_id:
            raise ValidationError({'bin': 'Bin must be in the component location.'})


# ---------------------------------------------------------------------------
# Routing (versioned, maker-checker)
# ---------------------------------------------------------------------------

class Routing(MfgModel):
    TYPES = [('SERIAL', 'Serial'), ('PARALLEL', 'Parallel')]
    routing_no = models.CharField(max_length=40)
    description = models.CharField(max_length=150)
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='routings')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    routing_type = models.CharField(max_length=10, choices=TYPES, default='SERIAL')
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'routings'
        ordering = ['routing_no']
        constraints = [models.UniqueConstraint(fields=('tenant', 'routing_no'), name='mfg_unique_routing_no')]

    def __str__(self):
        return f'{self.routing_no} - {self.description}'

    def active_version(self, day=None):
        day = day or dj_timezone.localdate()
        return self.versions.filter(status='CERTIFIED', effective_from__lte=day).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=day)).order_by('-effective_from', '-version_no').first()

    @property
    def status(self):
        if self.blocked:
            return 'BLOCKED'
        active = self.active_version()
        return 'ACTIVE' if active else (self.versions.order_by('-version_no').values_list('status', flat=True).first() or 'DRAFT')


class RoutingVersion(VersionMixin, MfgModel):
    routing = models.ForeignKey(Routing, on_delete=models.CASCADE, related_name='versions')

    class Meta:
        db_table = 'routing_versions'
        ordering = ['routing', '-version_no']
        constraints = [models.UniqueConstraint(fields=('routing', 'version_no'), name='mfg_unique_routing_version')]

    def __str__(self):
        return f'{self.routing.routing_no} {self.code}'


class RoutingLine(MfgModel):
    version = models.ForeignKey(RoutingVersion, on_delete=models.CASCADE, related_name='lines')
    operation_no = models.CharField(max_length=10)
    sequence = models.PositiveIntegerField(default=0)
    standard_operation = models.ForeignKey(StandardOperation, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=150)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name='routing_lines')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.PROTECT, null=True, blank=True, related_name='routing_lines')
    resource = models.ForeignKey(ProductionResource, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    setup_time = models.DecimalField(**MINUTES)
    run_time = models.DecimalField(**MINUTES, help_text='Minutes per unit.')
    wait_time = models.DecimalField(**MINUTES)
    move_time = models.DecimalField(**MINUTES)
    queue_time = models.DecimalField(**MINUTES)
    minimum_process_time = models.DecimalField(**MINUTES)
    maximum_process_time = models.DecimalField(**MINUTES)
    concurrent_capacity = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    efficiency = models.DecimalField(max_digits=7, decimal_places=2, default=100)
    scrap_percent = models.DecimalField(**PERCENT)
    scrap_reason = models.ForeignKey(ScrapReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    send_ahead_quantity = models.DecimalField(**QTY)
    routing_link_code = models.CharField(max_length=20, blank=True)
    quality_check_required = models.BooleanField(default=False)
    subcontracting = models.BooleanField(default=False)
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    labour_rate = models.DecimalField(**RATE, help_text='Per hour; 0 = work centre direct + indirect labour rate.')
    machine_rate = models.DecimalField(**RATE, help_text='Per hour; 0 = machine centre hourly cost.')
    overhead_rate = models.DecimalField(**RATE, help_text='Per hour; 0 = work centre overhead rate.')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'routing_lines'
        ordering = ['sequence', 'operation_no']
        constraints = [models.UniqueConstraint(fields=('version', 'operation_no'), name='mfg_unique_routing_operation')]

    def clean(self):
        self.check_same_tenant('work_center', 'machine_center', 'resource', 'standard_operation', 'subcontractor')
        if self.machine_center_id and self.machine_center.work_center_id != self.work_center_id:
            raise ValidationError({'machine_center': 'Machine centre belongs to another work centre.'})
        if self.efficiency <= 0:
            raise ValidationError({'efficiency': 'Efficiency must be greater than zero.'})

    def rates(self):
        wc, mc = self.work_center, self.machine_center
        return {
            'labour': self.labour_rate or (wc.direct_labour_rate + wc.indirect_labour_rate),
            'machine': self.machine_rate or (mc.hourly_cost if mc else ZERO),
            'overhead': self.overhead_rate or wc.overhead_rate,
        }

    @property
    def cost_per_hour(self):
        return sum(self.rates().values(), ZERO)


# ---------------------------------------------------------------------------
# Production orders
# ---------------------------------------------------------------------------

PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent')]


class ProductionOrder(MfgModel):
    STATUSES = [
        ('DRAFT', 'Draft'), ('PLANNED', 'Planned'), ('FIRM_PLANNED', 'Firm planned'), ('APPROVED', 'Approved'),
        ('RELEASED', 'Released'), ('MATERIAL_RESERVED', 'Material reserved'), ('MATERIAL_ISSUED', 'Material issued'),
        ('IN_PRODUCTION', 'In production'), ('PARTIALLY_COMPLETED', 'Partially completed'), ('QC_PENDING', 'QC pending'),
        ('QC_APPROVED', 'QC approved'), ('COMPLETED', 'Completed'), ('FINISHED', 'Finished'), ('CANCELLED', 'Cancelled'),
        ('CLOSED', 'Closed'),
    ]
    PLANNING_STATUSES = ('DRAFT', 'PLANNED', 'FIRM_PLANNED', 'APPROVED')
    EXECUTION_STATUSES = ('RELEASED', 'MATERIAL_RESERVED', 'MATERIAL_ISSUED', 'IN_PRODUCTION', 'PARTIALLY_COMPLETED',
                          'QC_PENDING', 'QC_APPROVED', 'COMPLETED')
    CLOSED_STATUSES = ('FINISHED', 'CANCELLED', 'CLOSED')
    ORDER_TYPES = [('STANDARD', 'Standard'), ('MAKE_TO_ORDER', 'Make to order'), ('REWORK', 'Rework'), ('REPAIR', 'Repair')]
    SOURCES = [('MANUAL', 'Manual'), ('SALES_ORDER', 'Sales order'), ('PLANNING', 'Planning'), ('REORDER', 'Reorder / demand'),
               ('REPLENISHMENT', 'Stock replenishment'), ('MAKE_TO_ORDER', 'Make to order'), ('IMPORT', 'Excel import'),
               ('REWORK', 'Rework')]

    order_no = models.CharField(max_length=40)
    order_type = models.CharField(max_length=20, choices=ORDER_TYPES, default='STANDARD')
    status = models.CharField(max_length=24, choices=STATUSES, default='DRAFT')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+', help_text='Production location.')
    department = models.CharField(max_length=60, blank=True)
    production_unit = models.CharField(max_length=60, blank=True)
    source_type = models.CharField(max_length=20, choices=SOURCES, default='MANUAL')
    source_no = models.CharField(max_length=60, blank=True)
    sales_order = models.ForeignKey('erp.SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    customer = models.ForeignKey('erp.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    project = models.CharField(max_length=60, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITIES, default='NORMAL')
    # Product
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='production_orders')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=200, blank=True)
    bom = models.ForeignKey(ProductionBOM, on_delete=models.PROTECT, null=True, blank=True, related_name='orders')
    bom_version = models.ForeignKey(BOMVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='orders')
    bom_effective_date = models.DateField(null=True, blank=True)
    routing = models.ForeignKey(Routing, on_delete=models.PROTECT, null=True, blank=True, related_name='orders')
    routing_version = models.ForeignKey(RoutingVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='orders')
    # Quantities (maintained by the posting engine)
    planned_qty = models.DecimalField(**QTY)
    released_qty = models.DecimalField(**QTY)
    started_qty = models.DecimalField(**QTY)
    produced_qty = models.DecimalField(**QTY, help_text='Good output of the final operation.')
    accepted_qty = models.DecimalField(**QTY)
    rejected_qty = models.DecimalField(**QTY)
    scrap_qty = models.DecimalField(**QTY)
    qc_pending_qty = models.DecimalField(**QTY)
    # Dates
    order_date = models.DateField(default=dj_timezone.localdate)
    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)
    # Inventory destinations (all actual inventory locations/bins of the multi-location module)
    material_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    material_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    production_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    wip_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finished_goods_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finished_goods_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    qc_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    qc_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    rework_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # Planned (standard) cost snapshot, written by Refresh
    costing_method = models.CharField(max_length=20, choices=COSTING_METHODS, default='STANDARD')
    planned_material_cost = models.DecimalField(**MONEY)
    planned_labour_cost = models.DecimalField(**MONEY)
    planned_machine_cost = models.DecimalField(**MONEY)
    planned_overhead_cost = models.DecimalField(**MONEY)
    planned_subcontract_cost = models.DecimalField(**MONEY)
    planned_byproduct_credit = models.DecimalField(**MONEY)
    planned_total_cost = models.DecimalField(**MONEY)
    planned_unit_cost = models.DecimalField(**COST, help_text='Expected cost per good unit - output is valued at this.')
    planned_minutes = models.DecimalField(**MINUTES)
    expected_net_weight = models.DecimalField(**WEIGHT, help_text='Expected net metal weight per piece.')
    # Control
    refreshed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    released_at = models.DateTimeField(null=True, blank=True)
    shortage_override_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finished_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=250, blank=True)
    parent_order = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_orders')
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'production_orders'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'order_no'), name='mfg_unique_order_no')]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'item')),
                   models.Index(fields=('tenant', 'due_date'))]

    def __str__(self):
        return self.order_no

    def clean(self):
        self.check_same_tenant('location', 'item', 'sku', 'variant', 'bom', 'routing', 'material_location', 'finished_goods_location')
        if self.planned_qty is not None and self.planned_qty <= 0:
            raise ValidationError({'planned_qty': 'Quantity must be greater than zero.'})

    @property
    def remaining_qty(self):
        return max(self.planned_qty - self.produced_qty, ZERO)

    @property
    def is_planning(self):
        return self.status in self.PLANNING_STATUSES

    @property
    def is_executable(self):
        return self.status in self.EXECUTION_STATUSES

    @property
    def is_closed(self):
        return self.status in self.CLOSED_STATUSES

    @property
    def is_late(self):
        return bool(self.due_date and not self.is_closed and self.due_date < dj_timezone.localdate())

    @property
    def progress_percent(self):
        return int(min(self.produced_qty / self.planned_qty * 100, 100)) if self.planned_qty else 0


class ProductionOrderLine(MfgModel):
    """What the order produces: the main finished item plus any co-products and by-products (e.g. metal recovery)."""
    TYPES = [('MAIN', 'Main product'), ('CO_PRODUCT', 'Co-product'), ('BY_PRODUCT', 'By-product')]
    order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    line_type = models.CharField(max_length=20, choices=TYPES, default='MAIN')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(**QTY)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    output_qty = models.DecimalField(**QTY)
    scrap_qty = models.DecimalField(**QTY)
    accepted_qty = models.DecimalField(**QTY)
    rejected_qty = models.DecimalField(**QTY)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    unit_cost = models.DecimalField(**COST, help_text='Output valuation (by-products: credit per unit).')
    cost_share_percent = models.DecimalField(max_digits=7, decimal_places=3, default=100)

    class Meta:
        db_table = 'production_order_lines'
        ordering = ['line_no']
        constraints = [models.UniqueConstraint(fields=('order', 'line_no'), name='mfg_unique_order_line')]

    def __str__(self):
        return f'{self.order.order_no}/{self.line_no} {self.item.item_no}'

    @property
    def remaining_qty(self):
        return max(self.quantity - self.output_qty, ZERO)


class ProductionOrderComponent(MfgModel):
    order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name='components')
    line_no = models.PositiveIntegerField()
    bom_line = models.ForeignKey(BOMLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=200, blank=True)
    component_type = models.CharField(max_length=20, choices=COMPONENT_TYPES, default='RAW_MATERIAL')
    consumption_basis = models.CharField(max_length=20, choices=CONSUMPTION_BASIS, default='QUANTITY')
    formula = models.CharField(max_length=250, blank=True)
    qty_per = models.DecimalField(max_digits=18, decimal_places=6, default=0, help_text='Requirement per finished piece incl. losses.')
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    scrap_percent = models.DecimalField(**PERCENT)
    fixed_scrap_qty = models.DecimalField(**QTY)
    expected_loss_percent = models.DecimalField(**PERCENT)
    expected_loss_qty = models.DecimalField(**QTY)
    routing_link_code = models.CharField(max_length=20, blank=True)
    operation_no = models.CharField(max_length=10, blank=True)
    flushing_method = models.CharField(max_length=20, choices=FLUSHING, default='MANUAL')
    supply_method = models.CharField(max_length=20, choices=SUPPLY_METHODS, default='INVENTORY')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    substitute_item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_required = models.BooleanField(default=False)
    serial_required = models.BooleanField(default=False)
    optional = models.BooleanField(default=False)
    metal = models.CharField(max_length=20, choices=METALS, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    # Requirement snapshot
    gross_requirement = models.DecimalField(**QTY, help_text='BOM quantity x production quantity, before losses.')
    scrap_requirement = models.DecimalField(**QTY)
    expected_qty = models.DecimalField(**QTY, help_text='Total planned requirement including scrap and process loss.')
    unit_cost = models.DecimalField(**COST, help_text='Standard unit cost snapshot.')
    expected_cost = models.DecimalField(**MONEY)
    # Progress (maintained by the posting engine)
    reserved_qty = models.DecimalField(**QTY)
    picked_qty = models.DecimalField(**QTY, help_text='Issued to the production bin.')
    returned_qty = models.DecimalField(**QTY, help_text='Issued material returned to stores.')
    consumed_qty = models.DecimalField(**QTY)
    consumed_from_issue_qty = models.DecimalField(**QTY)
    consumed_weight = models.DecimalField(**WEIGHT)
    actual_cost = models.DecimalField(**MONEY)

    class Meta:
        db_table = 'production_order_components'
        ordering = ['line_no']
        constraints = [models.UniqueConstraint(fields=('order', 'line_no'), name='mfg_unique_order_component')]
        indexes = [models.Index(fields=('tenant', 'item'))]

    def __str__(self):
        return f'{self.order.order_no}/{self.line_no} {self.item.item_no}'

    @property
    def remaining_qty(self):
        return max(self.expected_qty - self.consumed_qty, ZERO)

    @property
    def issued_open_qty(self):
        """Issued to the production bin and not yet consumed or returned."""
        return max(self.picked_qty - self.returned_qty - self.consumed_from_issue_qty, ZERO)

    @property
    def to_issue_qty(self):
        return max(self.expected_qty - self.consumed_qty - self.issued_open_qty, ZERO)

    @property
    def is_metal(self):
        return self.metal in ('GOLD', 'SILVER', 'PLATINUM') and self.uom_id and self.uom.code in ('GM', 'KG')


class ProductionOrderRoutingLine(MfgModel):
    STATUSES = [('NOT_STARTED', 'Not started'), ('READY', 'Ready'), ('STARTED', 'Started'), ('PAUSED', 'Paused'),
                ('COMPLETED', 'Completed'), ('QC_PENDING', 'QC pending'), ('QC_PASSED', 'QC passed'), ('QC_FAILED', 'QC failed')]
    order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name='operations')
    routing_line = models.ForeignKey(RoutingLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    operation_no = models.CharField(max_length=10)
    sequence = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=150)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name='order_operations')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.PROTECT, null=True, blank=True, related_name='order_operations')
    resource = models.ForeignKey(ProductionResource, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    routing_link_code = models.CharField(max_length=20, blank=True)
    subcontracting = models.BooleanField(default=False)
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    quality_check_required = models.BooleanField(default=False)
    is_rework = models.BooleanField(default=False)
    rework = models.ForeignKey('ProductionRework', on_delete=models.SET_NULL, null=True, blank=True, related_name='operations')
    # Planned (never overwritten by actuals)
    setup_time = models.DecimalField(**MINUTES)
    run_time = models.DecimalField(**MINUTES, help_text='Minutes per unit.')
    wait_time = models.DecimalField(**MINUTES)
    move_time = models.DecimalField(**MINUTES)
    queue_time = models.DecimalField(**MINUTES)
    efficiency = models.DecimalField(max_digits=7, decimal_places=2, default=100)
    concurrent_capacity = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    send_ahead_quantity = models.DecimalField(**QTY)
    scrap_percent = models.DecimalField(**PERCENT)
    planned_setup_minutes = models.DecimalField(**MINUTES)
    planned_run_minutes = models.DecimalField(**MINUTES)
    planned_total_minutes = models.DecimalField(**MINUTES, help_text='Capacity need: setup + run.')
    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    labour_rate = models.DecimalField(**RATE)
    machine_rate = models.DecimalField(**RATE)
    overhead_rate = models.DecimalField(**RATE)
    planned_labour_cost = models.DecimalField(**MONEY)
    planned_machine_cost = models.DecimalField(**MONEY)
    planned_overhead_cost = models.DecimalField(**MONEY)
    planned_subcontract_cost = models.DecimalField(**MONEY)
    # Actuals (maintained by the posting engine)
    actual_setup_minutes = models.DecimalField(**MINUTES)
    actual_run_minutes = models.DecimalField(**MINUTES)
    actual_wait_minutes = models.DecimalField(**MINUTES)
    actual_move_minutes = models.DecimalField(**MINUTES)
    actual_queue_minutes = models.DecimalField(**MINUTES)
    actual_downtime_minutes = models.DecimalField(**MINUTES)
    actual_break_minutes = models.DecimalField(**MINUTES)
    actual_cost = models.DecimalField(**MONEY)
    output_qty = models.DecimalField(**QTY)
    scrap_qty = models.DecimalField(**QTY)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='NOT_STARTED')
    operator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    clock_started_at = models.DateTimeField(null=True, blank=True, help_text='Start of the current running interval.')
    clocked_minutes = models.DecimalField(**MINUTES, help_text='Run minutes clocked by start/pause/resume, not yet posted.')

    class Meta:
        db_table = 'production_order_routing_lines'
        ordering = ['sequence', 'operation_no']
        constraints = [models.UniqueConstraint(fields=('order', 'operation_no'), name='mfg_unique_order_operation')]

    def __str__(self):
        return f'{self.order.order_no} op {self.operation_no} {self.description}'

    @property
    def actual_capacity_minutes(self):
        return self.actual_setup_minutes + self.actual_run_minutes

    @property
    def actual_total_minutes(self):
        return (self.actual_setup_minutes + self.actual_run_minutes + self.actual_wait_minutes + self.actual_move_minutes
                + self.actual_queue_minutes + self.actual_downtime_minutes)

    @property
    def efficiency_percent(self):
        """Standard runtime for the good output produced / actual runtime x 100."""
        actual = self.actual_capacity_minutes
        if not actual:
            return None
        standard = self.setup_time + self.run_time * (self.output_qty + self.scrap_qty)
        return (standard / actual * 100).quantize(Decimal('0.1'))

    @property
    def runtime_variance_minutes(self):
        return self.actual_capacity_minutes - self.planned_total_minutes


class OperationEvent(MfgModel):
    """Shop-floor clock events. Runtime entries are derived from these when an operation is completed."""
    EVENTS = [('START', 'Start'), ('PAUSE', 'Pause'), ('RESUME', 'Resume'), ('STOP', 'Stop'), ('COMPLETE', 'Complete')]
    order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name='operation_events')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.CASCADE, related_name='events')
    event = models.CharField(max_length=10, choices=EVENTS)
    at = models.DateTimeField(default=dj_timezone.now)
    operator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    downtime_reason = models.ForeignKey(DowntimeReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'production_operation_events'
        ordering = ['at', 'id']


# ---------------------------------------------------------------------------
# Warehouse: pick lists and material issues
# ---------------------------------------------------------------------------

class ProductionPickList(MfgModel):
    STATUSES = [('OPEN', 'Open'), ('POSTED', 'Posted'), ('CANCELLED', 'Cancelled')]
    pick_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='pick_lists')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    to_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')
    posted_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'production_pick_lists'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'pick_no'), name='mfg_unique_pick_no')]

    def __str__(self):
        return self.pick_no


class ProductionPickLine(MfgModel):
    pick = models.ForeignKey(ProductionPickList, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.PROTECT, related_name='pick_lines')
    reservation = models.ForeignKey('inventory.InventoryReservation', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    from_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    qty_to_pick = models.DecimalField(**QTY)
    qty_picked = models.DecimalField(**QTY)
    scanned_code = models.CharField(max_length=100, blank=True)
    confirmed = models.BooleanField(default=False)

    class Meta:
        db_table = 'production_pick_list_lines'
        ordering = ['line_no']


class ProductionMaterialIssue(LedgerModel):
    issue_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='material_issues')
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.PROTECT, related_name='issues')
    pick_line = models.ForeignKey(ProductionPickLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    direction = models.CharField(max_length=10, choices=[('ISSUE', 'Issue'), ('RETURN', 'Return')], default='ISSUE')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    from_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    to_bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    unit_cost = models.DecimalField(**COST)
    total_cost = models.DecimalField(**MONEY)
    issued_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_material_issues'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order'))]


# ---------------------------------------------------------------------------
# Production journal and posting batches
# ---------------------------------------------------------------------------

ENTRY_TYPES = [('CONSUMPTION', 'Consumption'), ('OUTPUT', 'Output'), ('RUNTIME', 'Capacity / runtime'), ('SCRAP', 'Scrap')]


class ProductionJournal(MfgModel):
    TYPES = [('PRODUCTION', 'Production journal'), ('CONSUMPTION', 'Consumption journal'), ('OUTPUT', 'Output journal'),
             ('CAPACITY', 'Capacity / runtime journal'), ('SCRAP', 'Scrap journal'), ('REWORK', 'Rework journal'),
             ('FLUSHING', 'Automatic flushing'), ('SUBCONTRACT', 'Subcontracting')]
    STATUSES = [('OPEN', 'Open'), ('PENDING_APPROVAL', 'Pending approval'), ('POSTED', 'Posted'), ('CANCELLED', 'Cancelled')]
    journal_no = models.CharField(max_length=40)
    journal_type = models.CharField(max_length=20, choices=TYPES, default='PRODUCTION')
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='journals')
    rework = models.ForeignKey('ProductionRework', on_delete=models.SET_NULL, null=True, blank=True, related_name='journals')
    posting_date = models.DateField(default=dj_timezone.localdate)
    description = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')
    posted_batch = models.ForeignKey('PostingBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_journals'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'journal_no'), name='mfg_unique_journal_no')]

    def __str__(self):
        return self.journal_no


class ProductionJournalLine(MfgModel):
    journal = models.ForeignKey(ProductionJournal, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES)
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    order_line = models.ForeignKey(ProductionOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY)
    scrap_qty = models.DecimalField(**QTY, help_text='Output lines: pieces scrapped at this operation.')
    gross_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    net_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    stone_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    output_units = models.JSONField(default=list, blank=True, help_text='[{barcode, serial_no, huid, gross_weight, stone_weight}]')
    setup_minutes = models.DecimalField(**MINUTES)
    run_minutes = models.DecimalField(**MINUTES)
    wait_minutes = models.DecimalField(**MINUTES)
    move_minutes = models.DecimalField(**MINUTES)
    queue_minutes = models.DecimalField(**MINUTES)
    downtime_minutes = models.DecimalField(**MINUTES)
    break_minutes = models.DecimalField(**MINUTES)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    operator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resource = models.ForeignKey(ProductionResource, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    downtime_reason = models.ForeignKey(DowntimeReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    scrap_reason = models.ForeignKey(ScrapReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    scrap_type = models.CharField(max_length=30, choices=SCRAP_TYPES, blank=True)
    recoverable = models.BooleanField(default=False)
    scrap_weight = models.DecimalField(**WEIGHT)
    finished = models.BooleanField(default=False, help_text='Mark the operation complete with this output.')
    source = models.CharField(max_length=20, default='MANUAL')
    note = models.CharField(max_length=250, blank=True)

    class Meta:
        db_table = 'production_journal_lines'
        ordering = ['line_no']


class PostingBatch(MfgModel):
    """One atomic manufacturing posting - the root every ledger row, inventory entry and G/L voucher traces back to."""
    KINDS = [('JOURNAL', 'Production journal'), ('ISSUE', 'Material issue'), ('RETURN', 'Material return'), ('QC', 'Quality control'),
             ('FINISH', 'Finish / cost settlement'), ('REOPEN', 'Reopen'), ('REVERSAL', 'Reversal'), ('SUBCONTRACT', 'Subcontracting'),
             ('HUID', 'HUID / hallmark assignment')]
    batch_no = models.CharField(max_length=40)
    kind = models.CharField(max_length=20, choices=KINDS, default='JOURNAL')
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='posting_batches')
    journal = models.ForeignKey(ProductionJournal, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    posting_date = models.DateField()
    posted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    posted_at = models.DateTimeField(default=dj_timezone.now)
    idempotency_key = models.CharField(max_length=80, blank=True)
    finance_voucher = models.ForeignKey('erp.FinanceVoucher', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gl_status = models.CharField(max_length=20, default='NOT_REQUIRED', help_text='POSTED / DISABLED / NOT_REQUIRED')
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversals')
    reason = models.CharField(max_length=250, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, default='POSTED')

    class Meta:
        db_table = 'production_posting_entries'
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=('tenant', 'batch_no'), name='mfg_unique_batch_no'),
            models.UniqueConstraint(fields=('tenant', 'idempotency_key'), condition=~Q(idempotency_key=''), name='mfg_unique_idempotency_key'),
        ]

    def __str__(self):
        return self.batch_no


# ---------------------------------------------------------------------------
# Production ledgers (immutable)
# ---------------------------------------------------------------------------

class ConsumptionEntry(LedgerModel):
    SOURCES = [('MANUAL', 'Manual'), ('FORWARD', 'Forward flush'), ('BACKWARD', 'Backward flush'), ('PICK', 'Pick-based'),
               ('SUBCONTRACT_LOSS', 'Subcontractor loss'), ('REWORK', 'Rework')]
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='consumption_entries')
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.PROTECT, related_name='consumption_entries')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(**QTY)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    fine_weight = models.DecimalField(**WEIGHT)
    metal = models.CharField(max_length=20, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    unit_cost = models.DecimalField(**COST)
    cost_amount = models.DecimalField(**MONEY)
    from_issue = models.BooleanField(default=False, help_text='Consumed from material issued to the production bin.')
    source = models.CharField(max_length=20, choices=SOURCES, default='MANUAL')
    is_rework = models.BooleanField(default=False)
    inventory_entry = models.ForeignKey('inventory.InventoryLedgerEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')

    class Meta:
        db_table = 'production_consumption_entries'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order')), models.Index(fields=('tenant', 'item')),
                   models.Index(fields=('tenant', 'lot_no')), models.Index(fields=('tenant', 'jewellery_unit'))]


class OutputEntry(LedgerModel):
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='output_entries')
    order_line = models.ForeignKey(ProductionOrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name='output_entries')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    sku = models.ForeignKey('inventory.SKU', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY, help_text='Good output.')
    scrap_qty = models.DecimalField(**QTY)
    uom = models.ForeignKey('inventory.UnitOfMeasure', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bin = models.ForeignKey('inventory.Bin', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    lot_no = models.CharField(max_length=60, blank=True)
    into_qc = models.BooleanField(default=False)
    is_final = models.BooleanField(default=False, help_text='Final operation: finished inventory was increased.')
    is_rework = models.BooleanField(default=False)
    operator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    unit_cost = models.DecimalField(**COST)
    cost_amount = models.DecimalField(**MONEY)
    inventory_entries = models.ManyToManyField('inventory.InventoryLedgerEntry', blank=True, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')

    class Meta:
        db_table = 'production_output_entries'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order')), models.Index(fields=('tenant', 'item'))]


class OutputUnit(MfgModel):
    """A serialized jewellery piece produced by an output entry, with its hallmarking details."""
    HALLMARK = [('NOT_REQUIRED', 'Not required'), ('PENDING', 'Pending'), ('SENT', 'Sent to assay centre'), ('HALLMARKED', 'Hallmarked'),
                ('FAILED', 'Failed')]
    output = models.ForeignKey(OutputEntry, on_delete=models.PROTECT, related_name='units')
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='output_units')
    jewellery_unit = models.OneToOneField('inventory.JewelleryUnit', on_delete=models.PROTECT, related_name='production_output')
    hallmark_status = models.CharField(max_length=20, choices=HALLMARK, default='PENDING')
    hallmark_date = models.DateField(null=True, blank=True)
    assay_centre = models.CharField(max_length=150, blank=True)
    certificate_no = models.CharField(max_length=100, blank=True)
    qc_status = models.CharField(max_length=20, default='PENDING')
    reversed = models.BooleanField(default=False)

    class Meta:
        db_table = 'production_output_units'
        ordering = ['id']


class RuntimeEntry(LedgerModel):
    """Capacity ledger: planned runtime lives on the order operation and is never overwritten by these actuals."""
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='runtime_entries')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, related_name='runtime_entries')
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    resource = models.ForeignKey(ProductionResource, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    employee = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    setup_minutes = models.DecimalField(**MINUTES)
    run_minutes = models.DecimalField(**MINUTES)
    wait_minutes = models.DecimalField(**MINUTES)
    move_minutes = models.DecimalField(**MINUTES)
    queue_minutes = models.DecimalField(**MINUTES)
    downtime_minutes = models.DecimalField(**MINUTES)
    break_minutes = models.DecimalField(**MINUTES)
    total_minutes = models.DecimalField(**MINUTES)
    downtime_reason = models.ForeignKey(DowntimeReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    labour_rate = models.DecimalField(**RATE)
    machine_rate = models.DecimalField(**RATE)
    overhead_rate = models.DecimalField(**RATE)
    labour_cost = models.DecimalField(**MONEY)
    machine_cost = models.DecimalField(**MONEY)
    overhead_cost = models.DecimalField(**MONEY)
    cost_amount = models.DecimalField(**MONEY)
    is_rework = models.BooleanField(default=False)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')

    class Meta:
        db_table = 'production_runtime_entries'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order')), models.Index(fields=('tenant', 'work_center', 'posting_date'))]


class ScrapEntry(LedgerModel):
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='scrap_entries')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    scrap_type = models.CharField(max_length=30, choices=SCRAP_TYPES, default='PROCESS_LOSS')
    reason = models.ForeignKey(ScrapReason, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY, help_text='Pieces scrapped.')
    weight = models.DecimalField(**WEIGHT, help_text='Metal / material weight scrapped.')
    recoverable = models.BooleanField(default=False)
    recovery_item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    recovery_entry = models.ForeignKey('inventory.InventoryLedgerEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    cost_amount = models.DecimalField(**MONEY, help_text='Estimated value lost (non-recoverable) or credited (recoverable).')
    operator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    machine_center = models.ForeignKey(MachineCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    is_rework = models.BooleanField(default=False)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')

    class Meta:
        db_table = 'production_scrap_entries'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order'))]


class CostEntry(LedgerModel):
    """The production value (WIP) ledger. Signed: + flows into the order's WIP, - flows out.

    WIP of an order = sum(amount). The G/L lines of the same posting batch are built from these rows.
    """
    TYPES = [('MATERIAL', 'Material'), ('LABOUR', 'Labour'), ('MACHINE', 'Machine'), ('OVERHEAD', 'Overhead'),
             ('SUBCONTRACT', 'Subcontracting'), ('OUTPUT', 'Output to finished goods'), ('BYPRODUCT', 'By-product / recovery credit'),
             ('VARIANCE', 'Variance settlement'), ('REVALUATION', 'Output revaluation (actual cost)'), ('SCRAP_WRITE_OFF', 'Scrapped output')]
    entry_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='cost_entries')
    cost_type = models.CharField(max_length=20, choices=TYPES)
    amount = models.DecimalField(**MONEY)
    is_rework = models.BooleanField(default=False)
    source_type = models.CharField(max_length=30, blank=True)
    source_id = models.PositiveBigIntegerField(null=True, blank=True)
    description = models.CharField(max_length=200, blank=True)
    debit_account = models.CharField(max_length=40, blank=True, help_text='Setup field of the debited G/L account.')
    credit_account = models.CharField(max_length=40, blank=True)

    class Meta:
        db_table = 'production_cost_entries'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'order', 'cost_type'))]


class VarianceEntry(LedgerModel):
    TYPES = [('MATERIAL', 'Material'), ('LABOUR', 'Labour'), ('MACHINE', 'Machine'), ('OVERHEAD', 'Overhead'),
             ('SUBCONTRACT', 'Subcontracting'), ('QUANTITY', 'Material quantity'), ('RUNTIME', 'Runtime (minutes)'), ('SCRAP', 'Scrap'),
             ('REWORK', 'Rework'), ('TOTAL', 'Total production variance')]
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='variance_entries')
    variance_type = models.CharField(max_length=20, choices=TYPES)
    standard_amount = models.DecimalField(**MONEY)
    actual_amount = models.DecimalField(**MONEY)
    variance_amount = models.DecimalField(**MONEY, help_text='Actual - standard (positive = unfavourable).')

    class Meta:
        db_table = 'production_variance_entries'
        ordering = ['id']


# ---------------------------------------------------------------------------
# Quality, rework, subcontracting
# ---------------------------------------------------------------------------

class ProductionQC(MfgModel):
    STAGES = [('INCOMING', 'Incoming material'), ('OPERATION', 'Operation'), ('FINAL', 'Final production'), ('JEWELLERY', 'Jewellery inspection')]
    RESULTS = [('PASS', 'Pass'), ('FAIL', 'Fail'), ('REWORK', 'Rework'), ('HOLD', 'Hold')]
    STATUSES = [('OPEN', 'Open'), ('POSTED', 'Posted'), ('REVERSED', 'Reversed')]
    qc_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='qc_inspections')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, null=True, blank=True, related_name='qc_inspections')
    stage = models.CharField(max_length=20, choices=STAGES, default='FINAL')
    inspected_qty = models.DecimalField(**QTY)
    passed_qty = models.DecimalField(**QTY)
    failed_qty = models.DecimalField(**QTY, help_text='Rejected - scrapped.')
    rework_qty = models.DecimalField(**QTY)
    hold_qty = models.DecimalField(**QTY)
    result = models.CharField(max_length=10, choices=RESULTS, default='PASS')
    units = models.ManyToManyField('inventory.JewelleryUnit', blank=True, related_name='+', help_text='Serialized pieces inspected.')
    unit_results = models.JSONField(default=list, blank=True, help_text='[{"unit": id, "result": "PASS"}] for serialized pieces.')
    inspector = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    inspected_at = models.DateTimeField(default=dj_timezone.now)
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')
    batch = models.ForeignKey(PostingBatch, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_qc'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'qc_no'), name='mfg_unique_qc_no')]

    def __str__(self):
        return self.qc_no


class ProductionQCLine(MfgModel):
    qc = models.ForeignKey(ProductionQC, on_delete=models.CASCADE, related_name='lines')
    parameter = models.ForeignKey(QualityParameter, on_delete=models.PROTECT, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    expected_value = models.CharField(max_length=60, blank=True)
    actual_value = models.CharField(max_length=60, blank=True)
    result = models.CharField(max_length=10, choices=[('PASS', 'Pass'), ('FAIL', 'Fail')], default='PASS')
    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'production_qc_lines'


class ProductionRework(MfgModel):
    STATUSES = [('OPEN', 'Open'), ('IN_PROGRESS', 'In progress'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled')]
    rework_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='reworks')
    qc = models.ForeignKey(ProductionQC, on_delete=models.PROTECT, null=True, blank=True, related_name='reworks')
    quantity = models.DecimalField(**QTY)
    reason = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'production_rework'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'rework_no'), name='mfg_unique_rework_no')]

    def __str__(self):
        return self.rework_no


class ProductionReworkLine(MfgModel):
    TYPES = [('OPERATION', 'Additional operation'), ('MATERIAL', 'Additional material')]
    rework = models.ForeignKey(ProductionRework, on_delete=models.CASCADE, related_name='lines')
    line_type = models.CharField(max_length=20, choices=TYPES, default='OPERATION')
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    description = models.CharField(max_length=150, blank=True)
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY)
    planned_minutes = models.DecimalField(**MINUTES)
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_rework_lines'


class SubcontractOrder(MfgModel):
    STATUSES = [('OPEN', 'Open'), ('SENT', 'Sent'), ('PARTIALLY_RECEIVED', 'Partially received'), ('RECEIVED', 'Received'),
                ('CLOSED', 'Closed'), ('CANCELLED', 'Cancelled')]
    subcontract_no = models.CharField(max_length=40)
    order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name='subcontract_orders')
    operation = models.ForeignKey(ProductionOrderRoutingLine, on_delete=models.PROTECT, related_name='subcontract_orders')
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.PROTECT, related_name='orders')
    vendor_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')
    quantity = models.DecimalField(**QTY, help_text='Pieces to be processed.')
    expected_return_date = models.DateField(null=True, blank=True)
    actual_return_date = models.DateField(null=True, blank=True)
    weight_sent = models.DecimalField(**WEIGHT)
    weight_returned = models.DecimalField(**WEIGHT)
    loss_weight = models.DecimalField(**WEIGHT)
    service_cost = models.DecimalField(**MONEY)
    purchase_invoice = models.ForeignKey('erp.SupplierInvoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    invoice_reference = models.CharField(max_length=60, blank=True)

    class Meta:
        db_table = 'production_subcontract_orders'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'subcontract_no'), name='mfg_unique_subcontract_no')]

    def __str__(self):
        return self.subcontract_no


class SubcontractLine(MfgModel):
    subcontract = models.ForeignKey(SubcontractOrder, on_delete=models.CASCADE, related_name='lines')
    component = models.ForeignKey(ProductionOrderComponent, on_delete=models.PROTECT, related_name='+')
    jewellery_unit = models.ForeignKey('inventory.JewelleryUnit', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    qty_sent = models.DecimalField(**QTY)
    gross_sent = models.DecimalField(**WEIGHT)
    qty_returned = models.DecimalField(**QTY)
    gross_returned = models.DecimalField(**WEIGHT)
    qty_lost = models.DecimalField(**QTY)

    class Meta:
        db_table = 'production_subcontract_lines'

    @property
    def qty_at_vendor(self):
        return max(self.qty_sent - self.qty_returned - self.qty_lost, ZERO)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class PlanningRun(MfgModel):
    run_no = models.CharField(max_length=40)
    horizon_days = models.PositiveIntegerField(default=30)
    run_at = models.DateTimeField(default=dj_timezone.now)
    parameters = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'production_planning_runs'
        ordering = ['-id']


class PlanningSuggestion(MfgModel):
    TYPES = [('PRODUCTION', 'Produce'), ('PURCHASE', 'Purchase'), ('TRANSFER', 'Transfer')]
    STATUSES = [('SUGGESTED', 'Suggested'), ('ACCEPTED', 'Accepted'), ('IGNORED', 'Ignored'), ('CONVERTED', 'Converted')]
    run = models.ForeignKey(PlanningRun, on_delete=models.CASCADE, related_name='suggestions')
    suggestion_type = models.CharField(max_length=20, choices=TYPES)
    level = models.PositiveSmallIntegerField(default=0, help_text='0 = end item, 1+ = dependent demand (sub-BOM / components).')
    item = models.ForeignKey('inventory.Item', on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey('inventory.ItemVariant', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, related_name='+')
    source_location = models.ForeignKey('inventory.Location', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    demand_qty = models.DecimalField(**QTY)
    supply_qty = models.DecimalField(**QTY)
    quantity = models.DecimalField(**QTY)
    due_date = models.DateField(null=True, blank=True)
    demand_source = models.CharField(max_length=200, blank=True)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    status = models.CharField(max_length=20, choices=STATUSES, default='SUGGESTED')
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    transfer_request = models.ForeignKey('inventory.TransferRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_planning_suggestions'
        ordering = ['level', 'due_date', 'id']


# ---------------------------------------------------------------------------
# Import staging and audit
# ---------------------------------------------------------------------------

class ManufacturingImportBatch(MfgModel):
    TYPES = [('boms', 'Production BOMs'), ('routings', 'Routings'), ('production_orders', 'Production orders')]
    STATUSES = [('UPLOADED', 'Uploaded'), ('VALIDATED', 'Validated'), ('FAILED', 'Has errors'), ('APPROVED', 'Approved'),
                ('IMPORTED', 'Imported')]
    import_type = models.CharField(max_length=30, choices=TYPES)
    file_name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUSES, default='UPLOADED')
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    result = models.JSONField(default=dict, blank=True)
    approved_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'production_import_batches'
        ordering = ['-id']


class ManufacturingAuditLog(models.Model):
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.PROTECT, related_name='+')
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=40)
    document_type = models.CharField(max_length=40)
    document_no = models.CharField(max_length=60, blank=True)
    order = models.ForeignKey(ProductionOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    batch = models.ForeignKey(PostingBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=dj_timezone.now)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = 'production_audit_logs'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'document_type', 'document_no')), models.Index(fields=('tenant', 'created_at'))]
