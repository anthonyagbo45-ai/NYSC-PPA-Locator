from django.urls import path

from . import views


app_name = 'messenger'
urlpatterns = [
    path('', views.inbox, name='inbox'),
    path('preferences/', views.preferences, name='preferences'),
    path('requests/question/<int:context_id>/', views.request_chat, {'context_type': 'question'}, name='request_question'),
    path('requests/answer/<int:context_id>/', views.request_chat, {'context_type': 'answer'}, name='request_answer'),
    path('<uuid:conversation_id>/', views.thread, name='thread'),
    path('<uuid:conversation_id>/respond/', views.respond, name='respond'),
    path('<uuid:conversation_id>/send/', views.send, name='send'),
    path('<uuid:conversation_id>/read/', views.read, name='read'),
    path('<uuid:conversation_id>/close/', views.close, name='close'),
    path('<uuid:conversation_id>/block/', views.block, name='block'),
    path('<uuid:conversation_id>/messages/<int:message_id>/report/', views.report, name='report'),
]
