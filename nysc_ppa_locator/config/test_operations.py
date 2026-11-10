import json
from io import StringIO
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from locations.models import LGA, State
from ppas.models import PPA


class PublicOperationsTests(TestCase):
    def test_database_failure_has_sanitized_public_503(self):
        with patch('config.public_views.connection.cursor', side_effect=DatabaseError('private-db-host and secret password')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertNotContains(response, 'private-db-host', status_code=503)
        self.assertNotContains(response, 'password', status_code=503)

    def test_cache_failure_has_sanitized_public_503(self):
        with patch('config.public_views.connection.cursor'), patch('config.public_views.cache.set', side_effect=ConnectionError('redis://private:secret@host')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertNotContains(response, 'redis', status_code=503)
        self.assertNotContains(response, 'secret', status_code=503)

    def test_unusable_cache_returns_503_and_health_is_get_only(self):
        with patch('config.public_views.connection.cursor'), patch('config.public_views.cache.set'), patch('config.public_views.cache.get', return_value=None):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertEqual(self.client.post(reverse('health')).status_code, 405)

    def test_service_worker_response_scope_and_update_headers(self):
        response = self.client.get(reverse('service_worker'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Service-Worker-Allowed'], '/')
        self.assertEqual(response['Cache-Control'], 'no-cache')
        self.assertIn('javascript', response['Content-Type'])

    def test_service_worker_caches_only_shell_assets(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Install Node.js to execute the service worker behavior checks (CI uses Node 22).')
        root = Path(settings.BASE_DIR)
        result = subprocess.run(
            [node, str(root / 'config' / 'service_worker_harness.js'), str(root / 'static' / 'locator' / 'service-worker.js')],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('shell-only caching checks passed', result.stdout)


class DirectoryOperationsTests(TestCase):
    def test_health_reads_actual_database_and_shared_cache(self):
        response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_directory_audit_is_counts_only_read_only_and_detects_candidates(self):
        state = State.objects.create(name='Private area name')
        lga = LGA.objects.create(name='Private LGA', state=state)
        for name in ('Private Organisation', ' private organisation '):
            PPA.objects.create(name=name, address='Unpublished address', state=state, lga=lga)
        PPA.objects.create(name='Different School', address='Another address', state=state, lga=lga, source='community', status='member_confirmed')
        PPA.objects.create(name='An old import', address='Another address', state=state, lga=lga, source='Private imported value')
        before = list(PPA.objects.order_by('pk').values())
        output = StringIO()
        call_command('audit_directory', as_json=True, stdout=output)
        counts = json.loads(output.getvalue())
        self.assertEqual(counts['total_places'], 4)
        self.assertEqual(counts['by_source'], {'community': 1, 'legacy': 2, 'unknown': 1})
        self.assertEqual(counts['by_status'], {'discovered': 3, 'member_confirmed': 1})
        self.assertEqual(counts['by_location_status'], {'unconfirmed': 4})
        self.assertEqual(counts['duplicate_name_location_groups'], 1)
        self.assertEqual(counts['duplicate_name_location_records'], 2)
        self.assertNotIn('Private', output.getvalue())
        self.assertNotIn('address', output.getvalue())
        self.assertEqual(before, list(PPA.objects.order_by('pk').values()))
