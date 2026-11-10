"""Real HTTP boundaries for consent-based photo sharing in private chats."""
from html.parser import HTMLParser
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from community.models import CommunityBlock, Question
from locations.models import LGA, State
from messenger import services as messenger
from messenger.models import Message, MessageReport, MessengerPreference
from .models import PhotoAttachment, PhotoEvidence


class ImageSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.sources.append(dict(attrs).get('src'))


@override_settings(
    PPA_PHOTOS_ENABLED=True,
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        'photos': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    },
)
class PrivatePhotoViewTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        # Every test owns a new in-memory image backend, never a real bucket.
        storages._storages.pop('photos', None)
        self.storage = storages['photos']
        self.state = State.objects.create(name='Private photo test state')
        self.lga = LGA.objects.create(state=self.state, name='Private photo test LGA')
        User = get_user_model()
        self.sender = User.objects.create_user(
            state_code='PH/26A/0101', email='photo-sender-private@example.test',
            full_name='PRIVATE PHOTO SENDER LEGAL NAME', display_name='Photo neighbour',
            email_verified=True,
        )
        self.recipient = User.objects.create_user(
            state_code='PH/26A/0102', email='photo-recipient-private@example.test',
            full_name='PRIVATE PHOTO RECIPIENT LEGAL NAME', display_name='Local friend',
            email_verified=True,
        )
        self.outsider = User.objects.create_superuser(
            state_code='PH/26A/9999', email='photo-outsider-private@example.test',
            full_name='PRIVATE PHOTO STAFF LEGAL NAME', display_name='Outside staff',
            email_verified=True,
        )
        self.question = Question.objects.create(
            author=self.recipient, title='Can someone explain the public route?',
            body='Public context, no private member identifiers.', category='transport',
            state=self.state, lga=self.lga,
        )
        for member in (self.sender, self.recipient):
            messenger.save_preferences(member, allow_requests=True, allow_photos=True)
        self.conversation = messenger.request_chat(
            self.sender, 'question', self.question.pk, 'A private introduction.', uuid4(),
        )
        messenger.respond_request(self.recipient, self.conversation, accept=True)
        self.client.force_login(self.sender)

    def tearDown(self):
        storages._storages.pop('photos', None)

    def upload_url(self, conversation=None):
        return reverse('photos:upload', args=[conversation or self.conversation])

    def content_url(self, photo):
        return reverse('photos:content', args=[getattr(photo, 'pk', photo)])

    def thread_url(self):
        return reverse('messenger:thread', args=[self.conversation])

    def image(self, *, color=(21, 131, 61), name='PRIVATE-ORIGINAL-FILENAME.png', content_type='image/png', metadata=False):
        data = BytesIO()
        image = Image.new('RGB', (80, 48), color)
        if metadata:
            exif = Image.Exif()
            exif[270] = 'PRIVATE CAMERA DESCRIPTION NEVER TO BE SHARED'
            exif[315] = 'PRIVATE CAMERA OWNER NEVER TO BE SHARED'
            image.save(data, format='JPEG', exif=exif)
        else:
            image.save(data, format='PNG')
        return SimpleUploadedFile(name, data.getvalue(), content_type=content_type)

    def post_photo(self, *, token=None, caption='A useful photo of a public landmark.', image=None, **extra):
        data = {
            'image': image if image is not None else self.image(),
            'caption': caption, 'client_token': str(token or uuid4()),
            'rights_confirmed': 'on',
        }
        data.update(extra)
        return self.client.post(self.upload_url(), data)

    def uploaded(self, **kwargs):
        response = self.post_photo(**kwargs)
        self.assertEqual(response.status_code, 302)
        return PhotoAttachment.objects.latest('created_at')

    def response_bytes(self, response):
        try:
            return b''.join(response.streaming_content) if response.streaming else response.content
        finally:
            response.close()

    def assert_no_identity(self, response):
        for member in (self.sender, self.recipient, self.outsider):
            for value in (member.state_code, member.email, member.full_name):
                self.assertNotContains(response, value)

    def test_upload_and_photo_content_require_authentication(self):
        photo = self.uploaded()
        self.client.logout()
        for url in (self.upload_url(), self.content_url(photo)):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('login'), response.url)
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_get_upload_is_side_effect_free_and_form_is_private_multipart(self):
        count = Message.objects.count()
        response = self.client.get(self.upload_url())
        self.assertContains(response, 'multipart/form-data')
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, 'name="image"')
        self.assertContains(response, 'name="rights_confirmed"')
        self.assertContains(response, 'name="client_token"')
        self.assertIn('no-store', response['Cache-Control'])
        self.assert_no_identity(response)
        self.assertEqual(Message.objects.count(), count)
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(self.storage.listdir(''), ([], []))

    def test_real_multipart_upload_creates_one_captioned_message_and_normalized_jpeg(self):
        photo = self.uploaded()
        photo.message.refresh_from_db()
        self.assertEqual(photo.message.conversation_id, self.conversation)
        self.assertEqual(photo.message.sender_id, self.sender.pk)
        self.assertEqual(photo.message.body, 'A useful photo of a public landmark.')
        response = self.client.get(self.content_url(photo))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        data = self.response_bytes(response)
        self.assertEqual(len(data), photo.byte_size)
        self.assertLessEqual(len(data), 2 * 1024 * 1024)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (80, 48))
        self.assertEqual(Message.objects.count(), 2)

    def test_download_has_private_same_origin_headers_and_generic_filename(self):
        photo = self.uploaded()
        response = self.client.get(self.content_url(photo))
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cross-Origin-Resource-Policy'], 'same-origin')
        self.assertIn('inline', response['Content-Disposition'])
        self.assertNotIn('PRIVATE-ORIGINAL-FILENAME', str(response.headers))
        self.assertNotIn(photo.storage_key, str(response.headers))
        self.response_bytes(response)

    def test_metadata_and_original_filename_are_not_retained_or_projected(self):
        photo = self.uploaded(image=self.image(metadata=True, name='PRIVATE-CAMERA-OWNER.jpg', content_type='image/jpeg'))
        data = self.response_bytes(self.client.get(self.content_url(photo)))
        self.assertNotIn(b'PRIVATE CAMERA', data)
        with Image.open(BytesIO(data)) as image:
            self.assertFalse(image.getexif())
        self.assertNotIn('PRIVATE-CAMERA-OWNER', photo.storage_key)
        response = self.client.get(self.thread_url())
        self.assert_no_identity(response)
        self.assertNotContains(response, photo.storage_key)
        self.assertNotContains(response, 'PRIVATE-CAMERA-OWNER')
        self.assertNotContains(response, '.s3.')

    def test_photo_sharing_defaults_off_and_preferences_are_explicit_and_owner_scoped(self):
        MessengerPreference.objects.filter(user__in=(self.sender, self.recipient)).update(allow_photos=False)
        response = self.client.get(reverse('messenger:preferences'))
        self.assertContains(response, 'name="allow_photos"')
        self.assertFalse(messenger.get_preferences(self.sender)['allow_photos'])
        response = self.client.post(reverse('messenger:preferences'), {
            'allow_requests': 'on', 'allow_photos': 'on', 'user': self.recipient.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(messenger.get_preferences(self.sender)['allow_photos'])
        self.assertFalse(messenger.get_preferences(self.recipient)['allow_photos'])
        self.assertIn(self.post_photo().status_code, (400, 403))
        self.assertFalse(PhotoAttachment.objects.exists())

    def test_pending_introductions_cannot_contain_photos(self):
        from messenger.models import Conversation
        Conversation.objects.filter(pk=self.conversation).update(status='pending')
        response = self.post_photo()
        self.assertIn(response.status_code, (400, 403))
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_either_participant_opt_out_prevents_new_photos(self):
        for member in (self.sender, self.recipient):
            with self.subTest(member=member.pk):
                MessengerPreference.objects.filter(user__in=(self.sender, self.recipient)).update(allow_photos=True)
                MessengerPreference.objects.filter(user=member).update(allow_photos=False)
                self.assertIn(self.post_photo().status_code, (400, 403))
                self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_unverified_sender_or_recipient_cannot_send_photos(self):
        for member in (self.sender, self.recipient):
            with self.subTest(member=member.pk):
                get_user_model().objects.filter(pk__in=(self.sender.pk, self.recipient.pk)).update(email_verified=True)
                get_user_model().objects.filter(pk=member.pk).update(email_verified=False)
                self.assertIn(self.post_photo().status_code, (302, 400, 403))
                self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_staff_superuser_outsider_has_no_upload_or_image_bypass(self):
        photo = self.uploaded()
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.upload_url()).status_code, 404)
        self.assertEqual(self.post_photo().status_code, 404)
        self.assertEqual(self.client.get(self.content_url(photo)).status_code, 404)
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_csrf_is_required_even_for_authenticated_multipart_uploads(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.sender)
        response = client.post(self.upload_url(), {
            'image': self.image(), 'caption': 'A properly captioned photo.',
            'rights_confirmed': 'on', 'client_token': str(uuid4()),
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PhotoAttachment.objects.exists())
        page = client.get(self.upload_url())
        self.assertEqual(page.status_code, 200)
        token = client.cookies['csrftoken'].value
        response = client.post(self.upload_url(), {
            'image': self.image(), 'caption': 'A properly captioned photo.',
            'rights_confirmed': 'on', 'client_token': str(uuid4()),
            'csrfmiddlewaretoken': token,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_invalid_caption_nonce_and_permission_fields_create_no_message(self):
        for extra in (
            {'caption': ''}, {'caption': 'x' * 501}, {'client_token': 'not-a-uuid'},
            {'rights_confirmed': ''},
        ):
            with self.subTest(extra=extra):
                response = self.post_photo(**extra)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(PhotoAttachment.objects.exists())
                self.assertEqual(Message.objects.count(), 1)

    def test_corrupt_or_svg_upload_cannot_be_smuggled_by_filename_or_mime(self):
        for content in (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', b'not an image'):
            with self.subTest(content=content[:8]):
                response = self.post_photo(image=SimpleUploadedFile('safe.jpg', content, content_type='image/jpeg'))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(PhotoAttachment.objects.exists())
        # Declared metadata cannot turn real PNG pixels into active HTML.
        photo = self.uploaded(image=self.image(name='unsafe.html', content_type='text/html'))
        response = self.client.get(self.content_url(photo))
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertTrue(self.response_bytes(response).startswith(b'\xff\xd8'))

    def test_duplicate_upload_replay_does_not_duplicate_message_or_binary(self):
        token = uuid4()
        first = self.uploaded(token=token)
        response = self.post_photo(token=token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(PhotoAttachment.objects.get().pk, first.pk)

    def test_reusing_photo_token_with_different_pixels_cannot_replace_photo(self):
        token = uuid4()
        photo = self.uploaded(token=token)
        old_key, old_hash = photo.storage_key, photo.sha256
        response = self.post_photo(token=token, image=self.image(color=(150, 20, 30)))
        self.assertEqual(response.status_code, 400)
        photo.refresh_from_db()
        self.assertEqual((photo.storage_key, photo.sha256), (old_key, old_hash))
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    def test_existing_text_message_token_cannot_gain_an_attachment(self):
        token = uuid4()
        messenger.send_message(self.sender, self.conversation, 'A useful photo of a public landmark.', token)
        response = self.post_photo(token=token)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 2)

    def test_forged_sender_and_conversation_fields_do_not_change_attachment_scope(self):
        photo = self.uploaded(
            sender=self.outsider.pk, recipient=self.outsider.pk,
            conversation=str(uuid4()), conversation_id=str(uuid4()),
            message_id=Message.objects.get().pk, storage_key='public/forged.svg',
        )
        self.assertEqual(photo.message.conversation_id, self.conversation)
        self.assertEqual(photo.message.sender_id, self.sender.pk)
        self.assertNotEqual(photo.storage_key, 'public/forged.svg')
        self.assertTrue(self.storage.exists(photo.storage_key))

    def test_blocks_stop_new_photos_but_keep_participant_history(self):
        photo = self.uploaded()
        self.client.force_login(self.recipient)
        response = self.client.post(reverse('messenger:block', args=[self.conversation]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CommunityBlock.objects.filter(user=self.recipient, blocked_user=self.sender).exists())
        self.client.force_login(self.sender)
        self.assertIn(self.post_photo().status_code, (400, 403))
        self.assertEqual(PhotoAttachment.objects.count(), 1)
        for member in (self.sender, self.recipient):
            self.client.force_login(member)
            response = self.client.get(self.content_url(photo))
            self.assertEqual(response.status_code, 200)
            self.response_bytes(response)

    def test_closed_or_unverified_participants_keep_existing_photo_history(self):
        photo = self.uploaded()
        self.assertEqual(self.client.post(reverse('messenger:close', args=[self.conversation])).status_code, 302)
        get_user_model().objects.filter(pk=self.sender.pk).update(email_verified=False)
        response = self.client.get(self.content_url(photo))
        self.assertEqual(response.status_code, 200)
        self.response_bytes(response)
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_deactivated_participant_cannot_fetch_photos(self):
        photo = self.uploaded()
        get_user_model().objects.filter(pk=self.sender.pk).update(is_active=False)
        self.assertIn(self.client.get(self.content_url(photo)).status_code, (302, 403, 404))
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_recipient_opt_out_preserves_history_and_offers_explicit_view_instead_of_preview(self):
        photo = self.uploaded()
        self.client.force_login(self.recipient)
        response = self.client.post(reverse('messenger:preferences'), {'allow_requests': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(messenger.get_preferences(self.recipient)['allow_photos'])
        response = self.client.get(self.thread_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.content_url(photo))
        parser = ImageSources()
        parser.feed(response.content.decode())
        self.assertNotIn(self.content_url(photo), parser.sources)
        response = self.client.get(self.content_url(photo))
        self.assertEqual(response.status_code, 200)
        self.response_bytes(response)

    def test_photo_report_preview_discloses_and_copies_only_selected_photo(self):
        first = self.uploaded(caption='Selected photo caption.')
        other = self.uploaded(caption='Adjacent private photo caption.', image=self.image(color=(150, 100, 10)))
        self.client.force_login(self.recipient)
        url = reverse('messenger:report', args=[self.conversation, first.message_id])
        response = self.client.get(url)
        self.assertContains(response, 'Selected photo caption.')
        self.assertContains(response, self.content_url(first))
        self.assertContains(response, 'photo')
        self.assertNotContains(response, self.content_url(other))
        self.assertNotContains(response, 'Adjacent private photo caption.')
        self.assert_no_identity(response)
        response = self.client.post(url, {'category': 'privacy', 'details': 'Please review the selected image.'})
        self.assertEqual(response.status_code, 302)
        report = MessageReport.objects.get()
        evidence = PhotoEvidence.objects.get(report=report)
        self.assertEqual(evidence.sha256, first.sha256)
        self.assertNotEqual(evidence.storage_key, first.storage_key)
        self.assertEqual(report.evidence['body'], 'Selected photo caption.')
        self.assertNotIn('Adjacent private photo caption.', str(report.evidence))

    def test_photo_opt_out_keeps_report_preview_explicit_and_safety_reporting_available(self):
        photo = self.uploaded(caption='Selected photo for a private safety report.')
        self.client.force_login(self.recipient)
        response = self.client.post(reverse('messenger:preferences'), {'allow_requests': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(messenger.get_preferences(self.recipient)['allow_photos'])
        files_before = self.storage.listdir('')
        url = reverse('messenger:report', args=[self.conversation, photo.message_id])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, photo.message.body)
        self.assertContains(response, self.content_url(photo))
        self.assertContains(response, 'attached photo')
        parser = ImageSources()
        parser.feed(response.content.decode())
        self.assertNotIn(self.content_url(photo), parser.sources)
        self.assertFalse(MessageReport.objects.exists())
        self.assertFalse(PhotoEvidence.objects.exists())
        self.assertEqual(self.storage.listdir(''), files_before)

        response = self.client.post(url, {'category': 'privacy', 'details': 'Review only the selected photo.'})
        self.assertEqual(response.status_code, 302)
        evidence = PhotoEvidence.objects.get()
        self.assertEqual(evidence.sha256, photo.sha256)
        self.assertNotEqual(evidence.storage_key, photo.storage_key)
        self.assertEqual(evidence.report.message_id, photo.message_id)

    def test_photo_caption_is_escaped_and_storage_identifier_stays_private(self):
        caption = '<script>window.photoScript=true</script>'
        photo = self.uploaded(caption=caption)
        response = self.client.get(self.thread_url())
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>window.photoScript')
        self.assertNotContains(response, photo.storage_key)
        self.assert_no_identity(response)

    def test_upload_cache_failure_stops_parsing_and_writes(self):
        with patch('config.security.cache.add', side_effect=ConnectionError('PRIVATE PROVIDER DETAIL')):
            response = self.post_photo()
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'PRIVATE PROVIDER DETAIL', status_code=503)
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_multiple_and_oversized_file_uploads_are_rejected_before_message_creation(self):
        response = self.client.post(self.upload_url(), {
            'image': [self.image(), self.image()], 'caption': 'Two files are not allowed.',
            'client_token': str(uuid4()), 'rights_confirmed': 'on',
        })
        self.assertEqual(response.status_code, 413)
        oversized = SimpleUploadedFile('large.jpg', b'x' * (8 * 1024 * 1024 + 1), content_type='image/jpeg')
        self.assertEqual(self.post_photo(image=oversized).status_code, 413)
        self.assertFalse(PhotoAttachment.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_global_photo_pause_prevents_new_uploads_and_new_image_fetches(self):
        photo = self.uploaded()
        with override_settings(PPA_PHOTOS_ENABLED=False):
            self.assertIn(self.post_photo().status_code, (400, 403, 503))
            self.assertIn(self.client.get(self.content_url(photo)).status_code, (403, 404, 503))
        self.assertEqual(PhotoAttachment.objects.count(), 1)

    def test_photo_content_endpoint_is_read_only_and_unknown_ids_are_unavailable(self):
        photo = self.uploaded()
        self.assertEqual(self.client.post(self.content_url(photo), {}).status_code, 405)
        self.assertEqual(self.client.get(self.content_url(uuid4())).status_code, 404)
        self.assertEqual(PhotoAttachment.objects.count(), 1)
