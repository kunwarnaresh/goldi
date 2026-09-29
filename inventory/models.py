"""Goldio multi-location inventory.

Every table here is tenant-scoped. Quantities are never edited directly: documents call
``inventory.engine.InventoryPostingEngine``, which writes the immutable ``InventoryLedgerEntry``
and keeps ``InventoryBalance`` in step inside the same database transaction.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone as dj_timezone

ZERO = Decimal('0')
QTY = dict(max_digits=18, decimal_places=3, default=0)
WEIGHT = dict(max_digits=18, decimal_places=3, default=0)
MONEY = dict(max_digits=18, decimal_places=2, default=0)


class Tenant(models.Model):
    COSTING_METHODS = [('FIFO', 'FIFO'), ('AVERAGE', 'Weighted average'), ('STANDARD', 'Standard cost')]
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=200)
    default_currency = models.CharField(max_length=10, default='INR')
    costing_method = models.CharField(max_length=20, choices=COSTING_METHODS, default='AVERAGE')
    allow_negative_inventory = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class TenantMembership(models.Model):
    ROLES = [('owner', 'Owner'), ('admin', 'Administrator'), ('member', 'Member')]
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tenant_memberships')
    role = models.CharField(max_length=20, choices=ROLES, default='member')
    all_locations = models.BooleanField(default=False, help_text='Grants every location permission in this tenant.')
    is_default = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('tenant', 'user'), name='inv_unique_tenant_member')]

    @property
    def is_admin(self):
        return self.role in ('owner', 'admin')


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)


class TenantModel(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=dj_timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True

    def check_same_tenant(self, *names):
        """Reject cross-tenant references - the named FKs (when set) must belong to this row's tenant."""
        wrong = [name for name in names
                 if getattr(self, f'{name}_id', None) is not None and getattr(self, name).tenant_id != self.tenant_id]
        if wrong:
            raise ValidationError({name: 'Belongs to another tenant.' for name in wrong})


class NumberSeries(TenantModel):
    document_type = models.CharField(max_length=40)
    prefix = models.CharField(max_length=20)
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=6)

    class Meta:
        db_table = 'inventory_number_series'
        constraints = [models.UniqueConstraint(fields=('tenant', 'document_type'), name='inv_unique_number_series')]


# ---------------------------------------------------------------------------
# Locations, security, routes, zones and bins
# ---------------------------------------------------------------------------

class Location(TenantModel):
    TYPES = [
        ('STORE', 'Store'), ('WAREHOUSE', 'Warehouse'), ('DISTRIBUTION_CENTER', 'Distribution centre'),
        ('E_COMMERCE', 'E-commerce'), ('MANUFACTURING', 'Manufacturing'), ('REPAIR', 'Repair'),
        ('QC', 'QC'), ('RETURN', 'Return'), ('TRANSIT', 'Transit'), ('VIRTUAL', 'Virtual'), ('JOB_WORKER', 'Job worker premises'),
        ('OTHER', 'Other'),
    ]
    company = models.ForeignKey('erp.Company', on_delete=models.PROTECT, null=True, blank=True, related_name='inventory_locations')
    store = models.OneToOneField('erp.Store', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_location',
                                 help_text='POS terminals of this store sell from this location.')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    location_type = models.CharField(max_length=30, choices=TYPES, default='STORE')
    region = models.CharField(max_length=100, blank=True)
    address_1 = models.CharField(max_length=200, blank=True)
    address_2 = models.CharField(max_length=200, blank=True)
    area = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=60, default='India')
    pin = models.CharField(max_length=10, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    default_currency = models.CharField(max_length=10, default='INR')
    time_zone = models.CharField(max_length=60, default='Asia/Kolkata')
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)
    # Transaction behaviour
    allow_purchase = models.BooleanField(default=True)
    allow_sales = models.BooleanField(default=True)
    allow_transfer_out = models.BooleanField(default=True)
    allow_transfer_in = models.BooleanField(default=True)
    allow_adjustment = models.BooleanField(default=True)
    allow_pos = models.BooleanField(default=False)
    allow_reservation = models.BooleanField(default=True)
    allow_physical_count = models.BooleanField(default=True)
    allow_direct_transfer = models.BooleanField(default=False)
    allow_negative_inventory = models.BooleanField(default=False)
    serial_tracking = models.BooleanField(default=True)
    lot_tracking = models.BooleanField(default=False)
    weight_tracking = models.BooleanField(default=True)
    # Warehouse setup
    is_warehouse = models.BooleanField(default=False)
    bin_mandatory = models.BooleanField(default=False)
    zone_mandatory = models.BooleanField(default=False)
    directed_putaway = models.BooleanField(default=False)
    directed_pick = models.BooleanField(default=False)
    require_qc = models.BooleanField(default=False)
    require_putaway = models.BooleanField(default=False)
    require_pick = models.BooleanField(default=False)
    # Financial / dimensions
    inventory_posting_group = models.CharField(max_length=40, blank=True)
    dimension_1 = models.CharField(max_length=40, blank=True)
    dimension_2 = models.CharField(max_length=40, blank=True)
    cost_centre = models.CharField(max_length=40, blank=True)
    profit_centre = models.CharField(max_length=40, blank=True)
    metal_rate_premium_per_gram = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                                      help_text='Location rate = central rate + this premium.')

    class Meta:
        db_table = 'inventory_locations'
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='inv_unique_location_code')]
        indexes = [models.Index(fields=('tenant', 'location_type'))]

    def __str__(self):
        return f'{self.code} - {self.name}'

    @property
    def is_transit(self):
        return self.location_type == 'TRANSIT'

    @property
    def usable(self):
        return self.active and not self.blocked


