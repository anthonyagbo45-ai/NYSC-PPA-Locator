"""Multipart limits run before CSRF/view side effects and close partial files."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings

from .processing import MAX_INPUT_BYTES
from .uploads import BoundedPhotoUploadHandler, MAX_REQUEST_BYTES, PhotoUploadLimitMiddleware


PHOTO_PATH = '/photos/conversations/11111111-1111-4111-8111-111111111111/add/'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class PhotoUploadLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.view = Mock(return_value=HttpResponse('view reached'))
        self.middleware = PhotoUploadLimitMiddleware(self.view)

    def request(self, content=b'a little image', **extra):
        return self.factory.post(PHOTO_PATH, {'photo': SimpleUploadedFile('PRIVATE-NAME.jpg', content)}, **extra)

    def test_valid_one_file_reaches_view_and_does_not_require_authenticated_request(self):
        request = self.request()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.FILES['photo'].read(), b'a little image')
        self.assertIsInstance(request.upload_handlers[0], BoundedPhotoUploadHandler)
        self.view.assert_called_once_with(request)
        request.close()

    def test_large_declared_envelope_is_rejected_before_body_parse(self):
        request = self.request()
        request.META['CONTENT_LENGTH'] = str(MAX_REQUEST_BYTES + 1)
        with patch.object(request, '_load_post_and_files', side_effect=AssertionError('Body must not be parsed.')):
            response = self.middleware(request)
        self.assertEqual(response.status_code, 413)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotIn(b'PRIVATE-NAME', response.content)
        self.view.assert_not_called()

    def test_oversized_actual_file_is_rejected_and_temporary_partial_is_removed(self):
        with TemporaryDirectory() as directory, override_settings(FILE_UPLOAD_TEMP_DIR=directory, FILE_UPLOAD_MAX_MEMORY_SIZE=64):
            request = self.request(b'x' * (MAX_INPUT_BYTES + 1))
            self.assertLess(int(request.META['CONTENT_LENGTH']), MAX_REQUEST_BYTES)
            response = self.middleware(request)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(list(request.FILES), [])
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.view.assert_not_called()
            request.close()

    def test_second_file_rejection_cannot_leave_first_file_available_to_view(self):
        with TemporaryDirectory() as directory, override_settings(FILE_UPLOAD_TEMP_DIR=directory, FILE_UPLOAD_MAX_MEMORY_SIZE=64):
            request = self.factory.post(PHOTO_PATH, {
                'photo': SimpleUploadedFile('first.jpg', b'a' * 1000),
                'extra': SimpleUploadedFile('second.jpg', b'b' * 1000),
            })
            response = self.middleware(request)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(list(request.FILES), [])
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.view.assert_not_called()
            request.close()

    def test_csrf_still_rejects_valid_sized_upload_and_request_cleanup_removes_temp_file(self):
        csrf = CsrfViewMiddleware(lambda request: HttpResponse('protected view'))
        def application_view(request):
            return self.view(request)
        def protected(request):
            rejection = csrf.process_view(request, application_view, (), {})
            return rejection if rejection is not None else application_view(request)
        middleware = PhotoUploadLimitMiddleware(protected)
        with TemporaryDirectory() as directory, override_settings(FILE_UPLOAD_TEMP_DIR=directory, FILE_UPLOAD_MAX_MEMORY_SIZE=64):
            request = self.request(b'x' * 1000)
            response = middleware(request)
            self.assertEqual(response.status_code, 403)
            self.view.assert_not_called()
            request.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_get_and_other_routes_leave_upload_handlers_and_body_untouched(self):
        for request in (self.factory.get(PHOTO_PATH), self.factory.post('/messenger/preferences/', {'allow_requests': 'on'}), self.factory.post('/photos/conversations/not-a-uuid/add/', {'photo': 'not a file'})):
            with self.subTest(path=request.path):
                original = list(request.upload_handlers)
                with patch.object(request, '_load_post_and_files', side_effect=AssertionError('Unrelated body parsed.')):
                    self.assertEqual(self.middleware(request).status_code, 200)
                self.assertEqual(request.upload_handlers, original)

    def test_handler_counts_actual_chunks_without_trusting_declared_file_size(self):
        request = self.factory.post(PHOTO_PATH)
        handler = BoundedPhotoUploadHandler(request)
        handler.new_file('photo', 'PRIVATE.jpg', 'image/jpeg', 1)
        self.assertEqual(handler.receive_data_chunk(b'a', 0), b'a')
        handler.bytes_seen = MAX_INPUT_BYTES
        with self.assertRaises(StopUpload):
            handler.receive_data_chunk(b'x', 1)
        self.assertTrue(request._photo_upload_rejected)

    def test_network_rate_limit_runs_before_parsing_and_cache_failure_is_closed(self):
        request = self.request()
        with patch('config.security.cache.add', side_effect=ConnectionError('private backend info')):
            with patch.object(request, '_load_post_and_files', side_effect=AssertionError('Body parsed despite unavailable limiter.')):
                response = self.middleware(request)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'private backend info', response.content)
        self.assertIn('no-store', response['Cache-Control'])
        self.view.assert_not_called()

    def test_network_request_budget_is_shared_before_body_parse(self):
        for _ in range(60):
            request = self.factory.post(PHOTO_PATH, {'note': 'small form'})
            self.assertEqual(self.middleware(request).status_code, 200)
            request.close()
        request = self.request()
        with patch.object(request, '_load_post_and_files', side_effect=AssertionError('Rate-limited body parsed.')):
            response = self.middleware(request)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '300')
