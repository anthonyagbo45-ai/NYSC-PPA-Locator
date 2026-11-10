from django.urls import path
from . import community_views as views

urlpatterns = [
    path('ppas/<int:pk>/confirm/', views.confirm_ppa, name='confirm_ppa'),
    path('ppas/<int:pk>/report/', views.report_ppa, name='report_ppa'),
    path('ppas/<int:pk>/bookmark/', views.bookmark_ppa, name='bookmark_ppa'),
    path('my-reports/', views.my_reports, name='my_reports'),
    path('saved/', views.saved_ppas, name='saved_ppas'),
]
