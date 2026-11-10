from django.urls import path
from . import views

app_name = 'local_guides'
urlpatterns = [
    path('', views.home, name='home'),
    path('saved/', views.saved, name='saved'),
    path('contributions/', views.mine, name='mine'),
    path('reports/', views.reports, name='reports'),
    path('add/<str:kind>/', views.create, name='create'),
    path('<int:pk>/', views.detail, name='detail'),
    path('<int:pk>/edit/', views.edit, name='edit'),
    path('<int:pk>/history/', views.history, name='history'),
    path('<int:pk>/withdraw/', views.archive, name='archive'),
    path('<int:pk>/reconfirm/', views.reconfirm, name='reconfirm'),
    path('<int:pk>/report/', views.report, name='report'),
    path('<int:pk>/save/', views.bookmark, name='bookmark'),
]
