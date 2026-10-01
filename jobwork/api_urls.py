from django.urls import path

from . import api_views as v

urlpatterns = [
    path('job-workers/', v.job_workers, name='api_job_workers'),
    path('job-workers/<int:pk>/stock/', v.job_worker_stock, name='api_job_worker_stock'),
    path('job-workers/<int:pk>/ledger/', v.job_worker_ledger, name='api_job_worker_ledger'),
    path('job-work-orders/', v.orders, name='api_job_work_orders'),
    path('job-work-orders/<int:pk>/', v.order_detail, name='api_job_work_order_detail'),
    path('job-work-orders/<int:pk>/<slug:action>/', v.order_action, name='api_job_work_order_action'),
    path('job-work/itc04/', v.itc04, name='api_job_work_itc04'),
    path('job-work/compliance/', v.compliance_status, name='api_job_work_compliance'),
    path('job-work/weight-captures/', v.weight_capture, name='api_job_work_weight_capture'),
]
