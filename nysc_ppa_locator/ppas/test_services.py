from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .location import assess_place_location
from .services import PlacesUnavailable, search_google_places


@override_settings(
    GOOGLE_MAPS_API_KEY='test-secret-that-must-never-be-logged',
    PLACES_DAILY_REQUEST_LIMIT=1000,
    PLACES_MINUTE_REQUEST_LIMIT=60,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'places-services-tests'}},
)
class PlacesServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def response(self, places=None):
        response = Mock()
        response.json.return_value = {'places': places or []}
        return response

    def assert_failure(self, code, operation):
        with self.assertRaises(PlacesUnavailable) as error:
            operation()
        self.assertEqual(error.exception.code, code)
        return error.exception

    @override_settings(GOOGLE_MAPS_API_KEY='')
    @patch('ppas.services.requests.post')
    def test_missing_configuration_does_not_call_provider_or_look_like_no_match(self, post):
        self.assert_failure('not_configured', lambda: search_google_places('school'))
        post.assert_not_called()

    @patch('ppas.services.requests.post')
    def test_successful_no_match_is_empty_list(self, post):
        post.return_value = self.response()
        self.assertEqual(search_google_places('school'), [])
        post.assert_called_once()

    @patch('ppas.services.requests.post')
    def test_transient_results_include_components_and_are_not_cached(self, post):
        components = [{'longText': 'Lagos', 'types': ['administrative_area_level_1']}]
        place = {'id': 'ChIJ-test', 'displayName': {'text': 'School'},
                 'formattedAddress': 'Street', 'location': {'latitude': 6, 'longitude': 3},
                 'addressComponents': components}
        post.return_value = self.response([place] * 25)
        for _ in range(2):
            result = search_google_places('school', 'Lagos', 'Ikeja')
            self.assertEqual(len(result), 20)
            self.assertEqual(result[0]['address_components'], components)
            self.assertEqual(result[0]['place_id'], 'ChIJ-test')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['timeout'], (3.05, 8))
        self.assertEqual(post.call_args.kwargs['json']['pageSize'], 20)
        self.assertIn('places.addressComponents', post.call_args.kwargs['headers']['X-Goog-FieldMask'])

    @patch('ppas.services.requests.post')
    def test_provider_attributions_keep_text_and_only_safe_https_links(self, post):
        post.return_value = self.response([{
            'id': 'ChIJ-test', 'displayName': {'text': 'School'},
            'attributions': [
                {'provider': '<script>bad</script>', 'providerUri': 'javascript:alert(1)'},
                {'provider': 'HTTP only', 'providerUri': 'http://provider.example/'},
                {'provider': 'Credentials', 'providerUri': 'https://user:password@provider.example/'},
                {'providerDisplayName': 'Safe provider', 'providerUri': 'https://provider.example/credits'},
                {'provider': 'Invalid port', 'providerUri': 'https://provider.example:bad/'},
                {'provider': 'Newline', 'providerUri': 'https://provider.example/\nmalicious'},
            ],
        }])
        credits = search_google_places('school')[0]['attributions']
        self.assertEqual([credit['provider_uri'] for credit in credits], ['', '', '', 'https://provider.example/credits', '', ''])
        from django.template import Context, Template
        rendered = Template('{% for credit in credits %}{{ credit.provider }}{% endfor %}').render(Context({'credits': credits}))
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('places.attributions', post.call_args.kwargs['headers']['X-Goog-FieldMask'])

    @patch('ppas.services.requests.post', side_effect=requests.Timeout('contains secret url'))
    def test_timeout_is_separate_and_logs_no_sensitive_exception(self, post):
        with self.assertLogs('ppas.services', level='WARNING') as logs:
            error = self.assert_failure('timeout', lambda: search_google_places('school'))
        self.assertNotIn('contains secret url', '\n'.join(logs.output))
        self.assertNotIn('test-secret', '\n'.join(logs.output))
        self.assertNotIn('contains secret url', error.message)

    @patch('ppas.services.requests.post')
    def test_provider_quota_and_malformed_response_are_not_no_matches(self, post):
        response = self.response()
        error_response = requests.Response()
        error_response.status_code = 429
        response.raise_for_status.side_effect = requests.HTTPError('secret', response=error_response)
        post.return_value = response
        self.assert_failure('provider_quota', lambda: search_google_places('school'))
        post.return_value = self.response()
        post.return_value.json.return_value = {'places': 'unexpected string'}
        self.assert_failure('invalid_response', lambda: search_google_places('school'))

    @override_settings(PLACES_DAILY_REQUEST_LIMIT=1)
    @patch('ppas.services.requests.post')
    def test_failed_outbound_attempt_still_consumes_budget(self, post):
        post.side_effect = requests.Timeout
        self.assert_failure('timeout', lambda: search_google_places('school'))
        self.assert_failure('daily_budget_exhausted', lambda: search_google_places('school'))
        self.assertEqual(post.call_count, 1)

    @override_settings(PLACES_DAILY_REQUEST_LIMIT=1, PLACES_MINUTE_REQUEST_LIMIT=2)
    @patch('ppas.services.requests.post')
    def test_daily_rejections_release_minute_reservation(self, post):
        post.return_value = self.response()
        search_google_places('school')
        self.assert_failure('daily_budget_exhausted', lambda: search_google_places('school'))
        with override_settings(PLACES_DAILY_REQUEST_LIMIT=2):
            search_google_places('school')
        self.assertEqual(post.call_count, 2)

    @override_settings(PLACES_MINUTE_REQUEST_LIMIT=3)
    def test_concurrent_requests_cannot_exceed_shared_minute_budget(self):
        lock = Lock()
        attempts = []

        def provider(*args, **kwargs):
            with lock:
                attempts.append(1)
            return self.response()

        def search(_):
            try:
                search_google_places('school')
                return 'accepted'
            except PlacesUnavailable as exc:
                return exc.code

        with patch('ppas.services.requests.post', side_effect=provider), ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(search, range(20)))
        self.assertEqual(outcomes.count('accepted'), 3)
        self.assertEqual(outcomes.count('minute_budget_exhausted'), 17)
        self.assertEqual(len(attempts), 3)

    @patch('ppas.services.requests.post')
    @patch('ppas.services.cache.add', side_effect=ConnectionError('secret cache URL'))
    def test_cache_failure_prevents_outbound_call(self, add, post):
        self.assert_failure('budget_unavailable', lambda: search_google_places('school'))
        post.assert_not_called()

    @patch('ppas.services.requests.post')
    @patch('ppas.services.cache.incr', side_effect=ValueError('counter expired'))
    @patch('ppas.services.cache.add', return_value=False)
    def test_uncertain_counter_fails_closed_instead_of_resetting(self, add, incr, post):
        self.assert_failure('budget_unavailable', lambda: search_google_places('school'))
        post.assert_not_called()

    @override_settings(PLACES_DAILY_REQUEST_LIMIT=5)
    @patch('ppas.services.requests.post')
    def test_budget_threshold_and_exhaustion_emit_sanitized_alerts(self, post):
        post.return_value = self.response()
        with self.assertLogs('ppas.services', level='WARNING') as logs:
            for _ in range(5):
                search_google_places('school')
            self.assert_failure('daily_budget_exhausted', lambda: search_google_places('school'))
        output = '\n'.join(logs.output)
        self.assertIn('80 percent', output)
        self.assertIn('budget exhausted', output)
        self.assertNotIn('test-secret', output)


