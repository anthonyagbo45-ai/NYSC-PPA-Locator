from django.urls import path
from . import views

app_name = 'service_resources'
urlpatterns = [
    path('', views.home, name='home'),
    path('cds/', views.cds, name='cds'),
    path('clearance/', views.clearance, name='clearance'),
    path('my-contributions/', views.mine, name='mine'),
    path('my-reports/', views.reports, name='reports'),
    path('my-cds-group/', views.select_group, name='select_group'),
    path('groups/add/', views.create_group, name='create_group'),
    path('groups/<int:pk>/', views.group_detail, name='group_detail'),
    path('groups/<int:pk>/edit/', views.edit_group, name='edit_group'),
    path('groups/<int:pk>/history/', views.group_history, name='group_history'),
    path('groups/<int:pk>/withdraw/', views.archive_group, name='archive_group'),
    path('groups/<int:pk>/report/', views.report_group, name='report_group'),
    path('notices/add/', views.create_notice, name='create_notice'),
    path('notices/<int:pk>/', views.notice_detail, name='notice_detail'),
    path('notices/<int:pk>/edit/', views.edit_notice, name='edit_notice'),
    path('notices/<int:pk>/history/', views.notice_history, name='notice_history'),
    path('notices/<int:pk>/withdraw/', views.archive_notice, name='archive_notice'),
    path('notices/<int:pk>/report/', views.report_notice, name='report_notice'),
]
