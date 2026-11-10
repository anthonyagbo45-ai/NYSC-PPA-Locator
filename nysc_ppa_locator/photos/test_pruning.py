"""Private-object maintenance is bounded, conservative and opt-in."""
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from messenger.models import Conversation, Message, MessageReport
from .models import PhotoAttachment, PhotoEvidence
from .storage import PrivateFileSystemStorage, PrivateS3PhotoStorage


COMMAND = 'photos.management.commands.prune_private_photos'


class PrivatePhotoPruningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.sender = users.create_user(state_code='PRUNE/PHOTO/SENDER')
        cls.recipient = users.create_user(state_code='PRUNE/PHOTO/RECV')
        cls.conversation = Conversation.objects.create(
            low_member=cls.sender, high_member=cls.recipient,
            initiated_by=cls.sender, status='closed',
        )
        cls.message = Message.objects.create(conversation=cls.conversation, sender=cls.sender, body='Retained photo caption.')
        cls.report = MessageReport.objects.create(
            message=cls.message, reporter=cls.recipient, category='privacy',
            details='Retain this selected evidence.', status='resolved',
            evidence={'body': cls.message.body},
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = PrivateFileSystemStorage(location=self.directory.name)
        self.now = timezone.now()
        self.old = self.now - timedelta(days=8)

    def object(self, namespace='conversation-photos', *, identifier=None, modified=None, name=None):
        identifier = identifier or uuid.uuid4()
        key = name or f'{namespace}/{identifier.hex}.jpg'
        self.assertEqual(self.storage.save(key, ContentFile(b'Synthetic private photo object.')), key)
        timestamp = (modified or self.old).timestamp()
        os.utime(self.storage.path(key), (timestamp, timestamp))
        return identifier, key

    def run_command(self, **options):
        output = StringIO()
        with patch(COMMAND + '.storages', {'photos': self.storage}):
            call_command('prune_private_photos', stdout=output, **options)
        return output.getvalue()

    def attachment(self, identifier, key):
        return PhotoAttachment.objects.create(
            id=identifier, message=self.message, storage_key=key,
            width=64, height=48, byte_size=31, sha256='a' * 64,
        )

    def evidence(self, identifier, key):
        return PhotoEvidence.objects.create(
            id=identifier, report=self.report, storage_key=key,
            width=64, height=48, byte_size=31, sha256='b' * 64,
        )

    def test_default_dry_run_preserves_every_object_and_prints_only_counts(self):
        _, key = self.object()
        output = self.run_command()
        self.assertTrue(self.storage.exists(key))
        self.assertIn('scanned=1, eligible=1, deleted=0, dry_run=True', output)
        self.assertNotIn(key, output)
        self.assertNotIn(self.directory.name, output)

    def test_explicit_delete_removes_only_old_unreferenced_objects(self):
        _, old_conversation = self.object()
        _, old_evidence = self.object('reported-photos')
        _, recent = self.object(modified=self.now - timedelta(hours=1))
        _, future = self.object(modified=self.now + timedelta(days=1))
        _, unknown = self.object(name='conversation-photos/not-an-opaque-photo.txt')
        attachment_id, attachment_key = self.object()
        evidence_id, evidence_key = self.object('reported-photos')
        self.attachment(attachment_id, attachment_key)
        self.evidence(evidence_id, evidence_key)
        output = self.run_command(delete=True)
        self.assertFalse(self.storage.exists(old_conversation))
        self.assertFalse(self.storage.exists(old_evidence))
        for key in (recent, future, unknown, attachment_key, evidence_key):
            self.assertTrue(self.storage.exists(key), key)
        self.assertIn('eligible=2, deleted=2, dry_run=False', output)
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        self.assertEqual(PhotoEvidence.objects.count(), 1)

    def test_references_in_either_table_are_preserved_regardless_of_namespace(self):
        attachment_id, unusual_attachment_key = self.object('reported-photos')
        evidence_id, unusual_evidence_key = self.object()
        self.attachment(attachment_id, unusual_attachment_key)
        self.evidence(evidence_id, unusual_evidence_key)
        self.run_command(delete=True)
        self.assertTrue(self.storage.exists(unusual_attachment_key))
        self.assertTrue(self.storage.exists(unusual_evidence_key))

    def test_deletion_rechecks_references_added_after_the_initial_snapshot(self):
        _, key = self.object()
        original_values = PhotoEvidence.objects.filter
        calls = []

        class ReferencedAfterSnapshot:
            def values_list(self, *args, **kwargs):
                return []

            def exists(self):
                return True

        def changing_references(**kwargs):
            calls.append(kwargs)
            if kwargs == {'storage_key': key}:
                return ReferencedAfterSnapshot()
            return original_values(**kwargs)

        with patch(COMMAND + '.PhotoEvidence.objects.filter', side_effect=changing_references):
            output = self.run_command(delete=True)
        self.assertIn({'storage_key': key}, calls)
        self.assertTrue(self.storage.exists(key))
        self.assertIn('eligible=1, deleted=0', output)

    def test_scan_budget_is_shared_by_both_namespaces(self):
        for namespace in ('conversation-photos', 'reported-photos'):
            for _ in range(5):
                self.object(namespace)
        real_iterator = self.storage.iter_private_photo_objects
        with patch.object(self.storage, 'iter_private_photo_objects', wraps=real_iterator) as inspected:
            output = self.run_command(limit=3)
        self.assertIn('scanned=3, eligible=3', output)
        self.assertEqual(inspected.call_args_list[0].args, ('conversation-photos/', 1))
        self.assertEqual(inspected.call_args_list[1].args, ('reported-photos/', 2))

    def test_minimum_age_and_limit_validation_never_echoes_supplied_values(self):
        private_input = 'not-a-limit-PRIVATEKEY'
        for options in (
            {'limit': 0}, {'limit': 5001}, {'limit': True}, {'limit': private_input},
            {'min_age_hours': 167}, {'min_age_hours': -1}, {'min_age_hours': private_input},
            {'min_age_hours': 9999999999}, {'delete': 'yes'},
        ):
            with self.subTest(options=options), self.assertRaises(CommandError) as caught:
                self.run_command(**options)
            self.assertNotIn(private_input, str(caught.exception))
            self.assertEqual(str(caught.exception), 'Choose a limit from 1 to 5000 and a minimum age of at least 168 hours.')

    def test_a_longer_retention_window_preserves_newer_orphans(self):
        _, key = self.object(modified=self.now - timedelta(days=8))
        output = self.run_command(delete=True, min_age_hours=24 * 30)
        self.assertTrue(self.storage.exists(key))
        self.assertIn('eligible=0', output)

    def test_invalid_or_naive_provider_timestamps_are_never_eligible(self):
        _, key = self.object()
        rows = [(key, self.old.replace(tzinfo=None)), (key, None), (key, 'not-a-date')]
        with patch.object(self.storage, 'iter_private_photo_objects', side_effect=[iter(rows), iter(())]):
            output = self.run_command(delete=True)
        self.assertTrue(self.storage.exists(key))
        self.assertIn('eligible=0', output)

    def test_provider_or_database_failure_has_fixed_private_error_and_aborts(self):
        _, key = self.object()
        marker = 'PRIVATE-provider-url-secret-storage-key'
        for target in ('iterator', 'database', 'delete'):
            with self.subTest(target=target):
                if target == 'iterator':
                    failure = patch.object(self.storage, 'iter_private_photo_objects', side_effect=RuntimeError(marker))
                elif target == 'database':
                    failure = patch(COMMAND + '.PhotoAttachment.objects.filter', side_effect=RuntimeError(marker))
                else:
                    failure = patch.object(self.storage, 'delete', side_effect=RuntimeError(marker))
                with failure, self.assertRaises(CommandError) as caught:
                    self.run_command(delete=True)
                self.assertEqual(str(caught.exception), 'Private photo maintenance could not complete. Remaining objects were not removed.')
                self.assertNotIn(marker, str(caught.exception))
                self.assertTrue(self.storage.exists(key))

    def test_filesystem_symlinks_and_nested_directories_are_not_followed(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / 'private-outside.jpg'
            outside_file.write_bytes(b'Never inspect or remove this outside file.')
            directory = Path(self.directory.name) / 'conversation-photos'
            directory.mkdir()
            link_key = f'conversation-photos/{uuid.uuid4().hex}.jpg'
            Path(self.storage.path(link_key)).symlink_to(outside_file)
            nested = directory / 'nested'
            nested.mkdir()
            (nested / 'private.jpg').write_bytes(b'Not a flat supported object.')
            output = self.run_command(delete=True)
            self.assertTrue(outside_file.exists())
            self.assertTrue(Path(self.storage.path(link_key)).is_symlink())
            self.assertTrue((nested / 'private.jpg').exists())
            self.assertIn('eligible=0', output)

    def test_namespace_symlink_causes_safe_abort_without_outside_access(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / f'{uuid.uuid4().hex}.jpg'
            outside_file.write_bytes(b'Never remove outside content.')
            (Path(self.directory.name) / 'conversation-photos').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(CommandError):
                self.run_command(delete=True)
            self.assertTrue(outside_file.exists())

    def object_storage(self, rows):
        calls, deleted = [], []

        class Collection:
            def filter(inner_self, **kwargs):
                calls.append(('filter', kwargs))
                candidates = sorted(
                    (row for row in rows if row.key.startswith(kwargs['Prefix'])
                     and row.key > kwargs.get('Marker', '')),
                    key=lambda row: row.key,
                )

                class Page:
                    def limit(page_self, limit):
                        calls.append(('limit', limit))
                        return iter(candidates[:limit])

                return Page()

        storage = PrivateS3PhotoStorage(
            bucket_name='synthetic-private-bucket', access_key='synthetic-key', secret_key='synthetic-secret',
        )
        storage._bucket = SimpleNamespace(objects=Collection())

        def delete(key):
            deleted.append(key)
            rows[:] = [row for row in rows if row.key != key]

        storage.delete = delete
        return storage, calls, deleted

    def test_s3_cursor_reaches_later_orphans_in_both_namespaces_without_rescanning_references(self):
        rows = []
        for prefix in ('conversation-photos/', 'reported-photos/'):
            rows.extend(SimpleNamespace(key=f'{prefix}{uuid.UUID(int=n).hex}.jpg', last_modified=self.old) for n in (1, 2, 3))
        attachment_key = 'conversation-photos/' + uuid.UUID(int=1).hex + '.jpg'
        evidence_key = 'reported-photos/' + uuid.UUID(int=1).hex + '.jpg'
        self.attachment(uuid.UUID(int=1), attachment_key)
        self.evidence(uuid.UUID(int=1), evidence_key)
        self.storage, calls, deleted = self.object_storage(rows)
        cursor = Path(self.directory.name) / 'private-pruning.json'

        first = self.run_command(limit=2, delete=True, cursor_file=str(cursor))
        self.assertIn('eligible=0', first)
        self.assertEqual(deleted, [])
        self.assertEqual(cursor.stat().st_mode & 0o777, 0o600)
        second = self.run_command(limit=2, delete=True, cursor_file=str(cursor))
        third = self.run_command(limit=2, delete=True, cursor_file=str(cursor))
        self.assertIn('eligible=2, deleted=2', second)
        self.assertIn('eligible=2, deleted=2', third)
        self.assertEqual(len(deleted), 4)
        self.assertNotIn(attachment_key, deleted)
        self.assertNotIn(evidence_key, deleted)
        self.assertEqual({row.key for row in rows}, {attachment_key, evidence_key})
        self.assertEqual([limit for action, limit in calls if action == 'limit'], [1] * 6)
        second_filters = [scope for action, scope in calls if action == 'filter'][2:4]
        self.assertEqual({scope['Marker'] for scope in second_filters}, {attachment_key, evidence_key})
        self.assertNotIn(attachment_key, first + second + third)
        self.assertNotIn(str(cursor), first + second + third)

    def test_s3_cursor_wraps_exhausted_namespaces_and_dry_run_never_deletes(self):
        key = 'reported-photos/' + uuid.UUID(int=5).hex + '.jpg'
        self.storage, _, deleted = self.object_storage([SimpleNamespace(key=key, last_modified=self.old)])
        cursor = Path(self.directory.name) / 'private-pruning.json'
        self.run_command(limit=1, cursor_file=str(cursor))
        self.assertEqual(json.loads(cursor.read_text())['after']['reported-photos/'], key)
        self.run_command(limit=1, cursor_file=str(cursor))
        self.assertIsNone(json.loads(cursor.read_text())['after']['reported-photos/'])
        output = self.run_command(limit=1, cursor_file=str(cursor))
        self.assertIn('eligible=1', output)
        self.assertEqual(deleted, [])

    def test_cursor_file_is_not_advanced_when_provider_delete_fails(self):
        key = 'conversation-photos/' + uuid.UUID(int=5).hex + '.jpg'
        self.storage, _, _ = self.object_storage([SimpleNamespace(key=key, last_modified=self.old)])
        cursor = Path(self.directory.name) / 'private-pruning.json'
        original = {'version': 1, 'after': {'conversation-photos/': None, 'reported-photos/': None}}
        cursor.write_text(json.dumps(original))
        cursor.chmod(0o600)
        with patch.object(self.storage, 'delete', side_effect=RuntimeError('Private provider details')):
            with self.assertRaises(CommandError):
                self.run_command(limit=2, delete=True, cursor_file=str(cursor))
        self.assertEqual(json.loads(cursor.read_text()), original)

    def test_cursor_files_require_private_permissions_scope_and_safe_paths(self):
        self.storage, _, _ = self.object_storage([])
        cursor = Path(self.directory.name) / 'cursor.json'
        for content, mode in (
            ('{"unrelated":"private configuration must not be overwritten"}', 0o600),
            ('{"version":1,"after":{"other-prefix/":"private"}}', 0o600),
            ('{"version":1,"after":{"reported-photos/":"conversation-photos/wrong-scope"}}', 0o600),
            ('{"version":1,"after":{}}', 0o644),
        ):
            cursor.write_text(content)
            cursor.chmod(mode)
            with self.subTest(content=content), self.assertRaises(CommandError) as caught:
                self.run_command(cursor_file=str(cursor))
            self.assertEqual(str(caught.exception), 'Use a private, operator-owned cursor file in a private directory.')
            self.assertEqual(cursor.read_text(), content)
        cursor.unlink()
        target = Path(self.directory.name) / 'real-private-file.json'
        target.write_text('{"version":1,"after":{}}')
        target.chmod(0o600)
        cursor.symlink_to(target)
        with self.assertRaises(CommandError):
            self.run_command(cursor_file=str(cursor))
        self.assertTrue(cursor.is_symlink())

    def test_filesystem_cursors_are_rejected_instead_of_claiming_resumable_ordering(self):
        cursor = Path(self.directory.name) / 'cursor.json'
        with self.assertRaises(CommandError) as caught:
            self.run_command(cursor_file=str(cursor))
        self.assertEqual(str(caught.exception), 'Persistent photo cursors require object-backed storage.')
        self.assertFalse(cursor.exists())


class PrivatePhotoObjectIteratorTests(SimpleTestCase):
    def s3(self, rows, *, location=''):
        self.calls = []

        class Collection:
            def filter(inner_self, **kwargs):
                self.calls.append(('filter', kwargs))
                return inner_self

            def limit(inner_self, limit):
                self.calls.append(('limit', limit))
                return iter(rows)

        storage = PrivateS3PhotoStorage(
            bucket_name='synthetic-private-bucket', access_key='synthetic-key',
            secret_key='synthetic-secret', location=location,
        )
        storage._bucket = SimpleNamespace(objects=Collection())
        return storage

    def test_s3_uses_bounded_prefix_collection_and_never_listdir(self):
        old = timezone.now() - timedelta(days=8)
        keys = [f'conversation-photos/{uuid.uuid4().hex}.jpg' for _ in range(5)]
        storage = self.s3([SimpleNamespace(key=key, last_modified=old) for key in keys])
        with patch.object(storage, 'listdir', side_effect=AssertionError('Unbounded listing is forbidden.')):
            rows = list(storage.iter_private_photo_objects('conversation-photos/', 2))
        self.assertEqual(rows, [(keys[0], old), (keys[1], old)])
        self.assertEqual(self.calls, [('filter', {'Prefix': 'conversation-photos/'}), ('limit', 2)])

    def test_s3_location_is_scoped_but_returned_keys_remain_application_relative(self):
        old = timezone.now() - timedelta(days=8)
        key = f'reported-photos/{uuid.uuid4().hex}.jpg'
        storage = self.s3([SimpleNamespace(key='protected/' + key, last_modified=old)], location='protected')
        self.assertEqual(list(storage.iter_private_photo_objects('reported-photos/', 1)), [(key, old)])
        self.assertEqual(self.calls[0], ('filter', {'Prefix': 'protected/reported-photos/'}))

    def test_s3_marker_is_scoped_to_its_private_prefix_and_storage_location(self):
        marker = f'reported-photos/{uuid.uuid4().hex}.jpg'
        storage = self.s3([], location='protected')
        self.assertEqual(list(storage.iter_private_photo_objects('reported-photos/', 20, after=marker)), [])
        self.assertEqual(self.calls, [
            ('filter', {'Prefix': 'protected/reported-photos/', 'Marker': 'protected/' + marker}),
            ('limit', 20),
        ])
        for invalid in ('conversation-photos/wrong-scope', 'https://private.example.test', 'reported-photos/\ninvalid'):
            with self.subTest(marker=invalid), self.assertRaises(ValueError):
                list(storage.iter_private_photo_objects('reported-photos/', 1, after=invalid))

    def test_scan_options_reject_arbitrary_paths_and_unbounded_limits(self):
        storage = self.s3([])
        for prefix, limit in (
            ('../', 1), ('arbitrary-bucket/', 1), ('conversation-photos', 1),
            ('conversation-photos/', 0), ('conversation-photos/', 5001),
            ('conversation-photos/', True), ('conversation-photos/', '20'),
        ):
            with self.subTest(prefix=prefix, limit=limit), self.assertRaises(ValueError):
                list(storage.iter_private_photo_objects(prefix, limit))
        self.assertEqual(self.calls, [])

    def test_filesystem_counts_unknown_entries_without_traversing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = PrivateFileSystemStorage(location=directory)
            for n in range(5):
                storage.save(f'conversation-photos/unknown-{n}.txt', ContentFile(b'Synthetic unknown object.'))
            self.assertEqual(len(list(storage.iter_private_photo_objects('conversation-photos/', 2))), 2)
            self.assertEqual(list(storage.iter_private_photo_objects('reported-photos/', 2)), [])
