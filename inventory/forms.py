from django import forms

from .models import (
    ApprovalRule, Bin, ChannelAllocation, InventoryPeriod, Item, ItemUOM, ItemVariant, JewelleryUnit, Location,
    LocationPermission, SKU, TransferRoute, UnitOfMeasure, Zone,
)


class TenantModelForm(forms.ModelForm):
    """Every choice list only offers rows of the current tenant; the tenant itself is never a form field."""

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)
        self.instance.tenant = tenant
        for field in self.fields.values():
            queryset = getattr(field, 'queryset', None)
            if queryset is not None and hasattr(queryset.model, 'tenant'):
                field.queryset = queryset.filter(tenant=tenant)
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'w-auto')

    def _get_validation_exclusions(self):
        # tenant is set on the instance, not the form - keep it so per-tenant unique constraints are validated
        exclusions = super()._get_validation_exclusions()
        exclusions.discard('tenant')
        return exclusions


LOCATION_SECTIONS = [
    ('General', ['code', 'name', 'location_type', 'company', 'store', 'region', 'active', 'blocked']),
    ('Address', ['address_1', 'address_2', 'area', 'city', 'state', 'country', 'pin', 'gstin', 'contact_person', 'phone', 'email',
                 'default_currency', 'time_zone']),
    ('Transaction setup', ['allow_purchase', 'allow_sales', 'allow_pos', 'allow_transfer_out', 'allow_transfer_in',
                           'allow_direct_transfer', 'allow_adjustment', 'allow_reservation', 'allow_physical_count',
                           'allow_negative_inventory', 'serial_tracking', 'lot_tracking', 'weight_tracking']),
    ('Warehouse setup', ['is_warehouse', 'bin_mandatory', 'zone_mandatory', 'directed_putaway', 'directed_pick', 'require_qc',
                         'require_putaway', 'require_pick']),
    ('Financial', ['inventory_posting_group', 'dimension_1', 'dimension_2', 'cost_centre', 'profit_centre',
                   'metal_rate_premium_per_gram']),
]


class LocationForm(TenantModelForm):
    sections = LOCATION_SECTIONS

    class Meta:
        model = Location
        fields = [f for _, names in LOCATION_SECTIONS for f in names]


class ItemForm(TenantModelForm):
    class Meta:
        model = Item
        fields = ['item_no', 'description', 'item_type', 'category', 'metal', 'purity', 'base_uom', 'serial_tracking', 'lot_tracking',
                  'weight_tracking', 'costing_method', 'standard_cost', 'design', 'collection', 'gender', 'hsn_code', 'active', 'blocked']


class ItemVariantForm(TenantModelForm):
    class Meta:
        model = ItemVariant
        fields = ['item', 'code', 'description', 'size', 'color', 'purity', 'active', 'blocked']


class ItemUOMForm(TenantModelForm):
    class Meta:
        model = ItemUOM
        fields = ['item', 'uom', 'conversion_factor', 'rounding_precision', 'barcode', 'active']


class UOMForm(TenantModelForm):
    class Meta:
        model = UnitOfMeasure
        fields = ['code', 'name', 'decimal_places']


class SKUForm(TenantModelForm):
    class Meta:
        model = SKU
        fields = ['code', 'item', 'variant', 'location', 'default_bin', 'barcode', 'reorder_point', 'minimum_stock', 'maximum_stock',
                  'safety_stock', 'reorder_quantity', 'lead_time_days', 'replenishment_system', 'replenishment_source',
                  'preferred_vendor', 'purchasing_uom', 'sales_uom', 'inventory_uom', 'unit_cost', 'standard_cost', 'retail_price',
                  'metal', 'purity', 'gross_weight', 'stone_weight', 'other_weight', 'making_charge', 'wastage_percent', 'design',
                  'collection', 'gender', 'size', 'color', 'hallmark', 'certificate_type', 'active', 'blocked']


class ZoneForm(TenantModelForm):
    class Meta:
        model = Zone
        fields = ['location', 'code', 'name', 'zone_type', 'sequence', 'active']


class BinForm(TenantModelForm):
    class Meta:
        model = Bin
        fields = ['location', 'zone', 'code', 'bin_type', 'description', 'capacity', 'weight_capacity', 'allow_mixed_items',
                  'allow_mixed_tracking', 'pick_sequence', 'putaway_sequence', 'blocked', 'active']


class TransferRouteForm(TenantModelForm):
    class Meta:
        model = TransferRoute
        fields = ['from_location', 'to_location', 'transit_location', 'transfer_days', 'shipping_agent', 'shipping_service',
                  'allow_transfer', 'active']


class LocationPermissionForm(TenantModelForm):
    class Meta:
        model = LocationPermission
        fields = ['user', 'location', 'can_view', 'can_sell', 'can_create_transfer', 'can_approve_transfer', 'can_ship', 'can_receive',
                  'can_adjust', 'can_approve_adjustment', 'can_count', 'can_reclassify', 'can_direct_transfer', 'approval_level']

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, tenant=tenant, **kwargs)
        self.fields['user'].queryset = self.fields['user'].queryset.filter(tenant_memberships__tenant=tenant)


class ApprovalRuleForm(TenantModelForm):
    class Meta:
        model = ApprovalRule
        fields = ['name', 'document_type', 'min_value', 'min_weight', 'metal', 'approver_level', 'active']


class InventoryPeriodForm(TenantModelForm):
    class Meta:
        model = InventoryPeriod
        fields = ['period_start', 'period_end', 'closed']
        widgets = {'period_start': forms.DateInput(attrs={'type': 'date'}), 'period_end': forms.DateInput(attrs={'type': 'date'})}


class ChannelAllocationForm(TenantModelForm):
    class Meta:
        model = ChannelAllocation
        fields = ['location', 'sku', 'channel', 'allocation_percent', 'allocation_qty', 'safety_stock']


class JewelleryUnitForm(TenantModelForm):
    """Register a new piece. It enters stock only when an opening/purchase receipt is posted."""
    class Meta:
        model = JewelleryUnit
        fields = ['sku', 'barcode', 'serial_no', 'huid', 'tag_no', 'purity', 'gross_weight', 'stone_weight', 'other_weight',
                  'diamond_carat', 'making_charge', 'wastage_percent', 'stone_value', 'metal_cost', 'making_cost', 'wastage_cost',
                  'stone_cost', 'other_cost', 'landed_cost', 'retail_price', 'certificate_no', 'certificate_type', 'hallmark']

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, tenant=tenant, **kwargs)
        self.fields['sku'].queryset = self.fields['sku'].queryset.filter(item__serial_tracking=True)
        self.fields['sku'].required = True


MASTERS = {
    'items': ('Items', ItemForm, 'item_no'),
    'variants': ('Item variants', ItemVariantForm, 'code'),
    'item-uoms': ('Item units of measure', ItemUOMForm, 'id'),
    'uoms': ('Units of measure', UOMForm, 'code'),
    'skus': ('Stockkeeping units', SKUForm, 'code'),
    'locations': ('Locations', LocationForm, 'code'),
    'zones': ('Warehouse zones', ZoneForm, 'code'),
    'bins': ('Bins', BinForm, 'code'),
    'routes': ('Transfer routes', TransferRouteForm, 'id'),
    'location-users': ('Location permissions', LocationPermissionForm, 'id'),
    'approval-rules': ('Transfer approval rules', ApprovalRuleForm, 'approver_level'),
    'periods': ('Inventory periods', InventoryPeriodForm, '-period_start'),
    'channel-allocations': ('Channel allocation', ChannelAllocationForm, 'id'),
}
