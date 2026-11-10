from django.urls import path
from django.contrib.auth import views as auth_views
from . import views, recovery

urlpatterns = [
    path('register/', views.register, name='register'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('profile/', views.profile, name='profile'),
    path('account/verify-email/', recovery.email_verification, name='email_verification'),
    path('account/verify-email/<str:token>/', recovery.verify_email, name='verify_email'),
    path('account/password-reset/', recovery.PasswordResetView.as_view(), name='password_reset'),
    path('account/password-reset/sent/', auth_views.PasswordResetDoneView.as_view(template_name='accounts/password_reset_done.html'), name='password_reset_done'),
    path('account/password-reset/<uidb64>/<token>/', recovery.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('account/password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(template_name='accounts/password_reset_complete.html'), name='password_reset_complete'),
    path('account/password-change/', recovery.PasswordChangeView.as_view(), name='password_change'),
    path('account/password-change/done/', auth_views.PasswordChangeDoneView.as_view(template_name='accounts/password_change_done.html'), name='password_change_done'),
    path('account/mfa/setup/', recovery.mfa_setup, name='mfa_setup'),
]
