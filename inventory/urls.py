from django.urls import path

from . import views as v

urlpatterns = [
    path('', v.dashboard, name='inventory_dashboard'),
    path('transfer-dashboard/', v.transfer_dashboard, name='inventory_transfer_dashboard'),

    path('locations/', v.location_list, name='inventory_locations'),
    path('locations/new/', v.location_card, name='inventory_location_new'),
    path('locations/<int:pk>/', v.location_card, name='inventory_location_card'),
    path('setup/<slug:kind>/', v.master_list, name='inventory_setup'),
    path('setup/<slug:kind>/new/', v.master_edit, name='inventory_setup_new'),
    path('setup/<slug:kind>/<int:pk>/', v.master_edit, name='inventory_setup_edit'),

    path('items/', v.item_list, name='inventory_items'),
    path('items/<int:pk>/', v.item_card, name='inventory_item_card'),
    path('skus/', v.sku_list, name='inventory_skus'),
    path('skus/<int:pk>/', v.sku_card, name='inventory_sku_card'),
    path('skus/<int:pk>/stock-card/', v.stock_card, name='inventory_stock_card'),
    path('jewellery-units/', v.unit_list, name='inventory_units'),
    path('bins/', v.bin_list, name='inventory_bins'),

    path('availability/', v.availability, name='inventory_availability'),
    path('trace/', v.trace, name='inventory_trace'),
    path('ledger/', v.ledger, name='inventory_ledger'),
    path('reservations/', v.reservation_list, name='inventory_reservations'),

    path('transfer-requests/', v.request_list, name='inventory_requests'),
    path('transfer-requests/<int:pk>/', v.request_detail, name='inventory_request_detail'),
    path('transfers/', v.transfer_list, name='inventory_transfers'),
    path('transfers/new/', v.transfer_new, name='inventory_transfer_new'),
    path('transfers/<int:pk>/', v.transfer_detail, name='inventory_transfer_detail'),
    path('transit/', v.transit, name='inventory_transit'),

    path('adjustments/', v.adjustment_list, name='inventory_adjustments'),
    path('adjustments/<int:pk>/', v.adjustment_detail, name='inventory_adjustment_detail'),
    path('reclassification/', v.reclass, name='inventory_reclass'),
    path('counts/', v.count_list, name='inventory_counts'),
    path('counts/<int:pk>/', v.count_detail, name='inventory_count_detail'),
    path('replenishment/', v.replenishment, name='inventory_replenishment'),

    path('reports/valuation/', v.valuation, name='inventory_valuation'),
    path('reports/stock-by-location/', v.stock_report, name='inventory_stock_report'),
    path('reports/transfer-register/', v.transfer_register, name='inventory_transfer_register'),

    path('imports/', v.import_center, name='inventory_imports'),
    path('imports/<int:pk>/', v.import_detail, name='inventory_import_detail'),
    path('imports/template/<slug:import_type>/', v.import_template, name='inventory_import_template'),
]
