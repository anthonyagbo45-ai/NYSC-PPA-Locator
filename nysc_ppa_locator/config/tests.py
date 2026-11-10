import os
import subprocess
import sys
from pathlib import Path
from django.test import SimpleTestCase


class ProductionConfigurationTests(SimpleTestCase):
    def run_settings(self, **changes):
        variables = {**os.environ, 'DEBUG': 'False', 'DJANGO_SECRET_KEY': 'test-configuration-only-' + 'x' * 60,
                     'ALLOWED_HOSTS': 'ppa.example.com', 'POSTGRES_DB': 'ppa', 'POSTGRES_USER': 'ppa', 'POSTGRES_SSLMODE': 'verify-full',
                     'REDIS_URL': 'redis://localhost:6379/0', **changes}
        code = '''from config import settings as s
assert s.SESSION_COOKIE_SECURE and s.CSRF_COOKIE_SECURE
assert s.SESSION_COOKIE_HTTPONLY and s.CSRF_COOKIE_HTTPONLY
assert s.SESSION_COOKIE_SAMESITE == 'Lax'
assert s.SECURE_SSL_REDIRECT and s.SECURE_HSTS_SECONDS > 0
assert s.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql'
assert s.DATABASES['default']['OPTIONS']['sslmode'] == 'verify-full'
assert s.CACHES['default']['BACKEND'] == 'django.core.cache.backends.redis.RedisCache'
'''
        return subprocess.run([sys.executable, '-c', code], env=variables,
                              cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)

    def test_secure_production_configuration(self):
        result = self.run_settings()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_production_configuration_fails_closed(self):
        for changes in ({'DJANGO_SECRET_KEY': 'short'}, {'DJANGO_SECRET_KEY': 'x' * 60}, {'ALLOWED_HOSTS': '*'}, {'ALLOWED_HOSTS': ''},
                        {'POSTGRES_DB': ''}, {'REDIS_URL': ''}):
            result = self.run_settings(**changes)
            self.assertNotEqual(result.returncode, 0, changes)
            self.assertIn('ImproperlyConfigured', result.stderr)
