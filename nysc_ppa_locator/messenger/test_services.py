import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from community.models import Answer, CommunityBlock, Question
from community.services import set_block
from locations.models import LGA, State
from . import services
from .models import Conversation, Message, MessageReport, MessengerPreference, ReportDecision


class Fixtures:
    @classmethod
    def make_fixtures(cls):
        cls.state = State.objects.create(name='Messenger test state')
        cls.lga = LGA.objects.create(name='Messenger test LGA', state=cls.state)
        User = get_user_model()
        cls.alice = User.objects.create_user(state_code='MS/26A/1111', email='alice-private@example.test', full_name='Private Alice Legal', display_name='Alice', email_verified=True, password='Synthetic-secret!pass')
        cls.bob = User.objects.create_user(state_code='MS/26A/2222', email='bob-private@example.test', full_name='Private Bob Legal', display_name='Bob', email_verified=True, password='Synthetic-secret!pass')
        cls.outsider = User.objects.create_superuser(state_code='MS/26A/9999', email='private-admin@example.test', full_name='Private Admin', password='Synthetic-secret!pass', email_verified=True)
        cls.question = Question.objects.create(author=cls.bob, title='Settling into this area', body='Public context only.', category='service', state=cls.state, lga=cls.lga)
        cls.alice_question = Question.objects.create(author=cls.alice, title='Another public context', body='Public context only.', category='service', state=cls.state, lga=cls.lga)
        cls.answer = Answer.objects.create(question=cls.alice_question, author=cls.bob, body='Public answer context.')
        for member in (cls.alice, cls.bob):
            MessengerPreference.objects.create(user=member, allow_requests=True)

    def request(self, **kwargs):
        values = dict(user=self.alice, context_type='question', context_id=self.question.pk, body='Could we discuss your advice?', client_token=uuid.uuid4())
        values.update(kwargs)
        return services.request_chat(**values)

    def accepted(self):
        conversation_id = self.request()
        services.respond_request(self.bob, conversation_id, True)
        return conversation_id


