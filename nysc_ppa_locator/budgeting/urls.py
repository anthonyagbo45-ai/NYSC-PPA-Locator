from django.urls import path

from . import views


app_name = 'budgeting'
urlpatterns = [
    path('', views.home, name='home'),
    path('plan/', views.budget_edit, name='budget_edit'),
    path('plan/delete/', views.budget_delete, name='budget_delete'),
    path('expense/add/', views.expense_add, name='expense_add'),
    path('expense/<int:pk>/edit/', views.expense_edit, name='expense_edit'),
    path('expense/<int:pk>/delete/', views.expense_delete, name='expense_delete'),
]
