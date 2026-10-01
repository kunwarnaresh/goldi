from django.urls import path

from . import api

urlpatterns = [
    path('locations/', api.locations, name='api_retail_locations'),
    path('stores/', api.stores, name='api_retail_stores'),
    path('stores/<int:pk>/', api.store_detail, name='api_retail_store'),
    path('stores/<int:pk>/staff/', api.store_staff, name='api_retail_store_staff'),
    path('stores/<int:pk>/terminals/', api.store_terminals, name='api_retail_store_terminals'),
    path('stores/<int:pk>/tenders/', api.store_tenders, name='api_retail_store_tenders'),
    path('stores/<int:pk>/tender-options/', api.store_tender_options, name='api_retail_store_tender_options'),
    path('staff/', api.staff_list, name='api_retail_staff'),
    path('staff/login/', api.staff_login, name='api_retail_staff_login'),
    path('staff/logout/', api.staff_logout, name='api_retail_staff_logout'),
    path('staff/<int:pk>/', api.staff_detail, name='api_retail_staff_detail'),
    path('pos-terminals/', api.terminals, name='api_retail_terminals'),
    path('pos-terminals/<int:pk>/', api.terminal_detail, name='api_retail_terminal'),
    path('tenders/', api.tenders, name='api_retail_tenders'),
]
