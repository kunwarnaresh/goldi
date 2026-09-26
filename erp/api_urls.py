from django.urls import path

from . import api_views

urlpatterns = [
    path('quotations/', api_views.quotation_list, name='api_quotation_list'),
    path('quotations/<int:pk>/', api_views.quotation_detail, name='api_quotation_detail'),
    path('quotations/<int:pk>/approve/', api_views.quotation_approve, name='api_quotation_approve'),
    path('quotations/<int:pk>/convert/', api_views.quotation_convert, name='api_quotation_convert'),

    path('sales-orders/', api_views.sales_order_list, name='api_sales_order_list'),
    path('sales-orders/<int:pk>/', api_views.sales_order_detail, name='api_sales_order_detail'),
    path('sales-orders/<int:pk>/approve/', api_views.sales_order_approve, name='api_sales_order_approve'),
    path('sales-orders/<int:pk>/convert/', api_views.sales_order_convert, name='api_sales_order_convert'),

    path('invoices/', api_views.invoice_list, name='api_invoice_list'),
    path('invoices/<int:pk>/', api_views.invoice_detail, name='api_invoice_detail'),
    path('invoices/<int:pk>/post/', api_views.invoice_post, name='api_invoice_post'),

    path('payment-receipts/', api_views.payment_receipt_list, name='api_payment_receipt_list'),
    path('payment-receipts/<int:pk>/', api_views.payment_receipt_detail, name='api_payment_receipt_detail'),
    path('payment-receipts/<int:pk>/allocate/', api_views.payment_receipt_allocate, name='api_payment_receipt_allocate'),
    path('payment-receipts/<int:pk>/post/', api_views.payment_receipt_post, name='api_payment_receipt_post'),

    path('purchase-orders/', api_views.purchase_order_list, name='api_purchase_order_list'),
    path('purchase-orders/<int:pk>/', api_views.purchase_order_detail, name='api_purchase_order_detail'),
    path('purchase-orders/<int:pk>/approve/', api_views.purchase_order_approve, name='api_purchase_order_approve'),
    path('purchase-orders/<int:pk>/receive/', api_views.purchase_order_receive, name='api_purchase_order_receive'),

    path('goods-receipts/<int:receipt_pk>/invoice/', api_views.purchase_invoice_from_receipt, name='api_purchase_invoice_from_receipt'),

    path('purchase-invoices/', api_views.purchase_invoice_list, name='api_purchase_invoice_list'),
    path('purchase-invoices/<int:pk>/', api_views.purchase_invoice_detail, name='api_purchase_invoice_detail'),
    path('purchase-invoices/<int:pk>/post/', api_views.purchase_invoice_post, name='api_purchase_invoice_post'),

    path('vendor-payments/', api_views.vendor_payment_list, name='api_vendor_payment_list'),
    path('vendor-payments/<int:pk>/', api_views.vendor_payment_detail, name='api_vendor_payment_detail'),
    path('vendor-payments/<int:pk>/post/', api_views.vendor_payment_post, name='api_vendor_payment_post'),
]