class LocationPermission(TenantModel):
    """Which locations a user may see and what they may do there (location-based document control)."""
    ACTIONS = ('view', 'sell', 'create_transfer', 'approve_transfer', 'ship', 'receive', 'adjust',
               'approve_adjustment', 'count', 'reclassify', 'direct_transfer')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='inventory_location_permissions')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='user_permissions')
    can_view = models.BooleanField(default=True)
    can_sell = models.BooleanField(default=False)
    can_create_transfer = models.BooleanField(default=False)
    can_approve_transfer = models.BooleanField(default=False)
    can_ship = models.BooleanField(default=False)
    can_receive = models.BooleanField(default=False)
    can_adjust = models.BooleanField(default=False)
    can_approve_adjustment = models.BooleanField(default=False)
    can_count = models.BooleanField(default=False)
    can_reclassify = models.BooleanField(default=False)
    can_direct_transfer = models.BooleanField(default=False)
    approval_level = models.PositiveSmallIntegerField(default=0, help_text='Compared with ApprovalRule.approver_level.')

    class Meta:
        db_table = 'inventory_location_users'
        constraints = [models.UniqueConstraint(fields=('user', 'location'), name='inv_unique_location_user')]

    def allows(self, action):
        return bool(getattr(self, f'can_{action}', False))


class TransferRoute(TenantModel):
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='routes_out')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='routes_in')
    transit_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='routes_via')
    transfer_days = models.PositiveSmallIntegerField(default=1)
    shipping_agent = models.CharField(max_length=100, blank=True)
    shipping_service = models.CharField(max_length=100, blank=True)
    allow_transfer = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'inventory_transfer_routes'
        constraints = [models.UniqueConstraint(fields=('tenant', 'from_location', 'to_location'), name='inv_unique_transfer_route')]

    def clean(self):
        self.check_same_tenant('from_location', 'to_location', 'transit_location')
        if self.from_location_id == self.to_location_id:
            raise ValidationError('From and To locations must differ.')
        if self.transit_location_id and not self.transit_location.is_transit:
            raise ValidationError({'transit_location': 'Must be a TRANSIT location.'})


class Zone(TenantModel):
    TYPES = [('RECEIVING', 'Receiving'), ('QC', 'QC'), ('STORAGE', 'Storage'), ('PICK', 'Pick'), ('PACKING', 'Packing'),
             ('DISPATCH', 'Dispatch'), ('DISPLAY', 'Display'), ('SAFE', 'Safe'), ('RETURN', 'Return'), ('OTHER', 'Other')]
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='zones')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    zone_type = models.CharField(max_length=20, choices=TYPES, default='STORAGE')
    sequence = models.PositiveIntegerField(default=100)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'inventory_zones'
        ordering = ['sequence', 'code']
        constraints = [models.UniqueConstraint(fields=('location', 'code'), name='inv_unique_zone_code')]

    def __str__(self):
        return f'{self.location.code}/{self.code}'


class Bin(TenantModel):
    TYPES = [('RECEIVING', 'Receiving'), ('QC', 'QC'), ('STORAGE', 'Storage'), ('PICK', 'Pick'), ('PUTAWAY', 'Put-away'),
             ('STAGING', 'Staging'), ('DISPATCH', 'Dispatch'), ('RETURN', 'Return'), ('REPAIR', 'Repair'),
             ('DAMAGED', 'Damaged'), ('TRANSIT', 'Transit'), ('SAFE', 'Safe'), ('DISPLAY', 'Display')]
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='bins')
    zone = models.ForeignKey(Zone, on_delete=models.PROTECT, null=True, blank=True, related_name='bins')
    code = models.CharField(max_length=40)
    bin_type = models.CharField(max_length=20, choices=TYPES, default='STORAGE')
    description = models.CharField(max_length=200, blank=True)
    capacity = models.DecimalField(**QTY)
    weight_capacity = models.DecimalField(**WEIGHT)
    allow_mixed_items = models.BooleanField(default=True)
    allow_mixed_tracking = models.BooleanField(default=True)
    pick_sequence = models.PositiveIntegerField(default=100)
    putaway_sequence = models.PositiveIntegerField(default=100)
    blocked = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'inventory_bins'
        ordering = ['location__code', 'code']
        constraints = [models.UniqueConstraint(fields=('location', 'code'), name='inv_unique_bin_code')]

    def clean(self):
        if self.zone_id and self.zone.location_id != self.location_id:
            raise ValidationError({'zone': 'Zone must belong to the same location as the bin.'})
        if self.location_id and self.location.zone_mandatory and not self.zone_id:
            raise ValidationError({'zone': 'This location requires a zone on every bin.'})

    def __str__(self):
        return self.code


# ---------------------------------------------------------------------------
# Item master, variants, UOM, SKU and physical jewellery units
# ---------------------------------------------------------------------------

METALS = [('GOLD', 'Gold'), ('SILVER', 'Silver'), ('PLATINUM', 'Platinum'), ('DIAMOND', 'Diamond'), ('NONE', 'None')]


class UnitOfMeasure(TenantModel):
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=60)
    decimal_places = models.PositiveSmallIntegerField(default=3)

    class Meta:
        db_table = 'inventory_uoms'
        constraints = [models.UniqueConstraint(fields=('tenant', 'code'), name='inv_unique_uom')]

    def __str__(self):
        return self.code


