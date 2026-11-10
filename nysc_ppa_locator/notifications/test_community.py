import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from community.models import Answer, CommunityBlock, Question, QuestionFollow
from community.services import set_follow
from local_rooms.models import Room, RoomMembership, RoomPost
from locations.models import LGA, State
from .community_services import (
    community_items, community_unread_count, dismiss_community_notification,
    dispatch_community, dispatch_user_community, mark_community_read,
    record_answer_activity, record_room_activity,
)
from .models import CommunityActivity, CommunityNotification, Notification, NotificationPreference
from .services import save_preferences


class CommunityAlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now()
        cls.delivery = cls.now + timedelta(minutes=1)
        cls.user = get_user_model().objects.create_user(state_code='ALERT/READER', email='alert-reader@example.com', email_verified=True)
        cls.actor = get_user_model().objects.create_user(state_code='ALERT/ACTOR', email='alert-actor@example.com', email_verified=True)
        cls.author = get_user_model().objects.create_user(state_code='ALERT/AUTHOR', email='alert-author@example.com', email_verified=True)
        cls.staff = get_user_model().objects.create_user(state_code='ALERT/STAFF', email='alert-staff@example.com', is_staff=True)
        cls.state = State.objects.create(name='Alert State')
        cls.lga = LGA.objects.create(name='Alert LGA', state=cls.state)

    def question(self, **changes):
        return Question.objects.create(**{'author': self.author, 'title': 'A public community question', 'body': 'A member question body',
            'category': 'other', 'state': self.state, 'lga': self.lga, **changes})

    def follow(self, question, user=None, **changes):
        return QuestionFollow.objects.create(**{'user': user or self.user, 'question': question, 'enabled': True,
            'enabled_at': self.now-timedelta(seconds=10), **changes})

    def opt_in(self, user=None):
        with patch('notifications.services.timezone.now', return_value=self.now-timedelta(seconds=10)):
            return save_preferences(user or self.user, community_replies=True)

    def answer(self, question=None, **changes):
        with transaction.atomic():
            answer = Answer.objects.create(**{'question': question or self.question(), 'author': self.actor,
                'body': 'Answer body never appears in an alert snapshot', **changes})
            record_answer_activity(answer)
        return answer

    def room(self, **changes):
        return Room.objects.create(**{'title': 'An approved private community room', 'description': 'Public discovery description',
            'state': self.state, 'lga': self.lga, 'visibility': 'members', **changes})

    def membership(self, room, user=None, **changes):
        return RoomMembership.objects.create(**{'room': room, 'user': user or self.user, 'status': 'approved',
            'approved_at': self.now-timedelta(seconds=20), 'notifications_enabled': True,
            'notifications_enabled_at': self.now-timedelta(seconds=10), **changes})

    def post(self, room, **changes):
        with transaction.atomic():
            post = RoomPost.objects.create(**{'room': room, 'author': self.actor, 'body': 'PRIVATE room body must never be copied', **changes})
            record_room_activity(post)
        return post

    def deliver_answer(self):
        self.opt_in()
        question = self.question()
        self.follow(question)
        answer = self.answer(question)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 1)
        return answer, CommunityNotification.objects.get(user=self.user)

    def assert_invisible(self, row):
        self.assertEqual(community_items(self.user, now=self.delivery), [])
        self.assertEqual(community_unread_count(self.user, now=self.delivery), 0)
        with self.assertRaises(Http404):
            mark_community_read(self.user, row.pk)
        with self.assertRaises(Http404):
            dismiss_community_notification(self.user, row.pk)

    def test_reads_default_off_do_not_create_preferences_or_deliver(self):
        question = self.question()
        self.follow(question)
        self.answer(question)
        self.assertEqual(community_items(self.user), [])
        self.assertEqual(community_unread_count(self.user), 0)
        self.assertFalse(NotificationPreference.objects.exists())
        self.assertFalse(CommunityNotification.objects.exists())
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)

    def test_global_and_question_opt_in_both_required_without_auto_follow(self):
        question = self.question(author=self.user)
        self.opt_in()
        self.answer(question)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        self.assertFalse(QuestionFollow.objects.exists())
        self.follow(question, enabled=False, enabled_at=None)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)

    def test_source_creation_records_activity_but_never_delivers_and_rolls_back(self):
        self.opt_in()
        question = self.question()
        self.follow(question)
        from community.services import save_answer
        answer = save_answer(user=self.actor, question=question, cleaned_data={'body': 'A useful suggestion', 'source_url': ''})
        self.assertEqual(CommunityActivity.objects.filter(answer=answer).count(), 1)
        self.assertFalse(CommunityNotification.objects.exists())
        before = CommunityActivity.objects.count()
        with self.assertRaises(RuntimeError), transaction.atomic():
            save_answer(user=self.actor, question=question, cleaned_data={'body': 'Rolled back reply', 'source_url': ''})
            raise RuntimeError('Abort source transaction')
        self.assertEqual(CommunityActivity.objects.count(), before)

    def test_delivery_idempotent_no_title_body_or_account_snapshot(self):
        answer, row = self.deliver_answer()
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        self.assertEqual(record_answer_activity(answer).pk, row.activity_id)
        self.assertEqual(CommunityNotification.objects.count(), 1)
        for model in (CommunityActivity, CommunityNotification):
            self.assertFalse({'title', 'body', 'email', 'state_code'} & {f.name for f in model._meta.fields})
            self.assertNotIn(model, admin.site._registry)
        item = community_items(self.user, now=self.delivery)[0]
        self.assertEqual(item['title'], answer.question.title)
        serialized = json.dumps(item, default=str)
        for value in (answer.body, self.actor.email, self.actor.state_code):
            self.assertNotIn(value, serialized)

    def test_self_activity_is_suppressed(self):
        question = self.question()
        self.follow(question)
        self.opt_in()
        self.answer(question, author=self.user)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)

    def test_global_enable_time_prevents_old_backfill(self):
        question = self.question()
        self.follow(question)
        answer = self.answer(question)
        with patch('notifications.services.timezone.now', return_value=self.delivery):
            save_preferences(self.user, community_replies=True)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        self.assertFalse(CommunityNotification.objects.filter(activity__answer=answer).exists())

    def test_follow_enable_time_prevents_old_backfill_and_unchanged_choice_preserves_time(self):
        self.opt_in()
        question = self.question()
        self.answer(question)
        with patch('community.services.timezone.now', return_value=self.delivery):
            follow = set_follow(user=self.user, question=question, following=True, enabled=True)
        self.assertEqual(follow.enabled_at, self.delivery)
        with patch('community.services.timezone.now', return_value=self.delivery+timedelta(hours=1)):
            unchanged = set_follow(user=self.user, question=question, following=True, enabled=True)
        self.assertEqual(unchanged.enabled_at, follow.enabled_at)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)

    def test_global_disable_dismisses_and_reenable_never_resends(self):
        answer, row = self.deliver_answer()
        save_preferences(self.user, community_replies=False)
        row.refresh_from_db()
        self.assertIsNotNone(row.dismissed_at)
        self.assert_invisible(row)
        with patch('notifications.services.timezone.now', return_value=self.delivery):
            save_preferences(self.user, community_replies=True)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        self.assertEqual(community_unread_count(self.user, now=self.delivery), 0)

    def test_legacy_calendar_preferences_preserve_community_consent(self):
        preference = self.opt_in()
        original = preference.community_enabled_at
        preference = save_preferences(self.user, personal_reminders=True, notice_reminders=False)
        self.assertTrue(preference.community_replies)
        self.assertEqual(preference.community_enabled_at, original)
        with self.assertRaises(ValidationError):
            save_preferences(self.user, community_replies='on')

    def test_answer_or_parent_status_and_versions_suppress_titles_links_and_count(self):
        answer, row = self.deliver_answer()
        for model, pk, update in (
            (Answer, answer.pk, {'status': 'hidden'}), (Answer, answer.pk, {'status': 'archived'}),
            (Answer, answer.pk, {'version': 2, 'body': 'PRIVATE changed body'}),
            (Question, answer.question_id, {'status': 'hidden', 'title': 'PRIVATE replacement'}),
            (Question, answer.question_id, {'status': 'archived'}),
            (Question, answer.question_id, {'version': 2, 'title': 'PRIVATE revised title'}),
        ):
            with self.subTest(update=update):
                original = model.objects.get(pk=pk)
                model.objects.filter(pk=pk).update(**update)
                self.assert_invisible(row)
                self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
                original.save()

    def test_block_either_direction_actor_or_question_author_revokes_alert(self):
        answer, row = self.deliver_answer()
        for blocked in (self.actor, self.author):
            for reverse_direction in (False, True):
                with self.subTest(blocked=blocked.pk, reverse=reverse_direction):
                    blocker, target = (blocked, self.user) if reverse_direction else (self.user, blocked)
                    block = CommunityBlock.objects.create(user=blocker, blocked_user=target)
                    self.assert_invisible(row)
                    block.delete()

    def test_old_and_future_activity_not_dispatched_and_grace_boundary_is_inclusive(self):
        self.opt_in()
        question = self.question()
        self.follow(question)
        answers = [self.answer(question) for _ in range(3)]
        # Explicitly enable before the 24h window, then vary source activity time.
        NotificationPreference.objects.filter(user=self.user).update(community_enabled_at=self.now-timedelta(days=2))
        QuestionFollow.objects.filter(user=self.user).update(enabled_at=self.now-timedelta(days=2))
        instants = [self.delivery-timedelta(hours=24, microseconds=1), self.delivery+timedelta(microseconds=1), self.delivery-timedelta(hours=24)]
        for answer, instant in zip(answers, instants):
            CommunityActivity.objects.filter(answer=answer).update(occurred_at=instant)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 1)
        self.assertEqual(CommunityNotification.objects.get().activity.answer_id, answers[-1].pk)

    def test_room_requires_approved_explicit_membership_and_no_preapproval_backfill(self):
        self.opt_in()
        room = self.room(visibility='public')
        post = self.post(room)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        membership = self.membership(room, approved_at=self.delivery)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        RoomMembership.objects.filter(pk=membership.pk).update(approved_at=self.now-timedelta(seconds=20), notifications_enabled_at=self.delivery)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 0)
        RoomMembership.objects.filter(pk=membership.pk).update(notifications_enabled_at=self.now-timedelta(seconds=10))
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 1)
        self.assertEqual(CommunityNotification.objects.get().activity.room_post_id, post.pk)

    def test_room_leave_ban_optout_archive_pause_and_source_edit_revoke_alert(self):
        self.opt_in()
        room = self.room()
        membership = self.membership(room)
        post = self.post(room)
        self.assertEqual(dispatch_user_community(self.user, now=self.delivery), 1)
        row = CommunityNotification.objects.get()
        for status in ('left', 'banned', 'requested'):
            RoomMembership.objects.filter(pk=membership.pk).update(status=status, notifications_enabled=False, notifications_enabled_at=None)
            self.assert_invisible(row)
            membership.save()
        for model, pk, changes in (
            (RoomMembership, membership.pk, {'notifications_enabled': False, 'notifications_enabled_at': None}),
            (Room, room.pk, {'status': 'archived', 'title': 'PRIVATE changed title'}),
            (Room, room.pk, {'status': 'paused'}), (Room, room.pk, {'version': 2}),
            (RoomPost, post.pk, {'status': 'hidden'}), (RoomPost, post.pk, {'version': 2}),
        ):
            with self.subTest(changes=changes):
                original = model.objects.get(pk=pk)
                model.objects.filter(pk=pk).update(**changes)
                self.assert_invisible(row)
                original.save()

    def test_read_dismiss_owner_scope_includes_staff(self):
        answer, row = self.deliver_answer()
        for action in (mark_community_read, dismiss_community_notification):
            with self.assertRaises(Http404):
                action(self.staff, row.pk)
        self.assertTrue(mark_community_read(self.user, row.pk))
        self.assertEqual(community_unread_count(self.user, now=self.delivery), 0)
        self.assertTrue(dismiss_community_notification(self.user, row.pk))
        self.assertEqual(community_items(self.user, now=self.delivery), [])

    def test_query_count_stays_constant_as_unread_items_grow(self):
        self.opt_in()
        question = self.question()
        self.follow(question)
        self.answer(question)
        dispatch_user_community(self.user, now=self.delivery)
        with CaptureQueriesContext(connection) as initial:
            community_items(self.user, now=self.delivery)
            community_unread_count(self.user, now=self.delivery)
        for _ in range(25):
            self.answer(question)
        dispatch_user_community(self.user, now=self.delivery)
        with CaptureQueriesContext(connection) as grown:
            items = community_items(self.user, now=self.delivery)
            count = community_unread_count(self.user, now=self.delivery)
        self.assertEqual(len(items), 26)
        self.assertEqual(count, 26)
        self.assertEqual(len(initial), 4)
        self.assertEqual(len(grown), 4)

    def test_bounded_global_batches_skip_delivered_and_blocked_candidates(self):
        question = self.question()
        answer = self.answer(question)
        users = [self.user, self.author, self.staff]
        for user in users:
            self.opt_in(user)
            self.follow(question, user=user)
        CommunityBlock.objects.create(user=self.user, blocked_user=self.actor)
        dry = dispatch_community(now=self.delivery, limit=1, dry_run=True)
        self.assertEqual(dry['would_create'], 1)
        self.assertEqual(dry['examined_users'], 1)
        self.assertFalse(CommunityNotification.objects.exists())
        for _ in range(2):
            self.assertEqual(dispatch_community(now=self.delivery, limit=1)['created'], 1)
        self.assertEqual(dispatch_community(now=self.delivery, limit=1)['created'], 0)
        self.assertCountEqual(list(CommunityNotification.objects.values_list('user_id', flat=True)), [self.author.pk, self.staff.pk])

    def test_command_is_private_bounded_and_dry_run(self):
        self.opt_in()
        question = self.question()
        self.follow(question)
        self.answer(question)
        output = io.StringIO()
        call_command('dispatch_community_notifications', '--limit', '1', '--dry-run', stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result['would_create'], 1)
        self.assertFalse(CommunityNotification.objects.exists())
        self.assertNotIn(question.title, output.getvalue())
        self.assertNotIn(self.user.email, output.getvalue())

    def test_database_enforces_one_source_and_unique_delivery(self):
        answer, row = self.deliver_answer()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommunityActivity.objects.create(actor=self.actor, source_version=1, parent_version=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommunityActivity.objects.create(answer=answer, actor=self.actor, source_version=1, parent_version=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommunityNotification.objects.create(user=self.user, activity=row.activity)
        legacy_constraint = next(c for c in Notification._meta.constraints if c.name == 'reminder_exactly_one_target')
        self.assertIsNotNone(legacy_constraint)

    def test_http_inbox_is_read_only_and_writes_require_post_csrf_and_ownership(self):
        self.client.force_login(self.user)
        self.opt_in()
        question = self.question(title='<script>private escaped title</script>')
        self.follow(question)
        self.answer(question)
        inbox = reverse('community_notifications:inbox')
        response = self.client.get(inbox)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CommunityNotification.objects.exists())
        self.assertEqual(self.client.get(reverse('community_notifications:check_due')).status_code, 405)
        self.assertRedirects(self.client.post(reverse('community_notifications:check_due')), inbox)
        row = CommunityNotification.objects.get()
        response = self.client.get(inbox)
        self.assertContains(response, '&lt;script&gt;private escaped title&lt;/script&gt;')
        self.assertNotContains(response, 'Answer body never')
        self.assertContains(response, 'member suggestions')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        for route in ('read', 'dismiss'):
            url = reverse('community_notifications:'+route, args=[row.pk])
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(csrf.post(url).status_code, 403)
            self.client.force_login(self.staff)
            self.assertEqual(self.client.post(url).status_code, 404)
            self.client.force_login(self.user)
        self.assertEqual(csrf.post(reverse('community_notifications:check_due')).status_code, 403)

    def test_http_global_choice_default_off_preserves_item_choice_with_paused_notice(self):
        question = self.question()
        follow = self.follow(question)
        self.client.force_login(self.user)
        response = self.client.get(reverse('community_notifications:inbox'))
        self.assertContains(response, 'Community alerts are paused')
        self.assertFalse(NotificationPreference.objects.exists())
        self.assertTrue(QuestionFollow.objects.get(pk=follow.pk).enabled)
        response = self.client.get(reverse('notifications:preferences'))
        self.assertFalse(response.context['form'].initial['community_replies'])
        self.client.post(reverse('notifications:preferences'), {'community_replies': 'on'})
        self.assertTrue(NotificationPreference.objects.get(user=self.user).community_replies)


class CommunityAlertConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_dispatch_serializes_recipient_and_closes_connections(self):
        user = get_user_model().objects.create_user(state_code='ALERT/RACE/READER', email='race-reader@example.com')
        actor = get_user_model().objects.create_user(state_code='ALERT/RACE/ACTOR', email='race-actor@example.com')
        state = State.objects.create(name='Race State')
        lga = LGA.objects.create(name='Race LGA', state=state)
        save_preferences(user, community_replies=True)
        question = Question.objects.create(author=user, title='Race question', body='Community question', category='other', state=state, lga=lga)
        QuestionFollow.objects.create(user=user, question=question, enabled=True, enabled_at=timezone.now()-timedelta(seconds=1))
        with transaction.atomic():
            answer = Answer.objects.create(question=question, author=actor, body='A useful reply')
            record_answer_activity(answer)
        now = timezone.now()+timedelta(seconds=1)
        ready = Barrier(2)

        def dispatch(_):
            close_old_connections()
            try:
                member = get_user_model().objects.get(pk=user.pk)
                ready.wait(timeout=10)
                return dispatch_user_community(member, now=now)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, range(2)))
        self.assertCountEqual(results, [0, 1])
        self.assertEqual(CommunityNotification.objects.count(), 1)
