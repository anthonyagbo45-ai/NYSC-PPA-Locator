"""HTTP boundaries for consent-based private messaging and safety actions."""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from community.models import Answer, CommunityBlock, Question
from locations.models import LGA, State
from . import services
from .models import Conversation, Message, MessageReport, MessengerPreference


class MessengerViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Messenger test state')
        cls.lga = LGA.objects.create(name='Messenger test LGA', state=cls.state)
        User = get_user_model()
        cls.sender = User.objects.create_user(
            state_code='MS/26A/1001', email='PRIVATE-SENDER@example.com',
            full_name='PRIVATE SENDER LEGAL NAME', display_name='Helpful neighbour',
            email_verified=True,
        )
        cls.recipient = User.objects.create_user(
            state_code='MS/26A/1002', email='PRIVATE-RECIPIENT@example.com',
            full_name='PRIVATE RECIPIENT LEGAL NAME', display_name='Local neighbour',
            email_verified=True,
        )
        cls.outsider = User.objects.create_user(
            state_code='MS/26A/1003', email='PRIVATE-STAFF@example.com',
            full_name='PRIVATE STAFF LEGAL NAME', display_name='Outside member',
            email_verified=True, is_staff=True, is_superuser=True,
        )
        cls.unverified = User.objects.create_user(
            state_code='MS/26A/1004', email='PRIVATE-UNVERIFIED@example.com',
            full_name='PRIVATE UNVERIFIED LEGAL NAME',
        )
        cls.question = Question.objects.create(
            author=cls.recipient, title='Where can I find the community bus stop?',
            body='A visible public question.', category='transport',
            state=cls.state, lga=cls.lga,
        )
        cls.answer = Answer.objects.create(
            author=cls.recipient, question=cls.question,
            body='A useful visible public answer.',
        )

    def setUp(self):
        cache.clear()

    def url(self, name, conversation=None, message_id=None):
        args = []
        if conversation is not None:
            args.append(getattr(conversation, 'pk', conversation))
        if message_id is not None:
            args.append(message_id)
        return reverse('messenger:' + name, args=args)

    def enable(self, *members):
        for member in members or (self.sender, self.recipient):
            services.save_preferences(member, allow_requests=True)

    def pending(self, body='PRIVATE INTRODUCTION ONLY FOR THE TWO PARTICIPANTS'):
        self.enable()
        return services.request_chat(
            self.sender, context_type='question', context_id=self.question.pk,
            body=body, client_token=uuid4(),
        )

    def accepted(self):
        conversation = self.pending()
        services.respond_request(self.recipient, conversation, accept=True)
        return conversation

    def assert_private_identity_absent(self, response):
        for member in (self.sender, self.recipient, self.outsider, self.unverified):
            for value in (member.state_code, member.email, member.full_name):
                self.assertNotContains(response, value)

    def test_every_private_page_requires_signin(self):
        conversation = self.pending()
        message = Message.objects.get()
        for url in [
            self.url('inbox'), self.url('preferences'), self.url('thread', conversation),
            reverse('messenger:request_question', args=[self.question.pk]),
            reverse('messenger:request_answer', args=[self.answer.pk]),
            self.url('report', conversation, message.pk),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(Message.objects.count(), 1)

    def test_preferences_default_off_and_get_has_no_side_effects(self):
        self.client.force_login(self.sender)
        self.assertEqual(self.client.get(self.url('inbox')).status_code, 200)
        self.assertEqual(self.client.get(self.url('preferences')).status_code, 200)
        self.assertFalse(MessengerPreference.objects.exists())
        self.assertFalse(services.get_preferences(self.sender)['allow_requests'])
        response = self.client.post(self.url('preferences'), {'allow_requests': 'on', 'user': self.recipient.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(services.get_preferences(self.sender)['allow_requests'])
        self.assertFalse(services.get_preferences(self.recipient)['allow_requests'])
        self.assertEqual(self.client.post(self.url('preferences'), {}).status_code, 302)
        self.assertFalse(services.get_preferences(self.sender)['allow_requests'])

    def test_complete_context_request_accept_send_and_read_flow(self):
        self.enable()
        self.client.force_login(self.sender)
        request_url = reverse('messenger:request_question', args=[self.question.pk])
        self.assertEqual(self.client.get(request_url).status_code, 200)
        self.assertFalse(Conversation.objects.exists())
        token = str(uuid4())
        response = self.client.post(request_url, {
            'body': 'May I ask a private follow-up?', 'client_token': token,
            'recipient': self.outsider.pk, 'sender': self.outsider.pk,
            'status': 'accepted',
        })
        self.assertEqual(response.status_code, 302)
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.status, 'pending')
        self.assertEqual(Message.objects.count(), 1)
        self.assertContains(self.client.get(self.url('thread', conversation)), 'May I ask a private follow-up?')
        self.client.force_login(self.recipient)
        response = self.client.post(self.url('respond', conversation), {'action': 'accept', 'user': self.outsider.pk})
        self.assertEqual(response.status_code, 302)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, 'accepted')
        response = self.client.post(self.url('send', conversation), {
            'body': 'Yes, happy to help.', 'client_token': str(uuid4()),
            'sender': self.outsider.pk, 'conversation': str(uuid4()),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Message.objects.count(), 2)
        self.client.force_login(self.sender)
        response = self.client.get(self.url('thread', conversation))
        self.assertContains(response, 'Yes, happy to help.')
        self.assert_private_identity_absent(response)
        latest = Message.objects.order_by('-pk').first()
        self.assertEqual(self.client.post(self.url('read', conversation), {'message_id': latest.pk}).status_code, 302)
        self.assertFalse(services.get_inbox(self.sender).object_list[0]['unread'])

    def test_answer_context_resolves_recipient_without_directory(self):
        self.enable()
        self.client.force_login(self.sender)
        response = self.client.post(reverse('messenger:request_answer', args=[self.answer.pk]), {
            'body': 'Can I ask about your answer?', 'client_token': str(uuid4()),
            'recipient': self.outsider.pk,
        })
        self.assertEqual(response.status_code, 302)
        conversation = Conversation.objects.get()
        self.client.force_login(self.recipient)
        self.assertContains(self.client.get(self.url('thread', conversation)), 'Can I ask about your answer?')
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url('thread', conversation)).status_code, 404)

    def test_decline_close_and_intro_limit_prevent_unwanted_followups(self):
        conversation = self.pending()
        self.client.force_login(self.sender)
        response = self.client.post(self.url('send', conversation), {'body': 'Unsolicited second request', 'client_token': str(uuid4())})
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(Message.objects.count(), 1)
        self.client.force_login(self.recipient)
        self.assertEqual(self.client.post(self.url('respond', conversation), {'action': 'decline'}).status_code, 302)
        self.client.force_login(self.sender)
        response = self.client.post(reverse('messenger:request_question', args=[self.question.pk]), {'body': 'Please answer again', 'client_token': str(uuid4())})
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.client.post(self.url('close', conversation)).status_code, 302)
        self.assertContains(self.client.get(self.url('thread', conversation)), 'PRIVATE INTRODUCTION')

    def test_only_recipient_can_accept_pending_request(self):
        conversation = self.pending()
        self.client.force_login(self.sender)
        self.assertIn(self.client.post(self.url('respond', conversation), {'action': 'accept'}).status_code, (400, 403))
        self.assertEqual(Conversation.objects.get().status, 'pending')
        self.assertEqual(Message.objects.count(), 1)

    def test_superuser_outsider_has_no_private_thread_or_mutation_override(self):
        conversation = self.accepted()
        message = Message.objects.get()
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(self.url('inbox')), message.body)
        for url, body in [
            (self.url('thread', conversation), None),
            (self.url('report', conversation, message.pk), None),
            (self.url('send', conversation), {'body': 'FORGED', 'client_token': str(uuid4())}),
            (self.url('read', conversation), {'message_id': message.pk}),
            (self.url('respond', conversation), {'action': 'accept'}),
            (self.url('close', conversation), {}),
            (self.url('block', conversation), {}),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url) if body is None else self.client.post(url, body)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(message.body.encode(), response.content)
        self.assertEqual(Message.objects.count(), 1)
        self.assertFalse(MessageReport.objects.exists())
        self.assertFalse(CommunityBlock.objects.exists())

    def test_get_never_accepts_sends_reads_closes_or_blocks(self):
        conversation = self.pending()
        self.client.force_login(self.recipient)
        before = list(Conversation.objects.values())
        for name in ('respond', 'send', 'read', 'close', 'block'):
            self.assertEqual(self.client.get(self.url(name, conversation)).status_code, 405)
        self.assertEqual(list(Conversation.objects.values()), before)
        self.assertEqual(Message.objects.count(), 1)
        self.assertFalse(CommunityBlock.objects.exists())

    def test_get_thread_does_not_mark_messages_read(self):
        conversation = self.accepted()
        services.send_message(self.recipient, conversation, body='A fresh private answer', client_token=uuid4())
        self.client.force_login(self.sender)
        self.assertTrue(services.get_inbox(self.sender).object_list[0]['unread'])
        self.assertContains(self.client.get(self.url('thread', conversation)), 'A fresh private answer')
        self.assertTrue(services.get_inbox(self.sender).object_list[0]['unread'])

    def test_csrf_is_required_for_all_private_actions(self):
        conversation = self.accepted()
        message = Message.objects.get()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.recipient)
        for url, body in [
            (self.url('preferences'), {'allow_requests': 'on'}),
            (self.url('respond', conversation), {'action': 'decline'}),
            (self.url('send', conversation), {'body': 'FORGED', 'client_token': str(uuid4())}),
            (self.url('read', conversation), {'message_id': message.pk}),
            (self.url('close', conversation), {}),
            (self.url('block', conversation), {}),
            (self.url('report', conversation, message.pk), {'category': 'abuse', 'details': 'Please review this message.'}),
        ]:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, body).status_code, 403)
        client.force_login(self.sender)
        self.assertEqual(client.post(reverse('messenger:request_question', args=[self.question.pk]), {'body': 'FORGED', 'client_token': str(uuid4())}).status_code, 403)
        self.assertEqual(Message.objects.count(), 1)
        self.assertFalse(MessageReport.objects.exists())

    def test_private_pages_never_cache_or_disclose_authentication_identity(self):
        conversation = self.pending()
        message = Message.objects.get()
        self.client.force_login(self.recipient)
        for url in [self.url('inbox'), self.url('preferences'), self.url('thread', conversation), self.url('report', conversation, message.pk)]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response['Cache-Control'])
            self.assert_private_identity_absent(response)
        response = self.client.get(self.url('thread', conversation))
        self.assertIsInstance(response.context['thread'], dict)
        for unsafe in ('low_member', 'high_member', 'initiated_by', 'email', 'state_code', 'full_name'):
            self.assertNotIn(unsafe, response.context['thread'])
        for message_row in response.context['message_rows']:
            self.assertIsInstance(message_row, dict)
            self.assertNotIn('sender_id', message_row)
            self.assertNotIn('sender', message_row)

    def test_message_and_report_previews_escape_untrusted_text(self):
        body = '<script>alert("PRIVATE")</script><img src=x onerror=alert(1)>'
        conversation = self.pending(body)
        message = Message.objects.get()
        self.client.force_login(self.recipient)
        for url in [self.url('thread', conversation), self.url('report', conversation, message.pk)]:
            response = self.client.get(url)
            self.assertContains(response, '&lt;script&gt;')
            self.assertNotContains(response, body)
            self.assertNotContains(response, '<img src=x')

    def test_malformed_and_oversized_message_forms_have_no_side_effect(self):
        self.enable()
        self.client.force_login(self.sender)
        request_url = reverse('messenger:request_question', args=[self.question.pk])
        for body in [dict(body='x', client_token='not-a-uuid'), dict(body='x' * 2001, client_token=str(uuid4())), dict(body='  ', client_token=str(uuid4())), dict(body='Missing token')]:
            with self.subTest(body_length=len(body['body'])):
                self.assertEqual(self.client.post(request_url, body).status_code, 400)
        self.assertFalse(Conversation.objects.exists())
        conversation = self.accepted()
        for body in [dict(body='x', client_token='not-a-uuid'), dict(body='x' * 2001, client_token=str(uuid4())), dict(body='')]:
            self.assertEqual(self.client.post(self.url('send', conversation), body).status_code, 400)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.client.get('/messenger/not-a-uuid/').status_code, 404)

    def test_invalid_action_and_read_target_are_rejected(self):
        conversation = self.pending()
        self.client.force_login(self.recipient)
        for body in ({}, {'action': 'promote'}, {'action': 'accepted'}):
            self.assertEqual(self.client.post(self.url('respond', conversation), body).status_code, 400)
        for body in ({}, {'message_id': 'NaN'}, {'message_id': '-1'}, {'message_id': '9' * 100}):
            self.assertEqual(self.client.post(self.url('read', conversation), body).status_code, 400)
        self.assertEqual(Conversation.objects.get().status, 'pending')

    def test_disabled_or_invisible_context_cannot_receive_message_requests(self):
        self.enable(self.sender)
        self.client.force_login(self.sender)
        url = reverse('messenger:request_question', args=[self.question.pk])
        for method in ('get', 'post'):
            response = getattr(self.client, method)(url, {'body': 'Unwanted request', 'client_token': str(uuid4())} if method == 'post' else {})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, reverse('messenger:preferences'), status_code=400)
            for private_value in (self.recipient.email, self.recipient.full_name, self.recipient.state_code):
                self.assertNotContains(response, private_value, status_code=400)
        self.assertFalse(Conversation.objects.exists())
        self.enable(self.recipient)
        Question.objects.filter(pk=self.question.pk).update(status='hidden')
        for route, pk in [('request_question', self.question.pk), ('request_answer', self.answer.pk)]:
            self.assertEqual(self.client.get(reverse('messenger:' + route, args=[pk])).status_code, 404)
        self.assertFalse(Conversation.objects.exists())

    def test_default_off_request_page_points_to_own_message_preferences(self):
        self.enable(self.recipient)
        self.client.force_login(self.sender)
        response = self.client.get(reverse('messenger:request_question', args=[self.question.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, reverse('messenger:preferences'), status_code=400)
        self.assertFalse(services.get_preferences(self.sender)['allow_requests'])
        self.assertFalse(Conversation.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_message_request_requires_fresh_verification(self):
        self.enable()
        self.client.force_login(self.sender)
        get_user_model().objects.filter(pk=self.sender.pk).update(email_verified=False)
        response = self.client.post(reverse('messenger:request_question', args=[self.question.pk]), {'body': 'A request after verification revoked', 'client_token': str(uuid4())})
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(Conversation.objects.exists())

    def test_block_stops_sends_but_keeps_reportable_private_history(self):
        conversation = self.accepted()
        message = Message.objects.get()
        self.client.force_login(self.recipient)
        self.assertEqual(self.client.post(self.url('block', conversation)).status_code, 302)
        self.assertTrue(CommunityBlock.objects.filter(user=self.recipient, blocked_user=self.sender).exists())
        self.assertContains(self.client.get(self.url('thread', conversation)), message.body)
        self.assertFalse(services.get_inbox(self.recipient).object_list[0]['unread'])
        response = self.client.post(self.url('report', conversation, message.pk), {'category': 'abuse', 'details': 'Please review only this selected message.', 'reporter': self.outsider.pk, 'status': 'resolved'})
        self.assertEqual(response.status_code, 302)
        report = MessageReport.objects.get()
        self.assertEqual(report.reporter_id, self.recipient.pk)
        self.assertEqual(report.status, 'open')
        self.assertIn(message.body, str(report.evidence))
        self.client.force_login(self.sender)
        self.assertIn(self.client.post(self.url('send', conversation), {'body': 'Blocked followup', 'client_token': str(uuid4())}).status_code, (400, 403))
        self.assertEqual(Message.objects.count(), 1)

    def test_safety_actions_remain_available_after_email_verification_revoked(self):
        conversation = self.accepted()
        message = Message.objects.get()
        get_user_model().objects.filter(pk=self.recipient.pk).update(email_verified=False)
        self.client.force_login(self.recipient)
        self.assertContains(self.client.get(self.url('thread', conversation)), message.body)
        self.assertEqual(self.client.post(self.url('report', conversation, message.pk), {'category': 'abuse', 'details': 'Private safety report after email change.'}).status_code, 302)
        self.assertEqual(self.client.post(self.url('preferences'), {}).status_code, 302)
        self.assertEqual(self.client.post(self.url('close', conversation)).status_code, 302)
        self.assertEqual(self.client.post(self.url('block', conversation)).status_code, 302)

    def test_reports_cannot_select_another_conversations_message(self):
        conversation = self.accepted()
        self.enable(self.outsider)
        other = services.request_chat(self.outsider, context_type='question', context_id=self.question.pk, body='UNRELATED PRIVATE THREAD', client_token=uuid4())
        other_message = Message.objects.filter(body='UNRELATED PRIVATE THREAD').get()
        self.client.force_login(self.sender)
        for method in ('get', 'post'):
            response = getattr(self.client, method)(self.url('report', conversation, other_message.pk), {'category': 'abuse', 'details': 'Forged evidence target.'} if method == 'post' else {})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b'UNRELATED PRIVATE THREAD', response.content)
        self.assertFalse(MessageReport.objects.exists())

    def test_cache_failures_stop_new_private_writes(self):
        self.enable()
        self.client.force_login(self.sender)
        with patch('config.security.cache.add', side_effect=RuntimeError('cache unavailable')):
            response = self.client.post(reverse('messenger:request_question', args=[self.question.pk]), {'body': 'Rate limit cannot be checked', 'client_token': str(uuid4())})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Conversation.objects.exists())

    def test_context_links_are_scoped_to_content_and_hide_recipient_identifiers(self):
        self.enable()
        self.client.force_login(self.sender)
        response = self.client.get(reverse('community:question_detail', args=[self.question.pk]))
        self.assertContains(response, reverse('messenger:request_question', args=[self.question.pk]))
        self.assertContains(response, reverse('messenger:request_answer', args=[self.answer.pk]))
        self.assert_private_identity_absent(response)
        self.assertNotContains(response, 'name="recipient"')
        self.assertNotContains(response, 'name="sender"')
        self.client.force_login(self.recipient)
        response = self.client.get(reverse('community:question_detail', args=[self.question.pk]))
        self.assertNotContains(response, reverse('messenger:request_question', args=[self.question.pk]))
        self.assertNotContains(response, reverse('messenger:request_answer', args=[self.answer.pk]))

    def test_cursor_pagination_does_not_mix_threads(self):
        conversation = self.accepted()
        for number in range(55):
            Message.objects.create(conversation_id=conversation, sender=self.sender, body=f'Only this conversation message {number:02d}', client_token=uuid4())
        self.client.force_login(self.recipient)
        response = self.client.get(self.url('thread', conversation))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.context['message_rows']), 50)
        first_batch_ids = {row['id'] for row in response.context['message_rows']}
        before = min(first_batch_ids)
        older = self.client.get(self.url('thread', conversation), {'before': before})
        self.assertEqual(older.status_code, 200)
        self.assertTrue(first_batch_ids.isdisjoint(row['id'] for row in older.context['message_rows']))
        for cursor in ['NaN', '-1', '0', '9' * 100]:
            self.assertEqual(self.client.get(self.url('thread', conversation), {'before': cursor}).status_code, 400)

    def test_send_form_replay_does_not_duplicate_message(self):
        conversation = self.accepted()
        self.client.force_login(self.sender)
        payload = {'body': 'One intentional message', 'client_token': str(uuid4())}
        for _ in range(2):
            self.assertEqual(self.client.post(self.url('send', conversation), payload).status_code, 302)
        self.assertEqual(Message.objects.filter(body='One intentional message').count(), 1)
