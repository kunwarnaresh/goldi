from django import forms

from .models import (
    Customer, Employee, ExchangeTransaction, GSTSlab, InvoiceSetting,
    Product, Role, SalesApproval, SalesInvoice, SalesReturn, Supplier,
    Warehouse, RepairOrder, CustomerOrnament,
)


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'item_category', 'sku', 'name', 'metal_type', 'purity', 'weight_grams',
            'making_charge', 'purchase_price', 'sale_price', 'mrp', 'barcode',
            'hsn_code', 'stock_quantity', 'is_active',
        ]
        widgets = {
            'item_category': forms.Select(attrs={'class': 'form-select'}),
            'sku': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'metal_type': forms.Select(attrs={'class': 'form-select'}),
            'purity': forms.TextInput(attrs={'class': 'form-control'}),
            'weight_grams': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'making_charge': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'sale_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'mrp': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'barcode': forms.TextInput(attrs={'class': 'form-control'}),
            'hsn_code': forms.TextInput(attrs={'class': 'form-control'}),
            'stock_quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class CustomerForm(forms.ModelForm):
    preferred_contact_method = forms.CharField(required=False, initial='phone', widget=forms.TextInput(attrs={'class': 'form-control'}))
    preferred_language = forms.CharField(required=False, initial='en-IN', widget=forms.TextInput(attrs={'class': 'form-control'}))

    class Meta:
        model = Customer
        fields = [
            'name', 'title', 'first_name', 'middle_name', 'last_name', 'phone', 'alternate_phone',
            'whatsapp_number', 'email', 'customer_type', 'customer_status', 'gst_customer_type',
            'gst_registration_type', 'gstin', 'gst_state_code', 'gst_legal_name', 'pan',
            'address', 'preferred_contact_method', 'preferred_language', 'occupation',
            'company_name', 'designation', 'date_of_birth', 'anniversary_date', 'is_active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'middle_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'alternate_phone': forms.TextInput(attrs={'class': 'form-control'}),
            'whatsapp_number': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'customer_status': forms.TextInput(attrs={'class': 'form-control'}),
            'gst_customer_type': forms.TextInput(attrs={'class': 'form-control'}),
            'gst_registration_type': forms.TextInput(attrs={'class': 'form-control'}),
            'gstin': forms.TextInput(attrs={'class': 'form-control'}),
            'gst_state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'gst_legal_name': forms.TextInput(attrs={'class': 'form-control'}),
            'pan': forms.TextInput(attrs={'class': 'form-control'}),
            'customer_type': forms.Select(attrs={'class': 'form-select'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'date_of_birth': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'anniversary_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }


class RepairOrderForm(forms.ModelForm):
    estimated_charges = forms.DecimalField(required=False, initial=0, widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}))

    class Meta:
        model = RepairOrder
        fields = ['customer', 'repair_type', 'repair_description', 'customer_remarks', 'priority', 'expected_completion_date', 'estimated_charges']
        widgets = {
            'customer': forms.Select(attrs={'class': 'form-select'}),
            'repair_type': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Polishing, resizing, stone setting'}),
            'repair_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'customer_remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'priority': forms.Select(attrs={'class': 'form-select'}, choices=[('normal', 'Normal'), ('high', 'High'), ('urgent', 'Urgent')]),
            'expected_completion_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'estimated_charges': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }


class CustomerOrnamentForm(forms.ModelForm):
    class Meta:
        model = CustomerOrnament
        fields = ['description', 'metal_type', 'purity', 'gross_weight', 'stone_weight', 'other_weight', 'huid', 'certificate_number', 'condition_at_receipt', 'customer_declared_value']
        widgets = {
            'description': forms.TextInput(attrs={'class': 'form-control'}),
            'metal_type': forms.TextInput(attrs={'class': 'form-control'}),
            'purity': forms.TextInput(attrs={'class': 'form-control'}),
            'gross_weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'stone_weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'other_weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'huid': forms.TextInput(attrs={'class': 'form-control'}),
            'certificate_number': forms.TextInput(attrs={'class': 'form-control'}),
            'condition_at_receipt': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'customer_declared_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ['name', 'phone', 'email', 'gstin', 'pan', 'address', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'gstin': forms.TextInput(attrs={'class': 'form-control'}),
            'pan': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ['name', 'description', 'permissions', 'is_system']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'permissions': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'is_system': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = ['employee_code', 'full_name', 'phone', 'email', 'department', 'designation', 'date_of_joining', 'role', 'is_active']
        widgets = {
            'employee_code': forms.TextInput(attrs={'class': 'form-control'}),
            'full_name': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'department': forms.TextInput(attrs={'class': 'form-control'}),
            'designation': forms.TextInput(attrs={'class': 'form-control'}),
            'date_of_joining': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ['code', 'name', 'address', 'city', 'state_code', 'manager', 'is_active']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'city': forms.TextInput(attrs={'class': 'form-control'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'manager': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class GSTSlabForm(forms.ModelForm):
    class Meta:
        model = GSTSlab
        fields = ['name', 'state_code', 'gst_rate', 'cess_rate', 'hsn_code', 'is_active', 'effective_from', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'gst_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'cess_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'hsn_code': forms.TextInput(attrs={'class': 'form-control'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'effective_from': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class POSSaleForm(forms.ModelForm):
    customer = forms.ModelChoiceField(required=False, queryset=Customer.objects.filter(is_active=True), widget=forms.Select(attrs={'class': 'form-select'}))
    invoice_no = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    discount_amount = forms.DecimalField(required=False, initial=0, widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}))
    customer_name = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'New customer name'}))
    customer_phone = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Phone'}))
    customer_gstin = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'GSTIN'}))
    place_of_supply = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'State code / place'}))
    terms_conditions = forms.CharField(required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}))

    class Meta:
        model = SalesInvoice
        fields = ['customer', 'invoice_no', 'discount_amount', 'notes', 'terms_conditions', 'place_of_supply']
        widgets = {
            'invoice_no': forms.TextInput(attrs={'class': 'form-control'}),
            'discount_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'customer': forms.Select(attrs={'class': 'form-select'}),
            'place_of_supply': forms.TextInput(attrs={'class': 'form-control'}),
            'terms_conditions': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class SalesReturnForm(forms.ModelForm):
    class Meta:
        model = SalesReturn
        fields = ['invoice', 'reason', 'status', 'notes']
        widgets = {
            'invoice': forms.Select(attrs={'class': 'form-select'}),
            'reason': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'status': forms.Select(attrs={'class': 'form-select'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class InvoiceSettingForm(forms.ModelForm):
    class Meta:
        model = InvoiceSetting
        fields = ['company_name', 'gstin', 'pan', 'address', 'city', 'state_code', 'phone', 'email', 'terms_and_conditions', 'is_default']
        widgets = {
            'company_name': forms.TextInput(attrs={'class': 'form-control'}),
            'gstin': forms.TextInput(attrs={'class': 'form-control'}),
            'pan': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'city': forms.TextInput(attrs={'class': 'form-control'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'terms_and_conditions': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'is_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class SalesApprovalForm(forms.ModelForm):
    class Meta:
        model = SalesApproval
        fields = ['status', 'notes']
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class ExchangeTransactionForm(forms.ModelForm):
    class Meta:
        model = ExchangeTransaction
        fields = ['customer', 'metal_type', 'weight_grams', 'old_product_name', 'market_rate_per_gram', 'exchange_value', 'notes']
        widgets = {
            'customer': forms.Select(attrs={'class': 'form-select'}),
            'metal_type': forms.Select(attrs={'class': 'form-select'}),
            'old_product_name': forms.TextInput(attrs={'class': 'form-control'}),
            'weight_grams': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'market_rate_per_gram': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'exchange_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
