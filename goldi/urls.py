"""
URL configuration for goldi project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/retail/', include('erp.retail.api_urls')),
    path('analytics/', include('analytics.urls')),
    path('api/', include('erp.api_urls')),
    path('api/', include('inventory.api_urls')),
    path('api/', include('jobwork.api_urls')),
    path('retail/', include('erp.retail.urls')),
    path('inventory/', include('inventory.urls')),
    path('manufacturing/', include('manufacturing.urls')),
    path('job-work/', include('jobwork.urls')),
    path('savings/', include('savings.urls')),
    path('', include('erp.urls')),
]
