import io
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from accounts.management.commands.bootstrap_initial_admin import (
    CONFIG_ERROR, CONFLICT_ERROR, CREATED_MESSAGE, DATABASE_ERROR,
    SKIPPED_MESSAGE, VALIDATION_ERROR,
)
from accounts.models import ContributorVerification


User = get_user_model()
ENVIRONMENT = {
    'PPA_BOOTSTRAP_ADMIN': 'True',
    'PPA_ADMIN_STATE_CODE': 'LA/26A/8765',
    'PPA_ADMIN_EMAIL': 'Private.Operator+tag@Example.COM',
    'PPA_ADMIN_FULL_NAME': 'Private Operator',
    'PPA_ADMIN_PASSWORD': '  Cobalt-figment-761!  ',
}


def invoke_bootstrap():
    output = io.StringIO()
    errors = io.StringIO()
    call_command('bootstrap_initial_admin', stdout=output, stderr=errors)
    return output.getvalue(), errors.getvalue()


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class InitialAdminBootstrapTests(TestCase):
    def run_bootstrap(self, **changes):
        with patch.dict(os.environ, {**ENVIRONMENT, **changes}, clear=True):
            return invoke_bootstrap()

    def assert_rejected(self, expected_error, **changes):
        environment = {**ENVIRONMENT, **changes}
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), self.assertNoLogs(level='DEBUG'):
            with self.assertRaises(CommandError) as caught:
                call_command('bootstrap_initial_admin', stdout=output, stderr=errors)
        self.assertEqual(str(caught.exception), expected_error)
        rendered = output.getvalue() + errors.getvalue() + str(caught.exception)
        for name, value in environment.items():
            if name != 'PPA_BOOTSTRAP_ADMIN' and value.strip():
                self.assertNotIn(value, rendered)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), '')

    def test_disabled_by_default_even_with_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertNumQueries(0):
            self.assertEqual(invoke_bootstrap(), (SKIPPED_MESSAGE + '\n', ''))
        self.assertFalse(User.objects.exists())

    def test_flag_is_case_insensitive_and_false_does_not_read_credentials(self):
        for value in ('False', 'FALSE', 'fAlSe'):
            with self.subTest(value=value), patch.dict(os.environ, {'PPA_BOOTSTRAP_ADMIN': value}, clear=True):
                with self.assertNumQueries(0):
                    self.assertEqual(invoke_bootstrap(), (SKIPPED_MESSAGE + '\n', ''))
        self.run_bootstrap(PPA_BOOTSTRAP_ADMIN='tRuE')
        self.assertEqual(User.objects.count(), 1)

    def test_invalid_flag_fails_closed(self):
        for value in ('', 'yes', '1', 'enabled', 'False True'):
            with self.subTest(value=value):
                self.assert_rejected(CONFIG_ERROR, PPA_BOOTSTRAP_ADMIN=value)
        self.assertFalse(User.objects.exists())

    def test_every_configuration_field_is_required_and_nonblank(self):
        names = [name for name in ENVIRONMENT if name != 'PPA_BOOTSTRAP_ADMIN']
        for name in names:
            for value in ('', ' \t\n '):
                with self.subTest(name=name, blank=repr(value)):
                    self.assert_rejected(CONFIG_ERROR, **{name: value})
            environment = dict(ENVIRONMENT)
            del environment[name]
            with self.subTest(name=name, missing=True), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesMessage(CommandError, CONFIG_ERROR):
                    invoke_bootstrap()
        self.assert_rejected(CONFIG_ERROR, **{name: '' for name in names})
        self.assertFalse(User.objects.exists())

    def test_weak_and_account_similar_passwords_are_rejected(self):
        for password in ('Short-123!', '1234567890123456', '1234567890qwertyuiop',
                         ENVIRONMENT['PPA_ADMIN_FULL_NAME'], ENVIRONMENT['PPA_ADMIN_EMAIL']):
            with self.subTest(password=password):
                self.assert_rejected(VALIDATION_ERROR, PPA_ADMIN_PASSWORD=password)
        self.assertFalse(User.objects.exists())

    @override_settings(AUTH_PASSWORD_VALIDATORS=[])
    def test_initial_admin_has_an_independent_twelve_character_minimum(self):
        self.assert_rejected(VALIDATION_ERROR, PPA_ADMIN_PASSWORD='Long-12345!')
        self.assertFalse(User.objects.exists())

    def test_model_validation_rejects_invalid_email_and_overlong_fields(self):
        for changes in ({'PPA_ADMIN_EMAIL': 'private-invalid-email'},
                        {'PPA_ADMIN_STATE_CODE': 'X' * 21},
                        {'PPA_ADMIN_FULL_NAME': 'N' * 256}):
            with self.subTest(field=next(iter(changes))):
                self.assert_rejected(VALIDATION_ERROR, **changes)
        self.assertFalse(User.objects.exists())

    def test_validator_and_database_errors_do_not_expose_supplied_values(self):
        private_error = ' '.join(ENVIRONMENT.values())
        with patch('accounts.management.commands.bootstrap_initial_admin.password_validation.validate_password',
                   side_effect=ValidationError(private_error)):
            self.assert_rejected(VALIDATION_ERROR)
        with patch.object(User, 'full_clean', side_effect=ValidationError(private_error)):
            self.assert_rejected(VALIDATION_ERROR)
        with patch.object(User.objects, 'create_superuser', side_effect=IntegrityError(private_error)):
            self.assert_rejected(DATABASE_ERROR)
        self.assertFalse(User.objects.exists())

    def test_empty_database_creates_hashed_active_staff_superuser_without_verification(self):
        with self.assertNoLogs(level='DEBUG'):
            output, errors = self.run_bootstrap()
        self.assertEqual((output, errors), (CREATED_MESSAGE + '\n', ''))
        user = User.objects.get()
        self.assertEqual(user.state_code, ENVIRONMENT['PPA_ADMIN_STATE_CODE'])
        self.assertEqual(user.email, 'private.operator+tag@example.com')
        self.assertEqual(user.full_name, ENVIRONMENT['PPA_ADMIN_FULL_NAME'])
        self.assertNotEqual(user.password, ENVIRONMENT['PPA_ADMIN_PASSWORD'])
        self.assertTrue(user.check_password(ENVIRONMENT['PPA_ADMIN_PASSWORD']))
        self.assertFalse(user.check_password(ENVIRONMENT['PPA_ADMIN_PASSWORD'].strip()))
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertFalse(user.email_verified)
        self.assertFalse(user.groups.exists())
        self.assertFalse(user.user_permissions.exists())
        self.assertFalse(user.totpdevice_set.exists())
        self.assertFalse(user.staticdevice_set.exists())
        self.assertFalse(ContributorVerification.objects.exists())

    def test_repeated_release_does_not_change_password_profile_flags_or_roles(self):
        self.run_bootstrap()
        admin = User.objects.get()
        User.objects.filter(pk=admin.pk).update(email_verified=True)
        admin.groups.add(Group.objects.create(name='Retained operator group'))
        admin.user_permissions.add(Permission.objects.get(
            content_type__app_label='accounts', codename='view_corpmember'))
        group_ids = list(admin.groups.values_list('pk', flat=True))
        permission_ids = list(admin.user_permissions.values_list('pk', flat=True))
        User.objects.create_user(state_code='REGULAR', email='member@example.com')
        before = list(User.objects.order_by('pk').values())
        self.assertEqual(self.run_bootstrap(PPA_ADMIN_FULL_NAME='Replacement Name',
                                          PPA_ADMIN_PASSWORD='Replacement-unrelated-934!'),
                         (SKIPPED_MESSAGE + '\n', ''))
        self.assertEqual(list(User.objects.order_by('pk').values()), before)
        self.assertEqual(list(admin.groups.values_list('pk', flat=True)), group_ids)
        self.assertEqual(list(admin.user_permissions.values_list('pk', flat=True)), permission_ids)

    def test_existing_regular_user_is_never_promoted_or_modified(self):
        user = User.objects.create_user(state_code=ENVIRONMENT['PPA_ADMIN_STATE_CODE'],
                                        email=ENVIRONMENT['PPA_ADMIN_EMAIL'],
                                        password='Existing-phrase-652!')
        before = User.objects.values().get(pk=user.pk)
        self.assert_rejected(CONFLICT_ERROR)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.values().get(pk=user.pk), before)

    def test_existing_unrelated_account_blocks_creation(self):
        User.objects.create_user(state_code='PREEXISTING', email='existing@example.com')
        self.assert_rejected(CONFLICT_ERROR)
        self.assertEqual(User.objects.count(), 1)

    def test_state_code_and_email_on_different_accounts_are_a_conflict(self):
        User.objects.create_user(state_code=ENVIRONMENT['PPA_ADMIN_STATE_CODE'], email='first@example.com')
        User.objects.create_superuser(state_code='OTHER', email=ENVIRONMENT['PPA_ADMIN_EMAIL'])
        before = list(User.objects.order_by('pk').values())
        self.assert_rejected(CONFLICT_ERROR)
        self.assertEqual(list(User.objects.order_by('pk').values()), before)

    def test_changed_bootstrap_identity_cannot_create_a_second_admin(self):
        self.run_bootstrap()
        before = list(User.objects.values())
        for changes in ({'PPA_ADMIN_STATE_CODE': 'OTHER-IDENTITY'},
                        {'PPA_ADMIN_EMAIL': 'other@example.com'},
                        {'PPA_ADMIN_STATE_CODE': 'OTHER-IDENTITY', 'PPA_ADMIN_EMAIL': 'other@example.com'}):
            with self.subTest(changes=changes):
                self.assert_rejected(CONFLICT_ERROR, **changes)
                self.assertEqual(list(User.objects.values()), before)

    def test_deactivated_or_demoted_matching_account_cannot_be_restored(self):
        self.run_bootstrap()
        admin = User.objects.get()
        for flag in ('is_active', 'is_staff', 'is_superuser'):
            with self.subTest(flag=flag):
                User.objects.filter(pk=admin.pk).update(is_active=True, is_staff=True, is_superuser=True)
                User.objects.filter(pk=admin.pk).update(**{flag: False})
                before = list(User.objects.values())
                self.assert_rejected(CONFLICT_ERROR)
                self.assertEqual(list(User.objects.values()), before)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL serializes first-account creation with a table lock.')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class InitialAdminBootstrapConcurrencyTests(TransactionTestCase):
    def test_concurrent_first_releases_create_exactly_one_administrator(self):
        start = Barrier(2)

        def deploy():
            def synchronize_lock(execute, sql, params, many, context):
                if sql.startswith('LOCK TABLE '):
                    start.wait(timeout=10)
                return execute(sql, params, many, context)

            try:
                with connection.execute_wrapper(synchronize_lock):
                    return invoke_bootstrap()
            finally:
                connections.close_all()

        with patch.dict(os.environ, ENVIRONMENT, clear=True):
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(deploy) for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
        self.assertCountEqual(results, [(CREATED_MESSAGE + '\n', ''), (SKIPPED_MESSAGE + '\n', '')])
        self.assertEqual(User.objects.count(), 1)
        admin = User.objects.get()
        self.assertTrue(admin.is_active and admin.is_staff and admin.is_superuser)
        self.assertTrue(admin.check_password(ENVIRONMENT['PPA_ADMIN_PASSWORD']))
