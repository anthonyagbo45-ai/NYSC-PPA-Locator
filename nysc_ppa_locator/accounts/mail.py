import logging
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def verification_token(user):
    return signing.dumps({'uid': user.pk, 'email': user.email}, salt='account-email-verification', compress=True)


def send_verification(request, user):
    if not user.email or user.email_verified:
        return False
    link = request.build_absolute_uri(reverse('verify_email', kwargs={'token': verification_token(user)}))
    try:
        send_mail('Verify your NYSC PPA Locator email',
                  f'Confirm your email to contribute to the PPA directory. Open this link and confirm within 24 hours:\n\n{link}\n\nIf you did not request this, ignore this message.',
                  settings.DEFAULT_FROM_EMAIL, [user.email])
    except Exception:
        # Avoid logging full email content, verification tokens or SMTP credentials.
        logger.warning('Account verification email delivery failed')
        return False
    return True
