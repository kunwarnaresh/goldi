from django.urls import path

from . import views

app_name = 'analytics'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('reports/', views.explorer, name='explorer'),
    path('reports/export/<str:format>/', views.export_report, name='export'),
    path('views/save/', views.save_view, name='save_view'),
    path('views/<int:pk>/delete/', views.delete_view, name='delete_view'),
    path('api/datasets/', views.datasets_api, name='datasets_api'),
    path('api/query/', views.query_api, name='query_api'),
]
