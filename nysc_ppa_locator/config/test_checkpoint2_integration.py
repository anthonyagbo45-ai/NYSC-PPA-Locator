"""Cross-app journeys at the public-directory/private-member boundary."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from budgeting.models import Expense
from local_guides.models import FareDetails, Listing
from local_guides.services import moderate_listing, save_listing
from locations.models import LGA, State
from ppas.models import PPA, PPASubmission
from service_year.models import MemberPPA
from service_year.services import dashboard_context


class CheckpointTwoIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Integration Service State')
        cls.lga = LGA.objects.create(state=cls.state, name='Integration Central LGA')
        cls.other_lga = LGA.objects.create(state=cls.state, name='Integration Other LGA')
        cls.ppa = PPA.objects.create(
            name='Integration Community School', state=cls.state, lga=cls.lga,
            address='Synthetic public school road', source=PPA.Source.COMMUNITY,
        )
        cls.other_ppa = PPA.objects.create(
            name='Integration Other Clinic', state=cls.state, lga=cls.other_lga,
            address='Synthetic public clinic road', source=PPA.Source.COMMUNITY,
        )
        cls.member = get_user_model().objects.create_user(
            state_code='CI/PRIVATE/101', email='integration-member@example.invalid',
            full_name='Private Integration Name', email_verified=True,
        )
        cls.unverified = get_user_model().objects.create_user(
            state_code='CI/PRIVATE/102', email='integration-unverified@example.invalid',
            full_name='Private Unverified Name', state_posted=cls.state, lga_posted=cls.other_lga,
        )
        cls.moderator = get_user_model().objects.create_user(
            state_code='CI/PRIVATE/103', email='integration-moderator@example.invalid', is_staff=True,
        )
        cls.moderator.user_permissions.add(Permission.objects.get(
            content_type__app_label='local_guides', codename='can_moderate_listings',
        ))
        cls.fare = cls.make_fare(cls.ppa, 'Central public fare estimate')
        cls.other_fare = cls.make_fare(cls.other_ppa, 'Other LGA public fare estimate')

    @classmethod
    def make_fare(cls, ppa, title, *, approved=True):
        listing = save_listing(user=cls.member, cleaned_data={
            'kind': Listing.Kind.FARE, 'title': title, 'area': 'Synthetic public bus station',
            'summary': 'Community-reported transport context from a synthetic fixture.',
            'state': ppa.state, 'lga': ppa.lga, 'ppa': ppa,
            'source_type': Listing.Source.MEMBER, 'source_url': '', 'observed_on': timezone.localdate(),
        }, details={
            'origin_area': 'Public bus station', 'destination_area': 'Public organisation entrance',
            'mode': FareDetails.Mode.BUS, 'amount_low': Decimal('200.00'), 'amount_high': Decimal('400.00'),
        }, submit=approved)
        if approved:
            listing = moderate_listing(listing=listing, actor=cls.moderator,
                                       values={'publication_status': Listing.Publication.APPROVED},
                                       reason='Reviewed synthetic community estimate for publication only.')
        return listing

    def setUp(self):
        cache.clear()

    def test_existing_guest_directory_and_detail_remain_free_with_new_module_links(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Local browsing must not call a provider')) as network:
            for name in ('landing', 'search_ppa', 'search_results', 'live_search'):
                with self.subTest(route=name):
                    response = self.client.get(reverse(name), {
                        'state': self.state.pk, 'lga': self.lga.pk, 'q': self.ppa.name,
                    })
                    self.assertContains(response, self.ppa.name)
                    self.assertContains(response, reverse('local_guides:home'))
                    self.assertContains(response, reverse('ppa_submissions:overview'))
                    self.assertNotContains(response, self.other_ppa.name)
            detail = self.client.get(reverse('ppa_detail', args=[self.ppa.pk]))
            scoped_link = reverse('local_guides:home') + f'?state={self.state.pk}&amp;lga={self.lga.pk}&amp;ppa={self.ppa.pk}'
            self.assertContains(detail, scoped_link)
            self.assertContains(detail, 'Get directions')
            network.assert_not_called()

    def test_guest_local_guide_and_manual_intro_make_no_google_requests(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Local browsing must not call a provider')) as network:
            response = self.client.get(reverse('local_guides:home'), {'state': self.state.pk, 'lga': self.lga.pk})
            self.assertContains(response, self.fare.title)
            self.assertNotContains(response, self.other_fare.title)
            self.assertNotContains(response, self.member.email)
            self.assertNotContains(response, self.member.state_code)
            detail = self.client.get(reverse('local_guides:detail', args=[self.fare.pk]))
            self.assertContains(detail, self.fare.title)
            self.assertContains(detail, 'not independently checked')
            intro = self.client.get(reverse('ppa_submissions:overview'))
            self.assertContains(intro, 'Place ID is not required')
            self.assertContains(intro, reverse('ppa_submissions:submit'))
            network.assert_not_called()

    def test_dashboard_uses_assigned_ppa_area_without_extra_queries_or_public_affiliation(self):
        MemberPPA.objects.create(user=self.unverified, ppa=self.ppa)
        self.client.force_login(self.unverified)
        with self.assertNumQueries(6):
            context = dashboard_context(self.unverified)
        self.assertEqual(context['current_assignment'].ppa_id, self.ppa.pk)
        # CP3 adds two reminder-summary reads and one private CDS selection read.
        with self.assertNumQueries(11):
            response = self.client.get(reverse('service_year_dashboard'))
        scoped_link = reverse('local_guides:home') + f'?state={self.state.pk}&amp;lga={self.lga.pk}'
        self.assertContains(response, scoped_link)
        self.assertContains(response, reverse('budgeting:home'))
        self.assertContains(response, reverse('local_guides:saved'))
        self.assertContains(response, reverse('local_guides:mine'))
        self.assertContains(response, reverse('ppa_submissions:overview'))
        self.assertNotContains(response, self.unverified.state_code)
        self.client.logout()
        public = self.client.get(reverse('ppa_detail', args=[self.ppa.pk]))
        self.assertNotContains(public, self.unverified.full_name)
        self.assertNotContains(public, self.unverified.email)
        self.assertNotContains(public, self.unverified.state_code)

    def test_unverified_member_private_tools_work_while_public_contributions_are_gated(self):
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.get(reverse('budgeting:home')).status_code, 200)
        response = self.client.post(reverse('budgeting:expense_add'), {
            'amount': '350.25', 'category': 'transport', 'spent_on': timezone.localdate().isoformat(),
            'note': 'PRIVATE BUDGET INTEGRATION MARKER',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Expense.objects.get(user=self.unverified).amount, Decimal('350.25'))
        self.assertRedirects(self.client.get(reverse('local_guides:create', args=['fare'])), reverse('email_verification'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('ppa_submissions:submit')), reverse('email_verification'), fetch_redirect_response=False)
        self.assertEqual(self.client.post(reverse('local_guides:bookmark', args=[self.fare.pk]), {'action': 'save'}).status_code, 302)
        self.assertContains(self.client.get(reverse('local_guides:saved')), self.fare.title)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('local_guides:create', args=['fare'])).status_code, 200)
        self.assertEqual(self.client.get(reverse('ppa_submissions:submit')).status_code, 200)
        self.assertNotContains(self.client.get(reverse('budgeting:home')), 'PRIVATE BUDGET INTEGRATION MARKER')

    def test_budget_and_author_drafts_are_private_no_store_across_modules(self):
        draft = self.make_fare(self.ppa, 'PRIVATE LOCAL DRAFT INTEGRATION MARKER', approved=False)
        self.client.force_login(self.member)
        proposal = self.client.post(reverse('ppa_submissions:submit'), {
            'name': 'PRIVATE PPA DRAFT INTEGRATION MARKER', 'address': 'Synthetic independently known organisation road',
            'state': self.state.pk, 'lga': self.lga.pk, 'category': 'Education',
            'source_type': 'member', 'source_url': '', 'observed_on': timezone.localdate().isoformat(),
            'privacy_confirmed': 'on', 'action': 'draft',
        })
        self.assertEqual(proposal.status_code, 302)
        own_proposal = PPASubmission.objects.get(author=self.member)
        for url in (
            reverse('budgeting:home'), reverse('local_guides:detail', args=[draft.pk]),
            reverse('local_guides:mine'), reverse('local_guides:history', args=[draft.pk]),
            reverse('ppa_submissions:mine'), reverse('ppa_submissions:history', args=[own_proposal.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn('private', response['Cache-Control'])
                self.assertIn('no-store', response['Cache-Control'])
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.get(reverse('local_guides:detail', args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('ppa_submissions:history', args=[own_proposal.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('local_guides:detail', args=[draft.pk])).status_code, 404)
        for url in (reverse('local_guides:home'), reverse('search_ppa')):
            response = self.client.get(url, {'state': self.state.pk, 'lga': self.lga.pk})
            self.assertNotContains(response, draft.title)
            self.assertNotContains(response, own_proposal.name)
        self.assertFalse(PPA.objects.filter(name=own_proposal.name).exists())

    def test_role_bootstrap_is_app_scoped_and_does_not_grant_financial_or_verifier_access(self):
        account_type = ContentType.objects.get_for_model(get_user_model())
        unrelated = Permission.objects.create(content_type=account_type, codename='can_moderate_listings', name='Unrelated test permission')
        moderator_group, _ = Group.objects.get_or_create(name='Moderator')
        moderator_group.permissions.add(unrelated)
        call_command('bootstrap_roles', stdout=StringIO())
        call_command('bootstrap_roles', stdout=StringIO())
        permissions = set(moderator_group.permissions.values_list('content_type__app_label', 'codename'))
        self.assertIn(('local_guides', 'can_moderate_listings'), permissions)
        self.assertIn(('local_guides', 'can_moderate_listing_reports'), permissions)
        self.assertIn(('ppas', 'can_moderate_submissions'), permissions)
        self.assertNotIn(('accounts', 'can_moderate_listings'), permissions)
        self.assertNotIn(('local_guides', 'can_verify_listings'), permissions)
        self.assertNotIn(('ppas', 'can_verify_organisations'), permissions)
        self.assertNotIn(('service_resources', 'can_verify_resources'), permissions)
        self.assertTrue(all(app in ('ppas', 'local_guides', 'service_resources', 'community', 'local_rooms') for app, _ in permissions))
        self.assertTrue({'community', 'local_rooms'}.issubset({app for app, _ in permissions}))
        self.assertFalse(any(app in ('notifications', 'budgeting', 'accounts', 'auth') for app, _ in permissions))
        self.member.refresh_from_db()
        self.unverified.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)
        self.assertTrue(self.member.email_verified)
        self.assertFalse(self.unverified.email_verified)
        self.assertFalse(self.member.groups.exists())
        self.assertFalse(self.unverified.groups.exists())
        self.assertFalse(Group.objects.get(name='VettedContributor').permissions.exists())

    def test_expired_local_context_is_removed_from_fresh_discovery_and_labelled_in_saved_details(self):
        Listing.objects.filter(pk=self.fare.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(reverse('local_guides:home'), {'state': self.state.pk, 'lga': self.lga.pk})
        self.assertNotContains(response, self.fare.title)
        detail = self.client.get(reverse('local_guides:detail', args=[self.fare.pk]))
        self.assertContains(detail, self.fare.title)
        self.assertContains(detail, 'historical; reconfirmation needed')
        self.assertNotContains(detail, self.member.state_code)