class Item(TenantModel):
    TYPES = [('JEWELLERY', 'Jewellery'), ('BULLION', 'Bullion'), ('DIAMOND', 'Diamond'), ('STONE', 'Precious stone'),
             ('PACKING', 'Packing'), ('OTHER', 'Other')]
    COSTING = [('', 'Tenant default'), ('FIFO', 'FIFO'), ('AVERAGE', 'Weighted average'),
               ('STANDARD', 'Standard'), ('SPECIFIC', 'Specific unit cost')]
    item_no = models.CharField(max_length=40)
    description = models.CharField(max_length=200)
    item_type = models.CharField(max_length=20, choices=TYPES, default='JEWELLERY')
    category = models.CharField(max_length=100, blank=True)
    metal = models.CharField(max_length=20, choices=METALS, default='GOLD')
    purity = models.CharField(max_length=20, blank=True)
    base_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='base_items')
    serial_tracking = models.BooleanField(default=False, help_text='Every piece is a JewelleryUnit with its own barcode.')
    lot_tracking = models.BooleanField(default=False)
    weight_tracking = models.BooleanField(default=True)
    costing_method = models.CharField(max_length=20, choices=COSTING, blank=True)
    standard_cost = models.DecimalField(**MONEY)
    design = models.CharField(max_length=100, blank=True)
    collection = models.CharField(max_length=100, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    hsn_code = models.CharField(max_length=20, blank=True)
    legacy_product = models.ForeignKey('erp.Product', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_items')
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'inventory_items'
        ordering = ['item_no']
        constraints = [models.UniqueConstraint(fields=('tenant', 'item_no'), name='inv_unique_item_no')]
        indexes = [models.Index(fields=('tenant', 'metal', 'purity')), models.Index(fields=('tenant', 'category'))]

    def __str__(self):
        return f'{self.item_no} - {self.description}'

    def effective_costing_method(self):
        if self.serial_tracking:
            return 'SPECIFIC'
        return self.costing_method or self.tenant.costing_method


class ItemVariant(TenantModel):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='variants')
    code = models.CharField(max_length=30)
    description = models.CharField(max_length=150, blank=True)
    size = models.CharField(max_length=30, blank=True)
    color = models.CharField(max_length=30, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'inventory_item_variants'
        constraints = [models.UniqueConstraint(fields=('item', 'code'), name='inv_unique_item_variant')]

    def __str__(self):
        return f'{self.item.item_no}/{self.code}'


class ItemUOM(TenantModel):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='uoms')
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    conversion_factor = models.DecimalField(max_digits=18, decimal_places=6, default=1, help_text='Base units per one of this UOM.')
    rounding_precision = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0.001'))
    barcode = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'inventory_item_uoms'
        constraints = [models.UniqueConstraint(fields=('item', 'uom'), name='inv_unique_item_uom')]

    def clean(self):
        if self.conversion_factor <= 0:
            raise ValidationError({'conversion_factor': 'Must be greater than zero.'})


class SKU(TenantModel):
    """An item configured for one location (and optionally one variant) - Business Central's Stockkeeping Unit."""
    REPLENISHMENT = [('TRANSFER', 'Transfer'), ('PURCHASE', 'Purchase'), ('PRODUCTION', 'Production'), ('NONE', 'None')]
    code = models.CharField(max_length=60)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='skus')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='skus')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='skus')
    default_bin = models.ForeignKey(Bin, on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_skus')
    barcode = models.CharField(max_length=100, blank=True, help_text='Non-serialized SKU barcode.')
    # Planning / replenishment (the SKU card is the replenishment rule, as in Business Central)
    reorder_point = models.DecimalField(**QTY)
    minimum_stock = models.DecimalField(**QTY)
    maximum_stock = models.DecimalField(**QTY)
    safety_stock = models.DecimalField(**QTY)
    reorder_quantity = models.DecimalField(**QTY)
    lead_time_days = models.PositiveSmallIntegerField(default=0)
    replenishment_system = models.CharField(max_length=20, choices=REPLENISHMENT, default='TRANSFER')
    replenishment_source = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name='replenishes_skus')
    preferred_vendor = models.ForeignKey('erp.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    purchasing_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sales_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    inventory_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # Cost (historical cost is never overwritten by metal price movements)
    unit_cost = models.DecimalField(**MONEY)
    last_cost = models.DecimalField(**MONEY)
    standard_cost = models.DecimalField(**MONEY)
    average_cost = models.DecimalField(**MONEY)
    retail_price = models.DecimalField(**MONEY)
    # Jewellery attributes (defaults for units of this SKU)
    metal = models.CharField(max_length=20, choices=METALS, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    gross_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    other_weight = models.DecimalField(**WEIGHT)
    making_charge = models.DecimalField(**MONEY)
    wastage_percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    design = models.CharField(max_length=100, blank=True)
    collection = models.CharField(max_length=100, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    size = models.CharField(max_length=30, blank=True)
    color = models.CharField(max_length=30, blank=True)
    hallmark = models.CharField(max_length=60, blank=True)
    certificate_type = models.CharField(max_length=60, blank=True)
    serial_tracking = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    blocked = models.BooleanField(default=False)

    class Meta:
        db_table = 'inventory_skus'
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(fields=('tenant', 'code'), name='inv_unique_sku_code'),
            models.UniqueConstraint(fields=('tenant', 'item', 'variant', 'location'), name='inv_unique_sku_item_variant_location'),
            models.UniqueConstraint(fields=('tenant', 'item', 'location'), condition=Q(variant__isnull=True), name='inv_unique_sku_item_location_novariant'),
        ]
        indexes = [models.Index(fields=('tenant', 'location')), models.Index(fields=('tenant', 'item')), models.Index(fields=('tenant', 'barcode'))]

    def __str__(self):
        return self.code

    @property
    def net_metal_weight(self):
        return max(self.gross_weight - self.stone_weight - self.other_weight, ZERO)

    def clean(self):
        self.check_same_tenant('item', 'location', 'variant', 'default_bin')
        if self.variant_id and self.variant.item_id != self.item_id:
            raise ValidationError({'variant': 'Variant must belong to the item.'})
        if self.default_bin_id and self.default_bin.location_id != self.location_id:
            raise ValidationError({'default_bin': 'Default bin must be in the SKU location.'})
        if self.location_id and self.location.is_transit:
            raise ValidationError({'location': 'SKUs cannot be created on a transit location.'})

    def save(self, *args, **kwargs):
        if self.item_id and not self.metal:
            self.metal = self.item.metal
            self.purity = self.purity or self.item.purity
        if self.item_id:
            self.serial_tracking = self.item.serial_tracking
        super().save(*args, **kwargs)


class JewelleryUnit(TenantModel):
    """One physical piece of jewellery. It has exactly one current location at any moment."""
    STATUSES = [
        ('AVAILABLE', 'Available'), ('RESERVED', 'Reserved'), ('PICKED', 'Picked'), ('IN_TRANSIT', 'In transit'),
        ('QC', 'In QC'), ('DAMAGED', 'Damaged'), ('REPAIR', 'In repair'), ('BLOCKED', 'Blocked'),
        ('SOLD', 'Sold'), ('MISSING', 'Missing'), ('SCRAPPED', 'Scrapped'), ('RETURNED_TO_VENDOR', 'Returned to vendor'),
        ('NOT_IN_STOCK', 'Not yet received'), ('CONSUMED', 'Consumed in production'),
    ]
    IN_STOCK_STATUSES = ('AVAILABLE', 'RESERVED', 'PICKED', 'QC', 'DAMAGED', 'REPAIR', 'BLOCKED')
    unit_no = models.CharField(max_length=40)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='units')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='units')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='units')
    barcode = models.CharField(max_length=100)
    serial_no = models.CharField(max_length=100)
    huid = models.CharField(max_length=20, null=True, blank=True)
    tag_no = models.CharField(max_length=60, blank=True)
    metal = models.CharField(max_length=20, choices=METALS, blank=True)
    purity = models.CharField(max_length=20, blank=True)
    gross_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    other_weight = models.DecimalField(**WEIGHT)
    net_metal_weight = models.DecimalField(**WEIGHT)
    diamond_carat = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    making_charge = models.DecimalField(**MONEY)
    wastage_percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    stone_value = models.DecimalField(**MONEY)
    # Historical specific cost - never rewritten when metal prices change
    metal_cost = models.DecimalField(**MONEY)
    making_cost = models.DecimalField(**MONEY)
    wastage_cost = models.DecimalField(**MONEY)
    stone_cost = models.DecimalField(**MONEY)
    other_cost = models.DecimalField(**MONEY)
    landed_cost = models.DecimalField(**MONEY)
    purchase_cost = models.DecimalField(**MONEY)
    retail_price = models.DecimalField(**MONEY)
    current_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='units')
    current_bin = models.ForeignKey(Bin, on_delete=models.SET_NULL, null=True, blank=True, related_name='units')
    status = models.CharField(max_length=20, choices=STATUSES, default='NOT_IN_STOCK')
    certificate_no = models.CharField(max_length=100, blank=True)
    certificate_type = models.CharField(max_length=60, blank=True)
    hallmark = models.CharField(max_length=60, blank=True)
    lot_no = models.CharField(max_length=60, blank=True)
    version = models.PositiveIntegerField(default=0, help_text='Optimistic-concurrency counter, bumped on every posting.')

    class Meta:
        db_table = 'inventory_jewellery_units'
        constraints = [
            models.UniqueConstraint(fields=('tenant', 'unit_no'), name='inv_unique_unit_no'),
            models.UniqueConstraint(fields=('tenant', 'barcode'), name='inv_unique_unit_barcode'),
            models.UniqueConstraint(fields=('tenant', 'serial_no'), name='inv_unique_unit_serial'),
            models.UniqueConstraint(fields=('tenant', 'huid'), condition=Q(huid__isnull=False), name='inv_unique_unit_huid'),
            models.CheckConstraint(condition=Q(gross_weight__gte=0) & Q(stone_weight__gte=0) & Q(other_weight__gte=0), name='inv_unit_weights_non_negative'),
        ]
        indexes = [models.Index(fields=('tenant', 'current_location', 'status')), models.Index(fields=('tenant', 'sku'))]

    def __str__(self):
        return f'{self.unit_no} ({self.barcode})'

    @property
    def total_cost(self):
        return self.purchase_cost or (self.metal_cost + self.making_cost + self.wastage_cost + self.stone_cost + self.other_cost + self.landed_cost)

    @property
    def in_stock(self):
        return self.status in self.IN_STOCK_STATUSES

    def clean(self):
        self.check_same_tenant('item', 'sku', 'current_location')
        if self.stone_weight + self.other_weight > self.gross_weight:
            raise ValidationError('Stone + other weight cannot exceed gross weight.')
        if self.huid and not self.huid.strip().isalnum():
            raise ValidationError({'huid': 'HUID must be alphanumeric.'})

    def save(self, *args, **kwargs):
        self.huid = (self.huid or '').strip().upper() or None
        self.net_metal_weight = max(self.gross_weight - self.stone_weight - self.other_weight, ZERO)
        if self.sku_id and not self.item_id:
            self.item_id = self.sku.item_id
        if self.item_id and not self.metal:
            self.metal = self.item.metal
            self.purity = self.purity or self.item.purity
        if not self.purchase_cost:
            self.purchase_cost = self.metal_cost + self.making_cost + self.wastage_cost + self.stone_cost + self.other_cost + self.landed_cost
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Balances, ledger, reservations
# ---------------------------------------------------------------------------

