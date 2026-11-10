from django.urls import path
from . import views

app_name = 'notifications'
urlpatterns = [
    path('', views.home, name='home'),
    path('events/add/', views.event_add, name='event_add'),
    path('events/<int:pk>/edit/', views.event_edit, name='event_edit'),
    path('events/<int:pk>/delete/', views.event_delete, name='event_delete'),
    path('preferences/', views.preferences, name='preferences'),
    path('inbox/', views.inbox, name='inbox'),
    path('check/', views.check_due, name='check_due'),
    path('inbox/<int:pk>/read/', views.read, name='read'),
    path('inbox/<int:pk>/dismiss/', views.dismiss, name='dismiss'),
    path('notices/<int:notice_id>/follow/', views.subscribe_notice, name='subscribe_notice'),
    path('subscriptions/<int:subscription_id>/stop/', views.unsubscribe_notice, name='unsubscribe_notice'),
]
