from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import Bookmark, PPA
from .models import FareDetails, HousingDetails, Listing, ListingBookmark, ListingReport, ListingRevision, ServiceDetails


class ListingViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Guide State')
        cls.lga = LGA.objects.create(name='Guide LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other Guide State')
        cls.other_lga = LGA.objects.create(name='Other Guide LGA', state=cls.other_state)
        cls.author = get_user_model().objects.create_user(state_code='LG/26A/0001', email='guide-author@example.com', full_name='Private author name', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='LG/26A/0002', email='guide-other@example.com', full_name='Other private name', email_verified=True)
        cls.unverified = get_user_model().objects.create_user(state_code='LG/26A/0003', email='guide-unverified@example.com', full_name='Unverified member')
        cls.ppa = PPA.objects.create(name='Guide Demo PPA', state=cls.state, lga=cls.lga, address='Public institutional address')

    def setUp(self):
        cache.clear()

    def listing(self, title='Reported housing', kind=Listing.Kind.HOUSING, **values):
        defaults = dict(title=title, kind=kind, area='Broad neighbourhood', summary='Reported community guidance.', state=self.state, lga=self.lga, author=self.author,
                        observed_on=timezone.localdate(), expires_at=timezone.now() + timedelta(days=14), publication_status=Listing.Publication.APPROVED,
                        moderator_note='PRIVATE INVESTIGATION EVIDENCE')
        defaults.update(values)
        item = Listing.objects.create(**defaults)
        if kind == Listing.Kind.HOUSING:
            HousingDetails.objects.create(listing=item, reported_rent=Decimal('450000.00'), price_period='yearly', room_type='shared', amenities=['water', 'power'], availability='available', contributor_role='owner', authorization_attested=True, authorization_basis='PRIVATE AUTHORISATION BASIS')
        elif kind == Listing.Kind.FARE:
            FareDetails.objects.create(listing=item, origin_area='Central bus stop', destination_area='Public market', mode='bus', amount_low=Decimal('300.00'), amount_high=Decimal('500.00'))
        else:
            ServiceDetails.objects.create(listing=item, category='printing', opening_hours='Weekdays', public_business_address='Public commercial centre')
        return item

    def housing_data(self, **changes):
        data = {'title': 'New reported room', 'area': 'Broad neighbourhood', 'summary': 'Information independently supplied.', 'state': self.state.pk, 'lga': self.lga.pk,
                'source_type': 'member', 'source_url': '', 'observed_on': timezone.localdate().isoformat(), 'privacy_confirmed': 'on', 'action': 'submit',
                'reported_rent': '250000.00', 'price_period': 'yearly', 'room_type': 'single', 'amenities': ['water'], 'availability': 'available',
                'contributor_role': 'member', 'authorization_attested': 'on', 'authorization_basis': ''}
        data.update(changes)
        return data

    def test_guests_browse_only_active_approved_reports_with_safe_projection(self):
        active = self.listing()
        self.listing('Private draft title', publication_status='draft')
        self.listing('Private pending title', publication_status='pending')
        self.listing('Expired room', expires_at=timezone.now() - timedelta(days=1))
        unavailable = self.listing('No longer available')
        HousingDetails.objects.filter(listing=unavailable).update(availability='unavailable')
        response = self.client.get(reverse('local_guides:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, active.title)
        for secret in ['Private draft title', 'Private pending title', 'Expired room', 'No longer available', 'PRIVATE INVESTIGATION EVIDENCE', 'PRIVATE AUTHORISATION BASIS', self.author.email, self.author.state_code]:
            self.assertNotContains(response, secret)
        item = response.context['listings'].object_list[0]
        self.assertIsInstance(item, dict)
        self.assertNotIn('author', item)
        self.assertNotIn('moderator_note', item)
        self.assertNotIn('authorization_basis', item['housing'])
        self.assertIsInstance(response.context['page_obj'].paginator.object_list[0], dict)

    def test_typed_filters_match_area_kind_and_fair_rent_period(self):
        yearly = self.listing('Yearly shared room')
        monthly = self.listing('Monthly shared room')
        HousingDetails.objects.filter(listing=monthly).update(reported_rent='50000.00', price_period='monthly')
        self.listing('Room in another state', state=self.other_state, lga=self.other_lga)
        self.listing('Public transport estimate', kind=Listing.Kind.FARE)
        response = self.client.get(reverse('local_guides:home'), {'kind': 'housing', 'state': self.state.pk, 'lga': self.lga.pk, 'max_rent': '500000', 'price_period': 'yearly', 'room_type': 'shared', 'amenity': 'water'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, yearly.title)
        for title in [monthly.title, 'Room in another state', 'Public transport estimate']:
            self.assertNotContains(response, title)
        bad = self.client.get(reverse('local_guides:home'), {'max_rent': '500000'})
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, 'Choose monthly or yearly', status_code=400)

    def test_fare_mode_service_category_and_invalid_filters(self):
        fare = self.listing('Bus fare report', kind=Listing.Kind.FARE)
        service = self.listing('Print service report', kind=Listing.Kind.SERVICE)
        response = self.client.get(reverse('local_guides:home'), {'mode': 'bus'})
        self.assertContains(response, fare.title)
        self.assertNotContains(response, service.title)
        response = self.client.get(reverse('local_guides:home'), {'category': 'printing'})
        self.assertContains(response, service.title)
        self.assertNotContains(response, fare.title)
        for values in [{'state': self.state.pk, 'lga': self.other_lga.pk}, {'room_type': 'invalid'}, {'amenity': 'password'}, {'max_rent': 'NaN'}, {'q': 'x' * 121}]:
            self.assertEqual(self.client.get(reverse('local_guides:home'), values).status_code, 400)

    def test_ppa_context_selects_its_locality_but_rejects_cross_state_filter(self):
        item = self.listing(ppa=self.ppa)
        other = self.listing('Unassociated report')
        response = self.client.get(reverse('local_guides:home'), {'ppa': self.ppa.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, item.title)
        self.assertNotContains(response, other.title)
        self.assertContains(response, 'not a measured distance')
        response = self.client.get(reverse('local_guides:home'), {'ppa': self.ppa.pk, 'state': self.other_state.pk, 'lga': self.other_lga.pk})
        self.assertEqual(response.status_code, 400)

    def test_expired_detail_is_historical_and_never_promises_available_room(self):
        item = self.listing(expires_at=timezone.now() - timedelta(hours=1), verification_status=Listing.Verification.CHECKED, checked_at=timezone.now() - timedelta(days=20), source_url='https://example.com/public')
        response = self.client.get(reverse('local_guides:detail', args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'historical report, not an available-room offer')
        self.assertContains(response, 'historical; reconfirmation needed')
        self.assertContains(response, 'PPA Locator collects no rent, deposits or fees')
        self.assertNotContains(response, 'PRIVATE INVESTIGATION EVIDENCE')
        self.assertNotContains(response, 'PRIVATE AUTHORISATION BASIS')

    def test_a_last_check_date_does_not_label_community_information_independent(self):
        item = self.listing(checked_at=timezone.now(), verification_status=Listing.Verification.COMMUNITY)
        response = self.client.get(reverse('local_guides:detail', args=[item.pk]))
        self.assertContains(response, 'Last checked')
        self.assertContains(response, 'Community-reported — not independently checked')
        self.assertNotContains(response, 'Independently checked ')

    def test_private_preview_and_edit_history_are_owner_only_even_for_staff(self):
        item = self.listing(publication_status='draft')
        self.assertEqual(self.client.get(reverse('local_guides:detail', args=[item.pk])).status_code, 404)
        self.other.is_staff = True
        self.other.save(update_fields=['is_staff'])
        self.client.force_login(self.other)
        for route in ['detail', 'edit', 'history', 'reconfirm']:
            self.assertEqual(self.client.get(reverse('local_guides:' + route, args=[item.pk])).status_code, 404)
        self.client.force_login(self.author)
        response = self.client.get(reverse('local_guides:detail', args=[item.pk]))
        self.assertContains(response, 'Your private contribution preview')
        self.assertNotContains(response, 'PRIVATE AUTHORISATION BASIS')

    def test_verified_member_creates_private_proposal_without_granting_badges(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse('local_guides:create', args=['housing']), self.housing_data(publication_status='approved', verification_status='official', author=self.other.pk, moderator_note='FORGED EVIDENCE'))
        self.assertRedirects(response, reverse('local_guides:mine'))
        item = Listing.objects.get(title='New reported room')
        self.assertEqual(item.author, self.author)
        self.assertEqual(item.publication_status, 'pending')
        self.assertEqual(item.verification_status, 'community')
        self.assertEqual(item.moderator_note, '')
        self.assertEqual(item.housing.reported_rent, Decimal('250000.00'))
        self.assertTrue(item.expires_at <= timezone.now() + timedelta(days=14))
        self.client.logout()
        self.assertEqual(self.client.get(reverse('local_guides:detail', args=[item.pk])).status_code, 404)

    def test_unverified_members_and_guests_cannot_contribute_but_may_browse(self):
        create_url = reverse('local_guides:create', args=['fare'])
        self.assertEqual(self.client.get(create_url).status_code, 302)
        self.client.force_login(self.unverified)
        self.assertRedirects(self.client.get(create_url), reverse('email_verification'))
        self.assertEqual(self.client.get(reverse('local_guides:home')).status_code, 200)
        self.assertFalse(Listing.objects.exists())

    def test_fare_and_service_forms_create_typed_pending_details(self):
        self.client.force_login(self.author)
        common = {key: value for key, value in self.housing_data().items() if key in ['title', 'area', 'summary', 'state', 'lga', 'source_type', 'source_url', 'observed_on', 'privacy_confirmed', 'action']}
        fare_data = {**common, 'title': 'New keke estimate', 'origin_area': 'Main public junction', 'destination_area': 'Public market', 'mode': 'keke', 'amount_low': '400.25', 'amount_high': '650.50'}
        response = self.client.post(reverse('local_guides:create', args=['fare']), fare_data)
        self.assertRedirects(response, reverse('local_guides:mine'))
        fare = Listing.objects.get(title='New keke estimate')
        self.assertEqual(fare.fare.amount_low, Decimal('400.25'))
        self.assertEqual(fare.publication_status, 'pending')
        response = self.client.post(reverse('local_guides:create', args=['fare']), {**fare_data, 'title': 'Invalid price estimate', 'amount_high': '100.00'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Listing.objects.filter(title='Invalid price estimate').exists())
        service_data = {**common, 'title': 'New print shop report', 'category': 'printing', 'opening_hours': 'Weekdays, 9am–5pm', 'public_business_address': 'Public shopping centre'}
        response = self.client.post(reverse('local_guides:create', args=['service']), service_data)
        self.assertRedirects(response, reverse('local_guides:mine'))
        service = Listing.objects.get(title='New print shop report')
        self.assertEqual(service.service.category, 'printing')
        self.assertEqual(service.publication_status, 'pending')

    def test_invalid_location_missing_consent_and_authority_never_save_partial_rows(self):
        self.client.force_login(self.author)
        for change in [{'lga': self.other_lga.pk}, {'privacy_confirmed': ''}, {'contributor_role': 'owner', 'authorization_basis': ''}, {'reported_rent': 'Infinity'}, {'reported_rent': '-1'}]:
            response = self.client.post(reverse('local_guides:create', args=['housing']), self.housing_data(**change))
            self.assertEqual(response.status_code, 400)
            self.assertFalse(Listing.objects.exists())
            self.assertFalse(HousingDetails.objects.exists())

    def test_edit_requires_fresh_consent_and_removes_old_publication_and_verification(self):
        item = self.listing(verification_status='checked', checked_at=timezone.now(), source_url='https://example.com/reference')
        self.client.force_login(self.author)
        response = self.client.post(reverse('local_guides:edit', args=[item.pk]), self.housing_data(title='Updated room details', privacy_confirmed=''))
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'approved')
        response = self.client.post(reverse('local_guides:edit', args=[item.pk]), self.housing_data(title='Updated room details'))
        self.assertRedirects(response, reverse('local_guides:mine'))
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'pending')
        self.assertEqual(item.verification_status, 'community')
        self.assertIsNone(item.checked_at)
        self.assertTrue(ListingRevision.objects.filter(listing=item).exists())

    def test_watchlist_is_private_and_hides_unpublished_edits_then_allows_removal(self):
        item = self.listing()
        Bookmark.objects.create(user=self.other, ppa=self.ppa)
        self.client.force_login(self.other)
        self.assertRedirects(self.client.post(reverse('local_guides:bookmark', args=[item.pk]), {'action': 'save'}), reverse('local_guides:detail', args=[item.pk]))
        self.client.post(reverse('local_guides:bookmark', args=[item.pk]), {'action': 'save'})
        self.assertEqual(ListingBookmark.objects.count(), 1)
        self.assertEqual(Bookmark.objects.count(), 1)
        Listing.objects.filter(pk=item.pk).update(publication_status='draft', title='NEW PRIVATE DRAFT CONTENT')
        response = self.client.get(reverse('local_guides:saved'))
        self.assertContains(response, 'Listing currently unavailable')
        self.assertNotContains(response, 'NEW PRIVATE DRAFT CONTENT')
        self.assertIsNone(response.context['bookmarks'].object_list[0]['listing'])
        self.assertRedirects(self.client.post(reverse('local_guides:bookmark', args=[item.pk]), {'action': 'remove'}), reverse('local_guides:saved'))
        self.assertFalse(ListingBookmark.objects.exists())
        self.assertEqual(Bookmark.objects.count(), 1)

    def test_another_member_cannot_read_or_remove_saved_entries(self):
        item = self.listing()
        ListingBookmark.objects.create(user=self.author, listing=item)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('local_guides:saved')), item.title)
        self.client.post(reverse('local_guides:bookmark', args=[item.pk]), {'action': 'remove'})
        self.assertEqual(ListingBookmark.objects.filter(user=self.author).count(), 1)

    def test_reports_are_private_and_do_not_leak_revised_private_title_or_notes(self):
        item = self.listing()
        self.client.force_login(self.other)
        response = self.client.post(reverse('local_guides:report', args=[item.pk]), {'category': 'scam', 'details': 'Private member correction', 'proposed_correction': 'Please check this'})
        self.assertRedirects(response, reverse('local_guides:reports'))
        report = ListingReport.objects.get()
        report.moderator_note = 'PRIVATE MODERATOR INVESTIGATION'
        report.save(update_fields=['moderator_note'])
        Listing.objects.filter(pk=item.pk).update(title='PRIVATE REVISED TITLE', publication_status='draft')
        response = self.client.get(reverse('local_guides:reports'))
        self.assertContains(response, 'Private member correction')
        self.assertNotContains(response, 'PRIVATE MODERATOR INVESTIGATION')
        self.assertNotContains(response, 'PRIVATE REVISED TITLE')
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(reverse('local_guides:reports')), 'Private member correction')

    def test_owner_history_excludes_private_notes_identity_and_authority(self):
        item = self.listing()
        ListingRevision.objects.create(listing=item, actor=self.author, reason='SECRET MODERATION REASON', snapshot={'kind': 'housing', 'title': item.title, 'area': 'Old broad area', 'summary': 'Earlier public version', 'publication_status': 'pending', 'moderator_note': 'SECRET OLD NOTE', 'details': {'authorization_basis': 'SECRET OLD AUTHORITY', 'authorization_attested': True, 'room_type': 'shared', 'reported_rent': '123456.78', 'price_period': 'yearly', 'availability': 'available'}})
        self.client.force_login(self.author)
        response = self.client.get(reverse('local_guides:history', args=[item.pk]))
        self.assertContains(response, 'Earlier public version')
        self.assertContains(response, '₦123456.78')
        self.assertContains(response, 'Awaiting moderation')
        for private in ['SECRET MODERATION REASON', 'SECRET OLD NOTE', 'SECRET OLD AUTHORITY', self.author.email, self.author.state_code]:
            self.assertNotContains(response, private)
        row = response.context['revisions'].object_list[0]
        self.assertNotIn('moderator_note', row)
        self.assertNotIn('authorization_basis', row['details'])

    def test_reconfirmation_is_explicit_post_resets_approval_and_checks_availability(self):
        item = self.listing(expires_at=timezone.now() - timedelta(days=1))
        self.client.force_login(self.author)
        url = reverse('local_guides:reconfirm', args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'approved')
        response = self.client.post(url, {'observed_on': timezone.localdate().isoformat(), 'confirmed': 'on', 'available': 'no'})
        self.assertRedirects(response, reverse('local_guides:mine'))
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'pending')
        self.assertIsNotNone(item.last_reconfirmed_at)
        self.assertEqual(item.housing.availability, 'unavailable')
        self.assertIsNone(item.checked_at)

    def test_archive_and_bookmarks_require_post_and_csrf(self):
        item = self.listing()
        self.client.force_login(self.author)
        for name in ['archive', 'bookmark']:
            self.assertEqual(self.client.get(reverse('local_guides:' + name, args=[item.pk])).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.author)
        for name, data in [('archive', {}), ('bookmark', {'action': 'save'}), ('reconfirm', {'observed_on': timezone.localdate().isoformat(), 'confirmed': 'on', 'available': 'yes'})]:
            self.assertEqual(csrf.post(reverse('local_guides:' + name, args=[item.pk]), data).status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'approved')
        self.client.post(reverse('local_guides:archive', args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual(item.publication_status, 'archived')

    def test_directory_pagination_is_bounded_and_preserves_filters(self):
        for index in range(23):
            self.listing('Paged housing ' + str(index))
        response = self.client.get(reverse('local_guides:home'), {'kind': 'housing', 'state': self.state.pk, 'lga': self.lga.pk})
        self.assertEqual(len(response.context['listings']), 20)
        self.assertEqual(response.context['page_obj'].paginator.count, 23)
        self.assertContains(response, 'kind=housing')
        self.assertContains(response, 'page=2')
        next_page = self.client.get(reverse('local_guides:home'), {'kind': 'housing', 'state': self.state.pk, 'lga': self.lga.pk, 'page': '2'})
        self.assertEqual(len(next_page.context['listings']), 3)

    def test_directory_query_count_does_not_grow_with_listings_or_lgas(self):
        self.listing()
        url = reverse('local_guides:home')
        query = {'state': self.state.pk, 'lga': self.lga.pk}
        with CaptureQueriesContext(connection) as small:
            self.client.get(url, query)
        for index in range(25):
            self.listing('Additional housing ' + str(index))
            LGA.objects.create(state=self.state, name='Additional area ' + str(index))
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(large), len(small) + 1)
        self.assertLessEqual(len(large), 9)
