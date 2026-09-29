from django.urls import path

from . import views as v

urlpatterns = [
    path('', v.dashboard, name='manufacturing_dashboard'),
    path('capacity/', v.capacity, name='manufacturing_capacity'),

    path('orders/', v.order_list, name='manufacturing_orders'),
    path('orders/new/', v.order_new, name='manufacturing_order_new'),
    path('orders/<int:pk>/', v.order_detail, name='manufacturing_order_detail'),

    path('boms/', v.bom_list, name='manufacturing_boms'),
    path('boms/<int:pk>/', v.bom_detail, name='manufacturing_bom_detail'),
    path('routings/', v.routing_list, name='manufacturing_routings'),
    path('routings/<int:pk>/', v.routing_detail, name='manufacturing_routing_detail'),
    path('work-centers/', v.work_center_list, name='manufacturing_work_centers'),

    path('subcontracting/', v.subcontract_list, name='manufacturing_subcontracts'),
    path('planning/', v.planning, name='manufacturing_planning'),
]
