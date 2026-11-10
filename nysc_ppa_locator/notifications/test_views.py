from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from locations.models import LGA, State
from service_resources.models import ServiceNotice
from .forms import EventForm
from .models import Notification, NotificationPreference, NoticeSubscription, PersonalEvent
from . import services


class ReminderViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user(state_code='REM/MEMBER', email='rem-member@example.com', email_verified=False)
        cls.other = users.create_user(state_code='REM/OTHER', email='rem-other@example.com', email_verified=True)
        cls.staff = users.create_user(state_code='REM/STAFF', email='rem-staff@example.com', is_staff=True)
        cls.state = State.objects.create(name='Reminder state')
        cls.lga = LGA.objects.create(name='Reminder LGA', state=cls.state)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def event_data(self, **changes):
        return {
            'title': 'My private planning event', 'note': 'Private personal note',
            'kind': PersonalEvent.Kind.CLEARANCE,
            'starts_at': timezone.now() + timedelta(days=1), 'ends_at': None,
            'timezone_name': 'Africa/Lagos', 'remind_before': 60,
            'notifications_enabled': False, 'status': PersonalEvent.Status.ACTIVE,
            **changes,
        }

    def event(self, user=None, **changes):
        return services.save_event(user or self.user, cleaned_data=self.event_data(**changes))

    def form_data(self, **changes):
        date = timezone.localdate() + timedelta(days=1)
        return {
            'title': 'Private calendar entry', 'kind': 'clearance',
            'event_date': date.isoformat(), 'event_time': '09:15',
            'timezone_name': 'Africa/Lagos', 'end_date': '', 'end_time': '',
            'note': 'Private form note', 'status': 'active', 'remind_before': '60',
            **changes,
        }

    def notice(self, **changes):
        now = timezone.now()
        return ServiceNotice.objects.create(
            author=self.other, title='Confirmed public notice', body='Public source context',
            kind=ServiceNotice.Kind.CLEARANCE, state=self.state, lga=self.lga,
            observed_on=timezone.localdate(), checked_at=now - timedelta(minutes=1),
            source_url='https://example.org/public-source', source_type='member',
            expires_at=now + timedelta(days=3), event_at=now + timedelta(hours=2),
            timezone_name='Africa/Lagos', publication_status='approved',
            verification_status='checked', shared_for_review=True,
            moderator_note='PRIVATE source review evidence', **changes,
        )

    def due_event(self, **changes):
        services.save_preferences(self.user, personal_reminders=True, notice_reminders=False)
        event = self.event(starts_at=timezone.now() - timedelta(minutes=5), remind_before=0, notifications_enabled=True, **changes)
        services.dispatch_user_reminders(self.user)
        return event, Notification.objects.get(event=event)

    def test_guest_private_routes_require_sign_in_and_public_hubs_remain_free(self):
        self.client.logout()
        for name in ('home', 'inbox', 'preferences', 'event_add'):
            response = self.client.get(reverse('notifications:' + name))
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].startswith('/login/'))
        self.assertEqual(self.client.get(reverse('service_resources:home')).status_code, 200)

    def test_get_views_do_not_create_preferences_events_subscriptions_or_deliveries(self):
        notice = self.notice()
        for url in (reverse('notifications:home'), reverse('notifications:inbox'), reverse('notifications:preferences'),
                    reverse('notifications:event_add'), reverse('notifications:subscribe_notice', args=[notice.pk])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response.headers['Cache-Control'])
        for model in (NotificationPreference, PersonalEvent, NoticeSubscription, Notification):
            self.assertEqual(model.objects.count(), 0)

    def test_unverified_member_can_create_private_event_and_forged_grants_are_ignored(self):
        response = self.client.post(reverse('notifications:event_add'), self.form_data(
            user=str(self.other.pk), version='999', verification_status='official', notice_id='123', notifications_enabled='on'))
        self.assertRedirects(response, reverse('notifications:home'))
        event = PersonalEvent.objects.get()
        self.assertEqual(event.user_id, self.user.pk)
        self.assertEqual(event.version, 1)
        self.assertTrue(event.notifications_enabled)
        self.assertEqual(event.starts_at.hour, 8)  # Nigeria 09:15 is UTC 08:15.
        self.assertEqual(event.starts_at.minute, 15)
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(NotificationPreference.objects.exists())

    def test_event_forms_reject_unknown_zone_dst_gap_and_fold(self):
        for changes in (
            {'timezone_name': 'Unknown/Not-A-Zone'},
            {'timezone_name': 'America/New_York', 'event_date': '2026-03-08', 'event_time': '02:30'},
            {'timezone_name': 'America/New_York', 'event_date': '2026-11-01', 'event_time': '01:30'},
        ):
            response = self.client.post(reverse('notifications:event_add'), self.form_data(**changes))
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(PersonalEvent.objects.exists())

    def test_event_end_pair_and_order_are_validated_before_writes(self):
        for changes in ({'end_date': '2026-12-01'}, {'end_time': '08:00'},
                        {'event_date': '2026-12-02', 'end_date': '2026-12-01', 'end_time': '08:00'}):
            form = EventForm(self.form_data(**changes))
            self.assertFalse(form.is_valid())
        self.assertFalse(PersonalEvent.objects.exists())

    def test_cross_member_and_staff_cannot_read_or_modify_private_event_ids(self):
        event = self.event()
        for user in (self.other, self.staff):
            self.client.force_login(user)
            for name in ('event_edit', 'event_delete'):
                url = reverse('notifications:' + name, args=[event.pk])
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, self.form_data(title='Forged replacement')).status_code, 404)
        event.refresh_from_db()
        self.assertEqual(event.title, 'My private planning event')

    def test_preferences_are_explicit_owner_scoped_and_default_off(self):
        response = self.client.get(reverse('notifications:preferences'))
        self.assertFalse(response.context['form'].initial['personal_reminders'])
        self.assertFalse(response.context['form'].initial['notice_reminders'])
        self.client.post(reverse('notifications:preferences'), {'personal_reminders': 'on', 'user': self.other.pk})
        preference = NotificationPreference.objects.get()
        self.assertEqual(preference.user_id, self.user.pk)
        self.assertTrue(preference.personal_reminders)
        self.assertFalse(preference.notice_reminders)
        self.client.post(reverse('notifications:preferences'), {})
        preference.refresh_from_db()
        self.assertFalse(preference.personal_reminders)

    def test_check_due_is_explicit_idempotent_and_inbox_does_not_publish_private_notes(self):
        event = self.event(starts_at=timezone.now() - timedelta(minutes=2), remind_before=0, notifications_enabled=True)
        services.save_preferences(self.user, personal_reminders=True, notice_reminders=False)
        self.client.get(reverse('notifications:inbox'))
        self.assertFalse(Notification.objects.exists())
        for _ in range(2):
            self.assertRedirects(self.client.post(reverse('notifications:check_due')), reverse('notifications:inbox'))
        self.assertEqual(Notification.objects.count(), 1)
        response = self.client.get(reverse('notifications:inbox'))
        self.assertContains(response, 'My private planning event')
        self.assertContains(response, 'Your personal reminder')
        self.assertNotContains(response, event.note)
        self.assertEqual(response.context['unread_count'], 1)

    def test_mark_read_dismiss_and_cross_member_ids(self):
        event, item = self.due_event()
        self.client.force_login(self.other)
        for name in ('read', 'dismiss'):
            self.assertEqual(self.client.post(reverse('notifications:' + name, args=[item.pk])).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('notifications:read', args=[item.pk])).status_code, 405)
        self.client.post(reverse('notifications:read', args=[item.pk]))
        response = self.client.get(reverse('notifications:inbox'))
        self.assertEqual(response.context['unread_count'], 0)
        self.assertContains(response, event.title)
        self.client.post(reverse('notifications:dismiss', args=[item.pk]))
        self.assertNotContains(self.client.get(reverse('notifications:inbox')), event.title)

    def test_completed_or_opted_out_events_disappear_from_unread_and_inbox(self):
        event, item = self.due_event()
        response = self.client.post(reverse('notifications:event_edit', args=[event.pk]), self.form_data(status='completed', notifications_enabled='on'))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse('notifications:inbox'))
        self.assertEqual(response.context['unread_count'], 0)
        self.assertNotContains(response, 'My private planning event')
        self.assertEqual(PersonalEvent.objects.get(pk=event.pk).status, 'completed')

    def test_delete_requires_confirmation_and_removes_only_owned_event_delivery(self):
        event, item = self.due_event()
        other = self.event(self.other, title='Other member event')
        url = reverse('notifications:event_delete', args=[event.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(PersonalEvent.objects.filter(pk=event.pk).exists())
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        self.client.post(url, {'confirm_delete': 'on'})
        self.assertFalse(PersonalEvent.objects.filter(pk=event.pk).exists())
        self.assertFalse(Notification.objects.filter(pk=item.pk).exists())
        self.assertTrue(PersonalEvent.objects.filter(pk=other.pk).exists())

    def test_unconfirmed_private_or_expired_notice_cannot_be_followed(self):
        for field, value in [('verification_status', 'community'), ('publication_status', 'draft'),
                             ('expires_at', timezone.now() - timedelta(minutes=1))]:
            notice = self.notice()
            values = {field: value}
            if field == 'publication_status' and value == 'draft':
                values['shared_for_review'] = False
            ServiceNotice.objects.filter(pk=notice.pk).update(**values)
            url = reverse('notifications:subscribe_notice', args=[notice.pk])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {'remind_before': '60', 'consent': 'on'}).status_code, 404)
        self.assertFalse(NoticeSubscription.objects.exists())

    def test_following_confirmed_notice_requires_explicit_consent_but_zero_lead_is_supported(self):
        notice = self.notice()
        url = reverse('notifications:subscribe_notice', args=[notice.pk])
        self.assertEqual(self.client.post(url, {'remind_before': '0'}).status_code, 400)
        self.assertFalse(NoticeSubscription.objects.exists())
        response = self.client.post(url, {'remind_before': '0', 'consent': 'on', 'user': self.other.pk, 'notice_version': '999'})
        self.assertEqual(response.status_code, 302)
        subscription = NoticeSubscription.objects.get()
        self.assertEqual(subscription.user_id, self.user.pk)
        self.assertEqual(subscription.notice_version, notice.version)
        self.assertEqual(subscription.remind_before, 0)
        self.assertFalse(Notification.objects.exists())

    def test_unsubscribe_is_owner_scoped_post_and_confirmed(self):
        subscription = services.subscribe_notice(self.user, notice=self.notice(), remind_before=60)
        url = reverse('notifications:unsubscribe_notice', args=[subscription.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        subscription.refresh_from_db()
        self.assertTrue(subscription.enabled)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(url, {'confirm': 'on'}).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url, {'confirm': 'on'}).status_code, 302)
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)

    def test_changed_private_notice_title_is_absent_from_subscription_and_notification_pages(self):
        notice = self.notice()
        services.save_preferences(self.user, personal_reminders=False, notice_reminders=True)
        services.subscribe_notice(self.user, notice=notice, remind_before=1440)
        services.dispatch_user_reminders(self.user)
        self.assertTrue(Notification.objects.exists())
        ServiceNotice.objects.filter(pk=notice.pk).update(publication_status='draft', shared_for_review=False, title='PRIVATE REPLACEMENT TITLE', version=2)
        for name in ('home', 'inbox'):
            response = self.client.get(reverse('notifications:' + name))
            self.assertNotContains(response, 'PRIVATE REPLACEMENT TITLE')
            self.assertNotContains(response, 'PRIVATE source review evidence')
            if name == 'inbox':
                self.assertEqual(response.context['unread_count'], 0)
            else:
                self.assertContains(response, 'Notice needs renewed confirmation')

    def test_event_timezone_is_displayed_without_server_timezone_reconversion(self):
        event = self.event(timezone_name='America/New_York')
        response = self.client.get(reverse('notifications:home'))
        expected = timezone.localtime(event.starts_at, ZoneInfo('America/New_York')).strftime('%H:%M')
        self.assertContains(response, expected)
        self.assertContains(response, 'America/New_York')

    def test_private_notes_are_escaped_not_executed(self):
        self.event(title='<script>alert(1)</script>', note='<img src=x onerror=alert(1)>')
        response = self.client.get(reverse('notifications:home'))
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<img src=x onerror=alert(1)>')

    def test_event_list_pagination_queries_do_not_grow_per_event(self):
        PersonalEvent.objects.bulk_create([PersonalEvent(user=self.user, title=f'Future event {number:02}', starts_at=timezone.now()+timedelta(days=number+1)) for number in range(45)])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('notifications:home'))
        self.assertEqual(len(response.context['events']), 20)
        self.assertLessEqual(len(queries), 12)
        self.assertContains(response, 'Future event 00')
        self.assertNotContains(response, 'Future event 20')
        self.assertEqual(len(self.client.get(reverse('notifications:home'), {'page': '3'}).context['events']), 5)

    def test_invalid_filters_show_errors_without_expanding_private_query(self):
        self.event()
        for name, params in [('home', {'period': 'everything-ever'}), ('inbox', {'status': 'all-users'})]:
            response = self.client.get(reverse('notifications:' + name), params)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.context['form'].errors)

    def test_all_write_routes_require_csrf(self):
        event, item = self.due_event()
        notice = self.notice()
        subscription = services.subscribe_notice(self.user, notice=notice, remind_before=60)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        urls = [reverse('notifications:event_add'), reverse('notifications:event_edit', args=[event.pk]),
                reverse('notifications:event_delete', args=[event.pk]), reverse('notifications:preferences'),
                reverse('notifications:check_due'), reverse('notifications:read', args=[item.pk]),
                reverse('notifications:dismiss', args=[item.pk]), reverse('notifications:subscribe_notice', args=[notice.pk]),
                reverse('notifications:unsubscribe_notice', args=[subscription.pk])]
        for url in urls:
            self.assertEqual(client.post(url, {}).status_code, 403)

    def test_check_due_throttle_and_private_records_have_no_admin_registration(self):
        for _ in range(10):
            self.assertEqual(self.client.post(reverse('notifications:check_due')).status_code, 302)
        self.assertEqual(self.client.post(reverse('notifications:check_due')).status_code, 429)
        for model in (PersonalEvent, NotificationPreference, NoticeSubscription, Notification):
            self.assertNotIn(model, admin.site._registry)
