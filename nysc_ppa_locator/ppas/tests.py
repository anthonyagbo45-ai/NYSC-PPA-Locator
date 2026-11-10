from unittest.mock import patch
from io import StringIO
import time
from django.core.management import call_command, CommandError
import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core import signing
from django.db import IntegrityError, transaction, connection
from django.test import TestCase, Client, override_settings
from django.test.utils import CaptureQueriesContext
from locations.models import State, LGA
from .models import PPA, Review
from .services import search_google_places, PlacesUnavailable


class QueryAndWriteSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.other = State.objects.create(name='Ogun')
        cls.user = get_user_model().objects.create_user(state_code='LA/TEST', email='test@example.com', full_name='Test', email_verified=True, state_posted=cls.state, lga_posted=cls.lga)
        cls.ppas = PPA.objects.bulk_create([PPA(name=f'School {i:03}', state=cls.state, lga=cls.lga, address='Address') for i in range(55)])

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_search_paginates_and_preserves_filters(self):
        response = self.client.get('/results/', {'q': 'School', 'state': self.state.pk, 'page': 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['ppas']), 25)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertContains(response, 'School 025')
        self.assertContains(response, 'q=School')
        self.assertNotContains(response, 'School 000')

    def test_live_search_allows_guests_and_bounds_saved_list(self):
        response = self.client.get('/live-search/')
        self.assertEqual(len(response.context['saved_ppas']), 25)
        self.client.logout()
        self.assertEqual(self.client.get('/live-search/', {'state': self.state.pk, 'lga': self.lga.pk}).status_code, 200)

    def test_queries_do_not_grow_per_result(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/results/')
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 10)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/live-search/')
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 10)

    def test_malformed_query_ids_return_400(self):
        for value in ('abc', '-1', '9' * 1000, '1 OR 1=1'):
            self.assertEqual(self.client.get('/api/load-lgas/', {'state': value}).status_code, 400)
            self.assertEqual(self.client.get('/results/', {'state': value}).status_code, 400)
        self.assertEqual(self.client.get('/results/', {'q': 'a' * 201}).status_code, 400)
        self.assertEqual(self.client.get('/live-search/', {'state': self.other.pk, 'lga': self.lga.pk}).status_code, 400)

    def test_review_is_validated_upserted_and_database_enforced(self):
        url = f'/ppas/{self.ppas[0].pk}/'
        for rating in (0, 6, 'bad'):
            self.assertEqual(self.client.post(url, {'rating': rating, 'comment': 'Test'}).status_code, 400)
        self.assertEqual(Review.objects.count(), 0)
        for rating in (4, 5):
            self.assertEqual(self.client.post(url, {'rating': rating, 'comment': 'Updated'}).status_code, 302)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Review.objects.get().rating, 5)
        for attrs in ({'user': self.user, 'ppa': self.ppas[0], 'rating': 4}, {'user': self.user, 'ppa': self.ppas[1], 'rating': 9}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Review.objects.create(**attrs, comment='Invalid')

    def test_review_requires_csrf_and_escapes_html(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = f'/ppas/{self.ppas[0].pk}/'
        self.assertEqual(client.post(url, {'rating': 4, 'comment': 'x'}).status_code, 403)
        self.client.post(url, {'rating': 4, 'comment': '<script>alert(1)</script>'})
        response = self.client.get(url)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def place(self, **extra):
        place = {'name': 'Community School', 'address': 'Street', 'state': self.state.pk, 'lga': self.lga.pk,
                'latitude': 6.5, 'longitude': 3.3, 'place_id': 'ChIJ-test', **extra}
        return {**place, 'community_details_confirmed': 'on', 'place_token': signing.dumps({'user_id': self.user.pk, 'place': place}, salt='google-place', compress=True)}

    def test_place_save_never_trusts_verified_or_url_and_is_idempotent(self):
        data = self.place(verified='True', google_maps_url='javascript:alert(1)')
        data['name'] = 'Member-provided PPA name'
        for _ in range(2):
            self.assertEqual(self.client.post('/save-live-place/', data).status_code, 302)
        ppa = PPA.objects.get(place_id='ChIJ-test')
        self.assertFalse(ppa.verified)
        self.assertEqual(ppa.name, 'Member-provided PPA name')
        self.assertEqual(ppa.source, 'community')
        self.assertIsNone(ppa.latitude)
        self.assertIsNone(ppa.longitude)
        self.assertFalse(ppa.google_maps_url)
        self.assertEqual(PPA.objects.filter(place_id='ChIJ-test').count(), 1)

    def test_place_save_rejects_invalid_location_coordinates_and_methods(self):
        for extra in ({'state': self.other.pk}, {'latitude': 'nan'}, {'longitude': 181}, {'name': 'a' * 256}, {'place_id': '<bad>'}):
            self.assertEqual(self.client.post('/save-live-place/', self.place(**extra)).status_code, 400)
        self.assertEqual(self.client.get('/save-live-place/').status_code, 405)

    def test_place_token_rejects_tampering_and_cross_user_replay(self):
        self.assertEqual(self.client.post('/save-live-place/', self.place(place_token='forged') | {'place_token': 'forged'}).status_code, 400)
        data = self.place()
        other = get_user_model().objects.create_user(state_code='OTHER', email='other@example.com', email_verified=True)
        self.client.force_login(other)
        self.assertEqual(self.client.post('/save-live-place/', data).status_code, 400)

    @override_settings(DEBUG=False)
    def test_mock_data_seeding_is_disabled_in_production(self):
        with self.assertRaises(CommandError):
            call_command('seed_ppas', total=1, stdout=StringIO())

    @override_settings(DEBUG=True)
    def test_mock_user_has_no_usable_password(self):
        call_command('seed_ppas', total=1, stdout=StringIO())
        self.assertFalse(get_user_model().objects.get(email='tester@nysc.gov.ng').has_usable_password())

    def test_expired_place_token_requires_new_search(self):
        now = time.time()
        with patch('django.core.signing.time.time', return_value=now - 1801):
            data = self.place()
        self.assertEqual(self.client.post('/save-live-place/', data).status_code, 400)

    def test_model_save_does_not_call_external_services(self):
        with patch('requests.get') as get, patch('requests.post') as post:
            PPA.objects.create(name='Local', state=self.state, lga=self.lga, address='Address')
        get.assert_not_called()
        post.assert_not_called()

    def test_reviews_are_paginated_with_constant_query_count(self):
        users = get_user_model().objects.bulk_create([get_user_model()(state_code=f'TEST/{i}', email=f'test{i}@example.com', full_name='Test') for i in range(30)])
        Review.objects.bulk_create([Review(ppa=self.ppas[0], user=u, rating=4, comment='Test') for u in users])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f'/ppas/{self.ppas[0].pk}/')
        self.assertEqual(len(response.context['reviews']), 20)
        self.assertLessEqual(len(queries), 10)


@override_settings(GOOGLE_MAPS_API_KEY='test-server-key')
class ExternalServiceTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch('ppas.services.requests.post')
    def test_google_search_has_timeout_limit_and_no_content_cache(self, post):
        post.return_value.json.return_value = {'places': [{'id': 'place', 'displayName': {'text': 'School'}, 'location': {'latitude': 6}}]}
        self.assertEqual(search_google_places('school')[0]['name'], 'School')
        self.assertEqual(search_google_places('school')[0]['name'], 'School')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['timeout'], (3.05, 8))
        self.assertEqual(post.call_args.kwargs['json']['pageSize'], 20)

    @patch('ppas.services.requests.post', side_effect=requests.Timeout)
    def test_google_failure_is_bounded(self, post):
        with self.assertRaises(PlacesUnavailable):
            search_google_places('school')

    @override_settings(GOOGLE_MAPS_BROWSER_API_KEY='browser-only-key')
    def test_templates_never_receive_server_api_key(self):
        from config.context_processors import google_maps_api
        self.assertEqual(google_maps_api(None)['GOOGLE_MAPS_API_KEY'], 'browser-only-key')
