"""Route safety checks; every provider request is mocked."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests
from django.core import signing
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import include, path

from locations.models import LGA, State
from .models import PPA
from .navigation import destination_data, directions_url, embed_directions_url
from .routing import RouteUnavailable, compute_route, destination_waypoint

urlpatterns = [path('', include('ppas.route_urls'))]
TEST_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'routes-tests'}}


class NavigationDestinationTests(SimpleTestCase):
    def test_place_id_is_preferred_over_coordinates_and_stored_urls(self):
        place = {'name': 'School', 'place_id': 'ChIJ-school', 'latitude': 6.5, 'longitude': 3.3,
                 'google_maps_url': 'https://attacker.invalid/steal'}
        data = destination_data(place)
        self.assertEqual(data['map_destination'], 'place_id:ChIJ-school')
        url = directions_url(place)
        self.assertEqual(urlparse(url).netloc, 'www.google.com')
        self.assertNotIn('attacker', url)
        self.assertEqual(parse_qs(urlparse(url).query)['destination_place_id'], ['ChIJ-school'])
        self.assertEqual(destination_waypoint(place), {'placeId': 'ChIJ-school'})

    def test_coordinate_or_complete_address_fallback(self):
        place = {'name': 'School', 'address': '21 PPA Road', 'state': 'Lagos', 'lga': 'Ikeja',
                 'latitude': 6.5, 'longitude': 3.3}
        self.assertEqual(destination_data(place)['map_destination'], '6.5000000,3.3000000')
        self.assertEqual(destination_waypoint(place), {'location': {'latLng': {'latitude': 6.5, 'longitude': 3.3}}})
        place['latitude'] = float('nan')
        data = destination_data(place)
        self.assertIsNone(data['latitude'])
        self.assertEqual(data['map_destination'], 'School, 21 PPA Road, Ikeja, Lagos, Nigeria')
        self.assertEqual(destination_waypoint(place), {'address': data['full_address']})

    def test_embed_has_fixed_origin_and_encodes_query_injection(self):
        url = embed_directions_url({'name': 'School', 'place_id': 'ChIJ-school'}, 'public-browser-key',
                                   'Lagos &key=attacker&destination=elsewhere', mode='walking')
        self.assertEqual(urlparse(url).netloc, 'www.google.com')
        self.assertEqual(urlparse(url).path, '/maps/embed/v1/directions')
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params['key'], ['public-browser-key'])
        self.assertEqual(params['destination'], ['place_id:ChIJ-school'])
        self.assertEqual(params['mode'], ['walking'])
        self.assertEqual(params['origin'], ['Lagos &key=attacker&destination=elsewhere'])
        coords = parse_qs(urlparse(embed_directions_url({'name': 'School'}, 'public', {'latitude': 6, 'longitude': 3})).query)
        self.assertEqual(coords['origin'], ['6.0000000,3.0000000'])

    def test_embed_rejects_invalid_origins_configuration_and_mode(self):
        for origin in ('', 'x' * 251, 'line\nbreak', 'DEL\x7fcharacter', {'latitude': 91, 'longitude': 3}, {'latitude': True, 'longitude': 3}):
            with self.subTest(origin=origin):
                self.assertEqual(embed_directions_url({'name': 'School'}, 'public', origin), '')
        self.assertEqual(embed_directions_url({'name': 'School'}, '', 'Lagos'), '')
        self.assertEqual(embed_directions_url({'name': 'School'}, 'public', 'Lagos', mode=[]), '')
        self.assertNotIn('destination_place_id', directions_url({'place_id': 'id&key=bad', 'name': 'School'}))


@override_settings(GOOGLE_ROUTES_API_KEY='secret-route-key', GOOGLE_MAPS_API_KEY='',
                   ROUTES_MINUTE_REQUEST_LIMIT=100, ROUTES_DAILY_REQUEST_LIMIT=1000, CACHES=TEST_CACHE)
class RoutingServiceTests(SimpleTestCase):
    place = {'name': 'Private school name', 'place_id': 'ChIJ-school'}
    origin = {'address': 'Private starting address'}

    def setUp(self):
        cache.clear()

    def response(self, payload=None):
        response = Mock()
        response.json.return_value = payload if payload is not None else {'routes': [{'distanceMeters': 1240, 'duration': '99.2s'}]}
        return response

    def assert_failure(self, code, operation=None):
        with self.assertRaises(RouteUnavailable) as error:
            (operation or (lambda: compute_route(self.place, self.origin)))()
        self.assertEqual(error.exception.code, code)
        return error.exception

    @patch('ppas.routing.requests.post')
    def test_request_has_only_needed_fields_fixed_endpoint_and_server_key_header(self, post):
        post.return_value = self.response()
        result = compute_route(self.place, {'latitude': 6.5, 'longitude': 3.3})
        self.assertEqual(result, {'status': 'ok', 'distance_meters': 1240, 'duration_seconds': 100})
        self.assertEqual(post.call_args.args, ('https://routes.googleapis.com/directions/v2:computeRoutes',))
        self.assertEqual(post.call_args.kwargs['headers'], {'X-Goog-Api-Key': 'secret-route-key', 'X-Goog-FieldMask': 'routes.distanceMeters,routes.duration'})
        self.assertEqual(post.call_args.kwargs['json'], {
            'origin': {'location': {'latLng': {'latitude': 6.5, 'longitude': 3.3}}},
            'destination': {'placeId': 'ChIJ-school'}, 'travelMode': 'DRIVE',
            'computeAlternativeRoutes': False, 'units': 'METRIC'})
        self.assertEqual(post.call_args.kwargs['timeout'], (3.05, 8))
        self.assertIs(post.call_args.kwargs['allow_redirects'], False)
        self.assertNotIn('secret-route-key', str(result))

    @override_settings(GOOGLE_ROUTES_API_KEY='', GOOGLE_MAPS_API_KEY='fallback-server-key')
    @patch('ppas.routing.requests.post')
    def test_address_walking_request_and_existing_server_key_fallback(self, post):
        post.return_value = self.response()
        compute_route(self.place, {'address': '  Ikeja &destination=other  '}, mode='walking')
        self.assertEqual(post.call_args.kwargs['json']['origin'], {'address': 'Ikeja &destination=other'})
        self.assertEqual(post.call_args.kwargs['json']['travelMode'], 'WALK')
        self.assertEqual(post.call_args.kwargs['headers']['X-Goog-Api-Key'], 'fallback-server-key')

    @override_settings(GOOGLE_ROUTES_API_KEY='', GOOGLE_MAPS_API_KEY='')
    @patch('ppas.routing.requests.post')
    def test_missing_key_cannot_spend_budget_or_send_request(self, post):
        with patch('ppas.routing._reserve_request') as budget:
            self.assert_failure('not_configured')
        budget.assert_not_called()
        post.assert_not_called()

    @patch('ppas.routing.requests.post')
    def test_invalid_origin_and_mode_cannot_spend_provider_requests(self, post):
        for origin in (None, 'address', {'latitude': 91, 'longitude': 0}, {'latitude': 0, 'longitude': -181},
                       {'latitude': float('nan'), 'longitude': 0}, {'latitude': 0, 'longitude': float('inf')},
                       {'latitude': True, 'longitude': 0}, {'latitude': 1}, {'address': ''}, {'address': 'x' * 251},
                       {'address': 'line\nbreak'}, {'address': 'DEL\x7fcharacter'}, {'address': 'Lagos', 'url': 'http://127.0.0.1'},
                       {'latitude': 0, 'longitude': 0, 'address': 'Lagos'}):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                compute_route(self.place, origin)
        for mode in ('bicycling', 'transit', None, [], True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                compute_route(self.place, self.origin, mode)
        post.assert_not_called()

    @patch('ppas.routing.requests.post')
    def test_successful_no_route_is_distinct_from_failure(self, post):
        post.return_value = self.response({'routes': []})
        error = self.assert_failure('no_route')
        self.assertEqual(error.status, 'no_route')
        self.assertEqual(error.http_status, 200)

    @patch('ppas.routing.requests.post')
    def test_malformed_response_is_never_forwarded_to_client(self, post):
        for payload in ([], {'error': 'secret-provider-message'}, {'routes': 'invalid'}, {'routes': [None]},
                        {'routes': [{'distanceMeters': True, 'duration': '1s'}]},
                        {'routes': [{'distanceMeters': -1, 'duration': '1s'}]},
                        {'routes': [{'distanceMeters': 10, 'duration': 'inf'}]},
                        {'routes': [{'distanceMeters': 10, 'duration': '999999999999999999999999s'}]}):
            post.return_value = self.response(payload)
            with self.subTest(payload=payload):
                error = self.assert_failure('invalid_response')
                self.assertNotIn('secret-provider-message', error.message)

    @patch('ppas.routing.requests.post', side_effect=requests.Timeout('secret-route-key Private starting address'))
    def test_timeout_has_sanitized_log_and_response(self, post):
        with self.assertLogs('ppas.routing', level='WARNING') as logs:
            error = self.assert_failure('timeout')
        output = '\n'.join(logs.output) + error.message
        for secret in ('secret-route-key', 'Private starting address', 'Private school name'):
            self.assertNotIn(secret, output)

    @patch('ppas.routing.requests.post')
    def test_provider_quota_network_error_and_bad_json(self, post):
        response = self.response()
        provider_response = requests.Response()
        provider_response.status_code = 429
        response.raise_for_status.side_effect = requests.HTTPError('secret-provider-message', response=provider_response)
        post.return_value = response
        self.assert_failure('provider_quota')
        post.side_effect = requests.ConnectionError('secret-provider-message')
        self.assert_failure('provider_unavailable')
        post.side_effect = None
        post.return_value = self.response()
        post.return_value.json.side_effect = ValueError('secret-provider-message')
        self.assert_failure('invalid_response')

    @override_settings(ROUTES_DAILY_REQUEST_LIMIT=1)
    @patch('ppas.routing.requests.post', side_effect=requests.Timeout)
    def test_failed_outbound_attempt_consumes_daily_budget(self, post):
        self.assert_failure('timeout')
        self.assert_failure('daily_budget_exhausted')
        self.assertEqual(post.call_count, 1)

    @override_settings(ROUTES_MINUTE_REQUEST_LIMIT=3)
    @patch('ppas.routing.requests.post')
    def test_concurrent_requests_cannot_exceed_shared_counter(self, post):
        post.return_value = self.response()

        def attempt(_):
            try:
                compute_route(self.place, self.origin)
                return 'ok'
            except RouteUnavailable as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(attempt, range(20)))
        self.assertEqual(outcomes.count('ok'), 3)
        self.assertEqual(outcomes.count('minute_budget_exhausted'), 17)
        self.assertEqual(post.call_count, 3)

    @override_settings(ROUTES_DAILY_REQUEST_LIMIT=1, ROUTES_MINUTE_REQUEST_LIMIT=2)
    @patch('ppas.routing.requests.post')
    def test_daily_rejection_returns_minute_reservation(self, post):
        post.return_value = self.response()
        compute_route(self.place, self.origin)
        self.assert_failure('daily_budget_exhausted')
        with override_settings(ROUTES_DAILY_REQUEST_LIMIT=2):
            compute_route(self.place, self.origin)
        self.assertEqual(post.call_count, 2)

    @patch('ppas.routing.requests.post')
    @patch('ppas.routing.cache.add', side_effect=ConnectionError('secret-cache-url'))
    def test_cache_failure_fails_closed_and_does_not_log_exception(self, add, post):
        with self.assertLogs('ppas.routing', level='WARNING') as logs:
            error = self.assert_failure('budget_unavailable')
        post.assert_not_called()
        self.assertNotIn('secret-cache-url', error.message + '\n'.join(logs.output))

    @patch('ppas.routing.requests.post')
    @patch('ppas.routing.cache.incr', side_effect=ValueError('expired counter'))
    @patch('ppas.routing.cache.add', return_value=False)
    def test_uncertain_counter_is_not_reset(self, add, incr, post):
        self.assert_failure('budget_unavailable')
        post.assert_not_called()

    @override_settings(ROUTES_MINUTE_REQUEST_LIMIT=0)
    @patch('ppas.routing.requests.post')
    def test_disabled_budget_is_configuration_error(self, post):
        self.assert_failure('not_configured')
        post.assert_not_called()


@override_settings(ROOT_URLCONF=__name__, CACHES=TEST_CACHE)
class RouteEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.ppa = PPA.objects.create(name='Community School', address='21 PPA Road', state=cls.state, lga=cls.lga,
                                     place_id='ChIJ-school', google_maps_url='http://127.0.0.1/private')

    def setUp(self):
        cache.clear()

    def payload(self, **extra):
        return {'ppa_id': self.ppa.pk, 'origin': {'address': 'Private starting address'}, 'mode': 'driving', **extra}

    def post(self, payload):
        return self.client.post('/api/route/', payload, content_type='application/json')

    @patch('ppas.route_views.compute_route')
    def test_guest_selects_local_destination_and_response_is_not_stored(self, compute):
        compute.return_value = {'status': 'ok', 'distance_meters': 1240, 'duration_seconds': 99}
        original = dict(PPA.objects.values().get(pk=self.ppa.pk))
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), compute.return_value)
        self.assertIn('no-store', response['Cache-Control'])
        args = compute.call_args.args
        self.assertEqual(args[0].pk, self.ppa.pk)
        self.assertEqual(args[1], {'address': 'Private starting address'})
        self.assertEqual(PPA.objects.count(), 1)
        self.assertEqual(PPA.objects.values().get(pk=self.ppa.pk), original)
        self.assertNotIn('Private starting address', response.content.decode())

    @patch('ppas.route_views.compute_route')
    def test_signed_preview_allows_guest_routes_without_creating_record(self, compute):
        place = {'name': 'Transient result', 'place_id': 'ChIJ-preview', 'address': 'Provider address'}
        token = signing.dumps({'place': place, 'state': self.state.pk, 'lga': self.lga.pk}, salt='place-preview')
        compute.return_value = {'status': 'ok', 'distance_meters': 12, 'duration_seconds': 10}
        response = self.post({'preview_token': token, 'origin': {'latitude': 6.5, 'longitude': 3.3}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(compute.call_args.args[0], place)
        self.assertEqual(PPA.objects.count(), 1)
        self.assertNotIn(token, response.content.decode())

    @patch('ppas.route_views.compute_route')
    def test_invalid_expired_and_wrong_salt_tokens_rejected(self, compute):
        for token in ('broken', signing.dumps({'place': {}}, salt='wrong'),
                      signing.dumps({'place': []}, salt='place-preview'), signing.dumps({}, salt='place-preview')):
            with self.subTest(token=token):
                response = self.post({'preview_token': token, 'origin': {'address': 'Lagos'}})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['code'], 'invalid_selection')
        token = signing.dumps({'place': {'name': 'School'}}, salt='place-preview')
        with patch('django.core.signing.time.time', return_value=signing.time.time() + 901):
            self.assertEqual(self.post({'preview_token': token, 'origin': {'address': 'Lagos'}}).status_code, 400)
        compute.assert_not_called()

    @patch('ppas.route_views.compute_route')
    def test_only_one_valid_destination_selector_allowed(self, compute):
        for payload in ({'origin': {}}, self.payload(preview_token='extra'), self.payload(ppa_id=True),
                        self.payload(ppa_id='1'), self.payload(ppa_id=-1), self.payload(ppa_id=10**30), []):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.post(self.payload(ppa_id=999999)).status_code, 404)
        compute.assert_not_called()

    @patch('ppas.route_views.compute_route')
    def test_post_json_and_csrf_are_required(self, compute):
        self.assertEqual(self.client.get('/api/route/').status_code, 405)
        self.assertEqual(self.client.post('/api/route/', self.payload()).status_code, 415)
        self.assertEqual(self.client.post('/api/route/', '{broken', content_type='application/json').status_code, 400)
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/api/route/', self.payload(), content_type='application/json').status_code, 403)
        compute.assert_not_called()

    @patch('ppas.route_views.compute_route')
    def test_oversized_or_deeply_nested_json_is_rejected(self, compute):
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=100):
            response = self.post(self.payload(origin={'address': 'x' * 250}))
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/route/', '[' * 2000 + ']' * 2000, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        compute.assert_not_called()

    @patch('ppas.route_views.compute_route')
    def test_safe_error_contract_and_retry_header(self, compute):
        for code, status, http_status in (('not_configured', 'unavailable', 503), ('timeout', 'unavailable', 503),
                                         ('no_route', 'no_route', 200), ('daily_budget_exhausted', 'unavailable', 429)):
            compute.side_effect = RouteUnavailable(code, 'Safe public message', status=status, http_status=http_status)
            response = self.post(self.payload())
            self.assertEqual(response.status_code, http_status)
            self.assertEqual(response.json(), {'status': status, 'code': code, 'message': 'Safe public message'})
            self.assertIn('no-store', response['Cache-Control'])
            if http_status == 429:
                self.assertEqual(response['Retry-After'], '60')
        compute.side_effect = ValueError('Choose a valid starting point.')
        self.assertEqual(self.post(self.payload()).status_code, 400)

    @patch('ppas.route_views.compute_route')
    def test_ip_abuse_limit_ignores_forwarded_ip(self, compute):
        compute.return_value = {'status': 'ok', 'distance_meters': 1, 'duration_seconds': 1}
        for index in range(20):
            response = self.client.post('/api/route/', self.payload(), content_type='application/json', HTTP_X_FORWARDED_FOR=f'10.0.0.{index}')
            self.assertEqual(response.status_code, 200)
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'client_limit')
        self.assertEqual(compute.call_count, 20)
        self.assertEqual(response['Retry-After'], '300')

    @patch('ppas.route_views.compute_route')
    @patch('config.security.cache.add', side_effect=ConnectionError('secret-cache-url'))
    def test_abuse_cache_failure_is_safe_json_without_outbound_request(self, add, compute):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'budget_unavailable')
        self.assertNotIn('secret-cache-url', response.content.decode())
        compute.assert_not_called()
