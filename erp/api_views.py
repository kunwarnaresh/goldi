from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    BankAccount, Customer, JewelleryItemUnit, PaymentMethod, PaymentTerm,
    POSStaff, Product, Quotation, SalesInvoice, SalesOrder, Store, PaymentReceipt,
    GoodsReceipt, PurchaseOrder, Staff, Supplier, SupplierInvoice, VendorPayment, Warehouse,
)
from .serializers import (
    AllocateSerializer, ApprovalSerializer, ConvertLinesSerializer, PaymentReceiptCreateSerializer,
    PaymentReceiptSerializer, QuotationCreateSerializer, QuotationSerializer,
    SalesInvoiceSerializer, SalesOrderSerializer,
    PurchaseOrderCreateSerializer, PurchaseOrderSerializer, ReceiptInvoiceSerializer,
    ReceiveGoodsSerializer, SupplierInvoiceSerializer, VendorPaymentCreateSerializer, VendorPaymentSerializer,
)
from .services import (
    add_payment_allocation, approve_purchase_order, approve_quotation, approve_sales_order,
    convert_quotation_to_sales_order, convert_receipt_to_purchase_invoice, convert_sales_order_to_invoice,
    create_payment_receipt, create_purchase_order, create_quotation, create_vendor_payment,
    post_payment_receipt, post_purchase_invoice, post_sales_invoice, post_vendor_payment, receive_goods,
)


def _error(exc):
    return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


# --- Quotations ---

