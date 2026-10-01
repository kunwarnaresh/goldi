from django.urls import path

from . import views as v

urlpatterns = [
    path('', v.home, name='retail_home'),
    path('locations/', v.location_list, name='retail_locations'),
    path('locations/new/', v.location_card, name='retail_location_new'),
    path('locations/<int:pk>/', v.location_card, name='retail_location_card'),

    path('stores/', v.store_list, name='retail_stores'),
    path('stores/new/', v.store_edit, name='retail_store_new'),
    path('stores/<int:pk>/', v.store_card, name='retail_store_card'),
    path('stores/<int:pk>/edit/', v.store_edit, name='retail_store_edit'),
    path('stores/<int:pk>/add-staff/', v.store_add_staff, name='retail_store_add_staff'),
    path('stores/<int:pk>/add-terminal/', v.store_add_terminal, name='retail_store_add_terminal'),
    path('stores/<int:pk>/add-tender/', v.store_add_tender, name='retail_store_add_tender'),

    path('staff/', v.staff_list, name='retail_staff'),
    path('staff/new/', v.staff_new, name='retail_staff_new'),
    path('staff/<int:pk>/', v.staff_card, name='retail_staff_card'),
    path('staff/<int:pk>/edit/', v.staff_edit, name='retail_staff_edit'),
    path('staff/<int:pk>/reset-password/', v.staff_reset_password, name='retail_staff_reset_password'),
    path('staff/<int:staff_pk>/terminal-access/new/', v.assignment_edit, name='retail_assignment_new'),
    path('staff-terminal-access/', v.assignment_list, name='retail_assignments'),
    path('staff-terminal-access/<int:pk>/', v.assignment_edit, name='retail_assignment_edit'),

    path('terminals/', v.terminal_list, name='retail_terminals'),
    path('terminals/new/', v.terminal_new, name='retail_terminal_new'),
    path('terminals/<int:pk>/', v.terminal_card, name='retail_terminal_card'),
    path('terminals/<int:pk>/edit/', v.terminal_edit, name='retail_terminal_edit'),

    path('tenders/', v.tender_list, name='retail_tenders'),
    path('tenders/new/', v.tender_card, name='retail_tender_new'),
    path('tenders/<int:pk>/', v.tender_card, name='retail_tender_card'),
    path('store-tenders/', v.store_tender_list, name='retail_store_tenders'),
    path('store-tenders/<int:pk>/', v.store_tender_edit, name='retail_store_tender_edit'),

    path('roles/', v.role_list, name='retail_roles'),
    path('roles/new/', v.role_card, name='retail_role_new'),
    path('roles/<int:pk>/', v.role_card, name='retail_role_card'),

    path('sessions/', v.session_list, name='retail_sessions'),
    path('shifts/', v.shift_list, name='retail_shifts'),
    path('shifts/<int:pk>/', v.shift_detail, name='retail_shift_detail'),

    path('imports/', v.import_center, name='retail_imports'),
    path('imports/<int:pk>/', v.import_detail, name='retail_import_detail'),
    path('imports/template/<slug:kind>/', v.import_template, name='retail_import_template'),
    path('export/<slug:kind>/', v.export, name='retail_export'),
    path('audit/', v.audit_list, name='retail_audit'),
]
