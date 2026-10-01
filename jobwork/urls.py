from django.urls import path

from . import views as v

urlpatterns = [
    path('', v.dashboard, name='jobwork_dashboard'),
    path('board/', v.board, name='jobwork_board'),
    path('material/', v.material, name='jobwork_material'),
    path('exceptions/', v.exceptions, name='jobwork_exceptions'),

    path('orders/', v.order_list, name='jobwork_orders'),
    path('orders/new/', v.order_new, name='jobwork_order_new'),
    path('orders/<int:pk>/', v.order_detail, name='jobwork_order_detail'),
    path('dispatches/', v.dispatch_list, name='jobwork_dispatches'),
    path('challans/<int:pk>/', v.challan, name='jobwork_challan'),
    path('invoices/', v.invoice_list, name='jobwork_invoices'),

    path('job-workers/', v.worker_list, name='jobwork_workers'),
    path('job-workers/<int:pk>/', v.worker_detail, name='jobwork_worker_detail'),
    path('prices/', v.price_list, name='jobwork_prices'),
    path('tax-rates/', v.tax_rates, name='jobwork_tax_rates'),
    path('rules/', v.rules, name='jobwork_rules'),

    path('worksheet/', v.worksheet, name='jobwork_worksheet'),
    path('register/', v.register, name='jobwork_register'),
    path('compliance/', v.compliance_view, name='jobwork_compliance'),
    path('itc04/', v.itc04, name='jobwork_itc04'),
    path('trace/', v.trace, name='jobwork_trace'),
]
