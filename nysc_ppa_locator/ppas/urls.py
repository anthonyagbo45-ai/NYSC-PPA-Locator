from django.urls import path
from . import views

urlpatterns = [
    path('', views.landing, name='landing'),
    path('portal/entry/', views.portal_entry, name='portal_entry'),
    path('search/', views.search_ppa, name='search'),
    path('search/results/', views.search_results, name='search_results'),
    path('ppa/<int:pk>/', views.ppa_detail, name='ppa_detail'),
    path('api/load-lgas/', views.load_lgas, name='load_lgas'),
    path('live-search/', views.dynamic_place_search_view, name='live_search'),
    path('live-search/save/', views.save_live_place_view, name='save_live_place'),  # <-- Added save route
]