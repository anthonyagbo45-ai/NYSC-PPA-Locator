import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.test import SimpleTestCase

from .configuration import private_photo_configuration
from .storage import PrivateFileSystemStorage, PrivateS3PhotoStorage


class PrivatePhotoConfigurationTests(SimpleTestCase):
    def build(self, env=None, *, debug=False):
        return private_photo_configuration(debug=debug, base_dir=Path('/app/project'), environ=env or {})

    def test_production_photos_start_disabled_without_storage_credentials(self):
        enabled, storage = self.build()
        self.assertFalse(enabled)
        self.assertEqual(storage['BACKEND'], 'photos.storage.PrivateS3PhotoStorage')

    def test_production_cannot_enable_ephemeral_filesystem(self):
        with self.assertRaises(ImproperlyConfigured):
            self.build({'PPA_PHOTOS_ENABLED': 'True', 'PPA_PHOTO_STORAGE_BACKEND': 'filesystem'})

    def test_enabling_object_storage_requires_a_bucket(self):
        with self.assertRaises(ImproperlyConfigured):
            self.build({'PPA_PHOTOS_ENABLED': 'True'})

    def test_https_endpoints_cannot_contain_credentials_or_query_strings(self):
        for endpoint in ('http://store.example.test', 'https://user:password@store.example.test',
                         'https://store.example.test?access_token=value', 'https://store.example.test/#fragment',
                         'https://store.example.test:invalid', 'https://store.example.test/\nvalue'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ImproperlyConfigured):
                self.build({'PPA_PHOTO_S3_ENDPOINT_URL': endpoint})

    def test_incomplete_storage_credentials_are_rejected_without_echoing_them(self):
        secret = 'synthetic-credential-must-not-appear-in-error'
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.build({'PPA_PHOTO_S3_SECRET_ACCESS_KEY': secret})
        self.assertNotIn(secret, str(caught.exception))

    def test_private_storage_uses_verified_tls_and_bounded_provider_timeouts(self):
        enabled, storage = self.build({'PPA_PHOTOS_ENABLED': 'True', 'PPA_PHOTO_BUCKET': 'private-test-bucket'})
        self.assertTrue(enabled)
        options = storage['OPTIONS']
        self.assertIsNone(options['default_acl'])
        self.assertIsNone(options['custom_domain'])
        self.assertTrue(options['verify'])
        self.assertTrue(options['use_ssl'])
        self.assertEqual(options['client_config'].connect_timeout, 5)
        self.assertEqual(options['client_config'].read_timeout, 10)
        self.assertEqual(options['object_parameters']['CacheControl'], 'private, no-store')

    def test_local_storage_cannot_use_relative_or_static_directories(self):
        for root in ('relative/private', '/app/project/static/photos', '/app/project/staticfiles', '/app/static/photos'):
            with self.subTest(root=root), self.assertRaises(ImproperlyConfigured):
                self.build({'PPA_PHOTO_STORAGE_ROOT': root}, debug=True)

    def test_invalid_feature_flags_and_backends_fail_closed(self):
        for env in ({'PPA_PHOTOS_ENABLED': 'yes'}, {'PPA_PHOTO_STORAGE_BACKEND': 'public'}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                self.build(env)

    def test_development_objects_are_private_files_without_direct_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config = self.build({'PPA_PHOTO_STORAGE_ROOT': directory}, debug=True)
            storage = PrivateFileSystemStorage(**config['OPTIONS'])
            name = storage.save('conversation-photos/example.jpg', ContentFile(b'synthetic-object'))
            path = Path(storage.path(name))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(storage.open(name).read(), b'synthetic-object')
            with self.assertRaises(ValueError):
                storage.url(name)

    def test_object_storage_does_not_generate_presigned_or_public_urls(self):
        storage = PrivateS3PhotoStorage(bucket_name='private-test-bucket', access_key='synthetic-key', secret_key='synthetic-secret')
        with self.assertRaises(ValueError):
            storage.url('conversation-photos/example.jpg')