class InventoryBalance(TenantModel):
    """Current quantity in each state for one stock bucket (item/variant/location/bin/tracking).

    Maintained only by InventoryPostingEngine. ``available_qty`` is always written from
    ``inventory.engine.compute_available`` so every screen shows the same number.
    """
    bucket_key = models.CharField(max_length=120)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='balances')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='balances')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='balances')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='balances')
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='balances')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='balances')
    lot_no = models.CharField(max_length=60, blank=True)
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    on_hand_qty = models.DecimalField(**QTY)
    reserved_qty = models.DecimalField(**QTY)
    blocked_qty = models.DecimalField(**QTY)
    qc_qty = models.DecimalField(**QTY)
    repair_qty = models.DecimalField(**QTY)
    damaged_qty = models.DecimalField(**QTY)
    in_transit_qty = models.DecimalField(**QTY)
    picked_qty = models.DecimalField(**QTY)
    available_qty = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    stone_weight = models.DecimalField(**WEIGHT)
    cost_value = models.DecimalField(**MONEY)

    class Meta:
        db_table = 'inventory_balances'
        constraints = [models.UniqueConstraint(fields=('tenant', 'bucket_key'), name='inv_unique_balance_bucket')]
        indexes = [
            models.Index(fields=('tenant', 'item', 'variant', 'location', 'bin', 'jewellery_unit'), name='inv_balance_dims'),
            models.Index(fields=('tenant', 'location')),
            models.Index(fields=('tenant', 'sku')),
        ]

    @staticmethod
    def make_key(item_id, variant_id, location_id, bin_id, unit_id, lot_no=''):
        return f'{item_id}:{variant_id or 0}:{location_id}:{bin_id or 0}:{unit_id or 0}:{lot_no or ""}'


