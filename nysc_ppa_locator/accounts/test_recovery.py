import re
import time
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from .mail import verification_token

User = get_user_model()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class RecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(state_code='LA/25A/1222', email='member@example.com', full_name='Private Full Name', password='Previous-Strong-Pass-123!')

    def setUp(self):
        cache.clear()

    def test_public_name_never_falls_back_to_private_identity(self):
        self.assertEqual(self.user.public_name, 'Corps member')
        self.user.display_name = 'Public Alias'
        self.assertEqual(self.user.public_name, 'Public Alias')

    def test_login_preserves_safe_destination_and_rejects_external_redirects(self):
        destination = '/ppas/1/report/'
        response = self.client.get('/login/', {'next': destination})
        self.assertContains(response, 'value="/ppas/1/report/"')
        response = self.client.post('/login/', {'username': self.user.state_code, 'password': 'Previous-Strong-Pass-123!', 'next': destination})
        self.assertEqual(response.url, destination)
        self.client.logout()
        for unsafe in ('https://attacker.invalid/', '//attacker.invalid/', 'javascript:alert(1)'):
            response = self.client.post('/login/', {'username': self.user.state_code, 'password': 'Previous-Strong-Pass-123!', 'next': unsafe})
            self.assertEqual(response.url, '/dashboard/')
            self.client.logout()

    def test_account_forms_use_local_scripts_and_preserve_invalid_posting_selection(self):
        response = self.client.get('/register/')
        self.assertContains(response, 'name="display_name"')
        self.assertContains(response, '/static/locator/accounts.js')
        self.assertNotContains(response, 'onclick=')
        self.assertNotContains(response, '<script>')
        from locations.models import State, LGA
        state = State.objects.create(name='Lagos')
        lga = LGA.objects.create(state=state, name='Ikeja')
        response = self.client.post('/register/', {'state_posted': state.pk, 'lga_posted': lga.pk})
        self.assertContains(response, f'value="{lga.pk}" selected')
        self.client.force_login(self.user)
        response = self.client.get('/profile/')
        self.assertContains(response, 'name="current_password"')
        self.assertNotContains(response, 'onclick=')
        self.assertNotContains(response, '<script>')

    def test_new_registration_requires_email(self):
        from .forms import RegistrationForm
        form = RegistrationForm({'full_name': 'New Person', 'state_code': 'LA/25A/9999', 'password': 'Strong-New-Password-456!', 'confirm_password': 'Strong-New-Password-456!'})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_verification_email_and_csrf_confirmation(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('email_verification'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/account/verify-email/', mail.outbox[0].body)
        url = reverse('verify_email', kwargs={'token': verification_token(self.user)})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)

    def test_verification_token_expiry_and_email_binding(self):
        now = time.time()
        with patch('django.core.signing.time.time', return_value=now - 86401):
            expired = verification_token(self.user)
        self.assertEqual(self.client.get(reverse('verify_email', kwargs={'token': expired})).status_code, 400)
        token = verification_token(self.user)
        self.user.email = 'changed@example.com'
        self.user.save(update_fields=['email'])
        self.assertEqual(self.client.post(reverse('verify_email', kwargs={'token': token})).status_code, 400)

    def test_email_change_clears_verification_and_requests_new_mail(self):
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.client.force_login(self.user)
        response = self.client.post('/profile/', {'full_name': self.user.full_name, 'state_code': self.user.state_code, 'email': 'updated@example.com', 'display_name': 'Nickname', 'current_password': 'Previous-Strong-Pass-123!'})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertEqual(self.user.display_name, 'Nickname')
        self.assertEqual(mail.outbox[0].to, ['updated@example.com'])

    def test_email_change_requires_current_password(self):
        self.client.force_login(self.user)
        payload = {'full_name': self.user.full_name, 'state_code': self.user.state_code, 'email': 'changed@example.com'}
        for password in ('', 'wrong'):
            response = self.client.post('/profile/', {**payload, 'current_password': password})
            self.assertEqual(response.status_code, 200)
            self.assertIn('current_password', response.context['form'].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'member@example.com')

    def test_duplicate_email_is_case_insensitively_rejected(self):
        from .forms import RegistrationForm
        form = RegistrationForm({'full_name': 'New Person', 'email': 'MEMBER@EXAMPLE.COM', 'state_code': 'LA/25A/9999', 'password': 'Strong-New-Password-456!', 'confirm_password': 'Strong-New-Password-456!'})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_password_reset_response_is_generic_and_token_one_use(self):
        other_session = Client()
        other_session.login(username=self.user.state_code, password='Previous-Strong-Pass-123!')
        known = self.client.post(reverse('password_reset'), {'email': self.user.email})
        unknown = self.client.post(reverse('password_reset'), {'email': 'unknown@example.com'})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.url, unknown.url)
        self.assertEqual(len(mail.outbox), 1)
        reset = re.search(r'http://testserver(/account/password-reset/[^/]+/[^/]+/)', mail.outbox[0].body).group(1)
        response = self.client.get(reset)
        self.assertEqual(response.status_code, 302)
        self.assertIn('set-password', response.url)
        self.assertEqual(self.client.post(response.url, {'new_password1': 'Fresh-Strong-Pass-456!', 'new_password2': 'Fresh-Strong-Pass-456!'}).status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Fresh-Strong-Pass-456!'))
        self.assertEqual(other_session.get('/profile/').status_code, 302)
        reused = Client().get(reset)
        self.assertEqual(reused.status_code, 200)
        self.assertContains(reused, 'invalid, expired or already used')

    def test_email_delivery_failure_does_not_disclose_account_or_token(self):
        with patch('accounts.recovery.EmailMultiAlternatives.send', side_effect=OSError('provider unavailable')):
            response = self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertRedirects(response, reverse('password_reset_done'))

    def test_recovery_and_verification_throttles(self):
        for _ in range(5):
            self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'unknown@example.com'}).status_code, 302)
        self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'unknown@example.com'}).status_code, 429)
        self.client.force_login(self.user)
        for _ in range(3):
            self.assertEqual(self.client.post(reverse('email_verification')).status_code, 302)
        self.assertEqual(self.client.post(reverse('email_verification')).status_code, 429)


