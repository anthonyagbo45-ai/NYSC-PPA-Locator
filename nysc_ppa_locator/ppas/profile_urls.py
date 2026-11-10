from django.urls import path
from . import profile_views as views

app_name = 'ppa_profiles'
urlpatterns = [
    path('ppas/<int:pk>/context/add/', views.submit_claim, name='submit_claim'),
    path('ppas/<int:pk>/context/<int:claim_id>/edit/', views.submit_claim, name='edit_claim'),
    path('my-contributions/', views.my_claims, name='my_claims'),
    path('my-contributions/<int:claim_id>/archive/', views.archive_claim, name='archive_claim'),
    path('reviews/<int:review_id>/history/', views.review_history, name='review_history'),
]