class ImmutableQuerySet(TenantQuerySet):
    def delete(self):
        raise ValidationError('Inventory ledger entries are immutable; post a reversal instead.')

    def update(self, **kwargs):
        if set(kwargs) - {'remaining_quantity'}:
            raise ValidationError('Inventory ledger entries are immutable; post a correction instead.')
        return super().update(**kwargs)


class InventoryLedgerEntry(TenantModel):
    TRANSACTION_TYPES = [
        ('PURCHASE', 'Purchase'), ('SALE', 'Sale'), ('RETURN', 'Return'),
        ('TRANSFER_SHIPMENT', 'Transfer shipment'), ('TRANSFER_RECEIPT', 'Transfer receipt'),
        ('ADJUSTMENT_POSITIVE', 'Positive adjustment'), ('ADJUSTMENT_NEGATIVE', 'Negative adjustment'),
        ('OPENING', 'Opening'), ('COUNT', 'Physical count'), ('RECLASSIFICATION', 'Reclassification'),
        ('REPAIR', 'Repair'), ('QC', 'QC'), ('DAMAGE', 'Damage'), ('STATUS', 'Status change'),
        ('ASSEMBLY', 'Assembly'), ('DISASSEMBLY', 'Disassembly'), ('PRODUCTION', 'Production'),
        ('TRANSFER_LOSS', 'Transfer short close'), ('REVERSAL', 'Reversal'),
        ('CONSUMPTION', 'Production consumption'), ('OUTPUT', 'Production output'), ('SCRAP', 'Production scrap'),
        ('REVALUATION', 'Revaluation'),
    ]
    STATES = [('ON_HAND', 'On hand'), ('IN_TRANSIT', 'In transit'), ('RESERVED', 'Reserved'), ('BLOCKED', 'Blocked'),
              ('QC', 'QC'), ('REPAIR', 'Repair'), ('DAMAGED', 'Damaged'), ('PICKED', 'Picked'), ('AVAILABLE', 'Available')]
    posting_date = models.DateField()
    posting_time = models.DateTimeField(default=dj_timezone.now)
    document_type = models.CharField(max_length=40)
    document_no = models.CharField(max_length=60)
    document_line_no = models.PositiveIntegerField(default=0)
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPES)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='ledger_entries')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='ledger_entries')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='ledger_entries')
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='ledger_entries')
    serial_no = models.CharField(max_length=100, blank=True)
    lot_no = models.CharField(max_length=60, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    huid = models.CharField(max_length=20, blank=True)
    # quantity = signed change of physical stock held at `location` (on-hand, or in-transit on a transit location)
    stock_state = models.CharField(max_length=20, choices=STATES, default='ON_HAND')
    quantity = models.DecimalField(**QTY)
    base_quantity = models.DecimalField(**QTY)
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # Status moves inside on-hand (e.g. AVAILABLE -> DAMAGED) do not change `quantity`
    status_from = models.CharField(max_length=20, blank=True)
    status_to = models.CharField(max_length=20, blank=True)
    status_quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    cost_amount = models.DecimalField(**MONEY)
    remaining_quantity = models.DecimalField(**QTY)
    reason_code = models.CharField(max_length=40, blank=True)
    reversal_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversals')
    company = models.ForeignKey('erp.Company', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    dimensions = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        db_table = 'inventory_ledger'
        ordering = ['posting_date', 'id']
        indexes = [
            models.Index(fields=('tenant', 'posting_date')),
            models.Index(fields=('tenant', 'document_no')),
            models.Index(fields=('tenant', 'item', 'location', 'posting_date')),
            models.Index(fields=('tenant', 'sku')),
            models.Index(fields=('tenant', 'jewellery_unit')),
            models.Index(fields=('tenant', 'barcode')),
            models.Index(fields=('tenant', 'huid')),
            models.Index(fields=('tenant', 'serial_no')),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.pop('_engine_allow_update', False):
            raise ValidationError('Inventory ledger entries are immutable; post a correction instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Inventory ledger entries are immutable; post a reversal instead.')


class InventoryReservation(TenantModel):
    SOURCES = [('SALES_ORDER', 'Sales order'), ('QUOTATION', 'Quotation'), ('POS', 'POS'), ('CUSTOMER_ORDER', 'Customer order'),
               ('TRANSFER_ORDER', 'Transfer order'), ('PRODUCTION_ORDER', 'Production order'), ('REPAIR_ORDER', 'Repair order'),
               ('ECOMMERCE_ORDER', 'E-commerce order'), ('JOB_WORK_ORDER', 'Job work order')]
    STATUSES = [('ACTIVE', 'Active'), ('RELEASED', 'Released'), ('CONSUMED', 'Consumed')]
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='reservations')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='reservations')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='reservations')
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='reservations')
    balance = models.ForeignKey(InventoryBalance, on_delete=models.PROTECT, related_name='reservations')
    quantity = models.DecimalField(**QTY)
    open_quantity = models.DecimalField(**QTY)
    source_type = models.CharField(max_length=30, choices=SOURCES)
    source_no = models.CharField(max_length=60)
    source_line_no = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'inventory_reservations'
        indexes = [models.Index(fields=('tenant', 'source_type', 'source_no')), models.Index(fields=('tenant', 'status'))]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent')]