@api_view(['GET', 'POST'])
def quotation_list(request):
    if request.method == 'GET':
        quotations = Quotation.objects.select_related('customer').order_by('-quotation_date')
        return Response(QuotationSerializer(quotations, many=True).data)

    payload = QuotationCreateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    lines_data = []
    for line in data['lines']:
        jewellery_unit = None
        if line.get('jewellery_barcode'):
            jewellery_unit = JewelleryItemUnit.objects.filter(barcode=line['jewellery_barcode'], current_status='available').first()
        lines_data.append({
            'product': get_object_or_404(Product, pk=line['product']), 'quantity': line['quantity'],
            'discount_amount': line.get('discount_amount') or Decimal('0'), 'jewellery_unit': jewellery_unit,
        })
    try:
        quotation = create_quotation(
            customer=get_object_or_404(Customer, pk=data['customer']),
            store=get_object_or_404(Store, pk=data['store']) if data.get('store') else None,
            salesperson=get_object_or_404(POSStaff, pk=data['salesperson']) if data.get('salesperson') else None,
            lines_data=lines_data, valid_until=data.get('valid_until'),
            payment_terms=get_object_or_404(PaymentTerm, pk=data['payment_terms']) if data.get('payment_terms') else None,
            delivery_terms=data.get('delivery_terms', ''), remarks=data.get('remarks', ''), user=request.user,
        )
    except ValueError as exc:
        return _error(exc)
    return Response(QuotationSerializer(quotation).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def quotation_detail(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    return Response(QuotationSerializer(quotation).data)


@api_view(['POST'])
def quotation_approve(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    payload = ApprovalSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        approve_quotation(quotation, request.user, approved=payload.validated_data['approved'], notes=payload.validated_data.get('notes', ''))
    except ValueError as exc:
        return _error(exc)
    return Response(QuotationSerializer(quotation).data)


@api_view(['POST'])
def quotation_convert(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    payload = ConvertLinesSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        sales_order = convert_quotation_to_sales_order(
            quotation, request.user, payload.validated_data['lines'],
            expected_delivery_date=payload.validated_data.get('expected_delivery_date'),
        )
    except ValueError as exc:
        return _error(exc)
    return Response(SalesOrderSerializer(sales_order).data, status=status.HTTP_201_CREATED)


# --- Sales Orders ---

@api_view(['GET'])
def sales_order_list(request):
    orders = SalesOrder.objects.select_related('customer').order_by('-order_date')
    return Response(SalesOrderSerializer(orders, many=True).data)


@api_view(['GET'])
def sales_order_detail(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    return Response(SalesOrderSerializer(order).data)


@api_view(['POST'])
def sales_order_approve(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    payload = ApprovalSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        order, warning = approve_sales_order(order, request.user, approved=payload.validated_data['approved'], notes=payload.validated_data.get('notes', ''))
    except ValueError as exc:
        return _error(exc)
    data = SalesOrderSerializer(order).data
    data['warning'] = warning
    return Response(data)


@api_view(['POST'])
def sales_order_convert(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    payload = ConvertLinesSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        invoice = convert_sales_order_to_invoice(order, request.user, payload.validated_data['lines'])
    except ValueError as exc:
        return _error(exc)
    return Response(SalesInvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


# --- Invoices ---

@api_view(['GET'])
def invoice_list(request):
    invoices = SalesInvoice.objects.select_related('customer').order_by('-sales_date')
    return Response(SalesInvoiceSerializer(invoices, many=True).data)


@api_view(['GET'])
def invoice_detail(request, pk):
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    return Response(SalesInvoiceSerializer(invoice).data)


@api_view(['POST'])
def invoice_post(request, pk):
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    try:
        post_sales_invoice(invoice, request.user)
    except ValueError as exc:
        return _error(exc)
    return Response(SalesInvoiceSerializer(invoice).data)


# --- Payment Receipts ---

@api_view(['GET', 'POST'])
def payment_receipt_list(request):
    if request.method == 'GET':
        receipts = PaymentReceipt.objects.select_related('customer').order_by('-receipt_date')
        return Response(PaymentReceiptSerializer(receipts, many=True).data)

    payload = PaymentReceiptCreateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    try:
        receipt = create_payment_receipt(
            customer=get_object_or_404(Customer, pk=data['customer']),
            store=get_object_or_404(Store, pk=data['store']) if data.get('store') else None,
            payment_method=get_object_or_404(PaymentMethod, pk=data['payment_method']),
            bank_account=get_object_or_404(BankAccount, pk=data['bank_account']) if data.get('bank_account') else None,
            amount=data['amount'], allocations=data.get('allocations') or {}, user=request.user,
            reference_no=data.get('reference_no', ''), transaction_id=data.get('transaction_id', ''),
            remarks=data.get('remarks', ''),
        )
    except ValueError as exc:
        return _error(exc)
    return Response(PaymentReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def payment_receipt_detail(request, pk):
    receipt = get_object_or_404(PaymentReceipt, pk=pk)
    return Response(PaymentReceiptSerializer(receipt).data)


@api_view(['POST'])
def payment_receipt_allocate(request, pk):
    receipt = get_object_or_404(PaymentReceipt, pk=pk)
    payload = AllocateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    invoice = get_object_or_404(SalesInvoice, pk=payload.validated_data['invoice'])
    try:
        add_payment_allocation(receipt, invoice, payload.validated_data['amount'], request.user)
    except ValueError as exc:
        return _error(exc)
    return Response(PaymentReceiptSerializer(receipt).data)


@api_view(['POST'])
def payment_receipt_post(request, pk):
    receipt = get_object_or_404(PaymentReceipt, pk=pk)
    try:
        post_payment_receipt(receipt, request.user)
    except ValueError as exc:
        return _error(exc)
    return Response(PaymentReceiptSerializer(receipt).data)


# --- Purchase & Payables ---

@api_view(['GET', 'POST'])
def purchase_order_list(request):
    if request.method == 'GET':
        orders = PurchaseOrder.objects.select_related('vendor').order_by('-order_date')
        return Response(PurchaseOrderSerializer(orders, many=True).data)

    payload = PurchaseOrderCreateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    lines_data = [
        {
            'product': get_object_or_404(Product, pk=line['product']), 'quantity': line['quantity'],
            'unit_price': line.get('unit_price') or Decimal('0'), 'discount_amount': line.get('discount_amount') or Decimal('0'),
            'tax_rate': line.get('tax_rate') or Decimal('18.00'),
        }
        for line in data['lines']
    ]
    try:
        po = create_purchase_order(
            vendor=get_object_or_404(Supplier, pk=data['vendor']),
            warehouse=get_object_or_404(Warehouse, pk=data['warehouse']) if data.get('warehouse') else None,
            buyer=get_object_or_404(Staff, pk=data['buyer']) if data.get('buyer') else None,
            lines_data=lines_data, expected_delivery_date=data.get('expected_delivery_date'),
            payment_terms=get_object_or_404(PaymentTerm, pk=data['payment_terms']) if data.get('payment_terms') else None,
            remarks=data.get('remarks', ''), user=request.user,
        )
    except ValueError as exc:
        return _error(exc)
    return Response(PurchaseOrderSerializer(po).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def purchase_order_detail(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    return Response(PurchaseOrderSerializer(po).data)


@api_view(['POST'])
def purchase_order_approve(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    payload = ApprovalSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        approve_purchase_order(po, request.user, approved=payload.validated_data['approved'], notes=payload.validated_data.get('notes', ''))
    except ValueError as exc:
        return _error(exc)
    return Response(PurchaseOrderSerializer(po).data)


@api_view(['POST'])
def purchase_order_receive(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    payload = ReceiveGoodsSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    try:
        receipt = receive_goods(
            po, request.user, payload.validated_data['lines'],
            rejected_quantities=payload.validated_data.get('rejected') or {},
        )
    except ValueError as exc:
        return _error(exc)
    return Response({'receipt_no': receipt.receipt_no, 'purchase_order': PurchaseOrderSerializer(po).data}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def purchase_invoice_from_receipt(request, receipt_pk):
    receipt = get_object_or_404(GoodsReceipt, pk=receipt_pk)
    payload = ReceiptInvoiceSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    try:
        invoice = convert_receipt_to_purchase_invoice(
            receipt, request.user, vendor_invoice_no=data['vendor_invoice_no'],
            vendor_invoice_date=data.get('vendor_invoice_date'), line_quantities=data['lines'],
        )
    except ValueError as exc:
        return _error(exc)
    return Response(SupplierInvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def purchase_invoice_list(request):
    invoices = SupplierInvoice.objects.select_related('supplier').order_by('-invoice_date')
    return Response(SupplierInvoiceSerializer(invoices, many=True).data)


@api_view(['GET'])
def purchase_invoice_detail(request, pk):
    invoice = get_object_or_404(SupplierInvoice, pk=pk)
    return Response(SupplierInvoiceSerializer(invoice).data)


@api_view(['POST'])
def purchase_invoice_post(request, pk):
    invoice = get_object_or_404(SupplierInvoice, pk=pk)
    try:
        post_purchase_invoice(invoice, request.user)
    except ValueError as exc:
        return _error(exc)
    return Response(SupplierInvoiceSerializer(invoice).data)


@api_view(['GET', 'POST'])
def vendor_payment_list(request):
    if request.method == 'GET':
        payments = VendorPayment.objects.select_related('vendor').order_by('-payment_date')
        return Response(VendorPaymentSerializer(payments, many=True).data)

    payload = VendorPaymentCreateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    try:
        payment = create_vendor_payment(
            vendor=get_object_or_404(Supplier, pk=data['vendor']),
            bank_account=get_object_or_404(BankAccount, pk=data['bank_account']) if data.get('bank_account') else None,
            payment_method=get_object_or_404(PaymentMethod, pk=data['payment_method']) if data.get('payment_method') else None,
            amount=data['amount'], allocations=data.get('allocations') or {}, user=request.user,
            reference_no=data.get('reference_no', ''), transaction_id=data.get('transaction_id', ''),
            remarks=data.get('remarks', ''),
        )
    except ValueError as exc:
        return _error(exc)
    return Response(VendorPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def vendor_payment_detail(request, pk):
    payment = get_object_or_404(VendorPayment, pk=pk)
    return Response(VendorPaymentSerializer(payment).data)


@api_view(['POST'])
def vendor_payment_post(request, pk):
    payment = get_object_or_404(VendorPayment, pk=pk)
    try:
        post_vendor_payment(payment, request.user)
    except ValueError as exc:
        return _error(exc)
    return Response(VendorPaymentSerializer(payment).data)
