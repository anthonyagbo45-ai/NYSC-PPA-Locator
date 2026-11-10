import importlib
import io
import logging
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice
from locations.models import State, LGA

from .admin import AccountAdminForm, AccountCreationForm
from .forms import ProfileForm
from .logging import SensitiveDataFilter
from .mail import verification_token
from .management.commands.bootstrap_roles import (
    MODERATOR_PERMISSIONS, LOCAL_MODERATOR_PERMISSIONS, RESOURCE_MODERATOR_PERMISSIONS,
    COMMUNITY_MODERATOR_PERMISSIONS, ROOM_MODERATOR_PERMISSIONS,
)
from .models import ContributorVerification


User = get_user_model()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CheckpointAccountSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(state_code='LA/25A/1777', email='member@example.com', full_name='Private Example Member', password='Previous-Strong-Pass-123!')

    def setUp(self):
        cache.clear()

    def test_sensitive_profile_password_checks_are_bounded_per_account(self):
        self.client.force_login(self.user)
        payload = {'full_name': self.user.full_name, 'state_code': self.user.state_code, 'email': 'changed@example.com', 'current_password': 'wrong'}
        with patch.object(User, 'check_password', return_value=False) as check:
            for index in range(10):
                self.assertEqual(self.client.post('/profile/', payload, REMOTE_ADDR=f'192.0.2.{index}').status_code, 200)
            response = self.client.post('/profile/', payload, REMOTE_ADDR='198.51.100.1')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(check.call_count, 10)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'member@example.com')

    def test_profile_password_throttle_fails_closed_when_cache_fails(self):
        self.client.force_login(self.user)
        with patch('config.security.cache.add', side_effect=ConnectionError), patch.object(User, 'check_password') as check:
            response = self.client.post('/profile/', {'email': 'changed@example.com'})
        self.assertEqual(response.status_code, 503)
        check.assert_not_called()

    def test_changing_login_state_code_also_requires_current_password(self):
        self.client.force_login(self.user)
        response = self.client.post('/profile/', {'full_name': self.user.full_name, 'email': self.user.email, 'state_code': 'CHANGED'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('current_password', response.context['form'].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.state_code, 'LA/25A/1777')

    def test_verification_write_is_bound_to_address_even_if_email_changes_concurrently(self):
        url = reverse('verify_email', kwargs={'token': verification_token(self.user)})
        real_update = QuerySet.update

        def raced_update(queryset, **values):
            if queryset.model == User and values == {'email_verified': True}:
                real_update(User.objects.filter(pk=self.user.pk), email='raced@example.com', email_verified=False)
            return real_update(queryset, **values)

        with patch.object(QuerySet, 'update', raced_update):
            self.assertEqual(self.client.post(url).status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertEqual(self.user.email, 'raced@example.com')

    def test_account_similarity_validator_uses_the_actual_identifier_and_name(self):
        for password in (self.user.state_code, self.user.full_name, self.user.email):
            with self.subTest(password_attribute=password):
                with self.assertRaises(ValidationError):
                    password_validation.validate_password(password, self.user)

    def test_normalizing_new_emails_preserves_plus_addressing(self):
        user = User.objects.create_user(state_code='PLUS', email='  Member+tag@Example.COM  ')
        self.assertEqual(user.email, 'member+tag@example.com')
        self.assertNotEqual(user.email, self.user.email)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(state_code='DUPLICATE', email='MEMBER+TAG@EXAMPLE.COM')])

    def test_case_insensitive_constraint_covers_writes_bypassing_forms(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(state_code='DUPLICATE', email='MEMBER@EXAMPLE.COM')])

    def test_unrelated_edits_preserve_historical_email_and_password_hash(self):
        User.objects.filter(pk=self.user.pk).update(email='Legacy.Case@Example.COM')
        self.user.refresh_from_db()
        original_password = self.user.password
        form = ProfileForm({'full_name': 'Updated Name', 'state_code': self.user.state_code, 'email': 'legacy.case@example.com'}, instance=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        saved.refresh_from_db()
        self.assertEqual(saved.email, 'Legacy.Case@Example.COM')
        self.assertEqual(saved.password, original_password)
        self.assertEqual(saved.pk, self.user.pk)

    def test_new_email_write_normalizes_and_resets_previous_verification(self):
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.user.email = 'New+Address@Example.COM'
        self.user.save(update_fields=['email'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new+address@example.com')
        self.assertFalse(self.user.email_verified)

    def test_profile_name_edit_preserves_concurrent_password_flags_and_email_changes(self):
        self.client.force_login(self.user)
        replacement_hash = make_password('Concurrent-Reset-Pass-456!')
        original_is_valid = ProfileForm.is_valid

        def interleaved_is_valid(form):
            valid = original_is_valid(form)
            if valid:
                User.objects.filter(pk=self.user.pk).update(password=replacement_hash, is_staff=True, email='concurrent@example.com', email_verified=True)
            return valid

        with patch.object(ProfileForm, 'is_valid', interleaved_is_valid):
            response = self.client.post('/profile/', {'full_name': 'Changed Name', 'state_code': self.user.state_code, 'email': self.user.email})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, 'Changed Name')
        self.assertEqual(self.user.password, replacement_hash)
        self.assertTrue(self.user.is_staff)
        self.assertEqual(self.user.email, 'concurrent@example.com')
        self.assertTrue(self.user.email_verified)

    def test_distributed_login_attempts_share_account_counter(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for index in range(20):
                response = self.client.post('/login/', {'username': self.user.state_code}, REMOTE_ADDR=f'192.0.2.{index}')
                self.assertEqual(response.status_code, 200)
            response = self.client.post('/admin/login/', {'username': self.user.state_code}, REMOTE_ADDR='198.51.100.7')
        self.assertEqual(response.status_code, 429)

    def test_unknown_accounts_have_the_same_distributed_throttle(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for index in range(20):
                self.assertEqual(self.client.post('/login/', {'username': 'UNKNOWN'}, REMOTE_ADDR=f'192.0.2.{index}').status_code, 200)
            self.assertEqual(self.client.post('/login/', {'username': 'UNKNOWN'}, REMOTE_ADDR='198.51.100.7').status_code, 429)

    def test_decoy_fields_cannot_change_the_identity_actually_being_authenticated(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for index in range(20):
                response = self.client.post('/login/', {'username': f'DECOY-{index}', 'state_code': self.user.state_code}, REMOTE_ADDR=f'192.0.2.{index}')
                self.assertEqual(response.status_code, 200)
            response = self.client.post('/admin/login/', {'username': self.user.state_code, 'state_code': 'DECOY'}, REMOTE_ADDR='198.51.100.7')
        self.assertEqual(response.status_code, 429)

    def test_distinct_members_on_shared_network_do_not_share_small_account_limit(self):
        with patch('django.contrib.auth.forms.AuthenticationForm.is_valid', return_value=False):
            for index in range(25):
                self.assertEqual(self.client.post('/login/', {'username': f'MEMBER-{index}'}).status_code, 200)

    def test_password_reset_has_account_limit_across_networks_without_enumeration(self):
        for index in range(5):
            self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'unknown@example.com'}, REMOTE_ADDR=f'192.0.2.{index}').status_code, 302)
        self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'UNKNOWN@example.com'}, REMOTE_ADDR='198.51.100.7').status_code, 429)


