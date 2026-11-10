from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import IntegrityError, transaction
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from config.security import account_rate_limit, check_rate_limit
from .forms import RegistrationForm, ProfileForm
from .mail import send_verification
from locations.models import State


@sensitive_post_parameters('password', 'confirm_password')
@require_http_methods(['GET', 'POST'])
@account_rate_limit('register', 10, fields=('state_code', 'email'))
def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegistrationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save(commit=False)
                user.set_password(form.cleaned_data['password'])
                user.save()
        except IntegrityError:
            form.add_error(None, 'Unable to create an account with these details.')
        else:
            login(request, user)
            send_verification(request, user)
            messages.info(request, 'Check your email to verify your account before contributing.')
            return redirect('dashboard')
    return render(request, 'accounts/register.html', {'form': form})


@sensitive_post_parameters('password', 'confirm_password')
@require_http_methods(['GET', 'POST'])
@account_rate_limit('login', 20, fields=('state_code', 'username'))
def user_login(request):
    target = request.POST.get('next', '') if request.method == 'POST' else request.GET.get('next', '')
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse('dashboard')
    if request.user.is_authenticated:
        return redirect(target)
    # AuthenticationForm authenticates USERNAME_FIELD (state_code), checks inactive
    # users and performs a dummy password hash for unknown users.
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and 'state_code' in data:
        data['username'] = data['state_code']
    form = AuthenticationForm(request, data=data)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        return redirect(target)
    return render(request, 'accounts/login.html', {'form': form, 'next': target})


@require_POST
def user_logout(request):
    logout(request)
    return redirect('landing')


@login_required
def dashboard(request):
    from service_year.views import dashboard as service_year_dashboard
    return service_year_dashboard(request)


@sensitive_post_parameters('current_password')
@login_required
@require_http_methods(['GET', 'POST'])
def profile(request):
    if request.method == 'POST':
        submitted_email = request.POST.get('email', '').strip().lower()
        if submitted_email != (request.user.email or '').strip().lower() or request.POST.get('state_code', '').strip() != request.user.state_code:
            limited = check_rate_limit(request, 'profile-email-password', 10)
            if limited is not None:
                return limited
    form = ProfileForm(request.POST if request.method == 'POST' else None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save(commit=False)
                # A profile edit must not restore a stale password hash,
                # privilege flag or unchanged email after a concurrent update.
                changed_fields = {
                    name for name in ProfileForm.Meta.fields
                    if name in form.changed_data
                    and user._meta.get_field(name).value_from_object(user) != form.initial.get(name)
                }
                user.save(update_fields=changed_fields)
        except IntegrityError:
            form.add_error(None, 'Unable to save these profile details.')
        else:
            if 'email' in changed_fields:
                send_verification(request, request.user)
            messages.success(request, 'Profile updated successfully.')
            return redirect('profile')
    return render(request, 'accounts/profile.html', {'form': form, 'states': State.objects.all()})
