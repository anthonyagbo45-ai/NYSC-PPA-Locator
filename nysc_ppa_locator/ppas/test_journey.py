from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import TestCase
from locations.models import State, LGA
from .models import PPA, Review
from .navigation import directions_url
from .services import PlacesUnavailable


class DirectoryJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.ikeja = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.epe = LGA.objects.create(name='Epe', state=cls.state)
        cls.ppa = PPA.objects.create(name='Government Secondary School', aliases='GSS, Government High School', state=cls.state, lga=cls.ikeja, address='Member supplied road', source='community', place_id='ChIJ-school')
        cls.other = PPA.objects.create(name='Epe Hospital', state=cls.state, lga=cls.epe, address='Epe road')
        cls.user = get_user_model().objects.create_user(state_code='LA/25A/1234', email='private@example.com', full_name='Private Legal Name', display_name='Helpful Member', email_verified=True, state_posted=cls.state, lga_posted=cls.epe)

    def setUp(self):
        cache.clear()

    def area(self, **extra):
        return {'state': self.state.pk, 'lga': self.ikeja.pk, **extra}

    @patch('ppas.views.search_google_places')
    def test_guest_browses_selected_area_without_google_or_login(self, google):
        response = self.client.get('/', self.area())
        self.assertContains(response, self.ppa.name)
        self.assertNotContains(response, self.other.name)
        self.assertNotContains(response, 'cdn.tailwindcss.com')
        google.assert_not_called()
        self.assertIn("script-src 'self'", response['Content-Security-Policy'])

    def test_selection_overrides_profile_and_profile_defaults_work(self):
        self.client.force_login(self.user)
        response = self.client.get('/search/', self.area())
        self.assertContains(response, self.ppa.name)
        self.assertNotContains(response, self.other.name)
        response = self.client.get('/search/')
        self.assertContains(response, self.other.name)
        self.assertNotContains(response, self.ppa.name)

    def test_alias_and_typo_search_are_scoped_to_lga(self):
        for query in ('GSS', 'Government Secondry School'):
            self.assertContains(self.client.get('/search/', self.area(q=query)), self.ppa.name)
        response = self.client.get('/search/', self.area(q='Government Secondry School'))
        self.assertTrue(response.context['possible_matches'])
        self.assertNotContains(self.client.get('/search/', self.area(q='Government Secondry School', lga=self.epe.pk)), self.ppa.name)

    @patch('ppas.views.search_google_places')
    def test_google_is_explicit_and_empty_differs_from_failure(self, google):
        google.return_value = []
        self.client.get('/search/', self.area(q='Missing school'))
        google.assert_not_called()
        response = self.client.get('/search/', self.area(q='Missing school', google='1'))
        self.assertContains(response, 'No additional matches')
        self.assertIn('no-store', response['Cache-Control'])
        google.side_effect = PlacesUnavailable('timeout', 'Google discovery took too long.')
        response = self.client.get('/search/', self.area(q='Missing school', google='1'))
        self.assertContains(response, 'Google discovery took too long.')
        self.assertNotContains(response, 'No additional matches')

    @patch('ppas.views.search_google_places')
    def test_empty_google_form_cannot_spend_requests(self, google):
        self.assertContains(self.client.get('/search/', {'google': '1'}), 'select a state and LGA')
        google.assert_not_called()

    @patch('ppas.views.search_google_places')
    def test_guest_google_directions_without_selection_token_or_persistence(self, google):
        google.return_value = [{'name': 'Google-only name', 'address': 'Temporary address', 'place_id': 'ChIJ-guest', 'latitude': 6.5, 'longitude': 3.3, 'address_components': [], 'attributions': [{'provider': '<script>credit</script>', 'provider_uri': ''}]}]
        response = self.client.get('/search/', self.area(q='School', google='1'))
        self.assertContains(response, 'name="preview_token"')
        preview = self.client.post('/places/preview/', {'preview_token': response.context['places'][0]['preview_token']})
        self.assertContains(preview, 'Get directions')
        self.assertEqual(preview.context['navigation_config']['destination'], 'place_id:ChIJ-guest')
        self.assertIn('no-store', preview['Cache-Control'])
        self.assertContains(response, '&lt;script&gt;credit&lt;/script&gt;')
        self.assertNotContains(response, 'name="place_token"')
        self.assertFalse(PPA.objects.filter(place_id='ChIJ-guest').exists())

    def test_directions_work_without_coordinates(self):
        self.assertContains(self.client.get(f'/ppas/{self.ppa.pk}/'), 'Get directions')
        query = parse_qs(urlparse(directions_url(self.ppa)).query)
        self.assertEqual(query['destination_place_id'], ['ChIJ-school'])
        self.assertEqual(query['travelmode'], ['driving'])
        self.assertTrue(parse_qs(urlparse(directions_url(self.other)).query)['destination'])

    def test_public_reviews_show_alias_without_private_identity(self):
        Review.objects.create(user=self.user, ppa=self.ppa, rating=4, comment='Helpful landmark')
        response = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertContains(response, 'Helpful Member')
        for private in ('Private Legal Name', 'LA/25A/1234', 'private@example.com'):
            self.assertNotContains(response, private)

    def test_member_cannot_republish_moderated_review(self):
        self.client.force_login(self.user)
        review = Review.objects.create(user=self.user, ppa=self.ppa, rating=4, comment='Hidden', is_public=False)
        self.client.post(f'/ppas/{self.ppa.pk}/', {'rating': 5, 'comment': 'Edited review'})
        review.refresh_from_db()
        self.assertFalse(review.is_public)
        own = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertContains(own, 'Your review is hidden by moderation')
        self.assertContains(own, 'Edited review')
        self.client.logout()
        self.assertNotContains(self.client.get(f'/ppas/{self.ppa.pk}/'), 'Edited review')

    def contribution(self, **extra):
        place = {'name': 'Google title', 'address': 'Google address', 'place_id': 'ChIJ-new', 'latitude': 6.5, 'longitude': 3.3, 'address_components': []}
        place.update(extra.pop('place', {}))
        token = signing.dumps({'user_id': self.user.pk, 'place': place}, salt='google-place', compress=True)
        return {**self.area(), 'name': 'Name from posting letter', 'address': 'Personally known address', 'community_details_confirmed': 'on', 'place_token': token, **extra}

    def test_contribution_keeps_independent_facts_and_place_id_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/save-live-place/', self.contribution()).status_code, 302)
        ppa = PPA.objects.get(place_id='ChIJ-new')
        self.assertEqual(ppa.name, 'Name from posting letter')
        self.assertEqual(ppa.address, 'Personally known address')
        self.assertEqual(ppa.source, 'community')
        self.assertEqual(ppa.location_status, 'unconfirmed')
        self.assertIsNone(ppa.latitude)
        self.assertIsNone(ppa.longitude)
        self.assertFalse(ppa.google_maps_url)

    def test_wrong_area_and_unacknowledged_facts_cannot_be_saved(self):
        self.client.force_login(self.user)
        components = [{'types': ['administrative_area_level_2'], 'longText': 'Epe'}]
        response = self.client.post('/save-live-place/', self.contribution(place={'address_components': components}))
        self.assertContains(response, 'different LGA', status_code=400)
        self.assertEqual(self.client.post('/save-live-place/', self.contribution(community_details_confirmed='')).status_code, 400)
        self.assertFalse(PPA.objects.filter(place_id='ChIJ-new').exists())

    def test_unverified_member_can_browse_but_not_contribute(self):
        self.user.email_verified = False
        self.user.save(update_fields=['email_verified'])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/search/', self.area()).status_code, 200)
        self.assertEqual(self.client.post('/save-live-place/', self.contribution()).status_code, 302)
        self.assertFalse(PPA.objects.filter(place_id='ChIJ-new').exists())
