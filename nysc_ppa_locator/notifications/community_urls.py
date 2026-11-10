from django.urls import path
from . import community_views as views

app_name = 'community_notifications'
urlpatterns = [
    path('', views.inbox, name='inbox'),
    path('check/', views.check_due, name='check_due'),
    path('<int:pk>/read/', views.read, name='read'),
    path('<int:pk>/dismiss/', views.dismiss, name='dismiss'),
]
