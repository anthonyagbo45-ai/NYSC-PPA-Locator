from django.urls import path
from . import views

app_name = 'community'
urlpatterns = [
    path('', views.home, name='home'),
    path('questions/add/', views.question_add, name='question_add'),
    path('questions/<int:pk>/', views.question_detail, name='question_detail'),
    path('questions/<int:pk>/edit/', views.question_add, name='question_edit'),
    path('questions/<int:pk>/archive/', views.question_archive, name='question_archive'),
    path('questions/<int:pk>/resolved/', views.question_resolve, name='question_resolve'),
    path('questions/<int:pk>/answer/', views.answer_add, name='answer_add'),
    path('answers/<int:answer_pk>/edit/', views.answer_add, name='answer_edit'),
    path('answers/<int:pk>/archive/', views.answer_archive, name='answer_archive'),
    path('answers/<int:pk>/helpful/', views.vote, name='vote'),
    path('questions/<int:pk>/follow/', views.follow, name='follow'),
    path('questions/<int:pk>/history/', views.history, {'content_type': 'question'}, name='question_history'),
    path('answers/<int:pk>/history/', views.history, {'content_type': 'answer'}, name='answer_history'),
    path('questions/<int:pk>/report/', views.report, {'content_type': 'question'}, name='report_question'),
    path('answers/<int:pk>/report/', views.report, {'content_type': 'answer'}, name='report_answer'),
    path('mine/', views.my_activity, name='my_activity'),
    path('reports/', views.reports, name='reports'),
    path('reports/<int:pk>/history/', views.report_history, name='report_history'),
    path('blocks/', views.blocks, name='blocks'),
    path('blocks/add/', views.block, name='block'),
    path('blocks/<int:pk>/remove/', views.unblock, name='unblock'),
]