class EmailCollisionMigrationTests(TransactionTestCase):
    def test_preflight_stops_without_merging_or_rewriting_accounts(self):
        constraint = User._meta.constraints[0]
        with connection.schema_editor() as editor:
            editor.remove_constraint(User, constraint)
        try:
            User.objects.bulk_create([User(state_code='LEGACY-ONE', email='Case@Example.COM', password='historical-hash-one'), User(state_code='LEGACY-TWO', email='case@example.com', password='historical-hash-two')])
            before = list(User.objects.order_by('pk').values_list('pk', 'email', 'password'))
            migration = importlib.import_module('accounts.migrations.0005_email_case_insensitive_unique')
            from django.apps import apps
            with self.assertRaisesRegex(RuntimeError, 'collisions exist'):
                migration.check_existing_email_collisions(apps, type('SchemaEditor', (), {'connection': connection})())
            self.assertEqual(list(User.objects.order_by('pk').values_list('pk', 'email', 'password')), before)
        finally:
            User.objects.filter(state_code__in=['LEGACY-ONE', 'LEGACY-TWO']).delete()
            with connection.schema_editor() as editor:
                editor.add_constraint(User, constraint)


class OperatorPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(state_code='ADMIN', email='admin@example.com', password='Administrator-Pass-123!')
        cls.support = User.objects.create_user(state_code='SUPPORT', email='support@example.com', is_staff=True)
        cls.member = User.objects.create_user(state_code='MEMBER', email='member@example.com')
        cls.support.user_permissions.add(*Permission.objects.filter(content_type__app_label='accounts', codename__in=['view_corpmember', 'change_corpmember', 'add_corpmember']))
        cls.support.user_permissions.add(*Permission.objects.filter(content_type__app_label='auth', codename__in=['view_group', 'change_group', 'add_group', 'delete_group']))

    def request_for(self, user):
        request = RequestFactory().get('/admin/')
        request.user = user
        return request

    def otp_login(self, user):
        self.client.force_login(user)
        device = TOTPDevice.objects.create(user=user, name='Test admin', confirmed=True)
        session = self.client.session
        session['otp_device_id'] = device.persistent_id
        session.save()

    def test_delegated_support_cannot_grant_roles_flags_or_add_accounts(self):
        account_admin = admin.site._registry[User]
        request = self.request_for(self.support)
        self.assertTrue(account_admin.has_view_permission(request, self.member))
        self.assertFalse(account_admin.has_change_permission(request, self.member))
        self.assertFalse(account_admin.has_change_permission(request, self.support))
        self.assertFalse(account_admin.has_add_permission(request))
        self.assertFalse(account_admin.has_delete_permission(request, self.member))
        with self.assertRaises(PermissionDenied):
            account_admin.save_model(request, self.support, None, True)
        group_admin = admin.site._registry[Group]
        self.assertFalse(group_admin.has_change_permission(request))
        self.assertFalse(group_admin.has_add_permission(request))

    def test_account_admin_read_view_does_not_disclose_password_hash(self):
        self.otp_login(self.support)
        response = self.client.get(reverse('admin:accounts_corpmember_change', args=[self.member.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.member.password)
        self.assertNotContains(response, 'name="password"')
        response = self.client.post(reverse('admin:accounts_corpmember_change', args=[self.support.pk]), {'is_superuser': True, 'is_staff': True})
        self.assertEqual(response.status_code, 403)
        self.support.refresh_from_db()
        self.assertFalse(self.support.is_superuser)

    def test_superuser_account_creation_hashes_password_and_preserves_email_policy(self):
        self.otp_login(self.superuser)
        response = self.client.post(reverse('admin:accounts_corpmember_add'), {'state_code': 'ADDED', 'full_name': 'Added Member', 'email': 'Added+tag@Example.COM', 'password1': 'Unrelated-Long-Phrase-123!', 'password2': 'Unrelated-Long-Phrase-123!', 'is_active': True, '_save': 'Save'})
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None))
        added = User.objects.get(state_code='ADDED')
        self.assertEqual(added.email, 'added+tag@example.com')
        self.assertTrue(added.check_password('Unrelated-Long-Phrase-123!'))
        self.assertFalse(added.email_verified)
        self.assertFalse(added.is_staff)

    def test_operator_account_form_rejects_cross_state_lga(self):
        first = State.objects.create(name='Lagos')
        second = State.objects.create(name='Ogun')
        lga = LGA.objects.create(state=first, name='Ikeja')
        form = AccountCreationForm({'state_code': 'NEW', 'full_name': 'New Member', 'email': 'new@example.com', 'state_posted': second.pk, 'lga_posted': lga.pk, 'password1': 'Unrelated-Long-Phrase-123!', 'password2': 'Unrelated-Long-Phrase-123!'})
        self.assertFalse(form.is_valid())
        self.assertIn('lga_posted', form.errors)

    def test_admin_email_edit_resets_verification_only_for_a_changed_mailbox(self):
        User.objects.filter(pk=self.member.pk).update(email='Legacy.Case@Example.COM', email_verified=True)
        original_password = self.member.password
        self.otp_login(self.superuser)
        path = reverse('admin:accounts_corpmember_change', args=[self.member.pk])
        payload = {'state_code': self.member.state_code, 'full_name': 'Private Member', 'email': 'legacy.case@example.com', 'is_active': True, '_save': 'Save'}
        self.assertEqual(self.client.post(path, payload).status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, 'Legacy.Case@Example.COM')
        self.assertTrue(self.member.email_verified)
        self.assertEqual(self.member.password, original_password)
        self.assertEqual(self.client.post(path, {**payload, 'email': 'New+Mailbox@Example.COM'}).status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, 'new+mailbox@example.com')
        self.assertFalse(self.member.email_verified)
        self.assertEqual(self.member.password, original_password)

    def test_admin_name_edit_preserves_concurrent_password_flags_and_email_changes(self):
        self.otp_login(self.superuser)
        replacement_hash = make_password('Concurrent-Reset-Pass-456!')
        original_is_valid = AccountAdminForm.is_valid

        def interleaved_is_valid(form):
            valid = original_is_valid(form)
            if valid:
                User.objects.filter(pk=self.member.pk).update(password=replacement_hash, is_staff=True, email='concurrent@example.com', email_verified=True)
            return valid

        with patch.object(AccountAdminForm, 'is_valid', interleaved_is_valid):
            response = self.client.post(reverse('admin:accounts_corpmember_change', args=[self.member.pk]), {'state_code': self.member.state_code, 'full_name': 'Changed Name', 'email': self.member.email, 'is_active': True, '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, 'Changed Name')
        self.assertEqual(self.member.password, replacement_hash)
        self.assertTrue(self.member.is_staff)
        self.assertEqual(self.member.email, 'concurrent@example.com')
        self.assertTrue(self.member.email_verified)

    def test_private_contributor_checks_are_append_only_and_never_assign_roles(self):
        model_admin = admin.site._registry[ContributorVerification]
        request = self.request_for(self.superuser)
        record = ContributorVerification(member=self.member, outcome=ContributorVerification.Outcome.CONFIRMED, evidence_summary='Reviewed contribution accuracy with public references.')
        model_admin.save_model(request, record, None, False)
        self.assertEqual(record.reviewed_by, self.superuser)
        self.assertFalse(model_admin.has_change_permission(request, record))
        self.assertFalse(model_admin.has_delete_permission(request, record))
        self.assertFalse(model_admin.has_view_permission(self.request_for(self.support), record))
        self.assertFalse(self.member.groups.exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.email_verified)
        self.assertFalse(self.member.is_staff)

    def test_role_bootstrap_is_idempotent_and_grants_only_scoped_permissions(self):
        # Canonical PPA permissions are installed by that app's migrations. The
        # content type lookup keeps this test independent of fixture order.
        ppa_content_type = ContentType.objects.get(app_label='ppas', model='ppa')
        for codename in MODERATOR_PERMISSIONS:
            if not Permission.objects.filter(content_type__app_label='ppas', codename=codename).exists():
                Permission.objects.create(content_type=ppa_content_type, codename=codename, name=codename)
        output = io.StringIO()
        call_command('bootstrap_roles', stdout=output)
        call_command('bootstrap_roles', stdout=output)
        moderator = Group.objects.get(name='Moderator')
        self.assertEqual(set(moderator.permissions.values_list('content_type__app_label', 'codename')),
                         {('ppas', name) for name in MODERATOR_PERMISSIONS} | {('local_guides', name) for name in LOCAL_MODERATOR_PERMISSIONS}
                         | {('service_resources', name) for name in RESOURCE_MODERATOR_PERMISSIONS}
                         | {('community', name) for name in COMMUNITY_MODERATOR_PERMISSIONS}
                         | {('local_rooms', name) for name in ROOM_MODERATOR_PERMISSIONS})
        self.assertFalse(moderator.permissions.filter(codename__in=['can_verify_organisations', 'can_verify_listings', 'can_verify_resources', 'change_ppa', 'change_corpmember', 'change_group']).exists())
        self.assertFalse(Group.objects.get(name='VettedContributor').permissions.exists())
        self.assertFalse(self.member.groups.exists())
        self.assertEqual(ContributorVerification.objects.count(), 0)


class SensitiveLogTests(TestCase):
    def test_access_log_redacts_queries_private_identity_and_signed_urls(self):
        record = logging.LogRecord('gunicorn.access', logging.INFO, '', 0, 'GET %s %s %s %s', ('/search/?q=private&state_code=LA%2F25A%2F1234', '/account/verify-email/signed-secret/', '/account/password-reset/dXNlcg/reset-secret/', 'member+tag@example.com LA/25A/1234'), None)
        self.assertTrue(SensitiveDataFilter().filter(record))
        output = record.getMessage()
        for private in ('private', 'signed-secret', 'reset-secret', 'dXNlcg', 'member+tag@example.com', 'LA/25A/1234'):
            self.assertNotIn(private, output)
        self.assertIn('/search/', output)

    def test_structured_values_and_exception_text_are_redacted(self):
        try:
            raise ValueError('password=super-secret token=private-token origin=6.123,3.234')
        except ValueError:
            import sys
            exception = sys.exc_info()
        record = logging.LogRecord('django.request', logging.ERROR, '', 0, '%s', ({'password': 'dictionary-secret', 'email': 'private@example.com'},), exception)
        SensitiveDataFilter().filter(record)
        output = logging.Formatter().format(record)
        for private in ('super-secret', 'private-token', '6.123', '3.234', 'dictionary-secret', 'private@example.com'):
            self.assertNotIn(private, output)
        self.assertIn('ValueError', output)
