from decimal import Decimal

from rest_framework import serializers

from .models import (
    PaymentAllocation, PaymentReceipt, Quotation, QuotationLine,
    SalesInvoice, SalesInvoiceItem, SalesOrder, SalesOrderLine,
    GoodsReceipt, PurchaseOrder, PurchaseOrderLine, SupplierInvoice,
    SupplierInvoiceLine, VendorPayment, VendorPaymentAllocation,
)


class QuotationLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    remaining_quantity = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)

    class Meta:
        model = QuotationLine
        fields = [
            'id', 'product', 'product_name', 'quantity', 'unit_price', 'discount_amount',
            'taxable_amount', 'tax_rate', 'gst_amount', 'line_total', 'converted_quantity',
            'remaining_quantity',
        ]


class QuotationSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    lines = QuotationLineSerializer(many=True, read_only=True)

    class Meta:
        model = Quotation
        fields = [
            'id', 'quotation_no', 'customer', 'customer_name', 'store', 'salesperson',
            'quotation_date', 'valid_until', 'payment_terms', 'delivery_terms', 'remarks',
            'status', 'subtotal', 'discount_amount', 'taxable_amount', 'gst_amount',
            'total_amount', 'lines', 'created_at', 'updated_at',
        ]
        read_only_fields = ['quotation_no', 'status', 'subtotal', 'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount']


class QuotationLineInputSerializer(serializers.Serializer):
    product = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0'))
    discount_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=Decimal('0'), min_value=Decimal('0'))
    jewellery_barcode = serializers.CharField(required=False, allow_blank=True, default='')


class QuotationCreateSerializer(serializers.Serializer):
    customer = serializers.IntegerField()
    store = serializers.IntegerField(required=False, allow_null=True)
    salesperson = serializers.IntegerField(required=False, allow_null=True)
    valid_until = serializers.DateField(required=False, allow_null=True)
    payment_terms = serializers.IntegerField(required=False, allow_null=True)
    delivery_terms = serializers.CharField(required=False, allow_blank=True, default='')
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    lines = QuotationLineInputSerializer(many=True)


class SalesOrderLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    remaining_quantity = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)

    class Meta:
        model = SalesOrderLine
        fields = [
            'id', 'product', 'product_name', 'quotation_line', 'quantity', 'unit_price',
            'discount_amount', 'taxable_amount', 'tax_rate', 'gst_amount', 'line_total',
            'reserved_quantity', 'invoiced_quantity', 'cancelled_quantity', 'remaining_quantity',
        ]


class SalesOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    lines = SalesOrderLineSerializer(many=True, read_only=True)

    class Meta:
        model = SalesOrder
        fields = [
            'id', 'order_no', 'customer', 'customer_name', 'quotation', 'store', 'salesperson',
            'order_date', 'expected_delivery_date', 'payment_terms', 'remarks', 'status',
            'subtotal', 'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount',
            'lines', 'created_at', 'updated_at',
        ]
        read_only_fields = ['order_no', 'status', 'subtotal', 'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount']


class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'product', 'product_name', 'quantity', 'unit_price', 'discount_amount',
            'taxable_amount', 'tax_rate', 'gst_amount', 'line_total',
        ]


class SalesInvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    balance_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    items = SalesInvoiceItemSerializer(many=True, read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_no', 'customer', 'customer_name', 'sales_order', 'quotation',
            'sales_date', 'status', 'payment_status', 'subtotal', 'discount_amount',
            'taxable_amount', 'gst_amount', 'total_amount', 'paid_amount', 'balance_amount',
            'items',
        ]
        read_only_fields = ['invoice_no', 'status', 'payment_status', 'paid_amount']


class ConvertLinesSerializer(serializers.Serializer):
    """Body for a quotation/sales-order convert action: {"lines": {"<line_id>": qty, ...}}."""
    lines = serializers.DictField(child=serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0')))
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)


