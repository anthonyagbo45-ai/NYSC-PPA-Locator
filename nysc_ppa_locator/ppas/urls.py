from django.urls import path
from . import views

urlpatterns = [
    path('', views.landing, name='landing'),
    path('portal-entry/', views.portal_entry, name='portal_entry'),
    path('dashboard/', views.dashboard, name='dashboard'),  # <-- Ensure this line is here
    path('search/', views.search_ppa, name='search_ppa'),
    path('results/', views.search_results, name='search_results'),
    path('live-search/', views.dynamic_place_search_view, name='live_search'),
    path('save-live-place/', views.save_live_place_view, name='save_live_place'),
    path('ppas/<int:pk>/', views.ppa_detail, name='ppa_detail'),
    path('api/load-lgas/', views.load_lgas, name='load_lgas'),
]