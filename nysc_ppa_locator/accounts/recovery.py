import base64
import time
import secrets
import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth import views as auth_views
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.http import HttpResponseForbidden
from django.db import transaction
from django.shortcuts import render, redirect
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from config.security import account_rate_limit, rate_limit
from .mail import send_verification
import django_otp
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken


logger = logging.getLogger(__name__)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('email-verification', 3, period=3600)
def email_verification(request):
    if request.method == 'POST':
        send_verification(request, request.user)
        messages.info(request, 'If your account has an unverified email address, a verification message has been requested. Check your inbox, or update your email in your profile.')
        return redirect('email_verification')
    return render(request, 'accounts/email_verification.html')


@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('verify-email', 20)
def verify_email(request, token):
    matching_account = None
    try:
        payload = signing.loads(token, salt='account-email-verification', max_age=settings.EMAIL_VERIFICATION_TIMEOUT)
        matching_account = get_user_model().objects.filter(pk=payload['uid'], email=payload['email'], is_active=True)
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        pass
    if request.method == 'POST':
        # Bind the verified address at the database write, not an earlier read:
        # an email change or account deactivation cannot race this confirmation.
        valid = matching_account is not None and bool(matching_account.update(email_verified=True))
        if valid:
            messages.success(request, 'Email verified. You can now contribute to the directory.')
            return redirect('profile' if request.user.is_authenticated else 'login')
    else:
        valid = matching_account is not None and matching_account.exists()
    return render(request, 'accounts/verify_email.html', {'valid': valid}, status=200 if valid else 400)


class SafePasswordResetForm(PasswordResetForm):
    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        # Keep failures generic without logging token-bearing message content or
        # raw SMTP exceptions; operators can monitor delivery through their provider.
        subject = ''.join(render_to_string(subject_template_name, context).splitlines())
        body = render_to_string(email_template_name, context)
        message = EmailMultiAlternatives(subject, body, from_email, [to_email])
        if html_email_template_name:
            message.attach_alternative(render_to_string(html_email_template_name, context), 'text/html')
        try:
            message.send()
        except Exception:
            logger.warning('Password reset email delivery failed')


@method_decorator(never_cache, name='dispatch')
@method_decorator(account_rate_limit('password-reset', 5, period=3600, fields=('email',)), name='dispatch')
class PasswordResetView(auth_views.PasswordResetView):
    form_class = SafePasswordResetForm
    template_name = 'accounts/password_reset_form.html'
    email_template_name = 'accounts/password_reset_email.txt'
    subject_template_name = 'accounts/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')


@method_decorator(never_cache, name='dispatch')
@method_decorator(rate_limit('password-reset-confirm', 20), name='dispatch')
class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
    success_url = reverse_lazy('password_reset_complete')


@method_decorator(never_cache, name='dispatch')
@method_decorator(rate_limit('password-change', 10), name='dispatch')
class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = 'accounts/password_change.html'
    success_url = reverse_lazy('password_change_done')


@login_required
@never_cache
@sensitive_post_parameters('password', 'token')
@require_http_methods(['GET', 'POST'])
@rate_limit('mfa-enroll', 10)
def mfa_setup(request):
    if not request.user.is_active or not request.user.is_staff:
        return HttpResponseForbidden('Administrator access required.')
    if TOTPDevice.objects.filter(user=request.user, confirmed=True).exists():
        return render(request, 'accounts/mfa_setup.html', {'enabled': True})
    error, device = '', None
    enrollment = request.session.get('mfa_enrollment', {})
    if enrollment.get('expires', 0) > time.time():
        device = TOTPDevice.objects.filter(pk=enrollment.get('device_id'), user=request.user, confirmed=False).first()
    if request.method == 'POST':
        if request.POST.get('step') == 'password':
            if request.user.check_password(request.POST.get('password', '')):
                TOTPDevice.objects.filter(user=request.user, confirmed=False).delete()
                device = TOTPDevice.objects.create(user=request.user, name='Authenticator', confirmed=False)
                request.session['mfa_enrollment'] = {'device_id': device.pk, 'expires': time.time() + 300}
            else:
                error = 'Password incorrect. Please try again.'
        elif request.POST.get('step') == 'verify':
            if device is None:
                error = 'Setup expired. Confirm your password to start again.'
            else:
                with transaction.atomic():
                    device = TOTPDevice.objects.select_for_update().get(pk=device.pk, user=request.user)
                    if device.verify_token(request.POST.get('token', '')):
                        device.confirmed = True
                        device.save(update_fields=['confirmed'])
                        django_otp.login(request, device)
                        request.session.pop('mfa_enrollment', None)
                        recovery = StaticDevice.objects.create(user=request.user, name='Recovery codes', confirmed=True)
                        codes = [secrets.token_hex(8) for _ in range(8)]
                        StaticToken.objects.bulk_create([StaticToken(device=recovery, token=code) for code in codes])
                        return render(request, 'accounts/mfa_setup.html', {'enabled': True, 'recovery_codes': codes})
                error = 'Code invalid or recently used. Wait for a fresh authenticator code and try again.'
    return render(request, 'accounts/mfa_setup.html', {'error': error, 'setup_key': base64.b32encode(device.bin_key).decode() if device else '', 'setup_url': device.config_url if device else ''})
