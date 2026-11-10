from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from locations.models import State, LGA
from .admin import CDSGroupAdmin, GroupModerationForm, ServiceNoticeAdmin
from .models import CDSGroup, GroupRevision, NoticeRevision, ResourceReport, ServiceNotice
from .services import archive_group, moderate_group, moderate_notice, save_group, save_notice, save_report


class ResourceAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state=State.objects.create(name='Resource Admin State')
        cls.lga=LGA.objects.create(name='Resource Admin LGA',state=cls.state)
        users=get_user_model().objects
        cls.author=users.create_user(state_code='RES/OWNER',email='res-owner@example.com',email_verified=True)
        cls.moderator=users.create_user(state_code='RES/MOD',email='res-mod@example.com',is_staff=True)
        cls.reporter_mod=users.create_user(state_code='RES/REPORT',email='res-report@example.com',is_staff=True)
        cls.support=users.create_user(state_code='RES/SUPPORT',email='res-support@example.com',is_staff=True)
        cls.supervisor=users.create_superuser(state_code='RES/SUPERVISOR',email='res-supervisor@example.com')
        permission=lambda code:Permission.objects.get(content_type__app_label='service_resources',codename=code)
        cls.moderator.user_permissions.add(permission('can_moderate_resources'))
        cls.reporter_mod.user_permissions.add(permission('can_moderate_resource_reports'))
        cls.support.user_permissions.add(permission('view_cdsgroup'),permission('change_cdsgroup'),permission('view_servicenotice'))

    def client_for(self,user):
        client=Client();client.force_login(user)
        device=TOTPDevice.objects.create(user=user,confirmed=True)
        session=client.session;session['otp_device_id']=device.persistent_id;session.save()
        return client

    def data(self,**changes):
        values=dict(title='Public CDS context',description='Community description.',venue_hint='Public hall',public_contact_url='',state=self.state,lga=self.lga,source_type='member',source_url='',observed_on=timezone.localdate())
        values.update(changes);return values

    def group(self,*,submit=True,**changes):
        return save_group(user=self.author,cleaned_data=self.data(**changes),submit=submit)

    def notice(self,*,submit=True,**changes):
        values=dict(kind='clearance',title='Public clearance context',body='Confirm with the designated official.',state=self.state,lga=self.lga,source_type='member',source_url='',observed_on=timezone.localdate(),group=None)
        values.update(changes)
        return save_notice(user=self.author,cleaned_data=values,submit=submit)

    def test_otp_admin_hides_private_group_and_notice_drafts(self):
        group=self.group(submit=False,title='SECRET private group')
        notice=self.notice(submit=False,title='SECRET private notice')
        client=self.client_for(self.moderator)
        for model,record,title in [('cdsgroup',group,'SECRET private group'),('servicenotice',notice,'SECRET private notice')]:
            response=client.get(reverse(f'admin:service_resources_{model}_changelist'))
            self.assertEqual(response.status_code,200);self.assertNotContains(response,title)
            detail=client.get(reverse(f'admin:service_resources_{model}_change',args=[record.pk]))
            self.assertIn(detail.status_code,(302,403,404))
        self.assertEqual(GroupRevision.objects.count(),1);self.assertEqual(NoticeRevision.objects.count(),1)

    def test_private_revisions_remain_hidden_after_submission(self):
        draft=self.group(submit=False,description='SECRET earlier private text')
        original=draft.revisions.get()
        draft=save_group(user=self.author,group=draft,cleaned_data=self.data(description='Submitted public context'),submit=True)
        client=self.client_for(self.moderator)
        response=client.get(reverse('admin:service_resources_grouprevision_changelist'))
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'SECRET earlier private text')
        response=client.get(reverse('admin:service_resources_grouprevision_change',args=[original.pk]))
        self.assertIn(response.status_code,(302,403,404))
        self.assertEqual(draft.revisions.count(),2)

    def test_archiving_never_submitted_draft_does_not_open_admin_access(self):
        group=self.group(submit=False,title='SECRET archived draft')
        archive_group(group=group,user=self.author)
        client=self.client_for(self.moderator)
        response=client.get(reverse('admin:service_resources_cdsgroup_changelist'))
        self.assertNotContains(response,'SECRET archived draft')
        self.assertEqual(group.revisions.count(),2)

    def test_superuser_can_deliberately_read_private_audit(self):
        group=self.group(submit=False,title='SECRET privileged only')
        client=self.client_for(self.supervisor)
        response=client.get(reverse('admin:service_resources_cdsgroup_change',args=[group.pk]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'SECRET privileged only')

    def test_password_only_session_cannot_enter_resource_admin(self):
        client=Client();client.force_login(self.moderator)
        response=client.get(reverse('admin:service_resources_cdsgroup_changelist'))
        self.assertEqual(response.status_code,302)
        self.assertIn('/admin/login/',response.url)

    def test_generic_model_permissions_do_not_bypass_custom_moderation(self):
        client=self.client_for(self.support)
        group=self.group()
        self.assertEqual(client.get(reverse('admin:service_resources_cdsgroup_changelist')).status_code,403)
        self.assertEqual(client.get(reverse('admin:service_resources_cdsgroup_change',args=[group.pk])).status_code,403)

    def test_report_only_reviewer_cannot_read_later_private_target_title(self):
        group=self.group()
        group=moderate_group(group=group,actor=self.moderator,values={'publication_status':'approved'},reason='Suitable community context.')
        report=save_report(user=self.author,resource=group,category='wrong_info',details='Public-source concern')
        save_group(user=self.author,group=group,cleaned_data=self.data(title='SECRET later title'),submit=False)
        client=self.client_for(self.reporter_mod)
        for url in [reverse('admin:service_resources_resourcereport_changelist'),reverse('admin:service_resources_resourcereport_change',args=[report.pk])]:
            response=client.get(url);self.assertEqual(response.status_code,200);self.assertNotContains(response,'SECRET later title')
        response=client.get(reverse('admin:service_resources_resourcereport_changelist'),{'q':'SECRET'})
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'SECRET later title')
        self.assertEqual(response.context['cl'].result_count,0)

    def test_model_admins_cannot_create_delete_or_mutate_audit_records(self):
        group=self.group();request=RequestFactory().get('/admin/');request.user=self.supervisor
        for model,record in [(CDSGroup,group),(GroupRevision,group.revisions.get())]:
            model_admin=admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request,record))
        self.assertFalse(admin.site._registry[GroupRevision].has_change_permission(request,group.revisions.get()))

    def test_admin_save_uses_services_and_ignores_mutated_identity(self):
        group=self.group();old_title=group.title
        group.title='Forged inline title';group.publication_status='approved'
        request=RequestFactory().post('/admin/');request.user=self.moderator
        form=type('SavedForm',(),{'cleaned_data':{'moderation_reason':'Reviewed source and context.'}})()
        admin.site._registry[CDSGroup].save_model(request,group,form,change=True)
        saved=CDSGroup.objects.get(pk=group.pk)
        self.assertEqual(saved.title,old_title)
        self.assertEqual(saved.publication_status,'approved')
        self.assertEqual(saved.verification_status,'community')
        self.assertEqual(saved.revisions.count(),2)

    def test_real_admin_post_publishes_community_notice_with_audit(self):
        notice=self.notice()
        client=self.client_for(self.moderator)
        response=client.post(reverse('admin:service_resources_servicenotice_change',args=[notice.pk]),{
            'source_type':'member','source_url':'','publication_status':'approved','verification_status':'community',
            'checked_at_0':'','checked_at_1':'','expires_at_0':notice.expires_at.strftime('%Y-%m-%d'),
            'expires_at_1':notice.expires_at.strftime('%H:%M:%S'),'moderator_note':'Reviewed public context.',
            'moderation_reason':'Community report reviewed; not an official clearance approval.','_save':'Save'})
        self.assertEqual(response.status_code,302)
        notice.refresh_from_db();self.assertEqual(notice.publication_status,'approved')
        self.assertEqual(notice.revisions.count(),2);self.assertEqual(notice.verification_status,'community')