class TransferRequest(TenantModel):
    STATUSES = [('REQUESTED', 'Requested'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'),
                ('CONVERTED', 'Converted'), ('CANCELLED', 'Cancelled')]
    request_no = models.CharField(max_length=40)
    request_date = models.DateField(default=dj_timezone.localdate)
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='+')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    priority = models.CharField(max_length=10, choices=PRIORITIES, default='NORMAL')
    status = models.CharField(max_length=20, choices=STATUSES, default='REQUESTED')
    reason = models.CharField(max_length=200, blank=True)
    remarks = models.TextField(blank=True)
    transfer_order = models.ForeignKey('TransferOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='requests')

    class Meta:
        db_table = 'inventory_transfer_requests'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'request_no'), name='inv_unique_transfer_request_no')]


class TransferRequestLine(TenantModel):
    request = models.ForeignKey(TransferRequest, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    requested_qty = models.DecimalField(**QTY)
    approved_qty = models.DecimalField(**QTY)
    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'inventory_transfer_request_lines'
        ordering = ['line_no']


class TransferOrder(TenantModel):
    STATUSES = [('DRAFT', 'Draft'), ('PENDING_APPROVAL', 'Pending approval'), ('APPROVED', 'Approved'), ('RELEASED', 'Released'),
                ('PARTIALLY_SHIPPED', 'Partially shipped'), ('SHIPPED', 'Shipped'), ('PARTIALLY_RECEIVED', 'Partially received'),
                ('RECEIVED', 'Received'), ('CLOSED', 'Closed'), ('CANCELLED', 'Cancelled')]
    SOURCES = [('MANUAL', 'Manual'), ('REQUEST', 'Transfer request'), ('REPLENISHMENT', 'Replenishment'),
               ('SALES_DEMAND', 'Sales demand'), ('MINIMUM_STOCK', 'Minimum stock'), ('ECOMMERCE', 'E-commerce demand'),
               ('PURCHASE_RECEIPT', 'Purchase receipt'), ('WAREHOUSE_PLANNING', 'Warehouse planning'), ('RETURN', 'Return transfer'),
               ('IMPORT', 'Excel import')]
    RETURN_REASONS = [('', '-'), ('SLOW_MOVING', 'Slow moving'), ('DISPLAY_RETURN', 'Display return'), ('REPAIR', 'Repair'),
                      ('QC', 'QC'), ('DAMAGED', 'Damaged'), ('CUSTOMER_RETURN', 'Customer return'), ('SEASONAL', 'Seasonal'),
                      ('STOCK_BALANCING', 'Stock balancing')]
    transfer_no = models.CharField(max_length=40)
    transfer_date = models.DateField(default=dj_timezone.localdate)
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='transfers_out')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='transfers_in')
    transit_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_via')
    route = models.ForeignKey(TransferRoute, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    direct_transfer = models.BooleanField(default=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    shipment_date = models.DateField(null=True, blank=True)
    expected_receipt_date = models.DateField(null=True, blank=True)
    actual_shipment_date = models.DateField(null=True, blank=True)
    actual_receipt_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITIES, default='NORMAL')
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    source_type = models.CharField(max_length=30, choices=SOURCES, default='MANUAL')
    return_reason = models.CharField(max_length=30, choices=RETURN_REASONS, blank=True)
    required_approval_level = models.PositiveSmallIntegerField(default=0)
    reason = models.CharField(max_length=200, blank=True)
    remarks = models.TextField(blank=True)
    shipping_agent = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = 'inventory_transfer_orders'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'transfer_no'), name='inv_unique_transfer_no')]
        indexes = [models.Index(fields=('tenant', 'status')), models.Index(fields=('tenant', 'from_location')),
                   models.Index(fields=('tenant', 'to_location'))]

    def __str__(self):
        return self.transfer_no

    def clean(self):
        self.check_same_tenant('from_location', 'to_location', 'transit_location')
        if self.from_location_id == self.to_location_id:
            raise ValidationError('A transfer needs two different locations.')
        if self.from_location.is_transit or self.to_location.is_transit:
            raise ValidationError('Transit locations cannot be a transfer source or destination.')
        if self.from_location.company_id != self.to_location.company_id:
            raise ValidationError('Locations belong to different companies: use an inter-company transfer (sales/purchase) instead.')

    @property
    def total_qty(self):
        return sum((line.quantity for line in self.lines.all()), ZERO)

    @property
    def total_value(self):
        return sum((line.line_value for line in self.lines.all()), ZERO)

    @property
    def total_weight(self):
        return sum((line.gross_weight for line in self.lines.all()), ZERO)


