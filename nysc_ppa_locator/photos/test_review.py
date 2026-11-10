"""Reported photos remain limited to the selected evidence and verified reviewers."""
from hashlib import sha256
from io import BytesIO
import uuid

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice
from PIL import Image

from messenger.models import Conversation, Message, MessageReport
from .models import PhotoAttachment, PhotoEvidence


@override_settings(
    PPA_PHOTOS_ENABLED=True,
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        'photos': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    },
)
class ReportedPhotoReviewTests(TransactionTestCase):
    def setUp(self):
        users = get_user_model().objects
        self.sender = users.create_user(
            state_code='PHOTO/PRIVATE/SENDER', email='private-photo-sender@example.test',
            full_name='Private Photo Sender Legal Name', display_name='Photo sender', email_verified=True,
        )
        self.recipient = users.create_user(
            state_code='PHOTO/PRIVATE/RECV', email='private-photo-recipient@example.test',
            full_name='Private Photo Recipient Legal Name', display_name='Photo recipient', email_verified=True,
        )
        self.reviewer = users.create_user(state_code='PHOTO/REVIEWER', is_staff=True)
        self.staff = users.create_user(state_code='PHOTO/STAFF', is_staff=True)
        self.public_moderator = users.create_user(state_code='PHOTO/PUBLIC/MOD', is_staff=True)
        self.nonstaff_reviewer = users.create_user(state_code='PHOTO/NONSTAFF', email_verified=True)
        self.supervisor = users.create_superuser(state_code='PHOTO/SUPER')
        self.review_permission = Permission.objects.get(
            content_type__app_label='messenger', codename='can_review_message_reports',
        )
        self.reviewer.user_permissions.add(self.review_permission)
        self.nonstaff_reviewer.user_permissions.add(self.review_permission)
        self.public_moderator.user_permissions.add(Permission.objects.get(
            content_type__app_label='community', codename='can_moderate_community_reports',
        ))
        low, high = sorted((self.sender.pk, self.recipient.pk))
        self.conversation = Conversation.objects.create(
            low_member_id=low, high_member_id=high, initiated_by=self.sender, status='accepted',
        )
        self.selected_message = Message.objects.create(
            conversation=self.conversation, sender=self.sender, body='Selected photo caption.',
        )
        self.neighbour_message = Message.objects.create(
            conversation=self.conversation, sender=self.sender, body='PRIVATE unrelated image caption.',
        )
        self.selected_bytes = self.jpeg('red')
        self.neighbour_bytes = self.jpeg('blue')
        self.attachment = self.store_image(PhotoAttachment, self.selected_bytes, message=self.selected_message)
        self.neighbour = self.store_image(PhotoAttachment, self.neighbour_bytes, message=self.neighbour_message)
        self.report = MessageReport.objects.create(
            message=self.selected_message, reporter=self.recipient, category='privacy',
            details='A participant explicitly selected this photo for review.',
            evidence={
                'body': self.selected_message.body, 'sent_at': self.selected_message.sent_at.isoformat(),
                'sender_name': self.sender.public_name,
            },
        )
        self.evidence = self.store_image(PhotoEvidence, self.selected_bytes, report=self.report)
        self.text_report = MessageReport.objects.create(
            message=self.neighbour_message, reporter=self.recipient, category='other',
            details='Only the text is in this separately constructed report.',
            evidence={
                'body': self.neighbour_message.body, 'sent_at': self.neighbour_message.sent_at.isoformat(),
                'sender_name': self.sender.public_name,
            },
        )

    @staticmethod
    def jpeg(colour):
        output = BytesIO()
        Image.new('RGB', (64, 48), colour).save(output, 'JPEG')
        return output.getvalue()

    def store_image(self, model, data, **relation):
        photo_id = uuid.uuid4()
        prefix = 'reported-photos' if model is PhotoEvidence else 'conversation-photos'
        key = f'{prefix}/{photo_id.hex}.jpg'
        self.assertEqual(storages['photos'].save(key, ContentFile(data)), key)
        return model.objects.create(
            id=photo_id, storage_key=key, width=64, height=48,
            byte_size=len(data), sha256=sha256(data).hexdigest(), **relation,
        )

    def client_for(self, user, *, otp=True):
        client = Client()
        client.force_login(user)
        if otp:
            device = TOTPDevice.objects.create(user=user, confirmed=True)
            session = client.session
            session['otp_device_id'] = device.persistent_id
            session.save()
        return client

    @staticmethod
    def response_bytes(response):
        try:
            return b''.join(response.streaming_content) if response.streaming else response.content
        finally:
            response.close()

    def reported_url(self, report=None):
        return reverse('photos:reported_content', args=[(report or self.report).pk])

    def test_otp_reviewer_can_fetch_only_selected_reported_image_with_private_headers(self):
        response = self.client_for(self.reviewer).get(self.reported_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cross-Origin-Resource-Policy'], 'same-origin')
        data = self.response_bytes(response)
        self.assertEqual(data, self.selected_bytes)
        self.assertNotEqual(data, self.neighbour_bytes)

    def test_staff_or_public_moderation_rights_do_not_grant_private_photo_review(self):
        for user in (self.staff, self.public_moderator, self.nonstaff_reviewer, self.recipient):
            with self.subTest(user=user.pk):
                response = self.client_for(user).get(self.reported_url())
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(self.evidence.storage_key.encode(), self.response_bytes(response))
        anonymous = Client().get(self.reported_url())
        self.assertIn(anonymous.status_code, (302, 403))

    def test_password_only_reviewer_has_no_photo_evidence_access(self):
        response = self.client_for(self.reviewer, otp=False).get(self.reported_url())
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.selected_bytes, self.response_bytes(response))

    def test_revoked_reviewer_permission_is_rechecked_on_next_fetch(self):
        client = self.client_for(self.reviewer)
        self.assertEqual(client.get(self.reported_url()).status_code, 200)
        self.reviewer.user_permissions.clear()
        self.assertEqual(client.get(self.reported_url()).status_code, 403)

    def test_deleted_or_unconfirmed_otp_device_revokes_reported_image_access(self):
        for confirmed in (False, None):
            with self.subTest(confirmed=confirmed):
                client = self.client_for(self.reviewer)
                self.assertEqual(client.get(self.reported_url()).status_code, 200)
                if confirmed is None:
                    TOTPDevice.objects.filter(user=self.reviewer).delete()
                else:
                    TOTPDevice.objects.filter(user=self.reviewer).update(confirmed=False)
                self.assertEqual(client.get(self.reported_url()).status_code, 403)

    def test_inactive_reviewer_no_longer_receives_photo_bytes(self):
        client = self.client_for(self.reviewer)
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        response = client.get(self.reported_url())
        self.assertIn(response.status_code, (302, 403))

    def test_admin_link_is_selected_report_only_without_storage_or_neighbour_information(self):
        response = self.client_for(self.reviewer).get(reverse('admin:messenger_messagereport_change', args=[self.report.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.reported_url())
        self.assertContains(response, 'View selected reported photo')
        for secret in (
            self.attachment.storage_key, self.evidence.storage_key, self.neighbour.storage_key,
            str(self.attachment.pk), str(self.neighbour.pk), str(self.conversation.pk),
            self.neighbour_message.body, self.sender.full_name, self.sender.email,
            self.sender.state_code, self.recipient.full_name, self.recipient.email,
            self.recipient.state_code,
        ):
            self.assertNotContains(response, secret)

    def test_text_only_report_exposes_no_attachment_even_when_original_message_has_one(self):
        client = self.client_for(self.reviewer)
        response = client.get(reverse('admin:messenger_messagereport_change', args=[self.text_report.pk]))
        self.assertContains(response, 'No photo included in this report.')
        self.assertNotContains(response, 'View selected reported photo')
        self.assertEqual(client.get(self.reported_url(self.text_report)).status_code, 404)
        self.assertEqual(client.get(reverse('photos:reported_content', args=[self.report.pk + 10000])).status_code, 404)

    def test_reviewers_and_superusers_cannot_use_participant_urls_for_unreported_images(self):
        for user in (self.reviewer, self.supervisor):
            client = self.client_for(user)
            for photo in (self.attachment, self.neighbour):
                with self.subTest(user=user.pk, photo=photo.pk):
                    response = client.get(reverse('photos:content', args=[photo.pk]))
                    self.assertEqual(response.status_code, 404)

    def test_reported_copy_survives_removal_of_original_message_and_image(self):
        original_key = self.attachment.storage_key
        self.assertNotEqual(original_key, self.evidence.storage_key)
        self.selected_message.delete()
        storages['photos'].delete(original_key)
        self.assertFalse(PhotoAttachment.objects.filter(pk=self.attachment.pk).exists())
        self.report.refresh_from_db()
        self.assertIsNone(self.report.message_id)
        self.assertEqual(self.report.evidence['body'], 'Selected photo caption.')
        response = self.client_for(self.reviewer).get(self.reported_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.response_bytes(response), self.selected_bytes)

    def test_admin_cannot_replace_selected_photo_and_private_models_have_no_browsing_permissions(self):
        for model in (PhotoAttachment, PhotoEvidence):
            self.assertNotIn(model, admin.site._registry)
            self.assertFalse(Permission.objects.filter(
                content_type__app_label='photos', content_type__model=model._meta.model_name,
            ).exists())
        original = (self.evidence.pk, self.evidence.storage_key, self.evidence.sha256, self.report.evidence)
        response = self.client_for(self.reviewer).post(reverse('admin:messenger_messagereport_change', args=[self.report.pk]), {
            'status': 'investigating', 'moderator_note': 'The participant-selected photo is under review.',
            'photo_evidence': str(self.neighbour.pk), 'storage_key': self.neighbour.storage_key,
            'evidence': '{"body":"Replaced caption"}', '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        self.evidence.refresh_from_db()
        self.report.refresh_from_db()
        self.assertEqual((self.evidence.pk, self.evidence.storage_key, self.evidence.sha256, self.report.evidence), original)
        self.assertEqual(self.report.status, 'investigating')

    def test_corrupted_storage_copy_fails_closed_without_revealing_keys(self):
        storages['photos'].delete(self.evidence.storage_key)
        storages['photos'].save(self.evidence.storage_key, ContentFile(self.neighbour_bytes))
        response = self.client_for(self.reviewer).get(self.reported_url())
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.evidence.storage_key.encode(), self.response_bytes(response))
