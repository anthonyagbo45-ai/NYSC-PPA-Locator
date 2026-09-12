from django.urls import path
from . import views

urlpatterns = [
    path('', views.landing, name='landing'),
    path('search/', views.search_ppa, name='search'),
    path('search/results/', views.search_results, name='search_results'),
    path('ppa/<int:pk>/', views.ppa_detail, name='ppa_detail'),
    path('api/load-lgas/', views.load_lgas, name='load_lgas'),
]