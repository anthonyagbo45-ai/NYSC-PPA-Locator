"""Private-message reviewers receive reported evidence, never an inbox browser."""
from io import StringIO

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.management.commands.bootstrap_roles import MESSENGER_REVIEW_GROUP
from .admin import MessageReportForm
from .models import Conversation, Message, MessageReport, MessengerPreference, ReportDecision
from .services import moderate_report


class MessageReportAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user(
            state_code='MSG/PRIVATE/AUTHOR', email='private-message-author@example.com',
            full_name='PRIVATE legal sender name', display_name='Public sender', email_verified=True,
        )
        cls.recipient = users.create_user(
            state_code='MSG/PRIVATE/RECIP', email='private-message-recipient@example.com',
            full_name='PRIVATE legal recipient name', display_name='Public recipient', email_verified=True,
        )
        cls.reviewer = users.create_user(
            state_code='MSG/PRIVATE/REVIEW', email='private-message-reviewer@example.com', is_staff=True,
        )
        cls.moderator = users.create_user(
            state_code='MSG/PRIVATE/MOD', email='private-message-moderator@example.com', is_staff=True,
        )
        cls.support = users.create_user(
            state_code='MSG/PRIVATE/SUPPORT', email='private-message-support@example.com', is_staff=True,
        )
        cls.supervisor = users.create_superuser(
            state_code='MSG/PRIVATE/SUPER', email='private-message-supervisor@example.com',
        )
        cls.reviewer.user_permissions.add(Permission.objects.get(
            content_type__app_label='messenger', codename='can_review_message_reports',
        ))
        cls.moderator.user_permissions.add(Permission.objects.get(
            content_type__app_label='community', codename='can_moderate_community_reports',
        ))
        low, high = sorted((cls.author.pk, cls.recipient.pk))
        cls.conversation = Conversation.objects.create(
            low_member_id=low, high_member_id=high, initiated_by=cls.author, status='accepted',
        )
        cls.selected = Message.objects.create(
            conversation=cls.conversation, sender=cls.author,
            body='<script>UNSAFE reported message</script>',
        )
        cls.unreported = Message.objects.create(
            conversation=cls.conversation, sender=cls.recipient, body='PRIVATE unreported neighbouring message',
        )
        cls.report = MessageReport.objects.create(
            message=cls.selected, reporter=cls.recipient, category='abuse', details='A participant chose this message.',
            evidence={'body': cls.selected.body, 'sent_at': cls.selected.sent_at.isoformat(), 'sender_name': 'Public sender'},
        )

    def request_for(self, user):
        request = RequestFactory().post('/admin/')
        request.user = user
        return request

    def client_for(self, user, *, otp=True):
        client = Client()
        client.force_login(user)
        if otp:
            device = TOTPDevice.objects.create(user=user, confirmed=True)
            session = client.session
            session['otp_device_id'] = device.persistent_id
            session.save()
        return client

    def test_password_only_reviewer_must_enroll_and_verify_otp_for_admin(self):
        response = self.client_for(self.reviewer, otp=False).get(reverse('admin:messenger_messagereport_changelist'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_public_report_permissions_and_staff_status_do_not_open_private_evidence(self):
        report_admin = admin.site._registry[MessageReport]
        for user in (self.author, self.support, self.moderator):
            request = self.request_for(user)
            self.assertFalse(report_admin.has_module_permission(request))
            self.assertFalse(report_admin.has_view_permission(request, self.report))
            self.assertFalse(report_admin.has_change_permission(request, self.report))
            self.assertFalse(report_admin.get_queryset(request).exists())
        for user in (self.support, self.moderator):
            client = self.client_for(user)
            for url in (reverse('admin:messenger_messagereport_changelist'),
                        reverse('admin:messenger_messagereport_change', args=[self.report.pk]),
                        reverse('admin:messenger_reportdecision_changelist')):
                self.assertEqual(client.get(url).status_code, 403)

    def test_report_reviewer_sees_only_selected_escaped_evidence_and_no_private_identities(self):
        response = self.client_for(self.reviewer).get(reverse('admin:messenger_messagereport_change', args=[self.report.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&lt;script&gt;UNSAFE reported message&lt;/script&gt;')
        self.assertNotContains(response, '<script>UNSAFE reported message</script>')
        self.assertContains(response, 'Public sender')
        for hidden in (self.unreported.body, self.author.state_code, self.recipient.state_code,
                       self.author.email, self.recipient.email, self.author.full_name,
                       self.recipient.full_name, str(self.conversation.pk)):
            self.assertNotContains(response, hidden)
        self.assertNotContains(response, 'id_message')
        self.assertNotContains(response, 'id_reporter')

    def test_private_conversations_messages_and_preferences_have_no_admin_even_for_superuser(self):
        for model in (Conversation, Message, MessengerPreference):
            self.assertNotIn(model, admin.site._registry)
            self.assertFalse(Permission.objects.filter(content_type__app_label='messenger', content_type__model=model._meta.model_name).exists())
        report_admin = admin.site._registry[MessageReport]
        for user in (self.reviewer, self.supervisor):
            request = self.request_for(user)
            self.assertFalse(report_admin.has_add_permission(request))
            self.assertFalse(report_admin.has_delete_permission(request, self.report))

    def test_moderation_post_ignores_forged_evidence_reporter_and_message(self):
        original = (self.report.message_id, self.report.reporter_id, self.report.details, dict(self.report.evidence))
        response = self.client_for(self.reviewer).post(reverse('admin:messenger_messagereport_change', args=[self.report.pk]), {
            'status': 'investigating', 'moderator_note': 'Selected reported evidence is being reviewed.',
            'evidence': '{"body":"Forged replacement"}', 'details': 'Forged details',
            'message': self.unreported.pk, 'reporter': self.author.pk, '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'investigating')
        self.assertEqual((self.report.message_id, self.report.reporter_id, self.report.details, self.report.evidence), original)
        decision = self.report.decisions.get()
        self.assertEqual((decision.previous_status, decision.new_status), ('open', 'investigating'))
        self.assertEqual(decision.actor_id, self.reviewer.pk)
        self.assertEqual(decision.moderator_note, 'Selected reported evidence is being reviewed.')

    def test_admin_service_rechecks_permission_and_uses_persisted_report(self):
        report_admin = admin.site._registry[MessageReport]
        self.report.evidence = {'body': 'Forged replacement'}
        self.report.details = 'Forged replacement'
        self.report.status = 'resolved'
        form = type('SavedForm', (), {'cleaned_data': {'moderator_note': 'Reviewed the selected evidence.'}})()
        report_admin.save_model(self.request_for(self.reviewer), self.report, form, change=True)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'resolved')
        self.assertEqual(self.report.evidence['body'], self.selected.body)
        self.assertEqual(self.report.details, 'A participant chose this message.')
        self.reviewer.user_permissions.clear()
        with self.assertRaises(PermissionDenied):
            report_admin.save_model(self.request_for(self.reviewer), self.report, form, change=True)
        with self.assertRaises(PermissionDenied):
            report_admin.save_model(self.request_for(self.supervisor), self.report, form, change=False)

    def test_reason_and_valid_outcome_are_required(self):
        for data, field in (({'status': 'resolved', 'moderator_note': '   '}, 'moderator_note'),
                            ({'status': 'invalid', 'moderator_note': 'A private outcome note.'}, 'status')):
            form = MessageReportForm(data=data, instance=self.report)
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)

    def test_inactive_reviewer_cannot_decide_even_with_stale_authenticated_instance(self):
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            moderate_report(actor=self.reviewer, report_id=self.report.pk, status='resolved', moderator_note='No longer authorized.')
        self.assertFalse(ReportDecision.objects.exists())
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'open')

    def test_report_evidence_and_decisions_cannot_be_edited_through_model_save(self):
        self.report.evidence = {'body': 'Forged replacement'}
        with self.assertRaises(ValidationError):
            self.report.save()
        self.report.refresh_from_db()
        moderate_report(actor=self.reviewer, report_id=self.report.pk, status='investigating', moderator_note='Reviewed reported text.')
        decision = self.report.decisions.get()
        decision.moderator_note = 'Changed history'
        with self.assertRaises(ValidationError):
            decision.save()
        with self.assertRaises(ValidationError):
            decision.delete()
        with self.assertRaises(ValidationError):
            ReportDecision.objects.filter(pk=decision.pk).update(moderator_note='Changed history')
        desk = admin.site._registry[ReportDecision]
        request = self.request_for(self.supervisor)
        self.assertFalse(desk.has_add_permission(request))
        self.assertFalse(desk.has_change_permission(request, decision))
        self.assertFalse(desk.has_delete_permission(request, decision))

    def test_bootstrap_creates_separate_minimal_role_without_assignments(self):
        before = dict(get_user_model().objects.values_list('pk', 'is_staff'))
        call_command('bootstrap_roles', stdout=StringIO())
        call_command('bootstrap_roles', stdout=StringIO())
        role = Group.objects.get(name=MESSENGER_REVIEW_GROUP)
        self.assertEqual(set(role.permissions.values_list('content_type__app_label', 'codename')),
                         {('messenger', 'can_review_message_reports')})
        self.assertFalse(role.user_set.exists())
        self.assertFalse(Group.objects.get(name='Moderator').permissions.filter(content_type__app_label='messenger').exists())
        self.assertEqual(dict(get_user_model().objects.values_list('pk', 'is_staff')), before)
        self.assertFalse(self.author.groups.exists())
        self.assertFalse(self.recipient.groups.exists())