class TransferOrderLine(TenantModel):
    STATUSES = [('OPEN', 'Open'), ('PARTIALLY_SHIPPED', 'Partially shipped'), ('SHIPPED', 'Shipped'),
                ('PARTIALLY_RECEIVED', 'Partially received'), ('RECEIVED', 'Received'), ('CLOSED', 'Closed'), ('CANCELLED', 'Cancelled')]
    transfer = models.ForeignKey(TransferOrder, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer_lines')
    to_sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer_in_lines')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer_lines')
    description = models.CharField(max_length=200, blank=True)
    from_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    requested_qty = models.DecimalField(**QTY)
    quantity = models.DecimalField(**QTY, help_text='Approved quantity.')
    qty_shipped = models.DecimalField(**QTY)
    qty_received = models.DecimalField(**QTY)
    qty_damaged = models.DecimalField(**QTY)
    qty_short_closed = models.DecimalField(**QTY)
    qty_cancelled = models.DecimalField(**QTY)
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='OPEN')

    class Meta:
        db_table = 'inventory_transfer_order_lines'
        ordering = ['line_no']
        constraints = [models.UniqueConstraint(fields=('transfer', 'line_no'), name='inv_unique_transfer_line')]

    @property
    def qty_to_ship(self):
        return max(self.quantity - self.qty_shipped - self.qty_cancelled, ZERO)

    @property
    def qty_in_transit(self):
        return max(self.qty_shipped - self.qty_received - self.qty_short_closed, ZERO)

    @property
    def qty_outstanding(self):
        return max(self.quantity - self.qty_received - self.qty_short_closed - self.qty_cancelled, ZERO)

    @property
    def line_value(self):
        return (self.quantity * self.unit_cost).quantize(Decimal('0.01'))


class TransferShipment(TenantModel):
    shipment_no = models.CharField(max_length=40)
    transfer = models.ForeignKey(TransferOrder, on_delete=models.PROTECT, related_name='shipments')
    posting_date = models.DateField()
    shipped_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_transfer_shipments'
        constraints = [models.UniqueConstraint(fields=('tenant', 'shipment_no'), name='inv_unique_shipment_no')]


class TransferShipmentLine(TenantModel):
    shipment = models.ForeignKey(TransferShipment, on_delete=models.CASCADE, related_name='lines')
    transfer_line = models.ForeignKey(TransferOrderLine, on_delete=models.PROTECT, related_name='shipment_lines')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    cost_amount = models.DecimalField(**MONEY)

    class Meta:
        db_table = 'inventory_transfer_shipment_lines'


class TransferReceipt(TenantModel):
    receipt_no = models.CharField(max_length=40)
    transfer = models.ForeignKey(TransferOrder, on_delete=models.PROTECT, related_name='receipts')
    posting_date = models.DateField()
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_transfer_receipts'
        constraints = [models.UniqueConstraint(fields=('tenant', 'receipt_no'), name='inv_unique_receipt_no')]


class TransferReceiptLine(TenantModel):
    REASONS = [('', '-'), ('MISSING', 'Missing'), ('DAMAGED', 'Damaged'), ('COUNTING_ERROR', 'Counting error'),
               ('WRONG_ITEM', 'Wrong item'), ('WRONG_QUANTITY', 'Wrong quantity'), ('OTHER', 'Other')]
    receipt = models.ForeignKey(TransferReceipt, on_delete=models.CASCADE, related_name='lines')
    transfer_line = models.ForeignKey(TransferOrderLine, on_delete=models.PROTECT, related_name='receipt_lines')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY, help_text='Total received, including damaged.')
    damaged_qty = models.DecimalField(**QTY)
    excess_qty = models.DecimalField(**QTY)
    reason_code = models.CharField(max_length=20, choices=REASONS, blank=True)
    cost_amount = models.DecimalField(**MONEY)

    class Meta:
        db_table = 'inventory_transfer_receipt_lines'


DOC_STATUSES = [('DRAFT', 'Draft'), ('SUBMITTED', 'Submitted'), ('APPROVED', 'Approved'), ('POSTED', 'Posted'), ('CANCELLED', 'Cancelled')]


class InventoryAdjustment(TenantModel):
    REASONS = [('DAMAGE', 'Damage'), ('LOSS', 'Loss / theft'), ('FOUND', 'Found'), ('COUNT', 'Count correction'),
               ('WEIGHT', 'Weight correction'), ('OPENING', 'Opening balance'), ('QC', 'QC'), ('SCRAP', 'Scrap'), ('OTHER', 'Other')]
    adjustment_no = models.CharField(max_length=40)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='adjustments')
    posting_date = models.DateField(default=dj_timezone.localdate)
    reason_code = models.CharField(max_length=20, choices=REASONS)
    reference = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=DOC_STATUSES, default='DRAFT')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    source_count = models.ForeignKey('PhysicalCount', on_delete=models.PROTECT, null=True, blank=True, related_name='adjustments')

    class Meta:
        db_table = 'inventory_adjustments'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'adjustment_no'), name='inv_unique_adjustment_no')]


