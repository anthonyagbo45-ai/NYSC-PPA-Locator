import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from .models import FareDetails, HousingDetails, Listing, ListingBookmark, ListingReport, ListingRevision, ServiceDetails, validate_public_url
from .services import (archive_listing, get_public_listing, moderate_listing, moderate_report,
                       owner_revisions, public_listings, reconfirm_listing, safe_public_listing,
                       save_listing, save_report, set_bookmark)


class LocalGuidanceCoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Guidance State')
        cls.lga = LGA.objects.create(name='Guidance LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other Guidance State')
        cls.other_lga = LGA.objects.create(name='Other Guidance LGA', state=cls.other_state)
        cls.ppa = PPA.objects.create(name='Local school', state=cls.state, lga=cls.lga, address='Public school address')
        cls.user = get_user_model().objects.create_user(state_code='GUIDE/PRIVATE/001', full_name='Private member', email='guide@example.com', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='GUIDE/PRIVATE/002', full_name='Other private member', email='other-guide@example.com', email_verified=True)
        cls.unverified = get_user_model().objects.create_user(state_code='GUIDE/PRIVATE/003', full_name='Unverified member', email='unverified-guide@example.com')
        cls.moderator = get_user_model().objects.create_user(state_code='GUIDE/MOD/001', full_name='Moderator', email='guide-mod@example.com')
        cls.moderator.user_permissions.add(*Permission.objects.filter(content_type__app_label='local_guides', codename__in=['can_moderate_listings', 'can_moderate_listing_reports']))
        cls.verifier = get_user_model().objects.create_user(state_code='GUIDE/VERIFY/001', full_name='Independent checker', email='guide-checker@example.com')
        cls.verifier.user_permissions.add(*Permission.objects.filter(content_type__app_label='local_guides', codename__in=['can_moderate_listings', 'can_verify_listings']))

    def data(self, kind=Listing.Kind.HOUSING, **updates):
        values = dict(kind=kind, title='Neighbourhood accommodation' if kind == 'housing' else 'Local guidance', area='Broad neighbourhood', summary='Useful independent community information.', state=self.state, lga=self.lga, ppa=self.ppa, source_type=Listing.Source.MEMBER, source_url='', observed_on=timezone.localdate())
        values.update(updates)
        return values

    def details(self, kind=Listing.Kind.HOUSING, **updates):
        if kind == Listing.Kind.HOUSING:
            values = dict(reported_rent=Decimal('120000.00'), price_period='yearly', room_type='single', amenities=['water'], availability='available', contributor_role='member', authorization_attested=True, authorization_basis='')
        elif kind == Listing.Kind.FARE:
            values = dict(origin_area='Town centre', destination_area='Public school', mode='bus', amount_low=Decimal('300.00'), amount_high=Decimal('500.00'))
        else:
            values = dict(category='printing', opening_hours='Weekdays', public_business_address='Public business premise')
        values.update(updates)
        return values

    def create(self, kind=Listing.Kind.HOUSING, submit=True, **updates):
        return save_listing(user=self.user, cleaned_data=self.data(kind, **updates), details=self.details(kind), submit=submit)

    def approve(self, listing, **values):
        return moderate_listing(listing=listing, actor=self.moderator, values={'publication_status': Listing.Publication.APPROVED, **values}, reason='Public information and sharing permission reviewed.')

    def test_sources_do_not_publish_or_verify_automatically(self):
        for kind in Listing.Kind.values:
            listing = self.create(kind)
            self.assertEqual(listing.publication_status, Listing.Publication.PENDING)
            self.assertEqual(listing.verification_status, Listing.Verification.COMMUNITY)
            self.assertIsNone(listing.checked_at)
            self.assertFalse(public_listings().filter(pk=listing.pk).exists())
            self.assertEqual(listing.revisions.count(), 1)
        self.ppa.refresh_from_db()
        self.assertFalse(self.ppa.verified)
        self.assertEqual(self.ppa.status, PPA.Status.DISCOVERED)

    def test_member_writes_ignore_forged_owner_status_and_verification(self):
        data = self.data(publication_status='approved', verification_status='official', author=self.other, moderator_note='Forged approval')
        listing = save_listing(user=self.user, cleaned_data=data, details=self.details(), submit=True)
        self.assertEqual(listing.author_id, self.user.pk)
        self.assertEqual(listing.publication_status, 'pending')
        self.assertEqual(listing.verification_status, 'community')
        self.assertEqual(listing.moderator_note, '')
        with self.assertRaises(PermissionDenied):
            save_listing(user=self.user, cleaned_data=self.data(source_type='official'), details=self.details())

    def test_unverified_or_revoked_mailbox_cannot_contribute(self):
        with self.assertRaises(PermissionDenied):
            save_listing(user=self.unverified, cleaned_data=self.data(), details=self.details())
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=False)
        self.assertTrue(self.user.email_verified)
        with self.assertRaises(PermissionDenied):
            self.create()

    def test_invalid_geography_and_ppa_are_rejected_without_records(self):
        for updates in ({'lga': self.other_lga}, {'state': self.other_state, 'lga': self.other_lga}):
            with self.assertRaises(ValidationError):
                self.create(**updates)
        self.assertFalse(Listing.objects.exists())
        self.assertFalse(ListingRevision.objects.exists())

    def test_nonexistent_foreign_key_is_validation_error(self):
        listing = Listing(kind='service', title='A service', area='An area', summary='Info', state_id=999999, lga_id=999999, observed_on=timezone.localdate())
        with self.assertRaises(ValidationError):
            listing.full_clean()

    def test_nested_validation_rolls_back_original_entry_and_history(self):
        listing = self.create()
        before = listing.title
        with self.assertRaises(ValidationError):
            save_listing(user=self.user, listing=listing, cleaned_data=self.data(title='Unsafe replacement'), details=self.details(authorization_attested=False), submit=True)
        listing.refresh_from_db()
        self.assertEqual(listing.title, before)
        self.assertEqual(listing.revisions.count(), 1)
        with self.assertRaises(ValidationError):
            save_listing(user=self.user, cleaned_data=self.data(title='Another entry'), details=self.details(amenities=['not-supported']), submit=True)
        self.assertEqual(Listing.objects.count(), 1)

    def test_owner_authority_requires_private_basis_and_review_evidence(self):
        with self.assertRaises(ValidationError):
            save_listing(user=self.user, cleaned_data=self.data(source_type='owner'), details=self.details(contributor_role='agent'), submit=True)
        listing = save_listing(user=self.user, cleaned_data=self.data(source_type='owner'), details=self.details(contributor_role='agent', authorization_basis='Private permission basis: approved to share broad listing details.'), submit=True)
        with self.assertRaises(ValidationError):
            self.approve(listing)
        listing = self.approve(listing, moderator_note='Authority to share was reviewed privately; no ownership guarantee.')
        projection = safe_public_listing(get_public_listing(listing.pk))
        self.assertNotIn('authorization_basis', json.dumps(projection, default=str))
        self.assertEqual(listing.verification_status, 'community')

    def test_cross_member_edit_archive_reconfirm_and_history_are_denied(self):
        listing = self.create()
        operations = [
            lambda: save_listing(user=self.other, listing=listing, cleaned_data=self.data(title='Changed'), details=self.details()),
            lambda: archive_listing(listing=listing, user=self.other),
            lambda: reconfirm_listing(listing=listing, user=self.other, observed_on=timezone.localdate(), available=True),
            lambda: owner_revisions(listing=listing, user=self.other),
            lambda: safe_public_listing(listing, user=self.other),
        ]
        for operation in operations:
            with self.assertRaises(PermissionDenied):
                operation()
        listing.refresh_from_db()
        self.assertEqual(listing.revisions.count(), 1)
        self.assertEqual(listing.title, 'Neighbourhood accommodation')

    def test_public_approval_is_not_independent_verification(self):
        listing = self.approve(self.create())
        self.assertTrue(public_listings().filter(pk=listing.pk).exists())
        self.assertEqual(listing.verification_status, 'community')
        self.assertIsNone(listing.checked_at)
        with self.assertRaises(PermissionDenied):
            moderate_listing(listing=listing, actor=self.moderator, values={'verification_status': 'checked', 'checked_at': timezone.now(), 'source_url': 'https://example.org/source', 'moderator_note': 'Checked evidence'}, reason='Checked report')

    def test_independent_check_needs_permission_traceable_evidence_and_different_actor(self):
        listing = self.create()
        strong = dict(publication_status='approved', verification_status='checked', checked_at=timezone.now(), source_url='https://example.org/source', moderator_note='Checked the attributable source.')
        verified = moderate_listing(listing=listing, actor=self.verifier, values=strong, reason='Evidence checked.')
        self.assertEqual(verified.verification_status, 'checked')
        with self.assertRaises(ValidationError):
            moderate_listing(listing=verified, actor=self.verifier, values={'source_type': 'official', 'source_url': ''}, reason='Cannot attribute')
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='local_guides', codename__in=['can_moderate_listings', 'can_verify_listings']))
        with self.assertRaises(PermissionDenied):
            moderate_listing(listing=verified, actor=self.user, values=strong, reason='Self-check attempted')

    def test_checked_author_edit_drops_old_badge_even_if_reference_removed(self):
        listing = self.create()
        listing = moderate_listing(listing=listing, actor=self.verifier, values=dict(publication_status='approved', verification_status='checked', checked_at=timezone.now(), source_url='https://example.org/source', moderator_note='Checked source.'), reason='Independent check')
        edited = save_listing(user=self.user, listing=listing, cleaned_data=self.data(summary='New report, independently supplied.', source_url=''), details=self.details(), submit=True)
        self.assertEqual(edited.publication_status, 'pending')
        self.assertEqual(edited.verification_status, 'community')
        self.assertIsNone(edited.checked_at)
        self.assertFalse(public_listings().filter(pk=edited.pk).exists())

    def test_private_drafts_are_not_moderated_and_archives_not_republished(self):
        draft = self.create(submit=False)
        with self.assertRaises(PermissionDenied):
            self.approve(draft)
        archived = archive_listing(listing=draft, user=self.user)
        with self.assertRaises(ValidationError):
            self.approve(archived)
        with self.assertRaises(ValidationError):
            save_listing(user=self.user, listing=archived, cleaned_data=self.data(), details=self.details(), submit=True)

    def test_housing_expiry_and_unavailability_leave_historical_public_detail(self):
        old = self.approve(self.create(observed_on=timezone.localdate() - timedelta(days=15)))
        self.assertTrue(old.is_stale)
        self.assertFalse(public_listings().filter(pk=old.pk).exists())
        projection = safe_public_listing(get_public_listing(old.pk))
        self.assertTrue(projection['is_stale'])
        self.assertIn('historical', projection['verification_label'])
        listing = save_listing(user=self.user, cleaned_data=self.data(title='Unavailable housing'), details=self.details(availability='unavailable'), submit=True)
        self.approve(listing)
        self.assertFalse(public_listings().filter(pk=listing.pk).exists())
        self.assertFalse(safe_public_listing(get_public_listing(listing.pk))['is_available'])

    def test_reconfirmation_resets_verification_and_schedules_fresh_review(self):
        listing = self.create()
        listing = moderate_listing(listing=listing, actor=self.verifier, values=dict(publication_status='approved', verification_status='checked', checked_at=timezone.now(), source_url='https://example.org/source', moderator_note='Independent check'), reason='Independent check')
        listing = reconfirm_listing(listing=listing, user=self.user, observed_on=timezone.localdate(), available=False)
        self.assertEqual(listing.publication_status, 'pending')
        self.assertEqual(listing.verification_status, 'community')
        self.assertIsNone(listing.checked_at)
        self.assertIsNotNone(listing.last_reconfirmed_at)
        self.assertEqual(listing.housing.availability, 'unavailable')
        self.assertAlmostEqual((listing.expires_at-timezone.now()).total_seconds()/86400, 14, places=3)
        self.assertEqual(listing.revisions.count(), 3)

    def test_private_snapshots_and_owner_history_do_not_leak_staff_or_authority_evidence(self):
        listing = self.create()
        listing = self.approve(listing, moderator_note='PRIVATE STAFF INVESTIGATION')
        projection = safe_public_listing(get_public_listing(listing.pk))
        history = owner_revisions(listing=listing, user=self.user)
        for value in (projection, history):
            encoded = json.dumps(value, default=str)
            self.assertNotIn('PRIVATE STAFF INVESTIGATION', encoded)
            self.assertNotIn('author_id', encoded)
            self.assertNotIn('moderated_by', encoded)
            self.assertNotIn(self.user.state_code, encoded)
        self.assertIn('PRIVATE STAFF INVESTIGATION', json.dumps(listing.revisions.first().snapshot))

    def test_duplicate_submission_is_rejected_and_an_edit_is_allowed(self):
        listing = self.create()
        with self.assertRaises(ValidationError):
            self.create(title='  NEIGHBOURHOOD accommodation ')
        save_listing(user=self.user, listing=listing, cleaned_data=self.data(summary='New observation'), details=self.details(), submit=True)
        self.assertEqual(Listing.objects.count(), 1)

    def test_public_list_projection_does_not_add_queries_per_listing(self):
        for n in range(5):
            self.approve(self.create(Listing.Kind.SERVICE, title=f'Public service {n}'))
        with self.assertNumQueries(1):
            rows = [safe_public_listing(x) for x in public_listings()]
        self.assertEqual(len(rows), 5)
        self.assertNotIn('author', rows[0])

    def test_incomplete_typed_data_cannot_publish_or_break_public_browsing(self):
        listing = Listing.objects.create(kind='service', title='Incomplete import', area='General area', summary='No typed details', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate(), publication_status='pending')
        with self.assertRaises(ValidationError):
            self.approve(listing)
        Listing.objects.filter(pk=listing.pk).update(publication_status='approved')
        self.assertFalse(public_listings().filter(pk=listing.pk).exists())
        from django.http import Http404
        with self.assertRaises(Http404):
            get_public_listing(listing.pk)

    def test_fare_money_and_amenities_validate_and_database_enforces_ranges(self):
        for low, high in [('500.00', '300.00'), ('-1.00', '300.00'), ('NaN', '300.00'), ('1.001', '2.00')]:
            with self.assertRaises(ValidationError):
                save_listing(user=self.user, cleaned_data=self.data(Listing.Kind.FARE), details=self.details(Listing.Kind.FARE, amount_low=Decimal(low), amount_high=Decimal(high)), submit=True)
        fare = self.create(Listing.Kind.FARE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FareDetails.objects.filter(listing=fare).update(amount_high=Decimal('1.00'))
        housing = self.create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            HousingDetails.objects.filter(listing=housing).update(reported_rent=Decimal('-1'))

    def test_source_links_dates_and_check_chronology_validate(self):
        for value in ['https://user:password@example.org/path', 'javascript:alert(1)', 'https://example.org/\x00bad', 'https://example.org:abc/']:
            with self.assertRaises(ValidationError):
                validate_public_url(value)
        with self.assertRaises(ValidationError):
            self.create(observed_on=timezone.localdate()+timedelta(days=1))
        listing = self.create()
        listing.source_type = Listing.Source.OFFICIAL
        with self.assertRaises(ValidationError):
            listing.full_clean()
        listing.source_type = Listing.Source.MEMBER
        listing.checked_at = timezone.now()
        listing.expires_at = listing.checked_at-timedelta(days=1)
        with self.assertRaises(ValidationError):
            listing.full_clean()
        for field in ('observed_on', 'checked_at', 'expires_at'):
            listing.refresh_from_db()
            setattr(listing, field, 'not-a-date')
            with self.assertRaises(ValidationError):
                listing.full_clean()

    def test_bookmark_idempotence_private_removal_and_report_review_do_not_rewrite_listing(self):
        listing = self.approve(self.create())
        set_bookmark(user=self.user, listing=listing)
        set_bookmark(user=self.user, listing=listing)
        set_bookmark(user=self.other, listing=listing)
        self.assertEqual(ListingBookmark.objects.count(), 2)
        report = save_report(user=self.unverified, listing=listing, category='scam', details='Payment request should be checked.')
        moderate_report(report=report, actor=self.moderator, status='investigating', moderator_note='PRIVATE investigation details')
        self.assertEqual(report.revisions.count(), 1)
        listing.refresh_from_db()
        self.assertEqual(listing.publication_status, 'approved')
        archive_listing(listing=listing, user=self.user)
        set_bookmark(user=self.user, listing=listing, save=False)
        self.assertTrue(ListingBookmark.objects.filter(user=self.other, listing=listing).exists())
        self.assertFalse(ListingBookmark.objects.filter(user=self.user, listing=listing).exists())

    def test_author_edits_and_reconfirmation_require_fresh_ownership_review(self):
        listing = save_listing(user=self.user, cleaned_data=self.data(source_type='owner'), details=self.details(contributor_role='owner', authorization_basis='Initial private permission basis'), submit=True)
        listing = self.approve(listing, moderator_note='Initial permission basis checked.')
        listing = save_listing(user=self.user, listing=listing, cleaned_data=self.data(source_type='owner'), details=self.details(contributor_role='owner', authorization_basis='Changed private permission basis'), submit=True)
        self.assertEqual(listing.moderator_note, '')
        with self.assertRaises(ValidationError):
            self.approve(listing)
        listing = self.approve(listing, moderator_note='Changed basis checked afresh.')
        listing = reconfirm_listing(listing=listing, user=self.user, observed_on=timezone.localdate(), available=True)
        self.assertEqual(listing.moderator_note, '')
        with self.assertRaises(ValidationError):
            self.approve(listing)
        private_audit = json.dumps(list(listing.revisions.values_list('snapshot', flat=True)), default=str)
        self.assertIn('Initial permission basis checked.', private_audit)
        self.assertIn('Changed basis checked afresh.', private_audit)

    def test_reconfirmation_windows_cannot_be_extended_indefinitely(self):
        for kind, maximum in [('housing', 30), ('fare', 180), ('service', 365)]:
            listing = self.create(kind)
            before = listing.expires_at
            with self.assertRaises(ValidationError):
                self.approve(listing, expires_at=timezone.now()+timedelta(days=maximum+1))
            listing.refresh_from_db()
            self.assertEqual(listing.expires_at, before)
            self.assertEqual(listing.publication_status, 'pending')

    def test_daily_contribution_quota_does_not_overwrite_earlier_entries(self):
        for number in range(20):
            self.create(Listing.Kind.SERVICE, title=f'Independently contributed service {number}')
        with self.assertRaises(ValidationError):
            self.create(Listing.Kind.SERVICE, title='Over daily allowance')
        self.assertEqual(Listing.objects.count(), 20)
        self.assertEqual(ListingRevision.objects.count(), 20)


class ConcurrentListingTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_first_submissions_serialize_duplicate_and_quota_checks(self):
        state = State.objects.create(name='Concurrent Guide State')
        lga = LGA.objects.create(name='Concurrent Guide LGA', state=state)
        user = get_user_model().objects.create_user(state_code='GUIDE/CONCURRENT/1', full_name='Private member', email='concurrent-guide@example.com', email_verified=True)
        ready = Barrier(2)

        def submit():
            close_old_connections()
            try:
                member = get_user_model().objects.get(pk=user.pk)
                ready.wait(timeout=10)
                try:
                    result = save_listing(user=member, cleaned_data=dict(kind='service', title='Concurrent public service', area='Town centre', summary='Independent info', state=state, lga=lga, source_type='member', observed_on=timezone.localdate()), details=dict(category='printing', opening_hours='Weekdays', public_business_address='Public business'), submit=True)
                    return ('saved', result.pk)
                except ValidationError:
                    return ('duplicate', None)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(x[0] for x in outcomes), ['duplicate', 'saved'])
        self.assertEqual(Listing.objects.count(), 1)
        self.assertEqual(ListingRevision.objects.count(), 1)
