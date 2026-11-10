from django.urls import path
from . import route_views

urlpatterns = [path('api/route/', route_views.route_estimate, name='route_estimate')]
