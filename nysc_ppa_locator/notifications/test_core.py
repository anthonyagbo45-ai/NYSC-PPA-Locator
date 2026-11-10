import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from threading import Barrier
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from locations.models import LGA, State
from service_resources.models import CDSGroup, ServiceNotice
from .models import NoticeSubscription, Notification, NotificationPreference, PersonalEvent
from .services import (
    dashboard_summary, delete_event, dismiss_notification, dispatch_reminders,
    dispatch_user_reminders, make_local_datetime, mark_read, save_event,
    save_preferences, subscribe_notice, subscription_rows, unread_count,
    unsubscribe_notice, visible_notifications,
)


class ReminderCoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.user = get_user_model().objects.create_user(state_code='REMINDER/OWNER', email='reminder-owner@example.com')
        cls.other = get_user_model().objects.create_user(state_code='REMINDER/OTHER', email='reminder-other@example.com', is_staff=True)
        cls.state = State.objects.create(name='Reminder State')
        cls.lga = LGA.objects.create(state=cls.state, name='Reminder LGA')

    def event(self, **updates):
        return save_event(self.user, {
            'title': 'Private personal reporting task', 'note': 'Private note never copied into an alert',
            'kind': PersonalEvent.Kind.PERSONAL, 'starts_at': self.now-timedelta(minutes=1),
            'ends_at': None, 'timezone_name': 'Africa/Lagos', 'remind_before': 0,
            'notifications_enabled': True, 'status': PersonalEvent.Status.ACTIVE,
            **updates,
        })

    def notice(self, **updates):
        return ServiceNotice.objects.create(**{
            'title': 'Source-checked local schedule', 'body': 'Confirm with your designated NYSC official.',
            'kind': ServiceNotice.Kind.CDS, 'state': self.state, 'lga': self.lga,
            'source_type': 'official', 'source_url': 'https://www.nysc.gov.ng/',
            'observed_on': timezone.localdate(self.now), 'checked_at': self.now-timedelta(seconds=30),
            'expires_at': self.now+timedelta(days=2), 'publication_status': 'approved', 'verification_status': 'official',
            'event_at': self.now+timedelta(hours=1), 'timezone_name': 'Africa/Lagos',
            'moderator_note': 'Private supporting evidence', **updates,
        })

    def opt_in(self, *, personal=True, notices=False):
        return save_preferences(self.user, personal_reminders=personal, notice_reminders=notices)

    def deliver(self):
        self.opt_in()
        event = self.event()
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        return event, Notification.objects.get(user=self.user)

    def test_no_implicit_preferences_or_delivery_on_reads(self):
        self.assertEqual(visible_notifications(self.user), [])
        self.assertEqual(subscription_rows(self.user), [])
        self.assertEqual(unread_count(self.user), 0)
        self.assertEqual(dashboard_summary(self.user)['upcoming_events'], [])
        self.assertFalse(NotificationPreference.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_personal_delivery_requires_global_and_item_opt_in(self):
        event = self.event(notifications_enabled=False)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 0)
        self.opt_in()
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 0)
        save_event(self.user, {'notifications_enabled': True}, event_id=event.pk)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        self.assertEqual(unread_count(self.user, now=self.now), 1)
        row = visible_notifications(self.user, now=self.now)[0]
        self.assertIn('personal reminder', row['label'])
        self.assertNotIn('note', row)
        self.assertNotIn('Private note', json.dumps(row, default=str))

    def test_delivery_is_idempotent_and_contains_no_title_snapshot(self):
        event, row = self.deliver()
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 0)
        self.assertEqual(Notification.objects.count(), 1)
        fields = {field.name for field in Notification._meta.fields}
        self.assertNotIn('title', fields)
        self.assertNotIn('body', fields)
        self.assertNotIn('note', fields)
        self.assertEqual(row.source_version, event.version)

    def test_event_changes_invalidate_old_alert_and_use_new_version(self):
        event, old = self.deliver()
        unchanged = save_event(self.user, {'title': event.title}, event_id=event.pk)
        self.assertEqual(unchanged.version, 1)
        changed = save_event(self.user, {'title': 'Revised private task'}, event_id=event.pk)
        self.assertEqual(changed.version, 2)
        self.assertEqual(visible_notifications(self.user, now=self.now), [])
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual([row['title'] for row in visible_notifications(self.user, now=self.now)], ['Revised private task'])

    def test_completion_cancellation_and_item_optout_remove_unread_alerts(self):
        for change in ({'status': 'completed'}, {'status': 'cancelled'}, {'notifications_enabled': False}):
            with self.subTest(change=change):
                event = self.event(title='Separate task '+str(change))
                self.opt_in()
                dispatch_user_reminders(self.user, now=self.now)
                row = Notification.objects.get(event=event)
                save_event(self.user, change, event_id=event.pk)
                self.assertEqual(unread_count(self.user, now=self.now), 0)
                with self.assertRaises(Http404):
                    mark_read(self.user, row.pk)

    def test_removing_personal_event_removes_its_notifications(self):
        event, _ = self.deliver()
        delete_event(self.user, event.pk)
        self.assertFalse(PersonalEvent.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_global_optout_dismisses_old_items_and_reenable_does_not_resend(self):
        event, old = self.deliver()
        self.opt_in(personal=False)
        self.assertEqual(unread_count(self.user), 0)
        old.refresh_from_db()
        self.assertIsNotNone(old.dismissed_at)
        self.opt_in()
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 0)
        self.assertEqual(unread_count(self.user, now=self.now), 0)

    def test_future_old_and_exact_boundary_personal_delivery(self):
        self.opt_in()
        event = self.event(starts_at=self.now+timedelta(hours=1), remind_before=60)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now-timedelta(microseconds=1)), 0)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        with patch('notifications.services.timezone.now', return_value=self.now):
            late = self.event(starts_at=self.now-timedelta(hours=24), title='At grace boundary')
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now+timedelta(seconds=1)), 0)
        self.assertFalse(Notification.objects.filter(event=late).exists())

    def test_overdue_enabled_event_rejected_without_corrupting_original(self):
        event = self.event()
        with self.assertRaises(ValidationError):
            save_event(self.user, {'starts_at': self.now-timedelta(days=3)}, event_id=event.pk)
        event.refresh_from_db()
        self.assertEqual(event.version, 1)
        self.assertEqual(event.starts_at, self.now-timedelta(minutes=1))

    def test_owner_scope_is_required_even_for_staff(self):
        event, row = self.deliver()
        for action in (
            lambda: save_event(self.other, {'title': 'Attack'}, event_id=event.pk),
            lambda: delete_event(self.other, event.pk),
            lambda: mark_read(self.other, row.pk),
            lambda: dismiss_notification(self.other, row.pk),
        ):
            with self.assertRaises(Http404):
                action()
        self.assertEqual(visible_notifications(self.other), [])

    def test_forged_owner_version_and_timestamps_are_ignored(self):
        event = self.event(user=self.other, version=500, created_at=self.now-timedelta(days=1))
        self.assertEqual(event.user_id, self.user.pk)
        self.assertEqual(event.version, 1)
        self.assertGreater(event.created_at, self.now-timedelta(seconds=1))

    def test_private_models_are_not_registered_in_admin(self):
        for model in (PersonalEvent, NotificationPreference, NoticeSubscription, Notification):
            self.assertNotIn(model, admin.site._registry)

    def test_timezone_conversion_and_dst_gap_fold_are_explicit(self):
        result = make_local_datetime(datetime(2026, 11, 9).date(), time(9, 30), 'Africa/Lagos')
        self.assertEqual(result, datetime(2026, 11, 9, 8, 30, tzinfo=datetime_timezone.utc))
        for day, clock in ((datetime(2026, 3, 8).date(), time(2, 30)), (datetime(2026, 11, 1).date(), time(1, 30))):
            with self.assertRaises(ValidationError):
                make_local_datetime(day, clock, 'America/New_York')
        with self.assertRaises(ValidationError):
            make_local_datetime(datetime(2026, 1, 1).date(), time(0), '../private')

    def test_model_dates_statuses_and_lead_constraints(self):
        event = self.event()
        for update in ({'starts_at': self.now.replace(tzinfo=None)}, {'ends_at': event.starts_at-timedelta(seconds=1)}, {'remind_before': 123}, {'status': 'officially_cleared'}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                save_event(self.user, update, event_id=event.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PersonalEvent.objects.filter(pk=event.pk).update(remind_before=123)

    def test_new_event_daily_quota_preserves_existing_records(self):
        for number in range(20):
            self.event(title=f'Personal task {number}', notifications_enabled=False)
        with self.assertRaises(ValidationError):
            self.event(title='Over daily allowance')
        self.assertEqual(PersonalEvent.objects.count(), 20)

    def test_subscribe_requires_current_checked_future_source(self):
        for changes in ({'verification_status': 'community'}, {'publication_status': 'pending'}, {'expires_at': self.now-timedelta(seconds=1)}, {'event_at': None}, {'source_url': ''}, {'event_at': self.now-timedelta(seconds=1)}):
            notice = self.notice(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                subscribe_notice(self.user, notice, 0)
        self.assertFalse(NoticeSubscription.objects.exists())

    def test_zero_lead_notice_delivers_one_minute_late_and_stays_readable(self):
        notice = self.notice()
        subscription = subscribe_notice(self.user, notice, 0)
        self.assertEqual(subscription.notice_version, notice.version)
        self.assertEqual(dispatch_user_reminders(self.user, now=notice.event_at), 0)
        self.opt_in(personal=False, notices=True)
        delivery = notice.event_at+timedelta(minutes=1)
        self.assertEqual(dispatch_user_reminders(self.user, now=delivery), 1)
        self.assertEqual(dispatch_user_reminders(self.user, now=delivery), 0)
        rows = visible_notifications(self.user, now=delivery)
        self.assertEqual(rows[0]['title'], notice.title)
        self.assertIn('Confirmed-source', rows[0]['label'])

    def test_notice_dispatch_drops_over_24_hour_late_work(self):
        notice = self.notice()
        subscribe_notice(self.user, notice, 0)
        self.opt_in(personal=False, notices=True)
        self.assertEqual(dispatch_user_reminders(self.user, now=notice.event_at+timedelta(hours=24, seconds=1)), 0)
        self.assertFalse(Notification.objects.exists())

    def test_notice_subscription_is_idempotent_and_scoped(self):
        notice = self.notice()
        subscription = subscribe_notice(self.user, notice, 60)
        duplicate = subscribe_notice(self.user, notice, 60)
        self.assertEqual(duplicate.pk, subscription.pk)
        self.assertEqual(duplicate.version, 1)
        with self.assertRaises(Http404):
            unsubscribe_notice(self.other, subscription.pk)
        unsubscribe_notice(self.user, subscription.pk)
        self.assertEqual(subscription_rows(self.user), [])

    def test_notice_version_and_private_edits_suppress_content_count_and_read(self):
        notice = self.notice()
        subscription = subscribe_notice(self.user, notice, 60)
        self.opt_in(personal=False, notices=True)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        row = Notification.objects.get(subscription=subscription)
        ServiceNotice.objects.filter(pk=notice.pk).update(title='SECRET later private title', publication_status='draft', version=2)
        self.assertEqual(visible_notifications(self.user, now=self.now), [])
        self.assertEqual(unread_count(self.user, now=self.now), 0)
        self.assertNotIn('SECRET', json.dumps(subscription_rows(self.user, now=self.now)))
        with self.assertRaises(Http404):
            mark_read(self.user, row.pk)
        with self.assertRaises(Http404):
            dismiss_notification(self.user, row.pk)
        ServiceNotice.objects.filter(pk=notice.pk).update(publication_status='approved', verification_status='official', title='New checked public title')
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 0)
        self.assertEqual(unread_count(self.user, now=self.now), 0)
        notice.refresh_from_db()
        renewed = subscribe_notice(self.user, notice, 60)
        self.assertEqual(renewed.version, 2)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)

    def test_expired_group_and_changed_group_revision_invalidate_old_subscription(self):
        group = CDSGroup.objects.create(title='Source-checked group', description='Public group context', state=self.state, lga=self.lga,
            observed_on=timezone.localdate(self.now), expires_at=self.now+timedelta(days=2), publication_status='approved')
        notice = self.notice(group=group)
        subscription = subscribe_notice(self.user, notice, 60)
        self.opt_in(personal=False, notices=True)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        CDSGroup.objects.filter(pk=group.pk).update(publication_status='draft', title='SECRET group edit', version=2)
        self.assertEqual(unread_count(self.user, now=self.now), 0)
        CDSGroup.objects.filter(pk=group.pk).update(publication_status='approved')
        self.assertEqual(unread_count(self.user, now=self.now), 0)
        notice = ServiceNotice.objects.select_related('group').get(pk=notice.pk)
        renewed = subscribe_notice(self.user, notice, 60)
        self.assertEqual(renewed.group_version, 2)
        self.assertEqual(dispatch_user_reminders(self.user, now=self.now), 1)
        CDSGroup.objects.filter(pk=group.pk).update(expires_at=self.now-timedelta(seconds=1))
        self.assertEqual(unread_count(self.user, now=self.now), 0)

    def test_unsafe_or_expired_source_never_reveals_existing_title(self):
        notice = self.notice()
        subscribe_notice(self.user, notice, 60)
        self.opt_in(personal=False, notices=True)
        dispatch_user_reminders(self.user, now=self.now)
        for update in ({'expires_at': self.now-timedelta(seconds=1)}, {'source_url': 'https://evil.example/not-nysc'}, {'verification_status': 'community'}, {'checked_at': self.now+timedelta(days=1)}):
            original = ServiceNotice.objects.get(pk=notice.pk)
            ServiceNotice.objects.filter(pk=notice.pk).update(**update)
            self.assertEqual(visible_notifications(self.user, now=self.now), [])
            self.assertEqual(unread_count(self.user, now=self.now), 0)
            original.save()

    def test_active_subscriptions_not_buried_by_disabled_history(self):
        notice = self.notice()
        active = subscribe_notice(self.user, notice, 60)
        others = [self.notice(title=f'Historical notice {index}') for index in range(100)]
        NoticeSubscription.objects.bulk_create([NoticeSubscription(user=self.user, notice=other, notice_version=1, enabled=False) for other in others])
        self.assertEqual([row['id'] for row in subscription_rows(self.user)], [active.pk])

    def test_read_dismiss_and_owner_projection_queries_are_bounded(self):
        event, row = self.deliver()
        self.assertTrue(mark_read(self.user, row.pk))
        self.assertEqual(unread_count(self.user), 0)
        self.assertTrue(dismiss_notification(self.user, row.pk))
        self.assertEqual(visible_notifications(self.user), [])
        with CaptureQueriesContext(connection) as queries:
            dashboard_summary(self.user)
        self.assertEqual(len(queries), 2)

    def test_batch_limit_fairness_dry_run_and_command_counts(self):
        self.opt_in()
        self.event()
        for number in range(3):
            user = get_user_model().objects.create_user(state_code=f'REMINDER/BATCH/{number}', email=f'batch{number}@example.com')
            save_preferences(user, personal_reminders=True)
            save_event(user, {'title': 'Private batch title', 'starts_at': self.now-timedelta(minutes=1), 'notifications_enabled': True, 'remind_before': 0})
        dry = dispatch_reminders(now=self.now, limit=1, dry_run=True)
        self.assertEqual(dry['created'], 0)
        self.assertEqual(dry['would_create'], 1)
        self.assertFalse(Notification.objects.exists())
        for _ in range(4):
            self.assertEqual(dispatch_reminders(now=self.now, limit=1)['created'], 1)
        self.assertEqual(dispatch_reminders(now=self.now, limit=1)['created'], 0)
        self.assertEqual(Notification.objects.values('user_id').distinct().count(), 4)
        output = io.StringIO()
        call_command('dispatch_reminders', '--dry-run', stdout=output)
        data = json.loads(output.getvalue())
        self.assertTrue(data['dry_run'])
        self.assertNotIn('Private batch title', output.getvalue())

    def test_batch_account_deactivation_race_skips_only_revoked_member(self):
        self.opt_in()
        self.event()
        other = self.other
        save_preferences(other, personal_reminders=True)
        save_event(other, {'title': 'Another private event', 'starts_at': self.now-timedelta(minutes=1), 'remind_before': 0, 'notifications_enabled': True})
        original = dispatch_user_reminders
        def deactivate(user, **kwargs):
            if user.pk == self.user.pk:
                get_user_model().objects.filter(pk=user.pk).update(is_active=False)
            return original(user, **kwargs)
        with patch('notifications.services.dispatch_user_reminders', side_effect=deactivate):
            self.assertEqual(dispatch_reminders(now=self.now)['created'], 1)
        self.assertFalse(Notification.objects.filter(user=self.user).exists())
        self.assertTrue(Notification.objects.filter(user=other).exists())


class ReminderConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_due_dispatch_delivers_once_and_closes_connections(self):
        user = get_user_model().objects.create_user(state_code='REMINDER/RACE', email='reminder-race@example.com')
        now = timezone.now()
        save_preferences(user, personal_reminders=True)
        save_event(user, {'title': 'Private race task', 'starts_at': now-timedelta(minutes=1), 'remind_before': 0, 'notifications_enabled': True})
        ready = Barrier(2)
        def dispatch(_):
            close_old_connections()
            try:
                member = get_user_model().objects.get(pk=user.pk)
                ready.wait(timeout=10)
                return dispatch_user_reminders(member, now=now)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(dispatch, range(2)))
        self.assertCountEqual(outcomes, [0, 1])
        self.assertEqual(Notification.objects.count(), 1)
