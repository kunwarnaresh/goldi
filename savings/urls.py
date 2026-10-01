from django.urls import path

from . import views as v

urlpatterns = [
    path('', v.dashboard, name='savings_dashboard'),

    path('members/', v.member_list, name='savings_members'),
    path('members/new/', v.member_new, name='savings_member_new'),
    path('members/<int:pk>/', v.member_detail, name='savings_member_detail'),
    path('customers/<int:pk>/', v.customer_detail, name='savings_customer_detail'),

    path('reports/enrolment/', v.report_enrollment, name='savings_report_enrollment'),
    path('reports/pending/', v.report_pending, name='savings_report_pending'),
    path('reports/collection/', v.report_collection, name='savings_report_collection'),

    path('schemes/', v.scheme_list, name='savings_schemes'),
    path('schemes/<int:pk>/', v.scheme_detail, name='savings_scheme_detail'),
    path('setup/', v.setup_view, name='savings_setup'),
]
