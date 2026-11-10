from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from service_resources.models import CDSGroup
from .models import (Room, RoomMembership, RoomMembershipRevision, RoomPost,
                     RoomPostRevision, RoomReport, RoomReportRevision, RoomRevision)
from .services import moderate_membership, moderate_report, request_membership, save_room, submit_report
from .test_core import RoomFixtures


class LocalRoomAdminTests(RoomFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_fixtures()
        users = get_user_model().objects
        cls.manager = users.create_user(state_code='ROOM/MANAGER', is_staff=True)
        cls.membership_mod = users.create_user(state_code='ROOM/MEMBERMOD', is_staff=True)
        cls.post_mod = users.create_user(state_code='ROOM/POSTMOD', is_staff=True)
        cls.report_mod = users.create_user(state_code='ROOM/REPORTMOD', is_staff=True)
        for user, code in ((cls.manager, 'can_manage_rooms'), (cls.membership_mod, 'can_moderate_room_memberships'), (cls.post_mod, 'can_moderate_room_posts'), (cls.report_mod, 'can_moderate_room_reports')):
            user.user_permissions.add(Permission.objects.get(content_type__app_label='local_rooms', codename=code))

    def setUp(self):
        cache.clear()

    def client_for(self, user, *, otp=True):
        client = Client()
        client.force_login(user)
        if otp:
            device = TOTPDevice.objects.create(user=user, confirmed=True)
            session = client.session
            session['otp_device_id'] = device.persistent_id
            session.save()
        return client

    def url(self, model, action='changelist', obj=None):
        return reverse(f'admin:local_rooms_{model._meta.model_name}_{action}', args=[obj.pk] if obj else [])

    def room_payload(self, **changes):
        return dict(title=self.room.title, description=self.room.description, guidelines=self.room.guidelines,
                    state=self.state.pk, lga=self.lga.pk, ppa='', group='', visibility=self.room.visibility,
                    status=self.room.status, moderator_note='', moderation_reason='Reviewed public descriptive metadata.',
                    expected_version=self.room.version, _save='Save', **changes)

    def test_otp_required_for_every_room_moderation_queue(self):
        membership = self.approve()
        post = self.post()
        report = submit_report(user=self.user, post=post, category='unsafe', details='Private report')
        client = self.client_for(self.moderator, otp=False)
        for model, obj in ((Room, self.room), (RoomMembership, membership), (RoomPost, post), (RoomReport, report)):
            response = client.get(self.url(model, 'change', obj))
            self.assertEqual(response.status_code, 302)
            self.assertIn('/admin/login/', response.url)
            self.assertNotIn(post.body, response.content.decode())

    def test_generic_staff_permissions_do_not_open_any_room_data_or_history(self):
        client = self.client_for(self.staff)
        for model in (Room, RoomMembership, RoomPost, RoomReport, RoomRevision, RoomMembershipRevision, RoomPostRevision, RoomReportRevision):
            self.assertEqual(client.get(self.url(model)).status_code, 403)

    def test_scoped_roles_cannot_read_other_moderation_desks(self):
        allowed = {self.manager: (Room, RoomRevision), self.membership_mod: (RoomMembership, RoomMembershipRevision), self.post_mod: (RoomPost, RoomPostRevision), self.report_mod: (RoomReport, RoomReportRevision)}
        for user, visible in allowed.items():
            client = self.client_for(user)
            for model in (Room, RoomMembership, RoomPost, RoomReport, RoomRevision, RoomMembershipRevision, RoomPostRevision, RoomReportRevision):
                self.assertEqual(client.get(self.url(model)).status_code, 200 if model in visible else 403, f'{user.pk} {model.__name__}')

    def test_report_only_admin_never_gets_private_target_body_or_source_group_title(self):
        self.approve()
        post = self.post(body='SECRET PRIVATE DISCUSSION BODY')
        report = submit_report(user=self.user, post=post, category='unsafe', details='Information needs review')
        group = CDSGroup.objects.create(title='SECRET PRIVATE SOURCE TITLE', description='Private source draft', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate())
        Room.objects.filter(pk=self.room.pk).update(group=group)
        client = self.client_for(self.report_mod)
        for url in (self.url(RoomReport), self.url(RoomReport, 'change', report)):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            for secret in (post.body, group.title, self.user.email, self.user.state_code, self.user.full_name):
                self.assertNotContains(response, secret)
        self.assertEqual(client.get(self.url(RoomPost, 'change', post)).status_code, 403)

    def test_room_admin_group_choices_and_labels_never_expose_private_sources(self):
        group = CDSGroup.objects.create(title='SECRET PRIVATE SOURCE TITLE', description='Private source draft', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate())
        Room.objects.filter(pk=self.room.pk).update(group=group)
        response = self.client_for(self.manager).get(self.url(Room, 'change', self.room))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, group.title)
        self.assertNotContains(response, self.user.state_code)

    def test_membership_and_post_admin_show_only_safe_member_labels(self):
        membership = self.approve()
        post = self.post()
        for model, obj, user in ((RoomMembership, membership, self.membership_mod), (RoomPost, post, self.post_mod)):
            response = self.client_for(user).get(self.url(model, 'change', obj))
            self.assertEqual(response.status_code, 200)
            for secret in (self.user.state_code, self.user.email, self.user.full_name):
                self.assertNotContains(response, secret)

    def test_room_admin_post_uses_audited_service_and_requires_reason(self):
        client = self.client_for(self.manager)
        url = self.url(Room, 'change', self.room)
        payload = self.room_payload()
        payload.update(title='Reviewed updated public title', moderation_reason=' ')
        self.assertEqual(client.post(url, payload).status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.version, 1)
        payload['moderation_reason'] = 'Title reviewed for public room discovery.'
        self.assertEqual(client.post(url, payload).status_code, 302)
        self.room.refresh_from_db()
        self.assertEqual(self.room.title, 'Reviewed updated public title')
        self.assertEqual(self.room.version, 2)
        self.assertEqual(self.room.revisions.count(), 2)
        self.assertEqual(self.room.revisions.first().actor_id, self.manager.pk)

    def test_stale_room_admin_form_cannot_overwrite_a_concurrent_moderator_change(self):
        payload = self.room_payload()
        payload['title'] = 'Stale browser title'
        save_room(actor=self.moderator, room=self.room, values={'title': 'Newer moderator title'}, reason='Reviewed newer title.')
        response = self.client_for(self.manager).post(self.url(Room, 'change', self.room), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'changed since you opened it')
        self.room.refresh_from_db()
        self.assertEqual(self.room.title, 'Newer moderator title')
        self.assertEqual(self.room.revisions.count(), 2)

    def test_membership_decisions_use_service_and_cannot_forge_user_or_notifications(self):
        membership = request_membership(room=self.room, user=self.user)
        payload = dict(status='approved', moderation_reason='Explicit request reviewed.', user=self.other.pk, notifications_enabled='on', _save='Save')
        response = self.client_for(self.membership_mod).post(self.url(RoomMembership, 'change', membership), payload)
        self.assertEqual(response.status_code, 302)
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'approved')
        self.assertEqual(membership.user_id, self.user.pk)
        self.assertFalse(membership.notifications_enabled)
        self.assertEqual(membership.revisions.count(), 2)

    def test_report_decisions_are_audited_and_private_note_does_not_become_post_text(self):
        report = submit_report(user=self.user, room=self.room, category='other', details='Private concern')
        client = self.client_for(self.report_mod)
        response = client.post(self.url(RoomReport, 'change', report), dict(status='resolved', moderator_note='Private reviewed outcome', _save='Save'))
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, 'resolved')
        self.assertEqual(report.revisions.count(), 1)
        self.assertEqual(report.revisions.first().actor_id, self.report_mod.pk)

    def test_admin_never_adds_memberships_posts_reports_or_edits_or_deletes_audits(self):
        request = RequestFactory().get('/admin/')
        request.user = self.moderator
        for model in (RoomMembership, RoomPost, RoomReport, RoomRevision, RoomMembershipRevision, RoomPostRevision, RoomReportRevision):
            desk = admin.site._registry[model]
            self.assertFalse(desk.has_add_permission(request))
            self.assertFalse(desk.has_delete_permission(request))
        for model in (RoomRevision, RoomMembershipRevision, RoomPostRevision, RoomReportRevision):
            self.assertFalse(admin.site._registry[model].has_change_permission(request))
        self.assertTrue(admin.site._registry[Room].has_add_permission(request))