class PrivateMessagingServiceTests(Fixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.make_fixtures()

    def test_default_off_and_both_participants_must_opt_in(self):
        MessengerPreference.objects.all().delete()
        self.assertEqual(services.get_preferences(self.alice), {'allow_requests': False, 'allow_photos': False})
        with self.assertRaises(ValidationError):
            self.request()
        services.save_preferences(self.bob, True)
        with self.assertRaises(ValidationError):
            self.request()
        services.save_preferences(self.alice, True)
        self.assertIsInstance(self.request(), uuid.UUID)

    def test_context_preview_and_request_use_only_public_names(self):
        preview = services.get_request_context(self.alice, 'answer', self.answer.pk)
        self.assertEqual(preview['recipient_name'], 'Bob')
        self.assertEqual(preview['context_title'], self.alice_question.title)
        self.assertEqual(preview['context_url'], f'/community/questions/{self.alice_question.pk}/')
        conversation_id = self.request(context_type='answer', context_id=self.answer.pk)
        data = services.get_thread(self.bob, conversation_id)
        self.assertEqual(data['messages'][0]['sender_name'], 'Alice')
        dumped = str(data)
        for private in ('alice-private', 'bob-private', 'Private Alice Legal', 'Private Bob Legal', 'MS/26A/'):
            self.assertNotIn(private, dumped)
        self.assertNotIn('sender_id', dumped)
        self.assertNotIn('low_member', dumped)

    def test_hidden_archived_or_deleted_context_cannot_start_requests(self):
        for status in ('hidden', 'archived'):
            Question.objects.filter(pk=self.question.pk).update(status=status)
            with self.assertRaises(Http404):
                self.request()
        with self.assertRaises(Http404):
            services.request_chat(self.alice, 'member', self.bob.pk, 'Hello', uuid.uuid4())
        with self.assertRaises(Http404):
            services.get_request_context(self.alice, 'answer', self.answer.pk+1000)
        self.question.delete()
        with self.assertRaises(Http404):
            self.request()

    def test_self_request_or_unavailable_target_fails_without_records(self):
        with self.assertRaises(ValidationError):
            self.request(context_id=self.alice_question.pk)
        get_user_model().objects.filter(pk=self.bob.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.request()
        self.assertFalse(Conversation.objects.exists())

    def test_fresh_verification_check_ignores_stale_user_object(self):
        get_user_model().objects.filter(pk=self.alice.pk).update(email_verified=False)
        self.assertTrue(self.alice.email_verified)
        with self.assertRaises(PermissionDenied):
            self.request()
        with self.assertRaises(PermissionDenied):
            services.save_preferences(self.alice, True)
        self.assertEqual(services.save_preferences(self.alice, False), {'allow_requests': False, 'allow_photos': False})

    def test_intro_is_single_request_and_replays_are_idempotent(self):
        nonce = uuid.uuid4()
        first = self.request(client_token=nonce)
        self.assertEqual(self.request(client_token=nonce), first)
        self.assertEqual(Message.objects.count(), 1)
        with self.assertRaises(ValidationError):
            self.request(client_token=nonce, body='Changed introduction')
        with self.assertRaises(ValidationError):
            self.request()
        with self.assertRaises(ValidationError):
            services.send_message(self.alice, first, 'Another unsolicited message', uuid.uuid4())
        self.assertEqual(Message.objects.count(), 1)

    def test_recipient_must_accept_before_both_can_send(self):
        conversation_id = self.request()
        with self.assertRaises(PermissionDenied):
            services.respond_request(self.alice, conversation_id, True)
        services.respond_request(self.bob, conversation_id, True)
        services.send_message(self.bob, conversation_id, 'Yes, glad to help.', uuid.uuid4())
        services.send_message(self.alice, conversation_id, 'Thank you.', uuid.uuid4())
        self.assertEqual(Message.objects.count(), 3)
        self.assertEqual(services.get_thread(self.bob, conversation_id)['conversation']['status'], 'accepted')

    def test_declined_and_closed_pairs_cannot_be_reopened_or_retried(self):
        for decline in (True, False):
            conversation_id = self.request()
            if decline:
                services.respond_request(self.bob, conversation_id, False)
            else:
                services.close_conversation(self.alice, conversation_id)
            with self.assertRaises(ValidationError):
                self.request()
            with self.assertRaises(ValidationError):
                services.request_chat(self.bob, 'question', self.alice_question.pk, 'Now my turn', uuid.uuid4())
            with self.assertRaises(ValidationError):
                services.respond_request(self.bob, conversation_id, True)
            Message.objects.all().delete()
            Conversation.objects.all().delete()

    def test_turning_off_requests_closes_pending_but_keeps_accepted(self):
        conversation_id = self.request()
        services.save_preferences(self.bob, False)
        self.assertEqual(Conversation.objects.get(pk=conversation_id).status, 'closed')
        services.save_preferences(self.bob, True)
        with self.assertRaises(ValidationError):
            self.request()
        Conversation.objects.all().delete()
        conversation_id = self.accepted()
        services.save_preferences(self.bob, False)
        self.assertEqual(Conversation.objects.get(pk=conversation_id).status, 'accepted')
        services.send_message(self.alice, conversation_id, 'An already accepted chat continues.', uuid.uuid4())

    def test_unverified_members_retain_safety_controls_and_history(self):
        conversation_id = self.request()
        get_user_model().objects.filter(pk=self.bob.pk).update(email_verified=False)
        self.assertEqual(len(services.get_thread(self.bob, conversation_id)['messages']), 1)
        services.report_message(self.bob, conversation_id, Message.objects.get().pk, 'scam', 'Please review this request.')
        services.respond_request(self.bob, conversation_id, False)
        services.block_peer(self.bob, conversation_id)
        self.assertTrue(CommunityBlock.objects.filter(user=self.bob, blocked_user=self.alice).exists())

    def test_blocks_in_either_direction_stop_sends_and_unread_but_retain_history(self):
        conversation_id = self.accepted()
        for blocker, target in ((self.bob, self.alice), (self.alice, self.bob)):
            CommunityBlock.objects.all().delete()
            set_block(user=blocker, blocked_user=target)
            with self.assertRaises(PermissionDenied):
                services.send_message(self.alice, conversation_id, 'Blocked', uuid.uuid4())
            with self.assertRaises(PermissionDenied):
                services.send_message(self.bob, conversation_id, 'Also blocked', uuid.uuid4())
            row = services.get_inbox(self.bob).object_list[0]
            self.assertFalse(row['unread'])
            thread = services.get_thread(self.bob, conversation_id)
            self.assertTrue(thread['conversation']['blocked'])
            self.assertFalse(thread['conversation']['can_send'])
            self.assertEqual(len(thread['messages']), 1)

    def test_deactivated_account_and_forged_active_object_cannot_read(self):
        conversation_id = self.accepted()
        get_user_model().objects.filter(pk=self.alice.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            services.get_thread(self.alice, conversation_id)
        with self.assertRaises(PermissionDenied):
            services.get_inbox(self.alice)
        with self.assertRaises(PermissionDenied):
            services.send_message(self.bob, conversation_id, 'Recipient is inactive', uuid.uuid4())

    def test_superuser_outsider_cannot_read_mutate_or_report_private_threads(self):
        conversation_id = self.accepted()
        message_id = Message.objects.get().pk
        for call in (
            lambda: services.get_thread(self.outsider, conversation_id),
            lambda: services.get_selected_message(self.outsider, conversation_id, message_id),
            lambda: services.send_message(self.outsider, conversation_id, 'Spy', uuid.uuid4()),
            lambda: services.mark_read(self.outsider, conversation_id, message_id),
            lambda: services.close_conversation(self.outsider, conversation_id),
            lambda: services.report_message(self.outsider, conversation_id, message_id, 'abuse', 'Spy'),
            lambda: services.block_peer(self.outsider, conversation_id),
        ):
            with self.assertRaises(Http404):
                call()
        self.assertEqual(services.get_inbox(self.outsider).object_list, [])

    def test_send_nonce_replay_does_not_duplicate_or_change_message(self):
        conversation_id = self.accepted()
        token = uuid.uuid4()
        first = services.send_message(self.alice, conversation_id, 'A single delivery', token)
        second = services.send_message(self.alice, conversation_id, 'A single delivery', token)
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(ValidationError):
            services.send_message(self.alice, conversation_id, 'Overwritten', token)
        self.assertEqual(Message.objects.count(), 2)

    def test_body_and_token_validation_rejects_blank_large_control_and_invalid(self):
        for body in ('', '   ', 'x'*2001, 'Nul\x00', 'Escape\x1b', None):
            with self.assertRaises(ValidationError):
                self.request(body=body)
        with self.assertRaises(ValidationError):
            self.request(client_token='invalid')
        conversation_id = self.request(body='<script>alert(1)</script>\nPlain text')
        self.assertEqual(Message.objects.get(conversation_id=conversation_id).body, '<script>alert(1)</script>\nPlain text')

    def test_message_and_request_daily_allowances_are_transactional(self):
        with patch.object(services, 'REQUESTS_PER_DAY', 0):
            with self.assertRaises(ValidationError):
                self.request()
        self.assertFalse(Conversation.objects.exists())
        with patch.object(services, 'REQUESTS_PER_FIVE_MINUTES', 0):
            with self.assertRaises(ValidationError):
                self.request()
        conversation_id = self.accepted()
        with patch.object(services, 'MESSAGES_PER_DAY', 1):
            with self.assertRaises(ValidationError):
                services.send_message(self.alice, conversation_id, 'Over daily allowance', uuid.uuid4())
        with patch.object(services, 'MESSAGES_PER_MINUTE', 1):
            with self.assertRaises(ValidationError):
                services.send_message(self.alice, conversation_id, 'Over minute allowance', uuid.uuid4())
        Message.objects.update(sent_at=timezone.now()-timedelta(days=2))
        with patch.object(services, 'MESSAGES_PER_DAY', 1), patch.object(services, 'MESSAGES_PER_MINUTE', 1):
            services.send_message(self.alice, conversation_id, 'Allowance has elapsed', uuid.uuid4())

    def test_read_highwater_never_mutates_on_get_or_discloses_receipts(self):
        conversation_id = self.accepted()
        services.get_thread(self.bob, conversation_id)
        self.assertTrue(services.get_inbox(self.bob).object_list[0]['unread'])
        row = Conversation.objects.get(pk=conversation_id)
        self.assertEqual(row.high_read_through + row.low_read_through, 0)
        intro_id = Message.objects.get().pk
        services.mark_read(self.bob, conversation_id, intro_id)
        self.assertFalse(services.get_inbox(self.bob).object_list[0]['unread'])
        data = services.get_thread(self.alice, conversation_id)
        self.assertNotIn('read_through', str(data))
        services.send_message(self.alice, conversation_id, 'New item', uuid.uuid4())
        self.assertTrue(services.get_inbox(self.bob).object_list[0]['unread'])
        services.mark_read(self.bob, conversation_id, intro_id)
        self.assertTrue(services.get_inbox(self.bob).object_list[0]['unread'])
        with self.assertRaises(Http404):
            services.mark_read(self.bob, conversation_id, intro_id+9999)

    def test_cursor_pagination_is_bounded_and_returns_each_message_once(self):
        conversation_id = self.accepted()
        Message.objects.bulk_create([Message(conversation_id=conversation_id, sender=self.alice, body=f'Message {n}') for n in range(110)])
        ids, before = [], None
        for _ in range(4):
            data = services.get_thread(self.bob, conversation_id, before=before, limit=500)
            self.assertLessEqual(len(data['messages']), 50)
            ids.extend(row['id'] for row in data['messages'])
            before = data['next_before']
            if before is None:
                break
        self.assertEqual(len(ids), 111)
        self.assertEqual(len(set(ids)), 111)
        with self.assertRaises(Http404):
            services.get_thread(self.bob, conversation_id, before='-2')

    def test_inbox_and_thread_have_bounded_query_counts(self):
        conversation_id = self.accepted()
        for n in range(15):
            peer = get_user_model().objects.create_user(state_code=f'MS/26B/{n}', display_name=f'Peer {n}', email_verified=True)
            low, high = sorted([self.alice.pk, peer.pk])
            row = Conversation.objects.create(low_member_id=low, high_member_id=high, initiated_by=self.alice)
            Message.objects.create(conversation=row, sender=peer, body='An incoming message')
        with CaptureQueriesContext(connection) as captured:
            page = services.get_inbox(self.alice)
            self.assertEqual(len(page.object_list), 16)
        self.assertLessEqual(len(captured), 5)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(len(services.get_thread(self.bob, conversation_id)['messages']), 1)
        self.assertLessEqual(len(captured), 6)

    def test_database_rejects_reversed_self_nonparticipant_and_duplicate_pairs(self):
        conversation_id = self.request()
        row = Conversation.objects.get(pk=conversation_id)
        for kwargs in (
            dict(low_member=self.bob, high_member=self.alice, initiated_by=self.alice),
            dict(low_member=self.alice, high_member=self.alice, initiated_by=self.alice),
            dict(low_member=self.alice, high_member=self.bob, initiated_by=self.outsider),
            dict(low_member=self.alice, high_member=self.bob, initiated_by=self.bob),
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Conversation.objects.create(**kwargs)
        bad = Message(conversation=row, sender=self.outsider, body='Illegal injection')
        with self.assertRaises(ValidationError):
            bad.full_clean()

    def test_report_contains_only_selected_message_and_is_not_editable(self):
        conversation_id = self.accepted()
        services.send_message(self.alice, conversation_id, 'Unreported private context', uuid.uuid4())
        report_id = services.report_message(self.bob, conversation_id, Message.objects.order_by('pk').first().pk, 'scam', 'Suspicious request')
        report = MessageReport.objects.get(pk=report_id)
        self.assertEqual(set(report.evidence), {'body', 'sent_at', 'sender_name'})
        self.assertEqual(report.evidence['body'], 'Could we discuss your advice?')
        self.assertNotIn('Unreported private context', str(report.evidence))
        self.assertNotIn('MS/26A', str(report.evidence))
        report.evidence = {'body': 'Changed'}
        with self.assertRaises(ValidationError):
            report.save()

    def test_reports_require_membership_selected_thread_and_other_sender(self):
        conversation_id = self.accepted()
        message_id = Message.objects.get().pk
        with self.assertRaises(ValidationError):
            services.report_message(self.alice, conversation_id, message_id, 'abuse', 'My own message')
        with self.assertRaises(Http404):
            services.report_message(self.bob, conversation_id, message_id+9999, 'abuse', 'No message')
        services.report_message(self.bob, conversation_id, message_id, 'abuse', 'One report')
        with self.assertRaises(ValidationError):
            services.report_message(self.bob, conversation_id, message_id, 'spam', 'Duplicate report')
        self.assertEqual(MessageReport.objects.count(), 1)

    def test_report_decisions_require_specific_permission_and_are_append_only(self):
        conversation_id = self.accepted()
        report_id = services.report_message(self.bob, conversation_id, Message.objects.get().pk, 'abuse', 'Review this')
        with self.assertRaises(PermissionDenied):
            services.moderate_report(self.alice, report_id, 'resolved', 'Reviewed')
        permission = Permission.objects.get(content_type__app_label='messenger', codename='can_review_message_reports')
        self.alice.user_permissions.add(permission)
        report = services.moderate_report(self.alice, report_id, 'resolved', 'Selected evidence reviewed')
        self.assertEqual(report.status, 'resolved')
        decision = ReportDecision.objects.get(report_id=report_id)
        self.assertEqual(decision.previous_status, 'open')
        self.assertEqual(decision.new_status, 'resolved')
        with self.assertRaises(ValidationError):
            decision.delete()
        with self.assertRaises(ValidationError):
            ReportDecision.objects.filter(pk=decision.pk).update(moderator_note='Changed')


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row-lock concurrency')
class PrivateMessagingConcurrencyTests(Fixtures, TransactionTestCase):
    def setUp(self):
        self.make_fixtures()

    def _parallel(self, actions):
        barrier = Barrier(len(actions))
        def run(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run, actions))

    def test_opposite_requests_create_exactly_one_conversation_intro(self):
        def request(actor, context_id):
            try:
                return services.request_chat(actor, 'question', context_id, 'One request', uuid.uuid4())
            except ValidationError:
                return 'already-exists'
        results = self._parallel([
            lambda: request(self.alice, self.question.pk),
            lambda: request(self.bob, self.alice_question.pk),
        ])
        self.assertEqual(results.count('already-exists'), 1)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)

    def test_duplicate_simultaneous_send_has_one_delivery(self):
        conversation_id = self.accepted()
        token = uuid.uuid4()
        results = self._parallel([
            lambda: services.send_message(self.alice, conversation_id, 'One delivery', token),
            lambda: services.send_message(self.alice, conversation_id, 'One delivery', token),
        ])
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(Message.objects.count(), 2)

    def test_concurrent_allowance_serializes_sender(self):
        conversation_id = self.accepted()
        def send():
            try:
                services.send_message(self.alice, conversation_id, 'One remaining allowance', uuid.uuid4())
                return 'sent'
            except ValidationError:
                return 'limited'
        with patch.object(services, 'MESSAGES_PER_MINUTE', 2):
            results = self._parallel([send, send])
        self.assertCountEqual(results, ['sent', 'limited'])
        self.assertEqual(Message.objects.count(), 2)

    def test_committed_block_prevents_a_send_waiting_on_participant_lock(self):
        conversation_id = self.accepted()
        block_held, sender_started, release_block = Event(), Event(), Event()
        def block():
            close_old_connections()
            try:
                with transaction.atomic():
                    set_block(user=self.bob, blocked_user=self.alice)
                    block_held.set()
                    if not release_block.wait(timeout=10):
                        raise AssertionError('Timed out waiting to commit the block.')
            finally:
                close_old_connections()
        def send():
            close_old_connections()
            try:
                sender_started.set()
                try:
                    services.send_message(self.alice, conversation_id, 'Must not be delivered', uuid.uuid4())
                    return 'sent'
                except PermissionDenied:
                    return 'blocked'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocker = pool.submit(block)
            try:
                self.assertTrue(block_held.wait(timeout=10))
                sender = pool.submit(send)
                self.assertTrue(sender_started.wait(timeout=10))
                self.assertFalse(sender.done())
            finally:
                release_block.set()
            blocker.result(timeout=10)
            self.assertEqual(sender.result(timeout=10), 'blocked')
        self.assertEqual(Message.objects.count(), 1)

    def test_preference_opt_out_prevents_waiting_acceptance(self):
        conversation_id = self.request()
        preference_held, accept_started, release_preference = Event(), Event(), Event()
        def disable():
            close_old_connections()
            try:
                with transaction.atomic():
                    services.save_preferences(self.bob, False)
                    preference_held.set()
                    if not release_preference.wait(timeout=10):
                        raise AssertionError('Timed out waiting to commit preference.')
            finally:
                close_old_connections()
        def accept():
            close_old_connections()
            try:
                accept_started.set()
                try:
                    services.respond_request(self.bob, conversation_id, True)
                    return 'accepted'
                except ValidationError:
                    return 'closed'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            disabler = pool.submit(disable)
            try:
                self.assertTrue(preference_held.wait(timeout=10))
                accepter = pool.submit(accept)
                self.assertTrue(accept_started.wait(timeout=10))
                self.assertFalse(accepter.done())
            finally:
                release_preference.set()
            disabler.result(timeout=10)
            self.assertEqual(accepter.result(timeout=10), 'closed')
        self.assertEqual(Conversation.objects.get(pk=conversation_id).status, 'closed')
