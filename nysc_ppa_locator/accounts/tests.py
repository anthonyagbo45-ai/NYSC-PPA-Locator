from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from locations.models import State, LGA
from .forms import RegistrationForm

User = get_user_model()


class AccountSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.other = State.objects.create(name='Ogun')
        cls.user = User.objects.create_user(state_code='LA/25A/1234', email='member@example.com', password='Strong-Test-Pass-123!', full_name='Test Member')

    def setUp(self):
        cache.clear()

    def registration(self, **extra):
        data = {'full_name': 'New Member', 'email': 'new@example.com', 'state_code': 'LA/25A/9999',
                'password': 'Strong-Test-Pass-456!', 'confirm_password': 'Strong-Test-Pass-456!',
                'state_posted': self.state.pk, 'lga_posted': self.lga.pk}
        return {**data, **extra}

    def test_get_cannot_authenticate_create_or_logout(self):
        self.assertEqual(self.client.get('/portal-entry/', {'state_code': 'NEW', 'password': 'secret'}).status_code, 405)
        self.assertEqual(User.objects.count(), 1)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/logout/').status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)

    def test_csrf_required_on_all_account_mutations(self):
        client = Client(enforce_csrf_checks=True)
        for path in ('/portal-entry/', '/login/', '/register/', '/logout/'):
            self.assertEqual(client.post(path, {}).status_code, 403, path)
        client.force_login(self.user)
        self.assertEqual(client.post('/profile/', {}).status_code, 403)

    def test_registration_validates_password_and_location(self):
        weak = RegistrationForm(self.registration(password='123', confirm_password='123'))
        self.assertFalse(weak.is_valid())
        self.assertIn('password', weak.errors)
        mismatch = RegistrationForm(self.registration(state_posted=self.other.pk))
        self.assertFalse(mismatch.is_valid())
        self.assertIn('lga_posted', mismatch.errors)

    def test_portal_registration_uses_full_name_and_hashes_password(self):
        response = self.client.post('/portal-entry/', self.registration(mode='register'))
        self.assertRedirects(response, '/dashboard/')
        user = User.objects.get(state_code='LA/25A/9999')
        self.assertEqual(user.full_name, 'New Member')
        self.assertTrue(user.check_password('Strong-Test-Pass-456!'))

    def test_login_uses_state_code_rotates_session_and_logout_flushes(self):
        session = self.client.session
        session['old'] = 'value'
        session.save()
        old_key = session.session_key
        response = self.client.post('/portal-entry/', {'mode': 'login', 'state_code': self.user.state_code, 'password': 'Strong-Test-Pass-123!'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)
        self.assertNotEqual(old_key, self.client.session.session_key)
        self.client.post('/logout/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unknown_login_never_registers(self):
        response = self.client.post('/portal-entry/', {'mode': 'login', 'state_code': 'UNKNOWN', 'password': 'Strong-Test-Pass-123!'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 1)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        self.client.post('/login/', {'username': self.user.state_code, 'password': 'Strong-Test-Pass-123!'})
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_rate_limit_shared_between_routes_and_no_header_spoofing(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for i in range(20):
                path = '/login/' if i % 2 else '/portal-entry/'
                self.assertEqual(self.client.post(path, {'mode': 'login'}, HTTP_X_FORWARDED_FOR=str(i)).status_code, 200)
            response = self.client.post('/login/', {})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)

    def test_admin_login_cannot_bypass_throttling(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for _ in range(20):
                self.assertEqual(self.client.post('/admin/login/', {}).status_code, 200)
            self.assertEqual(self.client.post('/login/', {}).status_code, 429)

    def test_rate_limit_fails_closed_when_cache_unavailable(self):
        with patch('config.security.cache.add', side_effect=ConnectionError):
            self.assertEqual(self.client.post('/login/', {}).status_code, 503)

    def test_profile_rejects_duplicate_identifiers_and_mismatched_locations(self):
        self.client.force_login(self.user)
        response = self.client.post('/profile/', {'full_name': 'Changed', 'state_code': self.user.state_code,
                                                 'state_posted': self.other.pk, 'lga_posted': self.lga.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn('lga_posted', response.context['form'].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, 'Test Member')
        other = User.objects.create_user(state_code='TAKEN', email='other@example.com')
        response = self.client.post('/profile/', {'full_name': 'Changed', 'state_code': other.state_code})
        self.assertIn('state_code', response.context['form'].errors)

    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_cookie_flags_and_private_caching(self):
        response = self.client.get('/login/')
        csrf = response.cookies['csrftoken']
        self.assertTrue(csrf['secure'])
        self.assertTrue(csrf['httponly'])
        self.assertEqual(csrf['samesite'], 'Lax')
        response = self.client.post('/login/', {'username': self.user.state_code, 'password': 'Strong-Test-Pass-123!'})
        cookie = response.cookies['sessionid']
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertIn('no-store', response['Cache-Control'])

    def test_landing_offers_guest_directory_without_registration(self):
        response = self.client.get('/')
        self.assertContains(response, 'method="get"')
        self.assertContains(response, 'Find your PPA')
        self.assertNotContains(response, 'name="password"')
