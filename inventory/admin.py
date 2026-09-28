from django.contrib import admin

from . import models


@admin.register(models.Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'costing_method', 'allow_negative_inventory', 'active')


@admin.register(models.TenantMembership)
class TenantMembershipAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'user', 'role', 'all_locations', 'active')
    list_filter = ('tenant', 'role')


@admin.register(models.Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'code', 'name', 'location_type', 'city', 'active', 'blocked')
    list_filter = ('tenant', 'location_type')


@admin.register(models.InventoryLedgerEntry)
class InventoryLedgerEntryAdmin(admin.ModelAdmin):
    """Read-only: the ledger is immutable and only the posting engine writes it."""
    list_display = ('posting_date', 'tenant', 'document_no', 'transaction_type', 'item', 'location', 'quantity', 'cost_amount')
    list_filter = ('tenant', 'transaction_type')
    search_fields = ('document_no', 'barcode', 'huid', 'serial_no')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.InventoryAuditLog)
class InventoryAuditLogAdmin(InventoryLedgerEntryAdmin):
    list_display = ('created_at', 'tenant', 'user', 'action', 'document_type', 'document_no', 'ip_address')
    list_filter = ('tenant', 'action', 'document_type')
    search_fields = ('document_no',)


for model in (models.Item, models.SKU, models.JewelleryUnit, models.Bin, models.Zone, models.TransferRoute, models.LocationPermission,
              models.ApprovalRule, models.InventoryPeriod):
    admin.site.register(model)
