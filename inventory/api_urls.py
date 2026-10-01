from django.urls import path

from . import api_views as v

urlpatterns = [
    path('locations/', v.location_list, name='api_location_list'),
    path('locations/<int:pk>/', v.location_detail, name='api_location_detail'),
    path('skus/', v.sku_list, name='api_sku_list'),
    path('skus/<int:pk>/', v.sku_detail, name='api_sku_detail'),
    path('items/<int:pk>/availability/', v.item_availability, name='api_item_availability'),
    path('jewellery-units/lookup/', v.unit_lookup, name='api_unit_lookup'),

    path('inventory/', v.inventory_balances, name='api_inventory_balances'),
    path('inventory/availability/', v.inventory_availability, name='api_inventory_availability'),
    path('inventory/transit/', v.inventory_transit, name='api_inventory_transit'),
    path('inventory/adjustments/', v.adjustment_create, name='api_inventory_adjustments'),
    path('inventory/adjustments/<int:pk>/<slug:action>/', v.adjustment_action, name='api_inventory_adjustment_action'),
    path('inventory/counts/', v.count_create, name='api_inventory_counts'),
    path('inventory/counts/<int:pk>/<slug:action>/', v.count_action, name='api_inventory_count_action'),
    path('inventory/reclassification/', v.reclassification_create, name='api_inventory_reclassification'),
    path('inventory/pos-sale/', v.pos_sale, name='api_inventory_pos_sale'),

    path('transfer-requests/', v.transfer_request_list, name='api_transfer_requests'),
    path('transfer-orders/', v.transfer_order_list, name='api_transfer_orders'),
    path('transfer-orders/<int:pk>/', v.transfer_order_detail, name='api_transfer_order_detail'),
    path('transfer-orders/<int:pk>/approve/', v.transfer_order_approve, name='api_transfer_order_approve'),
    path('transfer-orders/<int:pk>/ship/', v.transfer_order_ship, name='api_transfer_order_ship'),
    path('transfer-orders/<int:pk>/receive/', v.transfer_order_receive, name='api_transfer_order_receive'),
]