class ApprovalSerializer(serializers.Serializer):
    approved = serializers.BooleanField(default=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PaymentAllocationSerializer(serializers.ModelSerializer):
    invoice_no = serializers.CharField(source='invoice.invoice_no', read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ['id', 'invoice', 'invoice_no', 'allocated_amount', 'created_at']


class PaymentReceiptSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    allocated_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    unapplied_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = PaymentReceipt
        fields = [
            'id', 'receipt_no', 'customer', 'customer_name', 'store', 'payment_method',
            'bank_account', 'amount', 'receipt_date', 'status', 'reference_no', 'transaction_id',
            'remarks', 'allocated_amount', 'unapplied_amount', 'allocations', 'created_at', 'updated_at',
        ]
        read_only_fields = ['receipt_no', 'status']


class PaymentReceiptCreateSerializer(serializers.Serializer):
    customer = serializers.IntegerField()
    store = serializers.IntegerField(required=False, allow_null=True)
    payment_method = serializers.IntegerField()
    bank_account = serializers.IntegerField(required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    reference_no = serializers.CharField(required=False, allow_blank=True, default='')
    transaction_id = serializers.CharField(required=False, allow_blank=True, default='')
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    allocations = serializers.DictField(child=serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0')), required=False, default=dict)


class AllocateSerializer(serializers.Serializer):
    invoice = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))


# --- Purchase & Payables ---

class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    remaining_quantity = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)
    remaining_to_invoice = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)

    class Meta:
        model = PurchaseOrderLine
        fields = [
            'id', 'product', 'product_name', 'quantity', 'unit_price', 'discount_amount',
            'taxable_amount', 'tax_rate', 'gst_amount', 'line_total', 'received_quantity',
            'invoiced_quantity', 'cancelled_quantity', 'remaining_quantity', 'remaining_to_invoice',
        ]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    lines = PurchaseOrderLineSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'order_no', 'vendor', 'vendor_name', 'warehouse', 'buyer', 'order_date',
            'expected_delivery_date', 'payment_terms', 'remarks', 'status', 'subtotal',
            'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount', 'lines',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['order_no', 'status', 'subtotal', 'discount_amount', 'taxable_amount', 'gst_amount', 'total_amount']


class PurchaseOrderLineInputSerializer(serializers.Serializer):
    product = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'))
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=Decimal('0'), min_value=Decimal('0'))
    discount_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=Decimal('0'), min_value=Decimal('0'))
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=Decimal('18.00'), min_value=Decimal('0'))


class PurchaseOrderCreateSerializer(serializers.Serializer):
    vendor = serializers.IntegerField()
    warehouse = serializers.IntegerField(required=False, allow_null=True)
    buyer = serializers.IntegerField(required=False, allow_null=True)
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)
    payment_terms = serializers.IntegerField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    lines = PurchaseOrderLineInputSerializer(many=True)


class ReceiveGoodsSerializer(serializers.Serializer):
    """Body for the receive action: {"lines": {"<po_line_id>": qty}, "rejected": {"<po_line_id>": qty}}."""
    lines = serializers.DictField(child=serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0')))
    rejected = serializers.DictField(child=serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0')), required=False, default=dict)


class SupplierInvoiceLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = SupplierInvoiceLine
        fields = [
            'id', 'product', 'product_name', 'description', 'quantity', 'unit_cost',
            'discount_amount', 'taxable_amount', 'tax_rate', 'gst_amount', 'line_total',
        ]


class SupplierInvoiceSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='supplier.name', read_only=True)
    outstanding_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    lines = SupplierInvoiceLineSerializer(many=True, read_only=True)

    class Meta:
        model = SupplierInvoice
        fields = [
            'id', 'document_no', 'invoice_no', 'supplier', 'vendor_name', 'purchase_order',
            'goods_receipt', 'workflow_status', 'status', 'invoice_date', 'due_date',
            'gross_amount', 'tax_amount', 'net_amount', 'paid_amount', 'outstanding_amount', 'lines',
        ]
        read_only_fields = ['document_no', 'workflow_status', 'status', 'paid_amount']


class ReceiptInvoiceSerializer(serializers.Serializer):
    """Body for converting a Goods Receipt into a Purchase Invoice."""
    vendor_invoice_no = serializers.CharField()
    vendor_invoice_date = serializers.DateField(required=False, allow_null=True)
    lines = serializers.DictField(child=serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0')))


class VendorPaymentAllocationSerializer(serializers.ModelSerializer):
    invoice_no = serializers.CharField(source='supplier_invoice.invoice_no', read_only=True)

    class Meta:
        model = VendorPaymentAllocation
        fields = ['id', 'supplier_invoice', 'invoice_no', 'allocated_amount', 'created_at']


class VendorPaymentSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    allocated_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    unapplied_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    allocations = VendorPaymentAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = VendorPayment
        fields = [
            'id', 'payment_no', 'vendor', 'vendor_name', 'bank_account', 'payment_method',
            'amount', 'payment_date', 'status', 'reference_no', 'transaction_id', 'remarks',
            'allocated_amount', 'unapplied_amount', 'allocations', 'created_at', 'updated_at',
        ]
        read_only_fields = ['payment_no', 'status']


class VendorPaymentCreateSerializer(serializers.Serializer):
    vendor = serializers.IntegerField()
    bank_account = serializers.IntegerField(required=False, allow_null=True)
    payment_method = serializers.IntegerField(required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    reference_no = serializers.CharField(required=False, allow_blank=True, default='')
    transaction_id = serializers.CharField(required=False, allow_blank=True, default='')
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    allocations = serializers.DictField(child=serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0')), required=False, default=dict)
