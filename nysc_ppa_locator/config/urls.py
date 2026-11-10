from django.contrib import admin
from django.urls import path, include
from django_otp.admin import OTPAdminSite
from . import public_views

# Preserve registered model admins while requiring a verified OTP device.
admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path('privacy/', public_views.privacy, name='privacy'),
    path('terms/', public_views.terms, name='terms'),
    path('help/', public_views.feedback, name='feedback'),
    path('health/', public_views.health, name='health'),
    path('service-worker.js', public_views.service_worker, name='service_worker'),
    path('admin/', admin.site.urls),
    path('budget/', include('budgeting.urls')),
    path('local/', include('local_guides.urls')),
    path('service-resources/', include('service_resources.urls')),
    path('reminders/', include('notifications.urls')),
    path('community-notifications/', include('notifications.community_urls')),
    path('community/', include('community.urls')),
    path('rooms/', include('local_rooms.urls')),
    path('messenger/', include('messenger.urls')),
    path('photos/', include('photos.urls')),
    path('', include('ppas.submission_urls')),
    path('', include('service_year.urls')),
    path('', include('ppas.urls')),       # Root landing page, PPA search, details & location API
    path('', include('accounts.urls')),   # Auth, Dashboard & Profile routes
]
