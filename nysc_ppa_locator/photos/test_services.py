"""Consent, private storage integrity and transaction races for chat photos."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from PIL import Image

from community.models import CommunityBlock
from community.services import set_block
from messenger import services as messaging
from messenger.models import Conversation, Message, MessageReport, MessengerPreference
from . import services
from .models import PhotoAttachment, PhotoEvidence


PHOTO_STORAGE = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'photos': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


def image_upload(color='red', *, name='private-original.png'):
    data = BytesIO()
    with Image.new('RGB', (128, 80), color) as image:
        image.save(data, format='PNG')
    return SimpleUploadedFile(name, data.getvalue(), content_type='application/octet-stream')


class PhotoFixtures:
    @classmethod
    def make_fixtures(cls):
        User = get_user_model()
        cls.alice = User.objects.create_user(
            state_code='PHOTO/26A/1000', email='private-alice@example.test',
            full_name='Private Legal Alice', display_name='Alice', email_verified=True,
        )
        cls.bob = User.objects.create_user(
            state_code='PHOTO/26A/2000', email='private-bob@example.test',
            full_name='Private Legal Bob', display_name='Bob', email_verified=True,
        )
        cls.outsider = User.objects.create_superuser(
            state_code='PHOTO/26A/9000', email='private-admin@example.test',
            full_name='Private Admin', email_verified=True,
        )
        for user in (cls.alice, cls.bob):
            MessengerPreference.objects.create(user=user, allow_requests=True, allow_photos=True)
        low, high = sorted([cls.alice.pk, cls.bob.pk])
        cls.conversation = Conversation.objects.create(
            low_member_id=low, high_member_id=high, initiated_by=cls.alice,
            status=Conversation.Status.ACCEPTED,
        )
        cls.intro = Message.objects.create(
            conversation=cls.conversation, sender=cls.alice, body='Could we chat?',
        )

    def send(self, **kwargs):
        values = dict(
            user=self.alice, conversation_id=self.conversation.pk,
            upload=image_upload(), caption='A useful view of the place.',
            client_token=uuid.uuid4(), rights_confirmed=True,
        )
        values.update(kwargs)
        return services.send_photo(**values)

    def read(self, photo_id, user=None):
        result = services.open_photo(user or self.bob, photo_id)
        with result['file'] as stream:
            return stream.read()


class PrivatePhotoServiceTests(PhotoFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.make_fixtures()

    def setUp(self):
        override = override_settings(PPA_PHOTOS_ENABLED=True, STORAGES=PHOTO_STORAGE)
        override.enable()
        self.addCleanup(override.disable)

    def test_default_photo_consent_is_off_and_legacy_callers_preserve_it(self):
        MessengerPreference.objects.filter(user=self.alice).delete()
        self.assertEqual(messaging.get_preferences(self.alice), {'allow_requests': False, 'allow_photos': False})
        messaging.save_preferences(self.alice, True, allow_photos=True)
        messaging.save_preferences(self.alice, False)
        self.assertEqual(messaging.get_preferences(self.alice), {'allow_requests': False, 'allow_photos': True})
        messaging.save_preferences(self.alice, False, allow_photos=False)
        self.assertFalse(messaging.get_preferences(self.alice)['allow_photos'])

    def test_verified_explicit_photo_consent_is_required_for_both_people(self):
        for member in (self.alice, self.bob):
            MessengerPreference.objects.filter(user=member).update(allow_photos=False)
            with patch.object(services, 'normalize_photo') as decode, self.assertRaises(ValidationError):
                self.send()
            decode.assert_not_called()
            MessengerPreference.objects.filter(user=member).update(allow_photos=True)
        get_user_model().objects.filter(pk=self.alice.pk).update(email_verified=False)
        with self.assertRaises(PermissionDenied):
            messaging.save_preferences(self.alice, False, allow_photos=True)
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_pending_closed_and_declined_conversations_cannot_receive_photos(self):
        for status in ('pending', 'closed', 'declined'):
            Conversation.objects.filter(pk=self.conversation.pk).update(status=status)
            with patch.object(services, 'normalize_photo') as decode, self.assertRaises(ValidationError):
                self.send()
            decode.assert_not_called()
        self.assertEqual(Message.objects.count(), 1)

    def test_photo_sharing_feature_can_be_paused_before_decode_or_storage(self):
        with override_settings(PPA_PHOTOS_ENABLED=False), patch.object(services, 'normalize_photo') as decode:
            with self.assertRaises(ValidationError):
                self.send()
            decode.assert_not_called()
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_nonparticipant_and_superuser_get_no_photo_send_override(self):
        with patch.object(services, 'normalize_photo') as decode, self.assertRaises(Http404):
            self.send(user=self.outsider)
        decode.assert_not_called()

    def test_sender_and_recipient_need_current_active_verified_accounts(self):
        for user, field in ((self.alice, 'is_active'), (self.bob, 'is_active'), (self.alice, 'email_verified'), (self.bob, 'email_verified')):
            get_user_model().objects.filter(pk=user.pk).update(**{field: False})
            with self.assertRaises(PermissionDenied):
                self.send()
            get_user_model().objects.filter(pk=user.pk).update(**{field: True})
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_shared_block_prevents_photos_in_both_directions(self):
        for blocker, target in ((self.alice, self.bob), (self.bob, self.alice)):
            CommunityBlock.objects.all().delete()
            set_block(user=blocker, blocked_user=target)
            with self.assertRaises(PermissionDenied):
                self.send()
            with self.assertRaises(PermissionDenied):
                self.send(user=self.bob)
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_caption_permission_and_token_are_validated_before_decode(self):
        for changes in (
            {'caption': ''}, {'caption': 'x' * 501}, {'caption': 'Control\x00'},
            {'rights_confirmed': False}, {'rights_confirmed': 'yes'}, {'client_token': 'invalid'},
        ):
            with patch.object(services, 'normalize_photo') as decode, self.assertRaises(ValidationError):
                self.send(**changes)
            decode.assert_not_called()

    def test_sent_photo_is_normalized_private_and_does_not_retain_filename(self):
        message = self.send()
        photo = PhotoAttachment.objects.get()
        self.assertEqual(photo.message_id, message['id'])
        self.assertEqual(message['photo']['id'], photo.pk)
        self.assertNotIn('storage', str(message))
        self.assertNotIn('private-original', str(photo.__dict__))
        self.assertNotIn(self.alice.email, str(message))
        self.assertNotIn(self.alice.full_name, str(message))
        data = self.read(photo.pk)
        self.assertEqual(sha256(data).hexdigest(), photo.sha256)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (128, 80))
            self.assertFalse(image.getexif())
        self.assertEqual(photo.storage_key, f'conversation-photos/{photo.pk.hex}.jpg')

    def test_exact_replay_does_not_duplicate_message_or_storage(self):
        token = uuid.uuid4()
        first = self.send(client_token=token)
        with patch.object(storages['photos'], 'save', wraps=storages['photos'].save) as save:
            again = self.send(client_token=token)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        save.assert_not_called()
        with self.assertRaises(ValidationError):
            self.send(client_token=token, upload=image_upload('blue'))
        with self.assertRaises(ValidationError):
            self.send(client_token=token, caption='A replacement caption')

    def test_photo_cannot_be_added_to_an_existing_text_delivery_token(self):
        token = uuid.uuid4()
        messaging.send_message(self.alice, self.conversation.pk, 'A useful view of the place.', token)
        with self.assertRaises(ValidationError):
            self.send(client_token=token)
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_existing_history_remains_private_after_close_block_and_consent_change(self):
        photo_id = self.send()['photo']['id']
        messaging.close_conversation(self.bob, self.conversation.pk)
        messaging.block_peer(self.bob, self.conversation.pk)
        get_user_model().objects.filter(pk=self.bob.pk).update(email_verified=False)
        messaging.save_preferences(self.alice, True, allow_photos=False)
        self.assertTrue(self.read(photo_id))
        with self.assertRaises(Http404):
            self.read(photo_id, self.outsider)
        get_user_model().objects.filter(pk=self.bob.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.read(photo_id)

    def test_disabled_feature_missing_and_replaced_files_fail_closed(self):
        photo = PhotoAttachment.objects.get(pk=self.send()['photo']['id'])
        with override_settings(PPA_PHOTOS_ENABLED=False), self.assertRaises(Http404):
            self.read(photo.pk)
        storage = storages['photos']
        storage.delete(photo.storage_key)
        with self.assertRaises(Http404):
            self.read(photo.pk)
        storage.save(photo.storage_key, ContentFile(b'not-an-image'.ljust(photo.byte_size, b'x')))
        with self.assertRaises(Http404):
            self.read(photo.pk)
        storage.delete(photo.storage_key)
        storage.save(photo.storage_key, ContentFile(b'x' * (2 * 1024 * 1024 + 1)))
        with patch.object(storage, 'open', wraps=storage.open) as opened, self.assertRaises(Http404):
            self.read(photo.pk)
        opened.assert_not_called()

    def test_malformed_storage_key_is_rejected_before_backend_access(self):
        photo_id = self.send()['photo']['id']
        PhotoAttachment.objects.filter(pk=photo_id).update(storage_key='private-account-export.csv')
        with patch.object(storages['photos'], 'open') as opened, self.assertRaises(Http404):
            self.read(photo_id)
        opened.assert_not_called()

    def test_photo_and_common_message_allowances_use_actual_stored_bytes(self):
        first = self.send()
        byte_size = PhotoAttachment.objects.get(pk=first['photo']['id']).byte_size
        with patch.object(services, 'PHOTOS_PER_DAY', 1), self.assertRaises(ValidationError):
            self.send()
        with patch.object(services, 'PHOTOS_PER_FIVE_MINUTES', 1), self.assertRaises(ValidationError):
            self.send()
        with patch.object(services, 'MAX_MEMBER_STORED_BYTES', byte_size * 2 - 1), self.assertRaises(ValidationError):
            self.send()
        with patch.object(messaging, 'MESSAGES_PER_DAY', 2), self.assertRaises(ValidationError):
            self.send()
        PhotoAttachment.objects.all().update(created_at=timezone.now() - timedelta(days=2))
        with patch.object(services, 'PHOTOS_PER_DAY', 1), patch.object(services, 'PHOTOS_PER_FIVE_MINUTES', 1):
            self.send()

    def test_invalid_image_rolls_back_without_a_message_or_file(self):
        with patch.object(storages['photos'], 'save') as save, self.assertRaises(ValidationError):
            self.send(upload=SimpleUploadedFile('fake.jpg', b'<svg onload="alert(1)"></svg>'))
        self.assertEqual(Message.objects.count(), 1)
        self.assertFalse(PhotoAttachment.objects.exists())
        save.assert_not_called()

    def test_database_failure_deletes_just_written_asset(self):
        with patch.object(PhotoAttachment, 'save', side_effect=DatabaseError('Synthetic database failure')):
            with self.assertRaises(DatabaseError):
                self.send()
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(storages['photos'].listdir('conversation-photos')[1], [])

    def test_storage_failure_after_partial_write_rolls_back_and_cleans_file(self):
        storage = storages['photos']
        original = storage.save
        def write_then_fail(name, content, *args, **kwargs):
            original(name, content, *args, **kwargs)
            raise OSError('Synthetic provider failure containing a private key')
        with patch.object(storage, 'save', side_effect=write_then_fail), self.assertRaises(ValidationError) as caught:
            self.send()
        self.assertNotIn('private key', str(caught.exception))
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(storage.listdir('conversation-photos')[1], [])

    def test_real_outer_transaction_is_rejected_before_storage(self):
        with transaction.atomic(), patch.object(storages['photos'], 'save') as save:
            with self.assertRaises(ValidationError):
                self.send()
            save.assert_not_called()

    def test_message_projections_keep_queries_bounded_with_many_photos(self):
        with patch.object(services, 'PHOTOS_PER_FIVE_MINUTES', 50), patch.object(messaging, 'MESSAGES_PER_MINUTE', 50):
            for _ in range(10):
                self.send()
        with CaptureQueriesContext(connection) as queries:
            thread = messaging.get_thread(self.bob, self.conversation.pk)
        self.assertEqual(sum('photo' in row for row in thread['messages']), 10)
        self.assertLessEqual(len(queries), 6)

    def test_report_copies_only_selected_photo_and_survives_original_deletion(self):
        first = self.send()
        second = self.send(upload=image_upload('blue'), caption='Unreported private image')
        original = PhotoAttachment.objects.get(pk=first['photo']['id'])
        report_id = messaging.report_message(self.bob, self.conversation.pk, first['id'], 'privacy', 'Please review this image.')
        evidence = PhotoEvidence.objects.get(report_id=report_id)
        self.assertEqual(evidence.sha256, original.sha256)
        self.assertNotEqual(evidence.storage_key, original.storage_key)
        self.assertEqual(set(MessageReport.objects.get(pk=report_id).evidence), {'body', 'sent_at', 'sender_name'})
        self.assertNotIn('Unreported private image', str(MessageReport.objects.get(pk=report_id).evidence))
        self.assertNotEqual(evidence.sha256, PhotoAttachment.objects.get(pk=second['photo']['id']).sha256)
        Message.objects.get(pk=first['id']).delete()
        self.assertIsNone(MessageReport.objects.get(pk=report_id).message_id)
        self.assertTrue(storages['photos'].exists(evidence.storage_key))
        self.assertEqual(PhotoEvidence.objects.count(), 1)

    def test_photo_report_rejects_outsider_own_message_duplicates_and_outer_transaction(self):
        sent = self.send()
        for actor in (self.alice, self.outsider):
            with self.assertRaises((Http404, ValidationError)):
                messaging.report_message(actor, self.conversation.pk, sent['id'], 'abuse', 'Not allowed')
        with transaction.atomic(), self.assertRaises(ValidationError):
            messaging.report_message(self.bob, self.conversation.pk, sent['id'], 'abuse', 'Nested transaction')
        messaging.report_message(self.bob, self.conversation.pk, sent['id'], 'abuse', 'One report')
        with self.assertRaises(ValidationError):
            messaging.report_message(self.bob, self.conversation.pk, sent['id'], 'abuse', 'Duplicate')
        self.assertEqual(PhotoEvidence.objects.count(), 1)

    def test_report_copy_failure_rolls_back_report_and_new_storage_only(self):
        sent = self.send()
        original = PhotoAttachment.objects.get(pk=sent['photo']['id'])
        with patch.object(PhotoEvidence, 'save', side_effect=DatabaseError('Synthetic database failure')):
            with self.assertRaises(DatabaseError):
                messaging.report_message(self.bob, self.conversation.pk, sent['id'], 'abuse', 'Selected image')
        self.assertFalse(MessageReport.objects.exists())
        self.assertFalse(PhotoEvidence.objects.exists())
        self.assertTrue(storages['photos'].exists(original.storage_key))
        self.assertEqual(storages['photos'].listdir('reported-photos')[1], [])

    def test_stored_attachment_and_report_copy_are_immutable(self):
        sent = self.send()
        photo = PhotoAttachment.objects.get(pk=sent['photo']['id'])
        photo.width = 99
        with self.assertRaises(ValidationError):
            photo.save()
        report_id = messaging.report_message(self.bob, self.conversation.pk, sent['id'], 'abuse', 'Review')
        evidence = PhotoEvidence.objects.get(report_id=report_id)
        evidence.sha256 = '0' * 64
        with self.assertRaises(ValidationError):
            evidence.save()


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL ordered participant locks')
class PrivatePhotoConcurrencyTests(PhotoFixtures, TransactionTestCase):
    def setUp(self):
        override = override_settings(PPA_PHOTOS_ENABLED=True, STORAGES=PHOTO_STORAGE)
        override.enable()
        self.addCleanup(override.disable)
        self.make_fixtures()

    def _parallel(self, actions):
        barrier = Barrier(len(actions))
        def run(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run, actions))

    def test_simultaneous_same_nonce_stores_one_photo_and_message(self):
        nonce = uuid.uuid4()
        results = self._parallel([lambda: self.send(client_token=nonce), lambda: self.send(client_token=nonce)])
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(len(storages['photos'].listdir('conversation-photos')[1]), 1)

    def test_concurrent_photo_quota_is_serialized(self):
        def send():
            try:
                self.send()
                return 'sent'
            except ValidationError:
                return 'limited'
        with patch.object(services, 'PHOTOS_PER_DAY', 1):
            results = self._parallel([send, send])
        self.assertCountEqual(results, ['sent', 'limited'])
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_block_committed_during_decode_is_rechecked_before_storage(self):
        from .processing import normalize_photo
        decoded, release = Event(), Event()
        def decode(upload):
            result = normalize_photo(upload)
            decoded.set()
            if not release.wait(timeout=10):
                raise AssertionError('Timed out waiting for safety change.')
            return result
        def send():
            close_old_connections()
            try:
                try:
                    self.send()
                    return 'sent'
                except PermissionDenied:
                    return 'blocked'
            finally:
                close_old_connections()
        with patch.object(services, 'normalize_photo', side_effect=decode), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            try:
                self.assertTrue(decoded.wait(timeout=10))
                set_block(user=self.bob, blocked_user=self.alice)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), 'blocked')
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_photo_opt_out_committed_during_decode_is_rechecked_before_storage(self):
        from .processing import normalize_photo
        decoded, release = Event(), Event()
        def decode(upload):
            result = normalize_photo(upload)
            decoded.set()
            if not release.wait(timeout=10):
                raise AssertionError('Timed out waiting for consent change.')
            return result
        def send():
            close_old_connections()
            try:
                try:
                    self.send()
                    return 'sent'
                except ValidationError:
                    return 'disabled'
            finally:
                close_old_connections()
        with patch.object(services, 'normalize_photo', side_effect=decode), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            try:
                self.assertTrue(decoded.wait(timeout=10))
                messaging.save_preferences(self.bob, True, allow_photos=False)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), 'disabled')
        self.assertFalse(PhotoAttachment.objects.exists())