class AdministratorMFATests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_superuser(state_code='ADMIN', email='admin@example.com', full_name='Administrator', password='Administrator-Pass-123!')
        cls.member = User.objects.create_user(state_code='MEMBER', email='regular@example.com', password='Member-Pass-123!')

    def setUp(self):
        cache.clear()

    def test_admin_requires_an_otp_even_after_password_login(self):
        self.client.login(username=self.staff.state_code, password='Administrator-Pass-123!')
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)
        response = self.client.post('/admin/login/', {'username': self.staff.state_code, 'password': 'Administrator-Pass-123!'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'OTP')

    def test_enrollment_requires_staff_and_current_password(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('mfa_setup')).status_code, 403)
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(reverse('mfa_setup')), 'otpauth://')
        response = self.client.post(reverse('mfa_setup'), {'step': 'password', 'password': 'wrong'})
        self.assertNotContains(response, 'otpauth://')
        self.assertEqual(TOTPDevice.objects.count(), 0)
        response = self.client.post(reverse('mfa_setup'), {'step': 'password', 'password': 'Administrator-Pass-123!'})
        self.assertContains(response, 'otpauth://')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertFalse(TOTPDevice.objects.get().confirmed)

    def test_verified_enrollment_unlocks_admin_and_recovery_codes_are_one_use(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('mfa_setup'), {'step': 'password', 'password': 'Administrator-Pass-123!'})
        device = TOTPDevice.objects.get(user=self.staff)
        token = str(totp(device.bin_key)).zfill(device.digits)
        response = self.client.post(reverse('mfa_setup'), {'step': 'verify', 'token': token})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Save these recovery codes now')
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        backup = StaticDevice.objects.get(user=self.staff)
        code = backup.token_set.first().token
        self.assertEqual(backup.token_set.count(), 8)
        self.assertNotContains(self.client.get(reverse('mfa_setup')), code)
        fresh = Client()
        self.assertEqual(fresh.post('/admin/login/', {'username': self.staff.state_code, 'password': 'Administrator-Pass-123!', 'otp_token': code}).status_code, 302)
        self.assertEqual(fresh.get('/admin/').status_code, 200)
        self.assertFalse(StaticToken.objects.filter(device=backup, token=code).exists())

    def test_setup_requires_csrf_and_pending_enrollment_expires(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(reverse('mfa_setup'), {'step': 'password', 'password': 'Administrator-Pass-123!'}).status_code, 403)
        self.client.force_login(self.staff)
        self.client.post(reverse('mfa_setup'), {'step': 'password', 'password': 'Administrator-Pass-123!'})
        session = self.client.session
        session['mfa_enrollment']['expires'] = time.time() - 1
        session.save()
        response = self.client.post(reverse('mfa_setup'), {'step': 'verify', 'token': '123456'})
        self.assertContains(response, 'Setup expired')
        self.assertFalse(TOTPDevice.objects.get().confirmed)
