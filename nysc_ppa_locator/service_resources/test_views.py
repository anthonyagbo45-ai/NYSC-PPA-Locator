"""Public guidance and private planning must keep their separate authority."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification, NotificationPreference, PersonalEvent
from notifications.services import dispatch_user_reminders, save_event, save_preferences, subscribe_notice
from .models import CDSGroup, CDSPreference, ResourceReport, ServiceNotice
from .services import moderate_group, moderate_notice, save_group, save_notice, select_group


class ResourceViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from locations.models import State, LGA
        cls.state = State.objects.create(name='Resource UI State')
        cls.lga = LGA.objects.create(name='Resource UI LGA', state=cls.state)
        cls.other_lga = LGA.objects.create(name='Other Resource UI LGA', state=cls.state)
        users = get_user_model().objects
        cls.member = users.create_user(state_code='CP3/VIEW/1', email='view-member@example.invalid', email_verified=True)
        cls.other = users.create_user(state_code='CP3/VIEW/2', email='view-other@example.invalid', email_verified=True)
        cls.unverified = users.create_user(state_code='CP3/VIEW/3', email='view-unverified@example.invalid')
        cls.moderator = users.create_user(state_code='CP3/VIEW/M', email='view-moderator@example.invalid', is_staff=True)
        cls.moderator.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='service_resources',
            codename__in=['can_moderate_resources', 'can_verify_resources', 'can_moderate_resource_reports'],
        ))
        cls.group = cls.make_group('Group A context')
        cls.second_group = cls.make_group('Group B context')
        cls.far_group = cls.make_group('Different LGA group', lga=cls.other_lga)
        cls.community = cls.make_notice('Community meeting report', group=cls.group)
        cls.clearance = cls.make_notice('Checked clearance context', kind='clearance', checked=True)

    @classmethod
    def group_data(cls, **changes):
        return dict(title='A group', description='Independently reported public group context.',
                    venue_hint='Public community hall', public_contact_url='', state=cls.state, lga=cls.lga,
                    source_type='member', source_url='', observed_on=timezone.localdate(), **changes)

    @classmethod
    def make_group(cls, title, **changes):
        values = cls.group_data()
        values.update(title=title, **changes)
        group = save_group(user=cls.member, cleaned_data=values, submit=True)
        return moderate_group(group=group, actor=cls.moderator,
                              values={'publication_status': 'approved'}, reason='Synthetic public context reviewed.')

    @classmethod
    def make_notice(cls, title, *, kind='cds', group=None, checked=False):
        notice = save_notice(user=cls.member, cleaned_data={
            'title': title, 'body': 'Confirm with your designated NYSC official.', 'kind': kind,
            'state': cls.state, 'lga': cls.lga, 'group': group, 'source_type': 'member',
            'source_url': '', 'observed_on': timezone.localdate(),
            'event_at': timezone.now()+timedelta(minutes=30), 'timezone_name': 'Africa/Lagos',
        }, submit=True)
        values = {'publication_status': 'approved'}
        if checked:
            values.update(source_type='official', source_url='https://www.nysc.gov.ng/synthetic-test-reference',
                          checked_at=timezone.now(), verification_status='official',
                          moderator_note='PRIVATE synthetic evidence only; no real official notice was checked.')
        return moderate_notice(notice=notice, actor=cls.moderator, values=values,
                               reason='Synthetic source and privacy review.')

    def setUp(self):
        cache.clear()

    def payload(self, **changes):
        values = {'title': 'Private proposed schedule', 'kind': 'cds', 'body': 'Independent public context.',
                  'state': self.state.pk, 'lga': self.lga.pk, 'group': self.group.pk,
                  'source_type': 'member', 'source_url': '', 'observed_on': timezone.localdate().isoformat(),
                  'timezone_name': 'Africa/Lagos', 'privacy_confirmed': 'on', 'action': 'draft'}
        values.update(changes)
        return values

    def test_guest_hubs_and_scoped_groups_are_free_and_do_not_fetch_sources(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No live fetch')) as network:
            for name in ['home', 'cds', 'clearance']:
                response = self.client.get(reverse('service_resources:'+name))
                self.assertContains(response, 'designated NYSC official')
                self.assertContains(response, 'https://www.nysc.gov.ng/')
            response = self.client.get(reverse('service_resources:cds'), {
                'view': 'groups', 'state': self.state.pk, 'lga': self.lga.pk,
            })
            self.assertContains(response, self.group.title)
            self.assertContains(response, self.second_group.title)
            self.assertNotContains(response, self.far_group.title)
            network.assert_not_called()

    def test_group_and_topic_filters_do_not_assume_everyone_has_same_schedule(self):
        response = self.client.get(reverse('service_resources:home'), {
            'state': self.state.pk, 'lga': self.lga.pk, 'group': self.group.pk,
        })
        self.assertContains(response, self.community.title)
        self.assertNotContains(response, self.clearance.title)
        response = self.client.get(reverse('service_resources:home'), {
            'state': self.state.pk, 'lga': self.other_lga.pk, 'group': self.group.pk,
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, self.community.title, status_code=400)

    def test_community_source_never_offers_confirmed_notice_reminders(self):
        page = self.client.get(reverse('service_resources:notice_detail', args=[self.community.pk]))
        self.assertContains(page, 'not independently checked')
        self.assertNotContains(page, 'Choose a source-linked reminder')
        self.assertContains(page, 'private personal reminder')
        page = self.client.get(reverse('service_resources:notice_detail', args=[self.clearance.pk]))
        self.assertContains(page, 'Choose a source-linked reminder')
        self.assertNotContains(page, 'PRIVATE synthetic evidence')
        self.assertNotContains(page, self.member.email)
        self.assertNotContains(page, self.member.state_code)

    def test_expired_notices_leave_discovery_but_detail_is_historical(self):
        ServiceNotice.objects.filter(pk=self.community.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        home = self.client.get(reverse('service_resources:home'))
        self.assertNotContains(home, self.community.title)
        detail = self.client.get(reverse('service_resources:notice_detail', args=[self.community.pk]))
        self.assertContains(detail, 'Historical information')
        self.assertNotContains(detail, 'Choose a source-linked reminder')

    def test_author_draft_and_forged_grants_stay_private(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse('service_resources:create_notice'), self.payload(
            publication_status='approved', verification_status='official', author=self.other.pk,
            moderator_note='FORGED EVIDENCE',
        ))
        self.assertEqual(response.status_code, 302)
        draft = ServiceNotice.objects.get(title='Private proposed schedule')
        self.assertEqual((draft.publication_status, draft.verification_status, draft.author_id),
                         ('draft', 'community', self.member.pk))
        self.client.force_login(self.other)
        for route in ['notice_detail', 'edit_notice', 'notice_history']:
            self.assertEqual(self.client.get(reverse('service_resources:'+route, args=[draft.pk])).status_code, 404)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('service_resources:home')), draft.title)

    def test_cross_state_submission_errors_preserve_existing_records(self):
        from locations.models import State, LGA
        state = State.objects.create(name='Cross state')
        lga = LGA.objects.create(name='Cross LGA', state=state)
        self.client.force_login(self.member)
        before = ServiceNotice.objects.count()
        result = self.client.post(reverse('service_resources:create_notice'), self.payload(lga=lga.pk))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(ServiceNotice.objects.count(), before)

    def test_saving_group_returns_to_group_contributions_instead_of_notices(self):
        self.client.force_login(self.member)
        values = self.payload(title='New privately proposed group', description='Public group context.')
        response = self.client.post(reverse('service_resources:create_group'), values)
        self.assertRedirects(response, reverse('service_resources:mine')+'?view=groups')
        self.assertEqual(CDSGroup.objects.get(title='New privately proposed group').publication_status, 'draft')

    def test_private_selection_and_dashboard_hide_expired_or_private_group_titles(self):
        select_group(user=self.member, group=self.group)
        self.client.force_login(self.member)
        with self.assertNumQueries(11):
            response = self.client.get(reverse('service_year_dashboard'))
        self.assertContains(response, self.group.title)
        self.assertContains(response, 'not verified group membership')
        CDSGroup.objects.filter(pk=self.group.pk).update(
            title='PRIVATE CHANGED GROUP', publication_status='draft', shared_for_review=False,
        )
        for url in [reverse('service_year_dashboard'), reverse('service_resources:select_group'),
                    reverse('service_resources:home')]:
            self.assertNotContains(self.client.get(url), 'PRIVATE CHANGED GROUP')
        self.assertEqual(CDSPreference.objects.get(user=self.member).group_id, self.group.pk)

    def test_expired_selection_can_be_cleared_without_revealing_new_details(self):
        select_group(user=self.member, group=self.group)
        CDSGroup.objects.filter(pk=self.group.pk).update(title='Outdated selected group', expires_at=timezone.now()-timedelta(seconds=1))
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse('service_resources:home')), 'Outdated selected group')
        result = self.client.post(reverse('service_resources:select_group'), {'group': '', 'confirmed': 'on'})
        self.assertEqual(result.status_code, 302)
        self.assertIsNone(CDSPreference.objects.get(user=self.member).group_id)

    def test_read_routes_create_no_private_preferences_or_notifications(self):
        self.client.force_login(self.unverified)
        counts = [model.objects.count() for model in [CDSPreference, NotificationPreference, PersonalEvent, Notification]]
        for url in [reverse('service_year_dashboard'), reverse('service_resources:home'),
                    reverse('notifications:home'), reverse('notifications:inbox'),
                    reverse('notifications:preferences')]:
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(counts, [model.objects.count() for model in [CDSPreference, NotificationPreference, PersonalEvent, Notification]])

    def test_changed_source_suppresses_current_inbox_and_dashboard_count(self):
        save_preferences(self.unverified, notice_reminders=True)
        subscribe_notice(self.unverified, self.clearance, remind_before=60)
        self.assertEqual(dispatch_user_reminders(self.unverified), 1)
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.get(reverse('notifications:inbox')).context['unread_count'], 1)
        save_notice(user=self.member, notice=self.clearance, cleaned_data={'title': 'PRIVATE NEW SOURCE TITLE'}, submit=False)
        for url in [reverse('notifications:home'), reverse('notifications:inbox'), reverse('service_year_dashboard')]:
            result = self.client.get(url)
            self.assertNotContains(result, 'PRIVATE NEW SOURCE TITLE')
            self.assertNotContains(result, 'PRIVATE synthetic evidence')
        self.assertEqual(self.client.get(reverse('notifications:inbox')).context['unread_count'], 0)

    def test_all_resource_writes_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        for name, args in [('create_group', []), ('create_notice', []), ('edit_group', [self.group.pk]),
                           ('edit_notice', [self.community.pk]), ('archive_group', [self.group.pk]),
                           ('archive_notice', [self.community.pk]), ('report_notice', [self.community.pk]),
                           ('report_group', [self.group.pk]), ('select_group', [])]:
            with self.subTest(route=name):
                self.assertEqual(client.post(reverse('service_resources:'+name, args=args), {}).status_code, 403)

    def test_reports_keep_staff_evidence_and_other_reporters_private(self):
        self.client.force_login(self.unverified)
        result = self.client.post(reverse('service_resources:report_notice', args=[self.community.pk]), {
            'category': 'misleading', 'details': 'PRIVATE REPORT MARKER', 'proposed_correction': 'Check the source.',
        })
        self.assertEqual(result.status_code, 302)
        report = ResourceReport.objects.get(reporter=self.unverified)
        ResourceReport.objects.filter(pk=report.pk).update(moderator_note='PRIVATE STAFF ONLY')
        own = self.client.get(reverse('service_resources:reports'))
        self.assertContains(own, 'PRIVATE REPORT MARKER')
        self.assertNotContains(own, 'PRIVATE STAFF ONLY')
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('service_resources:reports')), 'PRIVATE REPORT MARKER')

    def test_public_pagination_keeps_safe_projections_and_query_count_bounded(self):
        # Import-style synthetic public data exercises the listing path at scale.
        ServiceNotice.objects.bulk_create([ServiceNotice(
            title=f'Extra context {i:02}', body='Safe public context', kind='clearance',
            state=self.state, lga=self.lga, observed_on=timezone.localdate(),
            expires_at=timezone.now()+timedelta(days=1), publication_status='approved',
            moderator_note='PRIVATE BULK EVIDENCE',
        ) for i in range(45)])
        with CaptureQueriesContext(connection) as queries:
            result = self.client.get(reverse('service_resources:clearance'), {'page': 2})
        self.assertEqual(len(result.context['resources']), 20)
        self.assertLessEqual(len(queries), 7)
        self.assertNotContains(result, 'PRIVATE BULK EVIDENCE')
        self.assertTrue(all(isinstance(item, dict) for item in result.context['page_obj'].object_list))

    def test_moderator_role_does_not_grant_verifier_or_private_calendar_access(self):
        from io import StringIO
        call_command('bootstrap_roles', stdout=StringIO())
        role = Group.objects.get(name='Moderator')
        self.assertTrue(role.permissions.filter(content_type__app_label='service_resources', codename='can_moderate_resources').exists())
        self.assertFalse(role.permissions.filter(codename='can_verify_resources').exists())
        self.assertFalse(role.permissions.filter(content_type__app_label='notifications').exists())
