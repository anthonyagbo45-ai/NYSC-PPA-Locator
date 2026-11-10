from functools import wraps
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect


def verified_email_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.email_verified:
            messages.info(request, 'Verify your email before contributing to the shared directory.')
            return redirect('email_verification')
        return view(request, *args, **kwargs)
    return wrapped