class AddressAssessmentTests(SimpleTestCase):
    def place(self, state='Lagos State', lga='Ikeja Local Government Area'):
        return {'address_components': [
            {'longText': state, 'types': ['administrative_area_level_1']},
            {'longText': lga, 'types': ['administrative_area_level_2']},
        ]}

    def test_explicit_matching_administrative_labels_are_address_checked(self):
        status, message = assess_place_location(self.place(), 'Lagos', 'Ikeja')
        self.assertEqual(status, 'address_checked')
        self.assertIn('exact PPA branch', message)

    def test_wrong_state_or_lga_are_flagged(self):
        self.assertEqual(assess_place_location(self.place(state='Ogun'), 'Lagos', 'Ikeja')[0], 'mismatch')
        self.assertEqual(assess_place_location(self.place(lga='Alimosho'), 'Lagos', 'Ikeja')[0], 'mismatch')

    def test_explicit_foreign_country_is_flagged(self):
        place = self.place()
        place['address_components'].append({'longText': 'Portugal', 'shortText': 'PT', 'types': ['country']})
        self.assertEqual(assess_place_location(place, 'Lagos', 'Ikeja')[0], 'mismatch')

    def test_fct_and_area_council_aliases_normalise(self):
        self.assertEqual(assess_place_location(self.place(state='FCT', lga='Abuja Municipal Area Council'),
                                              'Federal Capital Territory (Abuja)', 'Abuja Municipal')[0], 'address_checked')
        self.assertEqual(assess_place_location(self.place(lga='Ikeja L.G.A.'), 'Lagos State', 'Ikeja')[0], 'address_checked')

    def test_town_names_are_neither_lga_match_nor_mismatch_evidence(self):
        place = {'address_components': [
            {'longText': 'Lagos', 'types': ['administrative_area_level_1']},
            {'longText': 'Ojodu', 'types': ['locality']},
        ], 'address': 'Ikeja LGA, Lagos'}
        self.assertEqual(assess_place_location(place, 'Lagos', 'Ikeja')[0], 'unconfirmed')
        self.assertEqual(assess_place_location(place, 'Lagos', 'Ojodu')[0], 'unconfirmed')

    def test_malformed_component_types_cannot_raise_or_prove_location(self):
        place = {'address_components': [{'longText': 'Lagos', 'types': 1}, None]}
        self.assertEqual(assess_place_location(place, 'Lagos', 'Ikeja')[0], 'unconfirmed')

    def test_unstructured_address_and_coordinates_do_not_prove_lga(self):
        place = {'address': 'Ikeja, Lagos', 'latitude': 6.5, 'longitude': 3.3}
        self.assertEqual(assess_place_location(place, 'Lagos', 'Ikeja')[0], 'unconfirmed')
