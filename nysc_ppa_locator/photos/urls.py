from django.urls import path

from . import views


app_name = 'photos'
urlpatterns = [
    path('conversations/<uuid:conversation_id>/add/', views.upload, name='upload'),
    path('reports/<int:report_id>/content/', views.reported_content, name='reported_content'),
    path('<uuid:photo_id>/content/', views.content, name='content'),
]
