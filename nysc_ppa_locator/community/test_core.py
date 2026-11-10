"""Privacy, visibility and serialized community mutations."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from service_resources.models import CDSGroup
from .models import (
    Answer, AnswerRevision, CommunityBlock, CommunityReport, HelpfulReaction,
    Question, QuestionFollow, QuestionRevision, ReportRevision, validate_public_url,
)
from .services import (
    archive_answer, archive_question, get_public_answer, get_public_question,
    interaction_blocked, moderate_answer, moderate_question, moderate_report,
    owner_report_revisions, owner_reports, owner_revisions, public_answers,
    public_questions, safe_owner_answer, safe_owner_report, safe_public_answer,
    safe_public_question, save_answer, save_question, save_report, set_block,
    set_follow, set_helpful, set_question_resolved,
)


class CommunityCoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Community state')
        cls.lga = LGA.objects.create(name='Community LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Different community state')
        cls.other_lga = LGA.objects.create(name='Other community LGA', state=cls.other_state)
        users = get_user_model().objects
        cls.author = users.create_user(state_code='QA/PRIVATE/AUTHOR', email='author-qa@example.com', full_name='PRIVATE legal author name', display_name='Local contributor', email_verified=True)
        cls.other = users.create_user(state_code='QA/PRIVATE/OTHER', email='other-qa@example.com', full_name='PRIVATE other legal name', email_verified=True)
        cls.moderator = users.create_user(state_code='QA/PRIVATE/MODERATOR', email='mod-qa@example.com', is_staff=True)
        cls.report_mod = users.create_user(state_code='QA/PRIVATE/REPORTER', email='reportmod-qa@example.com', is_staff=True)
        cls.moderator.user_permissions.add(Permission.objects.get(content_type__app_label='community', codename='can_moderate_community'))
        cls.report_mod.user_permissions.add(Permission.objects.get(content_type__app_label='community', codename='can_moderate_community_reports'))

    def data(self, **changes):
        values = dict(title='How can I find transport?', body='Suggestions for public transport in this area?', category='transport', state=self.state, lga=self.lga, ppa=None, group=None)
        values.update(changes)
        return values

    def question(self, **changes):
        return save_question(user=self.author, cleaned_data=self.data(**changes))

    def answer(self, question=None, user=None, **changes):
        values = dict(body='Ask at the public transport park.', source_url='https://example.com/public-guide')
        values.update(changes)
        return save_answer(user=user or self.other, question=question or self.question(), cleaned_data=values)

    def test_public_posting_rechecks_verified_and_active_account_under_lock(self):
        stale = get_user_model().objects.get(pk=self.author.pk)
        get_user_model().objects.filter(pk=self.author.pk).update(email_verified=False)
        with self.assertRaises(PermissionDenied):
            save_question(user=stale, cleaned_data=self.data())
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(QuestionRevision.objects.count(), 0)
        get_user_model().objects.filter(pk=self.author.pk).update(email_verified=True, is_active=False)
        with self.assertRaises(PermissionDenied):
            save_question(user=stale, cleaned_data=self.data())
        with self.assertRaises(PermissionDenied):
            save_question(user=AnonymousUser(), cleaned_data=self.data())

    def test_member_field_allowlist_and_stale_versions_are_atomic(self):
        question = self.question(author=self.other, status='hidden', version=999, resolved=True)
        self.assertEqual((question.author_id, question.status, question.version, question.resolved), (self.author.pk, 'visible', 1, False))
        updated = save_question(user=self.author, question=question, expected_version=1, cleaned_data=self.data(body='Updated public text.', author=self.other, status='hidden'))
        self.assertEqual((updated.author_id, updated.status, updated.version), (self.author.pk, 'visible', 2))
        with self.assertRaises(ValidationError):
            save_question(user=self.author, question=question, expected_version=1, cleaned_data=self.data(body='Stale overwrite'))
        updated.refresh_from_db()
        self.assertEqual(updated.body, 'Updated public text.')
        self.assertEqual(updated.revisions.count(), 2)

    def test_foreign_ownership_and_answer_reparenting_are_rejected(self):
        question = self.question()
        other_question = self.question(title='Other public question')
        answer = self.answer(question)
        for operation in [
            lambda: save_question(user=self.other, question=question, cleaned_data=self.data()),
            lambda: archive_question(user=self.other, question=question),
            lambda: set_question_resolved(user=self.other, question=question, resolved=True),
            lambda: save_answer(user=self.author, question=question, answer=answer, cleaned_data={'body':'Foreign overwrite'}),
            lambda: save_answer(user=self.other, question=other_question, answer=answer, cleaned_data={'body':'Reparent attempt'}),
            lambda: archive_answer(user=self.author, answer=answer),
        ]:
            with self.assertRaises(PermissionDenied):
                operation()
        answer.refresh_from_db()
        self.assertEqual(answer.question_id, question.pk)
        self.assertEqual(answer.version, 1)

    def test_hidden_member_edits_never_restore_public_content(self):
        question = self.question()
        answer = self.answer(question)
        question = moderate_question(question=question, actor=self.moderator, status='hidden', reason='PRIVATE moderation evidence')
        answer = moderate_answer(answer=answer, actor=self.moderator, status='hidden', reason='PRIVATE answer evidence')
        question = save_question(user=self.author, question=question, cleaned_data=self.data(body='Author correction', status='visible'))
        answer = save_answer(user=self.other, question=question, answer=answer, cleaned_data={'body':'Answer correction','status':'visible'})
        self.assertEqual((question.status, answer.status), ('hidden', 'hidden'))
        self.assertFalse(public_questions().exists())
        self.assertFalse(public_answers(question).exists())
        for getter, pk in [(get_public_question, question.pk), (get_public_answer, answer.pk)]:
            with self.assertRaises(Http404):
                getter(pk)
        self.assertEqual(safe_owner_answer(answer, user=self.other)['body'], 'Answer correction')

    def test_parent_visibility_applies_to_answers_and_interactions(self):
        question = self.question()
        answer = self.answer(question)
        moderate_question(question=question, actor=self.moderator, status='hidden', reason='Parent under review')
        for operation in [
            lambda: get_public_answer(answer.pk),
            lambda: self.answer(question, body='Additional answer'),
            lambda: set_helpful(user=self.author, answer=answer),
            lambda: set_follow(user=self.other, question=question),
            lambda: save_report(user=self.author, answer=answer, category='spam', details='Report parent-hidden answer'),
        ]:
            with self.assertRaises(Http404):
                operation()

    def test_author_archive_preserves_other_answers_and_cannot_restore(self):
        question = self.question()
        answer = self.answer(question)
        archive_question(user=self.author, question=question)
        self.assertTrue(Answer.objects.filter(pk=answer.pk).exists())
        self.assertFalse(public_answers(question).exists())
        with self.assertRaises(ProtectedError):
            Question.objects.get(pk=question.pk).delete()
        with self.assertRaises(ValidationError):
            save_question(user=self.author, question=question, cleaned_data=self.data())
        with self.assertRaises(ValidationError):
            moderate_question(question=question, actor=self.moderator, status='visible', reason='Attempt restore')

    def test_deleting_account_retains_contributions_and_revision_history(self):
        question = self.question()
        self.author.delete()
        question.refresh_from_db()
        self.assertIsNone(question.author_id)
        self.assertEqual(question.revisions.count(), 1)
        self.assertIsNone(question.revisions.get().actor_id)

    def test_helpful_is_idempotent_unique_and_cannot_be_self_given(self):
        answer = self.answer()
        for _ in range(2):
            set_helpful(user=self.author, answer=answer)
        self.assertEqual(HelpfulReaction.objects.count(), 1)
        with self.assertRaises(ValidationError):
            set_helpful(user=self.other, answer=answer)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                HelpfulReaction.objects.create(user=self.author, answer=answer)
        self.assertEqual(get_public_answer(answer.pk).helpful_count, 1)
        set_helpful(user=self.author, answer=answer, helpful=False)
        self.assertFalse(HelpfulReaction.objects.exists())

    def test_blocks_are_private_bidirectional_and_prevent_interactions(self):
        question = self.question()
        answer = self.answer(question)
        block = set_block(user=self.author, blocked_user=self.other)
        self.assertTrue(interaction_blocked(self.other, self.author.pk))
        self.assertTrue(interaction_blocked(self.author.pk, self.other.pk))
        self.assertEqual(set_block(user=self.author, blocked_user=self.other).pk, block.pk)
        for operation in [
            lambda: self.answer(question),
            lambda: set_helpful(user=self.author, answer=answer),
            lambda: set_follow(user=self.other, question=question),
        ]:
            with self.assertRaises(PermissionDenied):
                operation()
        # Blocking never prevents a safety report about public content.
        save_report(user=self.author, answer=answer, category='abuse', details='Please review this interaction.')
        set_block(user=self.other, blocked_user=self.author, blocked=False)
        self.assertTrue(interaction_blocked(self.author, self.other))
        set_block(user=self.author, blocked_user=self.other, blocked=False)
        self.assertFalse(interaction_blocked(self.author, self.other))
        with self.assertRaises(ValidationError):
            set_block(user=self.author, blocked_user=self.author)

    def test_follow_notifications_are_explicit_and_optin_timestamp_is_stable(self):
        question = self.question()
        follow = set_follow(user=self.other, question=question)
        self.assertFalse(follow.enabled)
        self.assertIsNone(follow.enabled_at)
        follow = set_follow(user=self.other, question=question, enabled=True)
        first_enabled = follow.enabled_at
        self.assertIsNotNone(first_enabled)
        follow = set_follow(user=self.other, question=question, enabled=True)
        self.assertEqual(follow.enabled_at, first_enabled)
        follow = set_follow(user=self.other, question=question, enabled=False)
        self.assertIsNone(follow.enabled_at)
        set_follow(user=self.other, question=question, following=False)
        self.assertFalse(QuestionFollow.objects.exists())

    def test_geographic_and_current_public_source_validation(self):
        with self.assertRaises(ValidationError):
            self.question(lga=self.other_lga)
        ppa = PPA.objects.create(name='Different PPA', state=self.other_state, lga=self.other_lga, address='Public address')
        with self.assertRaises(ValidationError):
            self.question(ppa=ppa)
        group = CDSGroup.objects.create(title='Private CDS title', description='Group context', state=self.state, lga=self.lga, observed_on=timezone.localdate(), publication_status='draft')
        with self.assertRaises(ValidationError):
            self.question(group=group)
        group.publication_status = 'approved'
        group.expires_at = timezone.now()+timedelta(days=30)
        group.save()
        question = self.question(group=group)
        group.publication_status = 'draft'
        group.title = 'PRIVATE changed draft title'
        group.save()
        projected = safe_public_question(get_public_question(question.pk))
        self.assertEqual((projected['group_id'], projected['group_title']), (None, ''))
        self.assertNotIn('PRIVATE', json.dumps(projected, default=str))

    def test_source_urls_are_http_only_without_credentials_controls_or_fetches(self):
        for source in ['javascript:alert(1)', 'https://user:pass@example.com/a', 'https://example.com/a\nsecret', 'https://example.com:invalid/a']:
            with self.assertRaises(ValidationError):
                validate_public_url(source)
        question = self.question()
        with patch('urllib.request.urlopen', side_effect=AssertionError('Sources must never be fetched')):
            answer = self.answer(question)
        Answer.objects.filter(pk=answer.pk).update(source_url='javascript:alert(1)')
        self.assertEqual(safe_public_answer(get_public_answer(answer.pk))['source_url'], '')

    def test_public_projections_never_include_private_identity_or_history(self):
        question = self.question()
        answer = self.answer(question)
        projection = json.dumps([safe_public_question(get_public_question(question.pk)), safe_public_answer(get_public_answer(answer.pk))], default=str)
        self.assertIn('Local contributor', projection)
        for secret in ['QA/PRIVATE', 'PRIVATE legal', 'author-qa@example.com', 'author_id', 'state_code', 'email', 'snapshot']:
            self.assertNotIn(secret, projection)

    def test_owner_histories_exclude_moderation_evidence_and_other_owners(self):
        question = self.question()
        question = moderate_question(question=question, actor=self.moderator, status='hidden', reason='PRIVATE moderator evidence')
        history = list(owner_revisions(user=self.author, question=question))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['snapshot']['status'], 'hidden')
        serialized = json.dumps(history, default=str)
        for secret in ['PRIVATE moderator evidence', 'actor', 'reason', 'author_id', 'moderated_by']:
            self.assertNotIn(secret, serialized)
        with self.assertRaises(PermissionDenied):
            owner_revisions(user=self.other, question=question)
        audit = QuestionRevision.objects.first()
        audit.reason = 'Overwrite'
        with self.assertRaises(ValidationError):
            audit.save()
        with self.assertRaises(ValidationError):
            QuestionRevision.objects.filter(pk=audit.pk).update(reason='Bulk overwrite')
        with self.assertRaises(ValidationError):
            QuestionRevision.objects.filter(pk=audit.pk).delete()
        with self.assertRaises(ValidationError):
            QuestionRevision.objects.bulk_update([audit], ['reason'])

    def test_reports_have_one_explicit_target_and_scoped_private_owner_history(self):
        question = self.question()
        answer = self.answer(question)
        for target in [{}, {'question':question, 'answer':answer}]:
            with self.assertRaises(ValidationError):
                save_report(user=self.other, category='privacy', details='Please check', **target)
        report = save_report(user=self.other, answer=answer, category='privacy', details='My report explanation', private_evidence='My private evidence')
        with self.assertRaises(PermissionDenied):
            moderate_report(report=report, actor=self.moderator, status='resolved', moderator_note='Wrong permission')
        report = moderate_report(report=report, actor=self.report_mod, status='investigating', moderator_note='PRIVATE reviewer evidence')
        projected = safe_owner_report(report, user=self.other)
        self.assertEqual(projected['private_evidence'], 'My private evidence')
        self.assertNotIn('PRIVATE reviewer', json.dumps(projected, default=str))
        self.assertEqual(list(owner_reports(self.other)), [report])
        history = list(owner_report_revisions(report=report, user=self.other))
        self.assertEqual(history[0]['new_status'], 'investigating')
        self.assertNotIn('moderator_note', json.dumps(history, default=str))
        with self.assertRaises(PermissionDenied):
            safe_owner_report(report, user=self.author)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                CommunityReport.objects.create(reporter=self.other, question=question, answer=answer, category='spam', details='Invalid combined target')
        with self.assertRaises(ProtectedError):
            report.delete()

    def test_rapid_and_daily_limits_preserve_prior_records(self):
        for index in range(5):
            self.question(title=f'Independent question {index}')
        with self.assertRaises(ValidationError):
            self.question(title='Rapid extra question')
        self.assertEqual(Question.objects.count(), 5)
        Question.objects.update(created_at=timezone.now()-timedelta(hours=1))
        for index in range(15):
            self.question(title=f'Additional independent question {index}')
            Question.objects.update(created_at=timezone.now()-timedelta(hours=1))
        with self.assertRaises(ValidationError):
            self.question(title='Daily extra question')
        self.assertEqual(Question.objects.count(), 20)
        self.assertEqual(QuestionRevision.objects.count(), 20)

    def test_list_projections_use_bounded_queries_with_counts(self):
        questions = Question.objects.bulk_create([Question(author=self.author, title=f'Question {index}', body='Community context', category='other', state=self.state, lga=self.lga) for index in range(25)])
        answers = Answer.objects.bulk_create([Answer(question=question, author=self.other, body='Public answer') for question in questions])
        HelpfulReaction.objects.bulk_create([HelpfulReaction(user=self.author, answer=answer) for answer in answers])
        with self.assertNumQueries(1):
            projected = [safe_public_question(question) for question in public_questions()[:20]]
        self.assertEqual(len(projected), 20)
        self.assertTrue(all(row['answer_count'] == 1 and row['helpful_count'] == 1 for row in projected))
        with self.assertNumQueries(1):
            answer_rows = [safe_public_answer(answer) for answer in public_answers(questions[0])[:20]]
        self.assertEqual(answer_rows[0]['helpful_count'], 1)

    def test_answer_activity_is_atomic_with_creation_and_never_reemitted_on_edit(self):
        question = self.question()
        with patch('notifications.community_services.record_answer_activity', side_effect=RuntimeError('Activity failure')):
            with self.assertRaises(RuntimeError):
                self.answer(question)
        self.assertEqual(Answer.objects.count(), 0)
        self.assertEqual(AnswerRevision.objects.count(), 0)
        with patch('notifications.community_services.record_answer_activity') as record:
            answer = self.answer(question)
            save_answer(user=self.other, question=question, answer=answer, cleaned_data={'body':'Correction'})
        record.assert_called_once()


class ConcurrentCommunityTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_first_duplicate_question_creation_is_serialized_by_member_row(self):
        state = State.objects.create(name='Concurrent community state')
        lga = LGA.objects.create(name='Concurrent community LGA', state=state)
        user = get_user_model().objects.create_user(state_code='QA/CONCURRENT', email='concurrent-qa@example.com', email_verified=True)
        ready = Barrier(2)

        def create():
            close_old_connections()
            try:
                member = get_user_model().objects.get(pk=user.pk)
                ready.wait(timeout=10)
                try:
                    question = save_question(user=member, cleaned_data=dict(title='Concurrent question', body='Public context', category='other', state=state, lga=lga))
                    return ('saved', question.pk)
                except ValidationError:
                    return ('duplicate', None)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(result[0] for result in outcomes), ['duplicate', 'saved'])
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(QuestionRevision.objects.count(), 1)
