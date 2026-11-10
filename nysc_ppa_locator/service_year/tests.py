from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import Bookmark, Confirmation, PPA, PlaceReport, Review
from .forms import ServiceProfileForm
from .models import MemberPPA, ServiceProfile, StatusChange
from .services import assign_ppa, dashboard_context, end_assignment, update_progress


class ServiceYearDataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Service Year State')
        cls.lga = LGA.objects.create(name='Service Year LGA', state=cls.state)
        cls.user = get_user_model().objects.create_user(
            state_code='SY/PRIVATE/001', email='service-member@example.com',
            full_name='Private legal name', email_verified=True,
        )
        cls.other = get_user_model().objects.create_user(
            state_code='SY/PRIVATE/002', email='other-service-member@example.com',
            full_name='Other private legal name',
        )
        cls.ppa = PPA.objects.create(name='Service Year School', state=cls.state, lga=cls.lga, address='School address')
        cls.other_ppa = PPA.objects.create(name='Service Year Clinic', state=cls.state, lga=cls.lga, address='Clinic address')


class AssignmentServiceTests(ServiceYearDataMixin, TestCase):
    def test_save_is_explicit_and_independent_of_bookmarks_and_public_confirmation(self):
        Bookmark.objects.create(user=self.user, ppa=self.ppa)
        Confirmation.objects.create(user=self.user, ppa=self.ppa)
        self.assertFalse(MemberPPA.objects.exists())
        assignment, created = assign_ppa(self.user, self.ppa)
        self.assertTrue(created)
        self.assertEqual(assignment.status, MemberPPA.Status.YET_TO_REPORT)
        self.assertEqual(Bookmark.objects.count(), 1)
        self.assertEqual(Confirmation.objects.count(), 1)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.status, PPA.Status.DISCOVERED)
        self.assertFalse(self.ppa.verified)

    def test_database_enforces_one_current_assignment_and_valid_status(self):
        assign_ppa(self.user, self.ppa)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberPPA.objects.create(user=self.user, ppa=self.other_ppa)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberPPA.objects.create(user=self.other, ppa=self.other_ppa, status='official_approval')

    def test_database_rejects_end_before_start(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberPPA.objects.create(user=self.user, ppa=self.ppa, started_at=timezone.now(), ended_at=timezone.now() - timedelta(days=1))

    def test_repeat_save_is_idempotent_and_preserves_notes_and_progress(self):
        first, _ = assign_ppa(self.user, self.ppa)
        update_progress(self.user, first.pk, status=MemberPPA.Status.REPORTED, personal_notes='Private reporting note')
        repeated, created = assign_ppa(self.user, self.ppa)
        self.assertFalse(created)
        self.assertEqual(repeated.pk, first.pk)
        self.assertEqual(repeated.status, MemberPPA.Status.REPORTED)
        self.assertEqual(repeated.personal_notes, 'Private reporting note')
        self.assertEqual(MemberPPA.objects.count(), 1)

    def test_changing_ppa_retains_previous_record_and_progress(self):
        first, _ = assign_ppa(self.user, self.ppa)
        update_progress(self.user, first.pk, status=MemberPPA.Status.DECLINED, personal_notes='My private reason')
        current, created = assign_ppa(self.user, self.other_ppa)
        self.assertTrue(created)
        first.refresh_from_db()
        self.assertIsNotNone(first.ended_at)
        self.assertEqual(first.personal_notes, 'My private reason')
        self.assertEqual(first.status, MemberPPA.Status.DECLINED)
        self.assertEqual(current.status, MemberPPA.Status.YET_TO_REPORT)
        self.assertEqual(MemberPPA.objects.filter(user=self.user, ended_at__isnull=True).count(), 1)

    def test_failed_replacement_rolls_back_archiving(self):
        first, _ = assign_ppa(self.user, self.ppa)
        with patch('service_year.services.MemberPPA.objects.create', side_effect=IntegrityError('Simulated insert failure')):
            with self.assertRaises(IntegrityError):
                assign_ppa(self.user, self.other_ppa)
        first.refresh_from_db()
        self.assertIsNone(first.ended_at)
        self.assertEqual(MemberPPA.objects.count(), 1)

    def test_personal_progress_history_does_not_duplicate_notes_or_change_ppa_trust(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        update_progress(self.user, assignment.pk, status=MemberPPA.Status.ACCEPTED, personal_notes='Only visible to me')
        change = StatusChange.objects.get(assignment=assignment)
        self.assertEqual(change.previous_status, MemberPPA.Status.YET_TO_REPORT)
        self.assertEqual(change.new_status, MemberPPA.Status.ACCEPTED)
        self.assertFalse(hasattr(change, 'personal_notes'))
        self.ppa.refresh_from_db()
        self.assertFalse(self.ppa.verified)
        self.assertEqual(self.ppa.status, PPA.Status.DISCOVERED)

    def test_end_is_idempotent_and_later_ppa_can_be_assigned(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        end_assignment(self.user, assignment.pk)
        end_assignment(self.user, assignment.pk)
        self.assertFalse(MemberPPA.objects.filter(user=self.user, ended_at__isnull=True).exists())
        replacement, _ = assign_ppa(self.user, self.ppa)
        self.assertNotEqual(replacement.pk, assignment.pk)


@override_settings(ROOT_URLCONF='config.urls')
class ServiceYearPrivacyTests(ServiceYearDataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_private_routes_require_authentication_and_do_not_create_records_on_get(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        self.client.logout()
        for url in [reverse('service_year_dashboard'), reverse('assignment_history'), reverse('service_profile'), reverse('assignment_update', args=[assignment.pk]), reverse('assign_ppa', args=[self.other_ppa.pk])]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(MemberPPA.objects.count(), 1)
        self.assertFalse(ServiceProfile.objects.exists())

    def test_assignment_requires_post_csrf_and_explicit_confirmation(self):
        url = reverse('assign_ppa', args=[self.ppa.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(MemberPPA.objects.exists())
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url, {'confirm_assignment': 'on'}).status_code, 403)
        self.assertFalse(MemberPPA.objects.exists())
        response = self.client.post(url, {'confirm_assignment': 'on', 'user': self.other.pk})
        self.assertRedirects(response, reverse('service_year_dashboard'))
        self.assertEqual(MemberPPA.objects.get().user, self.user)

    def test_personal_records_and_status_history_cannot_be_read_or_changed_by_another_member(self):
        assignment, _ = assign_ppa(self.other, self.other_ppa)
        update_progress(self.other, assignment.pk, status=MemberPPA.Status.REPORTED, personal_notes='Other member secret')
        url = reverse('assignment_update', args=[assignment.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'status': 'accepted', 'personal_notes': 'Forged'}).status_code, 404)
        self.assertEqual(self.client.post(reverse('end_assignment', args=[assignment.pk]), {'confirm_end': 'on'}).status_code, 404)
        self.assertNotContains(self.client.get(reverse('assignment_history')), self.other_ppa.name)
        response = self.client.get(reverse('service_year_dashboard'))
        self.assertNotContains(response, 'Other member secret')
        self.assertIsNone(response.context['current_assignment'])
        assignment.refresh_from_db()
        self.assertEqual(assignment.personal_notes, 'Other member secret')
        self.assertIsNone(assignment.ended_at)

    def test_all_private_mutations_require_csrf(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        for url, data in [
            (reverse('assignment_update', args=[assignment.pk]), {'status': 'reported', 'personal_notes': 'Forged'}),
            (reverse('end_assignment', args=[assignment.pk]), {'confirm_end': 'on'}),
            (reverse('service_profile'), {'stage': 'completed', 'batch': 'Forged'}),
        ]:
            with self.subTest(url=url):
                self.assertEqual(csrf_client.post(url, data).status_code, 403)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, MemberPPA.Status.YET_TO_REPORT)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(ServiceProfile.objects.exists())

    def test_update_validates_status_notes_and_ignores_ownership_fields(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        url = reverse('assignment_update', args=[assignment.pk])
        self.assertEqual(self.client.post(url, {'status': 'official_approval', 'personal_notes': ''}).status_code, 400)
        self.assertEqual(self.client.post(url, {'status': 'reported', 'personal_notes': 'x' * 2001}).status_code, 400)
        self.assertEqual(self.client.post(url, {'status': 'seeking_guidance', 'personal_notes': '<script>alert(1)</script>', 'user': self.other.pk, 'ppa': self.other_ppa.pk}).status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.user, self.user)
        self.assertEqual(assignment.ppa, self.ppa)
        response = self.client.get(url)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def test_archived_assignment_is_read_only_and_ending_requires_confirmation(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        end_url = reverse('end_assignment', args=[assignment.pk])
        self.assertEqual(self.client.get(end_url).status_code, 405)
        self.assertEqual(self.client.post(end_url, {}).status_code, 400)
        assignment.refresh_from_db()
        self.assertIsNone(assignment.ended_at)
        self.assertEqual(self.client.post(end_url, {'confirm_end': 'on'}).status_code, 302)
        self.assertEqual(self.client.post(reverse('assignment_update', args=[assignment.pk]), {'status': 'reported', 'personal_notes': 'Rewritten'}).status_code, 400)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, MemberPPA.Status.YET_TO_REPORT)
        self.assertEqual(assignment.personal_notes, '')

    def test_unverified_member_can_use_private_tracker_without_public_confirmation(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('assign_ppa', args=[self.ppa.pk]), {'confirm_assignment': 'on'}).status_code, 302)
        response = self.client.get(reverse('service_year_dashboard'))
        self.assertContains(response, 'Verify your email')
        self.assertFalse(Confirmation.objects.exists())

    def test_assignment_throttling(self):
        url = reverse('assign_ppa', args=[self.ppa.pk])
        for _ in range(15):
            self.assertEqual(self.client.post(url, {'confirm_assignment': 'on'}).status_code, 302)
        self.assertEqual(self.client.post(url, {'confirm_assignment': 'on'}).status_code, 429)
        self.assertEqual(MemberPPA.objects.count(), 1)

    def test_private_pages_are_not_cached_and_never_render_identity_fields(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        for url in [reverse('service_year_dashboard'), reverse('assignment_history'), reverse('service_profile'), reverse('assignment_update', args=[assignment.pk]), reverse('assign_ppa', args=[self.ppa.pk])]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response['Cache-Control'])
                self.assertNotContains(response, self.user.state_code)
                self.assertNotContains(response, self.user.email)
                self.assertNotContains(response, self.user.full_name)

    def test_tracker_labels_remain_personal_and_forthcoming_modules_have_no_fake_links(self):
        assignment, _ = assign_ppa(self.user, self.ppa)
        progress = self.client.get(reverse('assignment_update', args=[assignment.pk]))
        self.assertContains(progress, 'Private personal tracker')
        self.assertContains(progress, 'does not grant posting, transfer, clearance or other NYSC approval')
        response = self.client.get(reverse('service_year_dashboard'))
        self.assertContains(response, 'planned for later checkpoints')
        self.assertContains(response, 'They are not available yet')
        self.assertNotContains(response, 'href="#"')

    def test_optional_profile_needs_no_backfill_and_only_edits_own_record(self):
        self.assertEqual(self.client.get(reverse('service_year_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('service_profile')).status_code, 200)
        self.assertFalse(ServiceProfile.objects.exists())
        response = self.client.post(reverse('service_profile'), {'stage': '', 'batch': '', 'user': self.other.pk})
        self.assertEqual(response.status_code, 302)
        profile = ServiceProfile.objects.get()
        self.assertEqual(profile.user, self.user)
        self.assertEqual(profile.stage, ServiceProfile.Stage.UNKNOWN)
        self.assertEqual(profile.batch, '')
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertFalse(ServiceProfileForm({'stage': 'official', 'batch': ''}).is_valid())
        self.assertFalse(ServiceProfileForm({'stage': 'serving', 'batch': 'x' * 51}).is_valid())


class DashboardQueryTests(ServiceYearDataMixin, TestCase):
    def test_query_count_is_constant_with_growing_private_activity(self):
        with self.assertNumQueries(6):
            context = dashboard_context(self.user)
            self.assertIsNone(context['current_assignment'])
        assign_ppa(self.user, self.ppa)
        for index in range(10):
            place = PPA.objects.create(name=f'Saved School {index}', state=self.state, lga=self.lga, address='Address')
            Bookmark.objects.create(user=self.user, ppa=place)
            PlaceReport.objects.create(reporter=self.user, ppa=place, category='other', details='My report')
            Review.objects.create(user=self.user, ppa=place, rating=4, comment='My experience')
        Bookmark.objects.create(user=self.other, ppa=self.ppa)
        PlaceReport.objects.create(reporter=self.other, ppa=self.ppa, category='other', details='Other report')
        Review.objects.create(user=self.other, ppa=self.ppa, rating=1, comment='Other review')
        with self.assertNumQueries(6):
            context = dashboard_context(self.user)
            self.assertEqual(context['bookmark_count'], 10)
            self.assertEqual(context['report_count'], 10)
            self.assertEqual(context['pending_report_count'], 10)
            self.assertEqual(context['review_count'], 10)
            self.assertEqual(len(context['recent_bookmarks']), 3)
            self.assertEqual(context['current_assignment'].ppa.state.name, self.state.name)
            for bookmark in context['recent_bookmarks']:
                self.assertEqual(bookmark.ppa.lga.name, self.lga.name)


class AssignmentConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_first_saves_are_serialized_on_member(self):
        """Run on PostgreSQL in deployment checks; SQLite has no row locks."""
        state = State.objects.create(name='Concurrent State')
        lga = LGA.objects.create(name='Concurrent LGA', state=state)
        user = get_user_model().objects.create_user(state_code='CONCURRENT', email='concurrent@example.com')
        places = [PPA.objects.create(name=f'Concurrent PPA {i}', state=state, lga=lga, address='Address') for i in range(2)]
        barrier = Barrier(2)

        def save(ppa_id):
            close_old_connections()
            try:
                member = get_user_model().objects.get(pk=user.pk)
                place = PPA.objects.get(pk=ppa_id)
                barrier.wait(timeout=10)
                return assign_ppa(member, place)[0].pk
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(save, [place.pk for place in places]))
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(MemberPPA.objects.filter(user=user).count(), 2)
        self.assertEqual(MemberPPA.objects.filter(user=user, ended_at__isnull=True).count(), 1)
