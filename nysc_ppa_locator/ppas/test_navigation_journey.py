import time
from unittest.mock import patch
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.core.cache import cache
from locations.models import State, LGA
from .models import PPA


class NavigationJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.ppa = PPA.objects.create(name='School on Posting Letter', state=cls.state, lga=cls.lga, address='Member known address', source='community', place_id='ChIJ-local')

    def setUp(self):
        cache.clear()

    def area(self, **extra):
        return {'state': self.state.pk, 'lga': self.lga.pk, **extra}

    def preview_token(self, **extra):
        return signing.dumps({'place': {'name': 'Transient provider school', 'address': 'Transient provider address', 'place_id': 'ChIJ-new', 'address_components': []}, 'state': self.state.pk, 'lga': self.lga.pk, 'query': 'My school', **extra}, salt='place-preview', compress=True)

    @patch('ppas.views.search_google_places', return_value=[])
    def test_submitted_search_uses_provider_only_after_no_local_match(self, provider):
        response = self.client.get('/search/', self.area(q='Posting Letter', find='1'))
        self.assertContains(response, self.ppa.name)
        provider.assert_not_called()
        self.client.get('/search/', self.area(q='No saved name', find='1'))
        provider.assert_called_once_with('No saved name', self.state.name, self.lga.name)

    @patch('ppas.views.search_google_places', return_value=[])
    def test_paging_and_missing_location_do_not_repeat_paid_discovery(self, provider):
        self.client.get('/search/', self.area(q='Missing', find='1', page='2'))
        self.client.get('/search/', {'q': 'Missing', 'find': '1'})
        provider.assert_not_called()

    @override_settings(GOOGLE_MAPS_API_KEY='private-server-only', GOOGLE_ROUTES_API_KEY='private-route-only', GOOGLE_MAPS_BROWSER_API_KEY='public-browser-key')
    def test_detail_config_contains_only_browser_key_and_no_loaded_frame(self):
        response = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertContains(response, 'Get directions')
        self.assertNotContains(response, '<iframe')
        self.assertNotContains(response, 'private-server-only')
        self.assertNotContains(response, 'private-route-only')
        config = response.context['navigation_config']
        self.assertEqual(config['destination'], 'place_id:ChIJ-local')
        self.assertEqual(config['browser_key'], 'public-browser-key')
        self.assertEqual(config['ppa_id'], self.ppa.pk)
        self.assertContains(response, 'Start live navigation')
        self.assertContains(response, 'Opens Google Maps')

    def test_preview_is_guest_accessible_transient_and_no_store(self):
        token = self.preview_token()
        before = PPA.objects.count()
        response = self.client.post('/places/preview/', {'preview_token': token})
        self.assertContains(response, 'Transient provider school')
        self.assertContains(response, 'Get directions')
        self.assertNotContains(response, 'name="place_token"')
        self.assertEqual(response.context['navigation_config']['preview_token'], token)
        self.assertEqual(PPA.objects.count(), before)
        self.assertIn('no-store', response['Cache-Control'])

    def test_preview_rejects_forged_expired_or_malformed_selection(self):
        now = time.time()
        with patch('django.core.signing.time.time', return_value=now-901):
            expired = self.preview_token()
        for token in ('bad-token', expired, self.preview_token(place=[])):
            self.assertEqual(self.client.post('/places/preview/', {'preview_token': token}).status_code, 400)
        self.assertEqual(self.client.get('/places/preview/').status_code, 405)

    def test_preview_requires_csrf_even_for_guests(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post('/places/preview/', {'preview_token': self.preview_token()}).status_code, 403)

    def test_destination_name_is_escaped_in_markup_and_route_json(self):
        self.ppa.name = '</script><script>alert(1)</script>'
        self.ppa.save(update_fields=['name'])
        response = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '\\u003C/script\\u003E')
