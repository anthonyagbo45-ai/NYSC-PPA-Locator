"""Moderation permissions and OTP remain separate from account administration."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from locations.models import LGA, State
from service_resources.models import CDSGroup
from .admin import QuestionModerationForm
from .models import (
    Answer, AnswerRevision, CommunityBlock, CommunityReport, HelpfulReaction,
    Question, QuestionFollow, QuestionRevision, ReportRevision,
)


class CommunityAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Community Admin State')
        cls.lga = LGA.objects.create(name='Community Admin LGA', state=cls.state)
        users = get_user_model().objects
        cls.author = users.create_user(state_code='QA/PRIVATE/OWNER', email='qa-owner@example.com', email_verified=True)
        cls.moderator = users.create_user(state_code='QA/PRIVATE/MOD', email='qa-mod@example.com', is_staff=True)
        cls.report_moderator = users.create_user(state_code='QA/PRIVATE/REPORT', email='qa-report@example.com', is_staff=True)
        cls.support = users.create_user(state_code='QA/PRIVATE/SUPPORT', email='qa-support@example.com', is_staff=True)
        cls.supervisor = users.create_superuser(state_code='QA/PRIVATE/SUPER', email='qa-super@example.com')
        permission = lambda code: Permission.objects.get(content_type__app_label='community', codename=code)
        cls.moderator.user_permissions.add(permission('can_moderate_community'))
        cls.report_moderator.user_permissions.add(permission('can_moderate_community_reports'))
        cls.support.user_permissions.add(permission('view_question'), permission('change_question'), permission('view_communityreport'))

    def question(self, **changes):
        values = dict(author=self.author, title='Community question', body='Community experience requested.',
                      category='other', state=self.state, lga=self.lga)
        values.update(changes)
        return Question.objects.create(**values)

    def answer(self, question=None, **changes):
        values = dict(question=question or self.question(), author=self.author, body='Community suggestion only.')
        values.update(changes)
        return Answer.objects.create(**values)

    def client_for(self, user, *, otp=True):
        client = Client()
        client.force_login(user)
        if otp:
            device = TOTPDevice.objects.create(user=user, confirmed=True)
            session = client.session
            session['otp_device_id'] = device.persistent_id
            session.save()
        return client

    def request_for(self, user):
        request = RequestFactory().post('/admin/')
        request.user = user
        return request

    def test_password_only_session_cannot_enter_community_admin(self):
        response = self.client_for(self.moderator, otp=False).get(reverse('admin:community_question_changelist'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_generic_model_permissions_do_not_grant_community_or_report_review(self):
        question = self.question()
        client = self.client_for(self.support)
        for url in [reverse('admin:community_question_changelist'),
                    reverse('admin:community_question_change', args=[question.pk]),
                    reverse('admin:community_communityreport_changelist')]:
            self.assertEqual(client.get(url).status_code, 403)

    def test_content_and_report_permissions_do_not_cross_scopes(self):
        question = self.question()
        report = CommunityReport.objects.create(question=question, reporter=self.author, category='privacy', details='Review this concern.', private_evidence='PRIVATE report evidence')
        content_client = self.client_for(self.moderator)
        self.assertEqual(content_client.get(reverse('admin:community_communityreport_change', args=[report.pk])).status_code, 403)
        self.assertEqual(content_client.get(reverse('admin:community_reportrevision_changelist')).status_code, 403)
        report_client = self.client_for(self.report_moderator)
        self.assertEqual(report_client.get(reverse('admin:community_question_change', args=[question.pk])).status_code, 403)
        self.assertEqual(report_client.get(reverse('admin:community_questionrevision_changelist')).status_code, 403)
        response = report_client.get(reverse('admin:community_communityreport_change', args=[report.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PRIVATE report evidence')

    def test_report_admin_uses_generic_target_and_does_not_expose_content(self):
        question = self.question(title='PRIVATE hidden question', body='PRIVATE hidden text', status='hidden')
        report = CommunityReport.objects.create(question=question, reporter=self.author, category='privacy', details='Report details')
        client = self.client_for(self.report_moderator)
        for url in [reverse('admin:community_communityreport_changelist'),
                    reverse('admin:community_communityreport_change', args=[report.pk])]:
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'Question #{question.pk}')
            self.assertNotContains(response, 'PRIVATE hidden question')
            self.assertNotContains(response, 'PRIVATE hidden text')

    def test_private_preferences_have_no_admin_registration_even_for_superuser(self):
        for model in (CommunityBlock, QuestionFollow, HelpfulReaction):
            self.assertNotIn(model, admin.site._registry)

    def test_admins_cannot_create_delete_or_mutate_audit_history(self):
        question = self.question()
        answer = self.answer(question)
        question_revision = QuestionRevision.objects.create(question=question, actor=self.author, snapshot={'body': 'Earlier public question'}, reason='Author edit')
        answer_revision = AnswerRevision.objects.create(answer=answer, actor=self.author, snapshot={'body': 'Earlier public answer'}, reason='Author edit')
        report = CommunityReport.objects.create(question=question, reporter=self.author, category='other', details='Concern')
        report_revision = ReportRevision.objects.create(report=report, actor=self.report_moderator, previous_status='open', new_status='investigating', moderator_note='Review started.')
        request = self.request_for(self.supervisor)
        for model, record in ((Question, question), (Answer, answer), (CommunityReport, report),
                              (QuestionRevision, question_revision), (AnswerRevision, answer_revision), (ReportRevision, report_revision)):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request, record))
        for model, record in ((QuestionRevision, question_revision), (AnswerRevision, answer_revision), (ReportRevision, report_revision)):
            self.assertFalse(admin.site._registry[model].has_change_permission(request, record))

    def test_admin_content_save_uses_locked_service_and_ignores_forged_body(self):
        question = self.question()
        old_title, old_body = question.title, question.body
        question.title, question.body, question.status = 'Forged title', 'Forged body', 'hidden'
        form = type('SavedForm', (), {'cleaned_data': {'moderation_reason': 'Private information removed.'}})()
        admin.site._registry[Question].save_model(self.request_for(self.moderator), question, form, change=True)
        question.refresh_from_db()
        self.assertEqual((question.title, question.body), (old_title, old_body))
        self.assertEqual(question.status, 'hidden')
        self.assertEqual(question.revisions.count(), 1)
        self.assertEqual(question.revisions.get().reason, 'Private information removed.')
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Question].save_model(self.request_for(self.moderator), question, form, change=False)

    def test_real_admin_post_hides_answer_and_records_reason(self):
        answer = self.answer()
        response = self.client_for(self.moderator).post(reverse('admin:community_answer_change', args=[answer.pk]), {
            'status': 'hidden', 'moderation_reason': 'Harassment concern checked.', '_save': 'Save',
            'body': 'Forged replacement ignored',
        })
        self.assertEqual(response.status_code, 302)
        answer.refresh_from_db()
        self.assertEqual(answer.status, 'hidden')
        self.assertEqual(answer.body, 'Community suggestion only.')
        self.assertEqual(answer.revisions.get().reason, 'Harassment concern checked.')

    def test_real_report_post_preserves_report_and_appends_private_audit(self):
        report = CommunityReport.objects.create(question=self.question(), reporter=self.author, category='privacy', details='Original concern', private_evidence='Original evidence')
        response = self.client_for(self.report_moderator).post(reverse('admin:community_communityreport_change', args=[report.pk]), {
            'status': 'investigating', 'moderator_note': 'Relevant public evidence is being checked.',
            'details': 'Forged report replacement', 'private_evidence': 'Forged evidence', '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, 'investigating')
        self.assertEqual(report.details, 'Original concern')
        self.assertEqual(report.private_evidence, 'Original evidence')
        self.assertEqual(report.revisions.get().moderator_note, 'Relevant public evidence is being checked.')

    def test_moderation_form_requires_a_real_reason(self):
        form = QuestionModerationForm(data={'status': 'hidden', 'moderation_reason': '   '}, instance=self.question())
        self.assertFalse(form.is_valid())
        self.assertIn('moderation_reason', form.errors)

    def test_archiving_belongs_to_author_workflow(self):
        question = self.question(status='archived')
        model_admin = admin.site._registry[Question]
        request = self.request_for(self.moderator)
        self.assertTrue(model_admin.has_view_permission(request, question))
        self.assertFalse(model_admin.has_change_permission(request, question))
        form = QuestionModerationForm(
            data={'status': 'archived', 'moderation_reason': 'Attempted moderator archive'},
            instance=self.question(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn('status', form.errors)

    def test_moderation_can_hide_question_after_its_source_becomes_private(self):
        group = CDSGroup.objects.create(
            title='Originally public group', description='Public community group',
            author=self.author, state=self.state, lga=self.lga,
            observed_on=timezone.localdate(), publication_status='approved', shared_for_review=True,
        )
        question = self.question(group=group)
        CDSGroup.objects.filter(pk=group.pk).update(
            publication_status='draft', shared_for_review=False, title='PRIVATE later source title',
        )
        client = self.client_for(self.moderator)
        url = reverse('admin:community_question_change', args=[question.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'PRIVATE later source title')
        response = client.post(url, {
            'status': 'hidden', 'moderation_reason': 'Source is no longer publicly available.', '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.status, 'hidden')
        self.assertEqual(question.revisions.get().reason, 'Source is no longer publicly available.')
