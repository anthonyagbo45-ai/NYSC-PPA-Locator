from django.urls import path
from . import views


urlpatterns = [
    path('service-year/', views.dashboard, name='service_year_dashboard'),
    path('service-year/assign/<int:ppa_id>/', views.assign_ppa, name='assign_ppa'),
    path('service-year/history/', views.assignment_history, name='assignment_history'),
    path('service-year/tracker/<int:pk>/', views.assignment_update, name='assignment_update'),
    path('service-year/tracker/<int:pk>/end/', views.end_assignment, name='end_assignment'),
    path('service-year/preferences/', views.service_profile, name='service_profile'),
]
