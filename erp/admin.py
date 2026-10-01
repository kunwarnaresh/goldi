from django.contrib import admin
from django import forms
from django.contrib.auth.hashers import make_password

from .models import (
    BinLocation, Business, Customer, Employee, ExchangeTransaction, Expense,
    GSTSlab, InventoryMovement, ItemBarcode, ItemBatch, ItemCategory, ItemPrice,
    ItemSerial, LedgerEntry, Payment, Product, PurchaseOrder, Role,
    SalesApproval, SalesInvoice, SalesInvoiceItem, Staff, StockLedger,
    Supplier, TaxLedger, UserProfile, Warehouse, WarehouseMovement, Store,
    POSStaff, POSTerminal, POSStaffAssignment, POSShift, POSSession,
    JewelleryMetalRate, JewelleryPricingRule, JewelleryItemUnit, JewelleryStone,
    CustomerAddress, CustomerShippingAddress, CustomerIdentityDocument,
    CustomerCommunicationPreference, CustomerJewelleryPreference,
    CustomerMarketingProfile, CustomerKYC, CustomerPriceGroup, CustomerDiscountGroup,
    CustomerDiscountRule, CustomerNumberSeries,
    InvoicePrintLayout, StoreInvoicePrintSetup, POSTerminalPrintSetup, InvoicePrintLog,
    Karigar, RepairService, RepairOrder, CustomerOrnament, RepairCustodyEvent,
    RepairKarigarAssignment, RepairQC, RepairInvoice,
    Quotation, QuotationLine, SalesOrder, SalesOrderLine, PaymentReceipt, PaymentAllocation,
    PurchaseOrderLine, GoodsReceipt, GoodsReceiptLine, SupplierInvoiceLine,
    VendorPayment, VendorPaymentAllocation, FinancePostingSetup,
)

admin.site.register(Business)
admin.site.register(Customer)
admin.site.register(Supplier)
admin.site.register(Staff)
admin.site.register(Role)
admin.site.register(Employee)
admin.site.register(UserProfile)
admin.site.register(Store)

class POSStaffAdminForm(forms.ModelForm):
    pin = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = POSStaff
        exclude = ('pin_hash',)

    def save(self, commit=True):
        staff = super().save(commit=False)
        if self.cleaned_data.get('pin'):
            staff.pin_hash = make_password(self.cleaned_data['pin'])
        if commit:
            staff.save()
        return staff


@admin.register(POSStaff)
class POSStaffAdmin(admin.ModelAdmin):
    form = POSStaffAdminForm
    list_display = ('employee_code', 'name', 'role', 'store', 'pos_access', 'is_active', 'is_blocked')
    list_filter = ('role', 'store', 'pos_access', 'is_active', 'is_blocked')
    search_fields = ('employee_code', 'name', 'mobile')
admin.site.register(POSTerminal)
admin.site.register(POSStaffAssignment)
admin.site.register(POSShift)
admin.site.register(POSSession)
admin.site.register(JewelleryMetalRate)
admin.site.register(JewelleryPricingRule)
admin.site.register(JewelleryItemUnit)
admin.site.register(JewelleryStone)
admin.site.register(CustomerAddress)
admin.site.register(CustomerShippingAddress)
admin.site.register(CustomerIdentityDocument)
admin.site.register(CustomerCommunicationPreference)
admin.site.register(CustomerJewelleryPreference)
admin.site.register(CustomerMarketingProfile)
admin.site.register(CustomerKYC)
admin.site.register(CustomerPriceGroup)
admin.site.register(CustomerDiscountGroup)
admin.site.register(CustomerDiscountRule)
admin.site.register(CustomerNumberSeries)
admin.site.register(InvoicePrintLayout)
admin.site.register(StoreInvoicePrintSetup)
admin.site.register(POSTerminalPrintSetup)
admin.site.register(InvoicePrintLog)
admin.site.register(Karigar)
admin.site.register(RepairService)
admin.site.register(RepairOrder)
admin.site.register(CustomerOrnament)
admin.site.register(RepairCustodyEvent)
admin.site.register(RepairKarigarAssignment)
admin.site.register(RepairQC)
admin.site.register(RepairInvoice)
admin.site.register(ItemCategory)
admin.site.register(Product)
admin.site.register(ItemPrice)
admin.site.register(ItemBarcode)
admin.site.register(ItemBatch)
admin.site.register(ItemSerial)
admin.site.register(GSTSlab)
admin.site.register(Warehouse)
admin.site.register(BinLocation)
admin.site.register(WarehouseMovement)
admin.site.register(InventoryMovement)
admin.site.register(SalesInvoice)
admin.site.register(SalesInvoiceItem)
admin.site.register(SalesApproval)
admin.site.register(ExchangeTransaction)
admin.site.register(Payment)
admin.site.register(TaxLedger)
admin.site.register(PurchaseOrder)
admin.site.register(StockLedger)
admin.site.register(Expense)
admin.site.register(LedgerEntry)
admin.site.register(Quotation)
admin.site.register(QuotationLine)
admin.site.register(SalesOrder)
admin.site.register(SalesOrderLine)
admin.site.register(PaymentReceipt)
admin.site.register(PaymentAllocation)
admin.site.register(PurchaseOrderLine)
admin.site.register(GoodsReceipt)
admin.site.register(GoodsReceiptLine)
admin.site.register(SupplierInvoiceLine)
admin.site.register(VendorPayment)
admin.site.register(VendorPaymentAllocation)
admin.site.register(FinancePostingSetup)


from .models import POSPayment, POSRole, StoreTender, Tender  # noqa: E402

admin.site.register(POSRole)
admin.site.register(Tender)
admin.site.register(StoreTender)


@admin.register(POSPayment)
class POSPaymentAdmin(admin.ModelAdmin):
    list_display = ('invoice', 'tender_name_snapshot', 'amount', 'change_amount', 'reference', 'created_at')

    def has_change_permission(self, request, obj=None):
        return False
