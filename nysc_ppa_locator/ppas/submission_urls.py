from django.urls import path
from . import submission_views as views

app_name = 'ppa_submissions'
urlpatterns = [
    path('contribute/ppa/', views.overview, name='overview'),
    path('contribute/ppa/new/', views.submit, name='submit'),
    path('my-ppa-proposals/', views.mine, name='mine'),
    path('my-ppa-proposals/<int:pk>/edit/', views.submit, name='edit'),
    path('my-ppa-proposals/<int:pk>/history/', views.history, name='history'),
    path('my-ppa-proposals/<int:pk>/withdraw/', views.withdraw, name='withdraw'),
]
