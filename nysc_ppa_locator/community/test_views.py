from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from locations.models import LGA, State
from notifications.models import NotificationPreference
from ppas.models import PPA
from service_resources.models import CDSGroup
from . import services
from .models import Answer, CommunityBlock, CommunityReport, HelpfulReaction, Question, QuestionFollow, ReportRevision


class CommunityViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Community state')
        cls.lga = LGA.objects.create(name='Community LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other community state')
        cls.other_lga = LGA.objects.create(name='Other community LGA', state=cls.other_state)
        cls.author = get_user_model().objects.create_user(state_code='CM/26A/0001', email='private-author@example.com', full_name='Private author legal name', display_name='Ada', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='CM/26A/0002', email='private-other@example.com', full_name='Private other legal name', display_name='Bola', email_verified=True)
        cls.unverified = get_user_model().objects.create_user(state_code='CM/26A/0003', email='unverified@example.com', full_name='Unverified legal name')
        cls.ppa = PPA.objects.create(name='Community linked PPA', state=cls.state, lga=cls.lga, address='Public institution')
        cls.group = CDSGroup.objects.create(author=cls.author, title='Community public CDS', description='Public group context', state=cls.state, lga=cls.lga, observed_on=timezone.localdate(), expires_at=timezone.now() + timedelta(days=7), publication_status='approved')

    def setUp(self):
        cache.clear()

    def question(self, **values):
        defaults = dict(author=self.author, title='How do I settle into this area?', body='Useful public question context.', category='ppa', state=self.state, lga=self.lga)
        defaults.update(values)
        return Question.objects.create(**defaults)

    def answer(self, question, **values):
        defaults = dict(question=question, author=self.other, body='A helpful community suggestion.', source_url='https://example.com/public')
        defaults.update(values)
        return Answer.objects.create(**defaults)

    def data(self, **changes):
        values = dict(title='Where is the public transport stop?', body='Public context with no personal details.', category='transport', state=self.state.pk, lga=self.lga.pk, ppa='', group='', privacy_confirmed='on')
        values.update(changes)
        return values

    def test_public_pages_use_safe_projections_and_never_expose_member_details(self):
        question = self.question(ppa=self.ppa, group=self.group)
        self.answer(question)
        hidden = self.question(title='Hidden private question', status='hidden')
        self.answer(question, body='Hidden answer content', status='hidden')
        for route in ('home', 'question_detail'):
            response = self.client.get(reverse('community:' + route, args=[question.pk] if route == 'question_detail' else []))
            self.assertEqual(response.status_code, 200)
            for secret in [self.author.email, self.author.state_code, self.author.full_name, self.other.email, self.other.full_name, 'Hidden private question', 'Hidden answer content']:
                self.assertNotContains(response, secret)
            page = response.context['page_obj']
            self.assertTrue(all(isinstance(item, dict) for item in page.object_list))
            self.assertTrue(all(isinstance(item, dict) for item in page.paginator.object_list))
            for item in page:
                self.assertNotIn('author', item)
                self.assertNotIn('author_id', item)
            self.assertNotContains(response, 'Official answer')
        self.assertEqual(self.client.get(reverse('community:question_detail', args=[hidden.pk])).status_code, 404)

    def test_typed_geographic_filters_and_invalid_cross_area_choices(self):
        item = self.question(ppa=self.ppa, group=self.group)
        other = self.question(title='Other place question', state=self.other_state, lga=self.other_lga)
        response = self.client.get(reverse('community:home'), dict(ppa=self.ppa.pk, group=self.group.pk, category='ppa', q='settle'))
        self.assertContains(response, item.title)
        self.assertNotContains(response, other.title)
        for values in [dict(state=self.state.pk, lga=self.other_lga.pk), dict(ppa=self.ppa.pk, state=self.other_state.pk, lga=self.other_lga.pk), dict(sort='private'), dict(category='official'), dict(q='x' * 121), dict(state='NaN')]:
            self.assertEqual(self.client.get(reverse('community:home'), values).status_code, 400)

    def test_sort_unanswered_resolved_and_helpful(self):
        unanswered = self.question(title='No answers yet')
        resolved = self.question(title='Resolved public question', resolved=True)
        answer = self.answer(resolved)
        HelpfulReaction.objects.create(user=self.author, answer=answer)
        response = self.client.get(reverse('community:home'), {'sort': 'unanswered'})
        self.assertContains(response, unanswered.title)
        self.assertNotContains(response, resolved.title)
        response = self.client.get(reverse('community:home'), {'sort': 'resolved'})
        self.assertContains(response, resolved.title)
        self.assertNotContains(response, unanswered.title)
        response = self.client.get(reverse('community:home'), {'sort': 'helpful'})
        self.assertEqual(response.context['questions'][0]['id'], resolved.pk)

    def test_verified_create_uses_allowlist_and_fresh_consent(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse('community:question_add'), self.data(author=self.other.pk, status='hidden', resolved='1', version='99', moderator_note='FORGED MODERATION'))
        item = Question.objects.get(title='Where is the public transport stop?')
        self.assertRedirects(response, reverse('community:question_detail', args=[item.pk]))
        self.assertEqual(item.author_id, self.author.pk)
        self.assertEqual(item.status, 'visible')
        self.assertFalse(item.resolved)
        self.assertEqual(item.version, 1)
        response = self.client.post(reverse('community:question_add'), self.data(title='No consent', privacy_confirmed=''))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Question.objects.filter(title='No consent').exists())

    def test_location_choices_can_refresh_without_publication_or_lost_draft(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse('community:question_add'), self.data(action='refresh', title='Draft preserved', ppa=self.ppa.pk))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.exists())
        self.assertEqual(response.context['form'].initial['title'], 'Draft preserved')
        self.assertContains(response, self.ppa.name)

    def test_guest_and_unverified_cannot_contribute_but_can_browse(self):
        question = self.question()
        for route, args in [('question_add', []), ('answer_add', [question.pk])]:
            url = reverse('community:' + route, args=args)
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(self.unverified)
            self.assertRedirects(self.client.get(url), reverse('email_verification'))
            self.client.logout()
        self.assertEqual(self.client.get(reverse('community:home')).status_code, 200)

    def test_hidden_edits_preserve_restriction_and_revision_history_is_owner_only(self):
        question = self.question(status='hidden')
        self.client.force_login(self.author)
        response = self.client.post(reverse('community:question_edit', args=[question.pk]), self.data(title='Owner edited hidden question', version=question.version, status='visible'))
        self.assertRedirects(response, reverse('community:my_activity'))
        question.refresh_from_db()
        self.assertEqual(question.status, 'hidden')
        response = self.client.get(reverse('community:question_history', args=[question.pk]))
        self.assertContains(response, 'Owner edited hidden question')
        for item in response.context['revisions']:
            self.assertNotIn('reason', item)
            self.assertNotIn('actor', item)
            self.assertNotIn('author_id', item['snapshot'])
        self.client.force_login(self.other)
        for route in ['question_edit', 'question_history']:
            self.assertEqual(self.client.get(reverse('community:' + route, args=[question.pk])).status_code, 404)

    def test_stale_or_missing_edit_version_is_rejected(self):
        question = self.question(version=2)
        self.client.force_login(self.author)
        for extra in [{}, {'version': 1}]:
            response = self.client.post(reverse('community:question_edit', args=[question.pk]), self.data(**extra))
            self.assertEqual(response.status_code, 400)
        question.refresh_from_db()
        self.assertEqual(question.version, 2)

    def test_owner_can_edit_hidden_answer_under_hidden_parent_without_parent_disclosure(self):
        question = self.question(title='Hidden parent title', body='Hidden parent private content', status='hidden')
        answer = self.answer(question, status='hidden')
        self.client.force_login(self.other)
        url = reverse('community:answer_edit', args=[answer.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, question.title)
        self.assertNotContains(response, question.body)
        response = self.client.post(url, dict(body='Updated my hidden answer', source_url='', privacy_confirmed='on', version=answer.version, status='visible'))
        self.assertRedirects(response, reverse('community:my_activity'))
        answer.refresh_from_db()
        self.assertEqual(answer.status, 'hidden')
        self.assertEqual(answer.body, 'Updated my hidden answer')
        response = self.client.get(reverse('community:my_activity'), {'kind': 'answers'})
        self.assertNotContains(response, 'Read public discussion')

    def test_answers_escape_markup_and_public_sources_never_grant_badges(self):
        question = self.question()
        self.client.force_login(self.other)
        response = self.client.post(reverse('community:answer_add', args=[question.pk]), dict(body='<script>alert("unsafe")</script>', source_url='https://example.com/official', privacy_confirmed='on', status='visible', author=self.author.pk, official='1'))
        self.assertRedirects(response, reverse('community:question_detail', args=[question.pk]))
        response = self.client.get(reverse('community:question_detail', args=[question.pk]))
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert')
        self.assertContains(response, 'rel="nofollow noopener noreferrer"')
        self.assertNotContains(response, 'Official answer')
        answer = Answer.objects.get(question=question)
        self.assertEqual(answer.author_id, self.other.pk)
        Answer.objects.filter(pk=answer.pk).update(source_url='javascript:alert(1)')
        self.assertNotContains(self.client.get(reverse('community:question_detail', args=[question.pk])), 'javascript:')

    def test_unsafe_source_and_missing_consent_do_not_create_answers(self):
        question = self.question()
        self.client.force_login(self.other)
        for values in [dict(body='Unsafe link', source_url='javascript:alert(1)', privacy_confirmed='on'), dict(body='Missing consent', source_url='')]:
            self.assertEqual(self.client.post(reverse('community:answer_add', args=[question.pk]), values).status_code, 400)
        self.assertFalse(Answer.objects.exists())

    def test_helpful_unique_and_cannot_vote_for_own_answer(self):
        question = self.question()
        answer = self.answer(question)
        self.client.force_login(self.other)
        self.client.post(reverse('community:vote', args=[answer.pk]), {'value': '1'})
        self.assertFalse(HelpfulReaction.objects.exists())
        self.client.force_login(self.author)
        for _ in range(2):
            self.client.post(reverse('community:vote', args=[answer.pk]), {'value': '1'})
        self.assertEqual(HelpfulReaction.objects.count(), 1)
        self.client.post(reverse('community:vote', args=[answer.pk]), {'value': '0'})
        self.assertFalse(HelpfulReaction.objects.exists())

    def test_follow_is_private_silent_default_and_item_choice_paused_globally(self):
        question = self.question()
        self.client.force_login(self.unverified)
        self.client.post(reverse('community:follow', args=[question.pk]), {'action': 'follow'})
        self.assertFalse(QuestionFollow.objects.get(user=self.unverified).enabled)
        self.client.post(reverse('community:follow', args=[question.pk]), {'action': 'follow', 'enabled': 'on'})
        self.assertTrue(QuestionFollow.objects.get(user=self.unverified).enabled)
        self.assertFalse(NotificationPreference.objects.filter(user=self.unverified, community_replies=True).exists())
        response = self.client.get(reverse('community:question_detail', args=[question.pk]))
        self.assertContains(response, 'Community notifications are paused')
        self.client.get(reverse('community:my_activity'), {'kind': 'following'})
        self.client.post(reverse('community:follow', args=[question.pk]), {'action': 'unfollow'})
        self.assertFalse(QuestionFollow.objects.exists())

    def test_private_reports_allow_unverified_reporter_and_hide_moderator_evidence(self):
        question = self.question()
        self.client.force_login(self.unverified)
        response = self.client.post(reverse('community:report_question', args=[question.pk]), dict(category='privacy', details='My private report details', private_evidence='My submitted evidence'))
        self.assertRedirects(response, reverse('community:reports'))
        CommunityReport.objects.update(moderator_note='PRIVATE MODERATOR INVESTIGATION')
        response = self.client.get(reverse('community:reports'))
        self.assertContains(response, 'My private report details')
        self.assertNotContains(response, 'PRIVATE MODERATOR INVESTIGATION')
        self.assertTrue(all(isinstance(item, dict) for item in response.context['page_obj'].paginator.object_list))
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('community:reports')), 'My private report details')

    def test_private_report_status_history_excludes_moderator_evidence_and_is_owner_only(self):
        question = self.question()
        report = CommunityReport.objects.create(reporter=self.unverified, question=question, category='privacy', details='My private evidence', moderator_note='SECRET MODERATOR REASON')
        ReportRevision.objects.create(report=report, actor=self.author, previous_status='open', new_status='reviewed', moderator_note='SECRET MODERATOR REASON')
        url = reverse('community:report_history', args=[report.pk])
        self.client.force_login(self.unverified)
        response = self.client.get(url)
        self.assertContains(response, 'My private evidence')
        self.assertNotContains(response, 'SECRET MODERATOR REASON')
        for row in response.context['revisions']:
            self.assertEqual(set(row), {'previous_status', 'new_status', 'created_at'})
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_blocks_derive_author_from_visible_content_and_prevent_interactions(self):
        question = self.question()
        answer = self.answer(question)
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.post(reverse('community:block'), {'user': self.author.pk}).status_code, 400)
        self.client.post(reverse('community:block'), dict(target_type='question', target_id=question.pk))
        entry = CommunityBlock.objects.get(user=self.unverified, blocked_user=self.author)
        response = self.client.get(reverse('community:blocks'))
        self.assertContains(response, 'Public questions, answers and room posts remain publicly readable')
        self.client.post(reverse('community:follow', args=[question.pk]), {'action': 'follow'})
        self.assertFalse(QuestionFollow.objects.filter(user=self.unverified).exists())
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse('community:unblock', args=[entry.pk])).status_code, 404)
        self.client.force_login(self.unverified)
        self.client.post(reverse('community:unblock', args=[entry.pk]))
        self.assertFalse(CommunityBlock.objects.exists())
        self.client.force_login(self.author)
        self.client.post(reverse('community:block'), dict(target_type='answer', target_id=answer.pk))
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('community:answer_add', args=[question.pk]), dict(body='Blocked response', source_url='', privacy_confirmed='on')).status_code, 302)
        self.assertFalse(Answer.objects.filter(body='Blocked response').exists())
        self.client.logout()
        self.assertEqual(self.client.get(reverse('community:question_detail', args=[question.pk])).status_code, 200)

    def test_state_changes_are_post_only_and_csrf_protected(self):
        question = self.question()
        answer = self.answer(question)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        routes = [('question_archive', question.pk), ('question_resolve', question.pk), ('vote', answer.pk), ('follow', question.pk)]
        for route, pk in routes:
            url = reverse('community:' + route, args=[pk])
            self.assertEqual(client.get(url).status_code, 405)
            self.assertEqual(client.post(url, {'value': '1', 'action': 'follow'}).status_code, 403)
        self.assertEqual(client.post(reverse('community:block'), dict(target_type='answer', target_id=answer.pk)).status_code, 403)
        question.refresh_from_db()
        self.assertEqual(question.status, 'visible')
        self.assertFalse(question.resolved)

    def test_archiving_and_resolved_marker_are_author_only(self):
        question = self.question()
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('community:question_resolve', args=[question.pk]), {'value': '1'}).status_code, 404)
        self.client.force_login(self.author)
        self.client.post(reverse('community:question_resolve', args=[question.pk]), {'value': '1'})
        question.refresh_from_db()
        self.assertTrue(question.resolved)
        self.client.post(reverse('community:question_archive', args=[question.pk]))
        self.client.logout()
        self.assertEqual(self.client.get(reverse('community:question_detail', args=[question.pk])).status_code, 404)

    def test_pagination_preserves_filters_and_bounds_public_page_queries(self):
        Question.objects.bulk_create([Question(author=self.author, title=f'Public paged question {index}', body='General public context', category='transport', state=self.state, lga=self.lga) for index in range(45)])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse('community:home'), dict(state=self.state.pk, lga=self.lga.pk, category='transport', q='Public', sort='newest', page=2))
        self.assertEqual(len(response.context['questions']), 20)
        self.assertEqual(response.context['page_obj'].paginator.count, 45)
        self.assertContains(response, 'category=transport')
        self.assertContains(response, 'sort=newest')
        self.assertContains(response, 'q=Public')
        self.assertLessEqual(len(captured), 16)
        question = Question.objects.first()
        Answer.objects.bulk_create([Answer(question=question, author=self.other, body=f'Public answer {index}') for index in range(45)])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse('community:question_detail', args=[question.pk]), {'page': 2})
        self.assertEqual(len(response.context['answers']), 20)
        self.assertEqual(response.context['page_obj'].paginator.count, 45)
        self.assertLessEqual(len(captured), 6)

    def test_home_links_to_existing_knowledge_modules(self):
        response = self.client.get(reverse('community:home'))
        self.assertContains(response, reverse('local_guides:home'))
        self.assertContains(response, reverse('service_resources:home'))

    def test_email_change_does_not_prevent_owner_withdrawing_existing_public_content(self):
        question = self.question()
        answer = self.answer(question, author=self.author)
        get_user_model().objects.filter(pk=self.author.pk).update(email_verified=False)
        self.client.force_login(self.author)
        self.assertRedirects(self.client.post(reverse('community:answer_archive', args=[answer.pk])), reverse('community:my_activity'))
        self.assertRedirects(self.client.post(reverse('community:question_archive', args=[question.pk])), reverse('community:my_activity'))
        question.refresh_from_db()
        answer.refresh_from_db()
        self.assertEqual((question.status, answer.status), ('archived', 'archived'))
        self.assertEqual(question.revisions.count(), 1)
        self.assertEqual(answer.revisions.count(), 1)

    def test_contextual_room_block_requires_current_post_visibility_and_room_access(self):
        from local_rooms.models import Room, RoomMembership, RoomPost
        room = Room.objects.create(title='Private room', description='Public discovery text', state=self.state, lga=self.lga, visibility='members')
        post = RoomPost.objects.create(room=room, author=self.other, body='Private discussion text')
        self.client.force_login(self.author)
        url = reverse('community:block')
        data = {'target_type': 'room_post', 'target_id': post.pk}
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertFalse(CommunityBlock.objects.exists())
        RoomMembership.objects.create(room=room, user=self.author, status='approved', approved_at=timezone.now())
        RoomPost.objects.filter(pk=post.pk).update(status='hidden')
        self.assertEqual(self.client.post(url, data).status_code, 404)
        RoomPost.objects.filter(pk=post.pk).update(status='visible')
        self.assertRedirects(self.client.post(url, data), reverse('community:blocks'))
        self.assertTrue(CommunityBlock.objects.filter(user=self.author, blocked_user=self.other).exists())
        self.assertNotContains(self.client.get(reverse('local_rooms:room_detail', args=[room.pk])), post.body)
