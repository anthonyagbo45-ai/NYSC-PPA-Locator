"""Authorization, private projections and explicit member choices at HTTP boundaries."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from service_resources.models import CDSGroup
from .models import Room, RoomMembership, RoomPost, RoomPostRevision, RoomReport


class RoomViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Rooms State')
        cls.lga = LGA.objects.create(name='Rooms LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other Rooms State')
        cls.other_lga = LGA.objects.create(name='Other Rooms LGA', state=cls.other_state)
        cls.member = get_user_model().objects.create_user(state_code='RM/26A/0011', email='private-room-member@example.com', full_name='PRIVATE FULL MEMBER NAME', display_name='Room neighbour', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='RM/26A/0012', email='private-room-other@example.com', full_name='PRIVATE OTHER MEMBER NAME', display_name='Another neighbour', email_verified=True, is_staff=True)
        cls.unverified = get_user_model().objects.create_user(state_code='RM/26A/0013', email='unverified-room@example.com', full_name='Unverified room member')
        cls.ppa = PPA.objects.create(name='Rooms PPA', state=cls.state, lga=cls.lga, address='Public PPA address')

    def setUp(self):
        cache.clear()

    def room(self, title='Local discussion', **changes):
        values = dict(title=title, description='Public discovery context.', guidelines='Be considerate. Share no personal contacts.', state=self.state, lga=self.lga, visibility='members', moderator_note='PRIVATE ROOM EVIDENCE')
        values.update(changes)
        return Room.objects.create(**values)

    def membership(self, room, user=None, status='approved', **changes):
        values = dict(room=room, user=user or self.member, status=status, moderator_note='PRIVATE ADMISSION EVIDENCE')
        if status == 'approved':
            values['approved_at'] = timezone.now()
        values.update(changes)
        return RoomMembership.objects.create(**values)

    def post(self, room, author=None, **changes):
        values = dict(room=room, author=author or self.member, body='MEMBER ONLY DISCUSSION TEXT', moderator_note='PRIVATE POST EVIDENCE')
        values.update(changes)
        return RoomPost.objects.create(**values)

    def post_data(self, **changes):
        values = {'body': 'A useful local discussion.', 'source_url': 'https://example.com/public', 'privacy_confirmed': 'on'}
        values.update(changes)
        return values

    def url(self, name, obj=None):
        return reverse('local_rooms:' + name, args=[obj.pk] if obj else [])

    def assert_private_identity_absent(self, response):
        for value in [self.member.state_code, self.member.email, self.member.full_name, self.other.state_code, self.other.email, self.other.full_name, 'PRIVATE ROOM EVIDENCE', 'PRIVATE ADMISSION EVIDENCE', 'PRIVATE POST EVIDENCE']:
            self.assertNotContains(response, value)

    def test_discovery_is_free_safe_and_includes_only_public_room_metadata(self):
        private = self.room()
        self.post(private)
        self.room('Paused discussion', status='paused')
        self.room('ARCHIVED ROOM TITLE', status='archived')
        response = self.client.get(self.url('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, private.title)
        self.assertContains(response, 'Paused discussion')
        self.assertNotContains(response, 'ARCHIVED ROOM TITLE')
        self.assertNotContains(response, 'MEMBER ONLY DISCUSSION TEXT')
        self.assert_private_identity_absent(response)
        self.assertIsInstance(response.context['rooms'].object_list[0], dict)
        self.assertIsInstance(response.context['page_obj'].paginator.object_list[0], dict)

    def test_private_room_detail_excludes_posts_until_explicit_approval_even_staff(self):
        room = self.room()
        post = self.post(room)
        for user in [None, self.member, self.other]:
            if user:
                self.client.force_login(user)
            response = self.client.get(self.url('room_detail', room))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, post.body)
            self.assertIsNone(response.context['posts'])
            self.assertFalse(response.context['room']['can_read'])
            self.assert_private_identity_absent(response)
            self.client.logout()
        self.membership(room)
        self.client.force_login(self.member)
        response = self.client.get(self.url('room_detail', room))
        self.assertContains(response, post.body)
        self.assertContains(response, self.member.display_name)
        self.assert_private_identity_absent(response)
        self.assertIsInstance(response.context['posts'].paginator.object_list[0], dict)

    def test_guest_public_reads_but_posting_requires_verified_approved_member(self):
        room = self.room(visibility='public')
        post = self.post(room)
        self.assertContains(self.client.get(self.url('room_detail', room)), post.body)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.url('post_add', room), self.post_data()).status_code, 403)
        self.membership(room)
        response = self.client.post(self.url('post_add', room), self.post_data(author=self.other.pk, status='hidden', moderator_note='FORGED MODERATION'))
        self.assertRedirects(response, self.url('room_detail', room))
        new = RoomPost.objects.get(body='A useful local discussion.')
        self.assertEqual(new.author_id, self.member.pk)
        self.assertEqual(new.status, 'visible')
        self.assertEqual(new.moderator_note, '')
        self.client.force_login(self.unverified)
        self.membership(room, self.unverified)
        self.assertRedirects(self.client.post(self.url('post_add', room), self.post_data()), reverse('email_verification'))

    def test_join_is_a_request_only_and_get_does_not_create_membership(self):
        room = self.room(ppa=self.ppa)
        self.member.state_posted = self.state
        self.member.lga_posted = self.lga
        self.member.save(update_fields=['state_posted', 'lga_posted'])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url('request_join', room)).status_code, 200)
        self.assertFalse(RoomMembership.objects.exists())
        response = self.client.post(self.url('request_join', room), {'confirmed': 'on', 'status': 'approved', 'user': self.other.pk, 'notifications_enabled': 'on', 'reviewed_by': self.other.pk})
        self.assertRedirects(response, self.url('room_detail', room))
        membership = RoomMembership.objects.get(room=room, user=self.member)
        self.assertEqual(membership.status, 'requested')
        self.assertFalse(membership.notifications_enabled)
        self.assertIsNone(membership.reviewed_by_id)
        self.assertFalse(RoomMembership.objects.filter(user=self.other).exists())

    def test_leave_cancels_or_revokes_private_access_and_notifications_immediately(self):
        room = self.room()
        membership = self.membership(room, notifications_enabled=True, notifications_enabled_at=timezone.now())
        post = self.post(room)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url('leave', room)).status_code, 200)
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'approved')
        response = self.client.post(self.url('leave', room), {'confirmed': 'on'})
        self.assertRedirects(response, self.url('my_rooms'))
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'left')
        self.assertFalse(membership.notifications_enabled)
        self.assertNotContains(self.client.get(self.url('room_detail', room)), post.body)
        for route in ['post_edit', 'post_history', 'post_archive', 'report_post']:
            self.assertEqual(self.client.get(self.url(route, post)).status_code, 404)
        requested_room = self.room('Requested room')
        requested = self.membership(requested_room, status='requested')
        self.client.post(self.url('leave', requested_room), {'confirmed': 'on'})
        requested.refresh_from_db()
        self.assertEqual(requested.status, 'left')

    def test_ban_does_not_allow_join_resubmission_or_private_read(self):
        room = self.room()
        membership = self.membership(room, status='banned')
        post = self.post(room)
        self.client.force_login(self.member)
        response = self.client.get(self.url('room_detail', room))
        self.assertContains(response, 'Participation is restricted')
        self.assertNotContains(response, post.body)
        self.assertNotContains(response, 'href="' + self.url('request_join', room) + '"')
        response = self.client.post(self.url('request_join', room), {'confirmed': 'on'})
        self.assertIn(response.status_code, [400, 403])
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'banned')

    def test_paused_and_archived_rooms_have_honest_states(self):
        room = self.room('Pause then archive room', status='paused', visibility='public')
        self.membership(room)
        post = self.post(room)
        self.assertNotContains(self.client.get(self.url('room_detail', room)), 'Sign in to request membership')
        self.client.force_login(self.member)
        response = self.client.get(self.url('room_detail', room))
        self.assertContains(response, 'This room is paused')
        self.assertContains(response, post.body)
        self.assertEqual(self.client.post(self.url('post_add', room), self.post_data()).status_code, 403)
        self.assertIn(self.client.post(self.url('request_join', room), {'confirmed': 'on'}).status_code, [400, 403])
        Room.objects.filter(pk=room.pk).update(status='archived')
        self.assertEqual(self.client.get(self.url('room_detail', room)).status_code, 404)
        self.assertNotContains(self.client.get(self.url('home')), room.title)

    def test_member_preferences_require_owner_and_explicit_opt_in(self):
        room = self.room()
        membership = self.membership(room)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url('preferences', room)).status_code, 404)
        self.assertEqual(self.client.post(self.url('leave', room), {'confirmed': 'on', 'user': self.member.pk}).status_code, 404)
        self.client.force_login(self.member)
        response = self.client.get(self.url('preferences', room))
        self.assertContains(response, 'Off by default')
        membership.refresh_from_db()
        self.assertFalse(membership.notifications_enabled)
        self.assertRedirects(self.client.post(self.url('preferences', room), {'notifications_enabled': 'on', 'user': self.other.pk}), self.url('my_rooms'))
        membership.refresh_from_db()
        self.assertTrue(membership.notifications_enabled)
        self.assertIsNotNone(membership.notifications_enabled_at)
        self.client.post(self.url('preferences', room), {})
        membership.refresh_from_db()
        self.assertFalse(membership.notifications_enabled)

    def test_post_owner_idor_and_hidden_edits_cannot_restore_visibility(self):
        room = self.room()
        self.membership(room)
        self.membership(room, self.other)
        post = self.post(room, status='hidden')
        self.client.force_login(self.other)
        for route in ['post_edit', 'post_history', 'post_archive']:
            self.assertEqual(self.client.get(self.url(route, post)).status_code, 404)
        self.client.force_login(self.member)
        response = self.client.post(self.url('post_edit', post), self.post_data(body='Updated hidden post', expected_version=post.version, status='visible', author=self.other.pk))
        self.assertRedirects(response, self.url('room_detail', room))
        post.refresh_from_db()
        self.assertEqual(post.status, 'hidden')
        self.assertEqual(post.author_id, self.member.pk)
        history = self.client.get(self.url('post_history', post))
        self.assertEqual(history.status_code, 200)
        self.assert_private_identity_absent(history)
        self.assertNotContains(self.client.get(self.url('room_detail', room)), 'Updated hidden post')

    def test_report_and_report_history_are_private_without_target_content_or_evidence(self):
        room = self.room()
        self.membership(room)
        post = self.post(room)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url('report_post', post)).status_code, 404)
        self.client.force_login(self.member)
        response = self.client.post(self.url('report_post', post), {'category': 'unsafe', 'details': 'PRIVATE REPORT DETAILS', 'reporter': self.other.pk, 'status': 'resolved', 'moderator_note': 'FORGED REVIEW'})
        self.assertRedirects(response, self.url('my_reports'))
        report = RoomReport.objects.get()
        self.assertEqual(report.reporter_id, self.member.pk)
        self.assertEqual(report.status, 'open')
        self.assertEqual(report.moderator_note, '')
        RoomReport.objects.filter(pk=report.pk).update(moderator_note='PRIVATE REPORT INVESTIGATION')
        response = self.client.get(self.url('my_reports'))
        self.assertContains(response, 'PRIVATE REPORT DETAILS')
        self.assertNotContains(response, post.body)
        self.assertNotContains(response, 'PRIVATE REPORT INVESTIGATION')
        self.assertIsInstance(response.context['reports'].paginator.object_list[0], dict)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.url('my_reports')), 'PRIVATE REPORT DETAILS')

    def test_my_memberships_reveal_only_self_status_and_no_member_directory(self):
        room = self.room()
        self.membership(room)
        self.membership(room, self.other, status='requested')
        self.client.force_login(self.member)
        response = self.client.get(self.url('my_rooms'))
        self.assertContains(response, room.title)
        self.assert_private_identity_absent(response)
        self.assertEqual(len(response.context['memberships']), 1)
        self.assertIsInstance(response.context['memberships'].paginator.object_list[0], dict)

    def test_private_or_expired_group_title_never_leaks_through_room_context_or_filters(self):
        group = CDSGroup.objects.create(title='PRIVATE CDS RESOURCE TITLE', description='Private resource body', state=self.state, lga=self.lga, author=self.member, observed_on=timezone.localdate(), publication_status='draft')
        room = self.room(group=group)
        for route in [self.url('home'), self.url('room_detail', room)]:
            self.assertNotContains(self.client.get(route), group.title)
        response = self.client.get(self.url('home'), {'group': group.pk, 'state': self.state.pk, 'lga': self.lga.pk})
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, group.title, status_code=400)
        CDSGroup.objects.filter(pk=group.pk).update(publication_status='approved', expires_at=timezone.now() - timedelta(days=1))
        self.assertNotContains(self.client.get(self.url('room_detail', room)), group.title)

    def test_blocked_posts_are_filtered_for_signed_in_reader_but_public_guest_reads_remain(self):
        from community.models import CommunityBlock
        room = self.room(visibility='public')
        post = self.post(room)
        CommunityBlock.objects.create(user=self.member, blocked_user=self.other)
        self.client.force_login(self.other)
        response = self.client.get(self.url('room_detail', room))
        self.assertNotContains(response, post.body)
        self.assertContains(response, 'Public discussion remains available to visitors')
        self.assertEqual(self.client.get(self.url('report_post', post)).status_code, 404)
        self.client.logout()
        self.assertContains(self.client.get(self.url('room_detail', room)), post.body)

    def test_filters_reject_forged_cross_state_ids_and_filter_current_context(self):
        room = self.room(ppa=self.ppa)
        response = self.client.get(self.url('home'), {'ppa': self.ppa.pk})
        self.assertContains(response, room.title)
        for values in [{'state': self.other_state.pk, 'lga': self.lga.pk}, {'state': self.state.pk, 'lga': self.lga.pk, 'ppa': '999999'}, {'visibility': 'secret'}, {'q': 'x' * 121}]:
            self.assertEqual(self.client.get(self.url('home'), values).status_code, 400)

    def test_post_consent_safe_urls_and_version_required(self):
        room = self.room()
        self.membership(room)
        self.client.force_login(self.member)
        for changes in [{'privacy_confirmed': ''}, {'source_url': 'https://user:password@example.com/private'}, {'source_url': 'https://example.com/?access_token=secret'}]:
            self.assertEqual(self.client.post(self.url('post_add', room), self.post_data(**changes)).status_code, 400)
        self.assertFalse(RoomPost.objects.exists())
        post = self.post(room)
        self.assertEqual(self.client.post(self.url('post_edit', post), self.post_data()).status_code, 400)
        post.refresh_from_db()
        self.assertEqual(post.body, 'MEMBER ONLY DISCUSSION TEXT')

    def test_legacy_unsafe_urls_are_not_rendered_as_post_or_history_links(self):
        room = self.room(visibility='public')
        self.membership(room)
        post = self.post(room, source_url='javascript:alert(document.cookie)')
        RoomPostRevision.objects.create(post=post, actor=self.member, snapshot={'body': 'Earlier post', 'source_url': 'javascript:alert(document.cookie)', 'version': 1, 'status': 'visible'}, reason='Private audit note')
        self.assertNotContains(self.client.get(self.url('room_detail', room)), 'javascript:')
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.url('post_history', post)), 'javascript:')

    def test_archived_room_keeps_owner_leave_and_unsubscribe_available(self):
        room = self.room(status='archived')
        membership = self.membership(room, notifications_enabled=True, notifications_enabled_at=timezone.now())
        self.client.force_login(self.member)
        self.assertContains(self.client.get(self.url('preferences', room)), 'notifications are paused')
        self.assertRedirects(self.client.post(self.url('preferences', room), {}), self.url('my_rooms'))
        membership.refresh_from_db()
        self.assertFalse(membership.notifications_enabled)
        self.assertEqual(self.client.get(self.url('leave', room)).status_code, 200)
        self.assertRedirects(self.client.post(self.url('leave', room), {'confirmed': 'on'}), self.url('my_rooms'))
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'left')

    def test_all_mutations_require_csrf_and_explicit_confirmation(self):
        room = self.room()
        self.membership(room)
        post = self.post(room)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.member)
        for name, item in [('request_join', room), ('leave', room), ('preferences', room), ('post_add', room), ('post_edit', post), ('post_archive', post), ('report_room', room), ('report_post', post)]:
            self.assertEqual(csrf_client.post(self.url(name, item), {'confirmed': 'on'}).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.url('leave', room), {}).status_code, 400)
        self.assertEqual(self.client.post(self.url('post_archive', post), {}).status_code, 400)
        post.refresh_from_db()
        self.assertEqual(post.status, 'visible')

    def test_pagination_preserves_filters_and_has_only_safe_rows(self):
        room = self.room(visibility='public')
        RoomPost.objects.bulk_create([RoomPost(room=room, author=self.member, body=f'Public post {index}') for index in range(25)])
        response = self.client.get(self.url('room_detail', room), {'page': '2', 'topic': 'local life'})
        self.assertEqual(len(response.context['posts']), 5)
        self.assertContains(response, 'topic=local+life')
        self.assertIsInstance(response.context['page_obj'].paginator.object_list[0], dict)
        self.assertNotIn('author', response.context['page_obj'].object_list[0])
        Room.objects.bulk_create([Room(title=f'Filtered room {index}', description='Public area context', state=self.state, lga=self.lga) for index in range(25)])
        response = self.client.get(self.url('home'), {'state': self.state.pk, 'lga': self.lga.pk, 'q': 'Filtered', 'page': 2})
        self.assertEqual(len(response.context['rooms']), 5)
        self.assertContains(response, f'state={self.state.pk}')
        self.assertContains(response, 'q=Filtered')

    def test_discovery_and_discussion_queries_are_bounded(self):
        room = self.room(visibility='public')
        Room.objects.bulk_create([Room(title=f'Bounded room {index}', description='Public context', state=self.state, lga=self.lga) for index in range(25)])
        RoomPost.objects.bulk_create([RoomPost(room=room, author=self.member, body=f'Bounded post {index}') for index in range(25)])
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url('home'))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 20)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url('room_detail', room))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 20)
        Room.objects.filter(pk=room.pk).update(visibility='members')
        self.membership(room)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url('room_detail', room))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 20)
