from django.urls import path
from . import views

app_name = 'local_rooms'
urlpatterns = [
    path('', views.home, name='home'),
    path('mine/', views.my_rooms, name='my_rooms'),
    path('reports/', views.my_reports, name='my_reports'),
    path('<int:pk>/', views.room_detail, name='room_detail'),
    path('<int:pk>/join/', views.request_join, name='request_join'),
    path('<int:pk>/leave/', views.leave, name='leave'),
    path('<int:pk>/preferences/', views.preferences, name='preferences'),
    path('<int:pk>/posts/add/', views.post_add, name='post_add'),
    path('<int:pk>/report/', views.report_room, name='report_room'),
    path('posts/<int:pk>/edit/', views.post_edit, name='post_edit'),
    path('posts/<int:pk>/archive/', views.post_archive, name='post_archive'),
    path('posts/<int:pk>/history/', views.post_history, name='post_history'),
    path('posts/<int:pk>/report/', views.report_post, name='report_post'),
]
