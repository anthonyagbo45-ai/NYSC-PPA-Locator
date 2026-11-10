import gzip
import tempfile
from io import StringIO
from unittest.mock import patch
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from ppas.services import PlacesUnavailable, search_google_places


class ProductionStaticTests(TestCase):
    def test_collected_hashed_css_is_served_compressed_with_debug_false(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            DEBUG=False, SECURE_SSL_REDIRECT=False, STATIC_ROOT=directory,
            WHITENOISE_USE_FINDERS=False, WHITENOISE_AUTOREFRESH=False,
            STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                      'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}},
        ):
            call_command('collectstatic', interactive=False, verbosity=0, stdout=StringIO())
            url = staticfiles_storage.url('locator/tailwind.css')
            self.assertNotEqual(url, '/static/locator/tailwind.css')
            response = Client().get(url, HTTP_ACCEPT_ENCODING='gzip')
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/css', response['Content-Type'])
            body = b''.join(response.streaming_content) if response.streaming else response.content
            if response.get('Content-Encoding') == 'gzip':
                body = gzip.decompress(body)
            self.assertIn(b'tailwindcss', body)
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response['X-Frame-Options'], 'DENY')
            offline = Client().get('/static/locator/offline.html')
            self.assertEqual(offline.status_code, 200)
            self.assertIn("frame-ancestors 'none'", offline['Content-Security-Policy'])


@override_settings(GOOGLE_MAPS_API_KEY='test-only-key')
class ProviderRedirectTests(TestCase):
    @patch('ppas.services._reserve_request')
    @patch('ppas.services.requests.post')
    def test_places_redirect_is_not_followed_or_accepted(self, post, reserve):
        post.return_value.status_code = 302
        with self.assertRaises(PlacesUnavailable):
            search_google_places('School')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