class InventoryAdjustmentLine(TenantModel):
    TYPES = [('POSITIVE', 'Positive'), ('NEGATIVE', 'Negative'), ('STATUS', 'Status change'), ('WEIGHT', 'Weight correction')]
    adjustment = models.ForeignKey(InventoryAdjustment, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    adjustment_type = models.CharField(max_length=20, choices=TYPES)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(**QTY)
    gross_weight = models.DecimalField(**WEIGHT)
    net_weight = models.DecimalField(**WEIGHT)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)

    class Meta:
        db_table = 'inventory_adjustment_lines'
        ordering = ['line_no']


class Reclassification(TenantModel):
    reclass_no = models.CharField(max_length=40)
    posting_date = models.DateField(default=dj_timezone.localdate)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=DOC_STATUSES, default='DRAFT')
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'inventory_reclassification'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'reclass_no'), name='inv_unique_reclass_no')]


class ReclassificationLine(TenantModel):
    reclass = models.ForeignKey(Reclassification, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='+')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='+')
    from_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(**QTY)

    class Meta:
        db_table = 'inventory_reclassification_lines'
        ordering = ['line_no']


class PhysicalCount(TenantModel):
    STATUSES = [('DRAFT', 'Draft'), ('COUNTING', 'Counting (snapshot taken)'), ('SUBMITTED', 'Submitted'),
                ('APPROVED', 'Approved'), ('POSTED', 'Posted'), ('CANCELLED', 'Cancelled')]
    count_no = models.CharField(max_length=40)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='physical_counts')
    count_date = models.DateField(default=dj_timezone.localdate)
    blind_count = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    snapshot_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_physical_inventory'
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=('tenant', 'count_no'), name='inv_unique_count_no')]


class PhysicalCountLine(TenantModel):
    count = models.ForeignKey(PhysicalCount, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='+')
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    jewellery_unit = models.ForeignKey(JewelleryUnit, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    system_qty = models.DecimalField(**QTY)
    system_gross_weight = models.DecimalField(**WEIGHT)
    counted_qty = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    counted_gross_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    scanned = models.BooleanField(default=False)
    unexpected = models.BooleanField(default=False, help_text='Scanned but not in the snapshot.')

    class Meta:
        db_table = 'inventory_physical_inventory_lines'

    @property
    def variance_qty(self):
        return (self.counted_qty or ZERO) - self.system_qty

    @property
    def variance_weight(self):
        return (self.counted_gross_weight or ZERO) - self.system_gross_weight


class ReplenishmentLine(TenantModel):
    STATUSES = [('SUGGESTED', 'Suggested'), ('APPROVED', 'Approved'), ('IGNORED', 'Ignored'), ('CONVERTED', 'Converted')]
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name='replenishment_lines')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='+')
    source_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    current_qty = models.DecimalField(**QTY)
    minimum_qty = models.DecimalField(**QTY)
    maximum_qty = models.DecimalField(**QTY)
    reorder_point = models.DecimalField(**QTY)
    shortage_qty = models.DecimalField(**QTY)
    suggested_qty = models.DecimalField(**QTY)
    status = models.CharField(max_length=20, choices=STATUSES, default='SUGGESTED')
    transfer_order = models.ForeignKey(TransferOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'inventory_replenishment_worksheets'


class ApprovalRule(TenantModel):
    DOCUMENTS = [('TRANSFER', 'Transfer order'), ('ADJUSTMENT', 'Inventory adjustment')]
    name = models.CharField(max_length=120)
    document_type = models.CharField(max_length=20, choices=DOCUMENTS, default='TRANSFER')
    min_value = models.DecimalField(**MONEY)
    min_weight = models.DecimalField(**WEIGHT)
    metal = models.CharField(max_length=20, choices=METALS, blank=True, help_text='Blank = any metal.')
    approver_level = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'inventory_approval_rules'


class InventoryPeriod(TenantModel):
    period_start = models.DateField()
    period_end = models.DateField()
    closed = models.BooleanField(default=False)

    class Meta:
        db_table = 'inventory_periods'


class ChannelAllocation(TenantModel):
    CHANNELS = [('POS', 'POS'), ('ECOMMERCE', 'E-commerce'), ('MARKETPLACE', 'Marketplace'), ('WHOLESALE', 'Wholesale'), ('RETAIL', 'Retail')]
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='channel_allocations')
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, null=True, blank=True, related_name='channel_allocations')
    channel = models.CharField(max_length=20, choices=CHANNELS)
    allocation_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    allocation_qty = models.DecimalField(**QTY)
    safety_stock = models.DecimalField(**QTY)

    class Meta:
        db_table = 'inventory_channel_allocations'
        constraints = [models.UniqueConstraint(fields=('location', 'sku', 'channel'), name='inv_unique_channel_allocation')]


class ImportBatch(TenantModel):
    TYPES = [('opening_inventory', 'Opening inventory / barcode import'), ('locations', 'Locations'), ('transfer_orders', 'Transfer orders')]
    STATUSES = [('UPLOADED', 'Uploaded'), ('VALIDATED', 'Validated'), ('FAILED', 'Has errors'), ('IMPORTED', 'Imported')]
    import_type = models.CharField(max_length=30, choices=TYPES)
    file_name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUSES, default='UPLOADED')
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'inventory_import_batches'
        ordering = ['-id']


class InventoryAuditLog(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='+')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True)
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=30)
    document_type = models.CharField(max_length=40)
    document_no = models.CharField(max_length=60, blank=True)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = 'inventory_audit_logs'
        ordering = ['-id']
        indexes = [models.Index(fields=('tenant', 'document_type', 'document_no')), models.Index(fields=('tenant', 'created_at'))]
