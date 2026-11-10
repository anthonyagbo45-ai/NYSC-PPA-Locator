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

from community.models import CommunityBlock
from locations.models import LGA, State
from ppas.models import PPA
from service_resources.models import CDSGroup
from .models import (Room, RoomMembership, RoomMembershipRevision, RoomPost,
                     RoomPostRevision, RoomReport, RoomRevision, validate_source_url)
from .services import (archive_post, can_post_room, can_read_room, get_room,
                       leave_room, moderate_membership, moderate_post, moderate_report,
                       my_memberships, my_reports, owner_post_revisions, public_rooms,
                       request_membership, room_posts, safe_owner_membership,
                       safe_owner_report, safe_public_post, safe_public_room,
                       save_post, save_room, set_membership_notifications, submit_report)


class RoomFixtures:
    @classmethod
    def build_fixtures(cls):
        cls.state = State.objects.create(name='Room state')
        cls.lga = LGA.objects.create(name='Room LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Different room state')
        cls.other_lga = LGA.objects.create(name='Different room LGA', state=cls.other_state)
        users = get_user_model().objects
        cls.user = users.create_user(state_code='ROOM/PRIVATE/001', full_name='PRIVATE FULL NAME', email='private-room@example.com', email_verified=True, display_name='Public participant')
        cls.other = users.create_user(state_code='ROOM/PRIVATE/002', full_name='OTHER PRIVATE FULL NAME', email='other-room@example.com', email_verified=True)
        cls.unverified = users.create_user(state_code='ROOM/PRIVATE/003', full_name='Unverified', email='unverified-room@example.com')
        cls.moderator = users.create_user(state_code='ROOM/MOD/001', full_name='Moderator', is_staff=True)
        cls.moderator.user_permissions.add(*Permission.objects.filter(content_type__app_label='local_rooms', codename__in=['can_manage_rooms', 'can_moderate_room_memberships', 'can_moderate_room_posts', 'can_moderate_room_reports']))
        cls.staff = users.create_user(state_code='ROOM/STAFF/001', full_name='Generic staff', is_staff=True)
        cls.staff.user_permissions.add(*Permission.objects.filter(content_type__app_label='local_rooms').exclude(codename__startswith='can_'))
        cls.ppa = PPA.objects.create(name='A local school', address='Public school location', state=cls.state, lga=cls.lga)
        cls.room = save_room(actor=cls.moderator, values=dict(title='Public discovery title', description='Public description of a members-only discussion.', guidelines='Respect privacy.', state=cls.state, lga=cls.lga), reason='Room scope and public descriptive metadata reviewed.')

    def approve(self, user=None, room=None):
        membership = request_membership(room=room or self.room, user=user or self.user)
        return moderate_membership(membership=membership, actor=self.moderator, status='approved', reason='Request reviewed for this community room, without official status claims.')

    def post(self, user=None, room=None, **values):
        return save_post(room=room or self.room, user=user or self.user, cleaned_data={'body': 'Discussion body that should stay private.', 'source_url': '', **values})


class LocalRoomCoreTests(RoomFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_fixtures()

    def test_only_explicit_room_permission_can_create_or_edit_metadata(self):
        for actor in (self.user, self.staff):
            with self.assertRaises(PermissionDenied):
                save_room(actor=actor, room=self.room, values={'title': 'Forged official room'}, reason='Attempt')
            with self.assertRaises(PermissionDenied):
                save_room(actor=actor, values={'title': 'New room'}, reason='Attempt')
        self.assertEqual(Room.objects.count(), 1)
        self.assertEqual(RoomRevision.objects.count(), 1)
        with self.assertRaises(ValidationError):
            save_room(actor=self.moderator, room=self.room, values={}, reason=' ')

    def test_geography_ppa_and_group_must_match_and_group_must_be_currently_public(self):
        for values in ({'lga': self.other_lga}, {'ppa': PPA.objects.create(name='Other school', address='Public', state=self.other_state, lga=self.other_lga)}):
            with self.assertRaises(ValidationError):
                save_room(actor=self.moderator, room=self.room, values=values, reason='Review mismatched source')
        group = CDSGroup.objects.create(title='PRIVATE SOURCE GROUP TITLE', description='Private draft', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate())
        with self.assertRaises(ValidationError):
            save_room(actor=self.moderator, room=self.room, values={'group': group}, reason='Review private resource')
        self.assertEqual(RoomRevision.objects.count(), 1)

    def test_private_discovery_contains_only_public_metadata_and_own_private_choice(self):
        membership = self.approve()
        membership.moderator_note = 'PRIVATE MEMBERSHIP REVIEW'
        membership.save(update_fields=['moderator_note'])
        row = safe_public_room(get_room(self.room.pk), user=self.other)
        encoded = json.dumps(row, default=str)
        for secret in ('PRIVATE MEMBERSHIP REVIEW', self.user.email, self.user.state_code, self.user.full_name, 'memberships', 'moderated_by'):
            self.assertNotIn(secret, encoded)
        self.assertFalse(row['can_read'])
        self.assertIsNone(row['my_membership'])
        self.assertFalse(my_memberships(self.other).exists())
        with self.assertRaises(PermissionDenied):
            safe_owner_membership(membership, user=self.other)

    def test_known_private_room_and_post_ids_do_not_bypass_approval(self):
        self.approve()
        post = self.post()
        for user in (AnonymousUser(), self.other, self.staff):
            self.assertFalse(can_read_room(self.room, user))
            with self.assertRaises(PermissionDenied):
                room_posts(room=self.room, user=user)
            with self.assertRaises(PermissionDenied):
                safe_public_post(post, user=user)
        self.assertTrue(can_read_room(self.room, self.moderator))
        self.assertFalse(can_post_room(self.room, self.moderator))

    def test_request_never_approves_or_optins_and_verification_is_rechecked(self):
        with self.assertRaises(PermissionDenied):
            request_membership(room=self.room, user=self.unverified)
        membership = request_membership(room=self.room, user=self.user)
        self.assertEqual(membership.status, 'requested')
        self.assertFalse(membership.notifications_enabled)
        self.assertIsNone(membership.notifications_enabled_at)
        self.assertFalse(can_read_room(self.room, self.user))
        self.assertEqual(request_membership(room=self.room, user=self.user).pk, membership.pk)
        self.assertEqual(membership.revisions.count(), 1)
        get_user_model().objects.filter(pk=self.other.pk).update(email_verified=False)
        with self.assertRaises(PermissionDenied):
            request_membership(room=self.room, user=self.other)

    def test_membership_approval_is_not_official_membership_or_source_preference(self):
        from service_resources.services import select_group
        group = CDSGroup.objects.create(title='CDS public resource', description='Public resource', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate(), publication_status='approved')
        select_group(user=self.user, group=group)
        self.assertFalse(RoomMembership.objects.filter(user=self.user).exists())
        membership = self.approve()
        self.assertFalse(membership.notifications_enabled)
        self.assertIsNotNone(membership.approved_at)
        self.assertTrue(can_post_room(self.room, self.user))
        self.assertEqual(self.user.state_posted_id, None)
        self.assertEqual(RoomMembership.objects.count(), 1)

    def test_notification_choice_requires_approved_owner_and_resets_on_leave_and_reapproval(self):
        with self.assertRaises(PermissionDenied):
            set_membership_notifications(room=self.room, user=self.other, enabled=True)
        membership = self.approve()
        membership = set_membership_notifications(room=self.room, user=self.user, enabled=True)
        self.assertTrue(membership.notifications_enabled)
        first_optin = membership.notifications_enabled_at
        self.assertIsNotNone(first_optin)
        leave_room(room=self.room, user=self.user)
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'left')
        self.assertFalse(membership.notifications_enabled)
        self.assertIsNone(membership.notifications_enabled_at)
        self.assertFalse(can_read_room(self.room, self.user))
        with self.assertRaises(ValidationError):
            moderate_membership(membership=membership, actor=self.moderator, status='approved', reason='Cannot join for another person')
        membership = self.approve()
        self.assertFalse(membership.notifications_enabled)
        self.assertIsNone(membership.notifications_enabled_at)

    def test_ban_requires_review_reason_and_cannot_be_cleared_by_leave_rejoin(self):
        membership = self.approve()
        for actor, reason in ((self.staff, 'Generic permission'), (self.moderator, ' ')):
            with self.assertRaises((PermissionDenied, ValidationError)):
                moderate_membership(membership=membership, actor=actor, status='banned', reason=reason)
        membership = moderate_membership(membership=membership, actor=self.moderator, status='banned', reason='Abuse reviewed privately.')
        leave_room(room=self.room, user=self.user)
        with self.assertRaises(PermissionDenied):
            request_membership(room=self.room, user=self.user)
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'banned')
        self.assertFalse(can_post_room(self.room, self.user))
        membership = moderate_membership(membership=membership, actor=self.moderator, status='approved', reason='Restriction explicitly reconsidered after review.')
        self.assertEqual(membership.status, 'approved')
        self.assertFalse(membership.notifications_enabled)

    def test_public_rooms_are_free_guest_reads_but_posting_needs_explicit_approval(self):
        room = save_room(actor=self.moderator, room=self.room, values={'visibility': 'public'}, reason='Public discussion scope reviewed.')
        self.approve(room=room)
        post = self.post(room=room)
        self.assertTrue(can_read_room(room, AnonymousUser()))
        self.assertEqual(safe_public_post(room_posts(room=room).get(pk=post.pk))['body'], post.body)
        with self.assertRaises(PermissionDenied):
            self.post(user=self.other, room=room)
        self.assertNotIn('author_id', safe_public_post(post))

    def test_member_post_whitelist_ignores_forged_author_status_and_room(self):
        self.approve()
        post = self.post(author=self.other, status='visible', moderator_note='Forged review', room_id=999)
        self.assertEqual(post.author_id, self.user.pk)
        self.assertEqual(post.room_id, self.room.pk)
        self.assertEqual(post.moderator_note, '')
        self.assertEqual(post.status, 'visible')
        self.assertEqual(post.revisions.count(), 1)

    def test_hidden_post_owner_edit_never_restores_it_or_exposes_moderator_evidence(self):
        self.approve()
        post = moderate_post(post=self.post(), actor=self.moderator, status='hidden', reason='PRIVATE MODERATOR EVIDENCE')
        post = save_post(room=self.room, user=self.user, post=post, cleaned_data={'body': 'Corrected content', 'status': 'visible'}, expected_version=post.version)
        self.assertEqual(post.status, 'hidden')
        self.assertFalse(room_posts(room=self.room, user=self.user).filter(pk=post.pk).exists())
        history = owner_post_revisions(post=post, user=self.user)
        self.assertNotIn('PRIVATE MODERATOR EVIDENCE', json.dumps(history, default=str))
        self.assertNotIn('moderated_by', json.dumps(history, default=str))
        self.assertIn('PRIVATE MODERATOR EVIDENCE', json.dumps(post.revisions.first().snapshot))
        with self.assertRaises(PermissionDenied):
            safe_public_post(post, user=self.user)

    def test_post_idor_version_conflicts_and_soft_archive_are_enforced(self):
        self.approve()
        self.approve(user=self.other)
        post = self.post()
        for action in (
            lambda: save_post(room=self.room, user=self.other, post=post, cleaned_data={'body': 'Stolen'}),
            lambda: archive_post(post=post, user=self.other),
            lambda: owner_post_revisions(post=post, user=self.other),
        ):
            with self.assertRaises(PermissionDenied):
                action()
        changed = save_post(room=self.room, user=self.user, post=post, cleaned_data={'body': 'Updated'}, expected_version=1)
        with self.assertRaises(ValidationError):
            save_post(room=self.room, user=self.user, post=post, cleaned_data={'body': 'Stale overwrite'}, expected_version=1)
        archived = archive_post(post=changed, user=self.user)
        self.assertEqual(archived.status, 'archived')
        self.assertEqual(RoomPost.objects.count(), 1)
        with self.assertRaises(ValidationError):
            moderate_post(post=archived, actor=self.moderator, status='visible', reason='Cannot restore withdrawal')

    def test_leaving_revokes_private_owner_history_and_edits(self):
        self.approve()
        post = self.post()
        leave_room(room=self.room, user=self.user)
        with self.assertRaises(PermissionDenied):
            owner_post_revisions(post=post, user=self.user)
        with self.assertRaises(PermissionDenied):
            save_post(room=self.room, user=self.user, post=post, cleaned_data={'body': 'Unavailable'})

    def test_paused_and_archived_room_gates_recheck_locked_current_room(self):
        membership = self.approve()
        set_membership_notifications(room=self.room, user=self.user, enabled=True)
        for status in ('paused', 'archived'):
            current = Room.objects.get(pk=self.room.pk)
            save_room(actor=self.moderator, room=current, values={'status': status}, reason='Participation paused for review.')
            with self.assertRaises(ValidationError):
                self.post()
            with self.assertRaises(ValidationError):
                request_membership(room=self.room, user=self.other)
        self.assertFalse(public_rooms().exists())
        with self.assertRaises(Http404):
            get_room(self.room.pk)
        membership = set_membership_notifications(room=self.room, user=self.user, enabled=False)
        self.assertFalse(membership.notifications_enabled)
        self.assertEqual(leave_room(room=self.room, user=self.user).status, 'left')

    def test_block_filters_signed_in_discussion_and_reports_in_both_directions(self):
        self.approve()
        self.approve(user=self.other)
        post = self.post()
        CommunityBlock.objects.create(user=self.other, blocked_user=self.user)
        self.assertFalse(room_posts(room=self.room, user=self.other).exists())
        with self.assertRaises(PermissionDenied):
            safe_public_post(post, user=self.other)
        with self.assertRaises(PermissionDenied):
            submit_report(user=self.other, post=post, category='abuse', details='Cannot use blocked interaction')
        public = save_room(actor=self.moderator, room=self.room, values={'visibility': 'public'}, reason='Public visibility chosen.')
        self.assertTrue(room_posts(room=public, user=AnonymousUser()).exists())

    def test_reports_exactly_one_target_private_ownership_and_audited_outcomes(self):
        self.approve()
        post = self.post()
        for values in ({}, {'room': self.room, 'post': post}):
            with self.assertRaises(ValidationError):
                submit_report(user=self.user, category='unsafe', details='Private report', **values)
        with self.assertRaises(PermissionDenied):
            submit_report(user=self.other, post=post, category='unsafe', details='Not accessible')
        report = submit_report(user=self.user, post=post, category='unsafe', details='PRIVATE REPORT DETAILS')
        self.assertEqual(my_reports(self.user).count(), 1)
        self.assertFalse(my_reports(self.other).exists())
        with self.assertRaises(PermissionDenied):
            safe_owner_report(report, user=self.other)
        with self.assertRaises(PermissionDenied):
            moderate_report(report=report, actor=self.staff, status='resolved', moderator_note='Generic staff permission')
        report = moderate_report(report=report, actor=self.moderator, status='resolved', moderator_note='PRIVATE STAFF OUTCOME')
        self.assertEqual(report.revisions.count(), 1)
        self.assertEqual(report.revisions.first().previous_status, 'open')
        self.assertNotIn('PRIVATE STAFF OUTCOME', json.dumps(safe_owner_report(report, user=self.user), default=str))

    def test_current_group_visibility_is_rechecked_without_private_title_or_link_leaks(self):
        group = CDSGroup.objects.create(title='Published group', description='Public', state=self.state, lga=self.lga, author=self.user, observed_on=timezone.localdate(), publication_status='approved')
        room = save_room(actor=self.moderator, room=self.room, values={'group': group}, reason='Current local source reviewed.')
        self.assertEqual(safe_public_room(room)['group_title'], 'Published group')
        CDSGroup.objects.filter(pk=group.pk).update(publication_status='draft', title='PRIVATE REPLACEMENT TITLE', public_contact_url='https://example.com/private-invite')
        row = safe_public_room(get_room(room.pk))
        self.assertFalse(row['group_available'])
        self.assertTrue(row['group_unavailable'])
        self.assertIsNone(row['group_id'])
        self.assertNotIn('PRIVATE REPLACEMENT TITLE', json.dumps(row, default=str))
        self.assertNotIn('private-invite', json.dumps(row, default=str))

    def test_audits_are_append_only_and_protect_original_records(self):
        membership = self.approve()
        post = self.post()
        for audit in (self.room.revisions.first(), membership.revisions.first(), post.revisions.first()):
            with self.assertRaises(ValidationError):
                audit.save()
            with self.assertRaises(ValidationError):
                audit.delete()
            with self.assertRaises(ValidationError):
                type(audit).objects.filter(pk=audit.pk).update(reason='Overwrite')
            with self.assertRaises(ValidationError):
                type(audit).objects.filter(pk=audit.pk).delete()
        with self.assertRaises(ProtectedError):
            post.delete()
        original = post.revisions.first()
        with self.assertRaises(ValidationError):
            RoomPostRevision(pk=original.pk, post=post, actor=self.user, snapshot={'body': 'Overwrite'}, reason='Forged').save()
        with self.assertRaises(ValidationError):
            RoomPostRevision.objects.bulk_create([RoomPostRevision(pk=original.pk, post=post, actor=self.user, snapshot={'body': 'Overwrite'}, reason='Forged')], update_conflicts=True, update_fields=['snapshot'], unique_fields=['id'])
        with self.assertRaises(ValidationError):
            RoomPostRevision.objects.bulk_create([RoomPostRevision(pk=original.pk, post=post, actor=self.user, snapshot={}, reason='Forged')], None, False, True, ['reason'], ['id'])

    def test_model_database_constraints_guard_membership_notifications_and_reports(self):
        membership = request_membership(room=self.room, user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoomMembership.objects.create(room=self.room, user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoomMembership.objects.filter(pk=membership.pk).update(notifications_enabled=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoomReport.objects.create(reporter=self.user, category='other', details='No target')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Room.objects.filter(pk=self.room.pk).update(visibility='forged')

    def test_public_sources_are_links_only_and_reject_credentials_and_private_hosts(self):
        for value in ('javascript:alert(1)', 'https://user:password@example.com/', 'http://127.0.0.1/admin', 'https://localhost/x', 'http://192.168.1.2/', 'https://example.com/?access_token=secret'):
            with self.assertRaises(ValidationError, msg=value):
                validate_source_url(value)
        validate_source_url('https://example.org/attributed-source')
        validate_source_url('')

    def test_post_and_report_allowances_are_checked_under_user_lock(self):
        self.approve()
        RoomPost.objects.bulk_create([RoomPost(room=self.room, author=self.user, body=f'Recent {n}') for n in range(5)])
        with self.assertRaises(ValidationError):
            self.post()
        RoomReport.objects.bulk_create([RoomReport(room=self.room, reporter=self.user, category='other', details='Private') for n in range(20)])
        with self.assertRaises(ValidationError):
            submit_report(user=self.user, room=self.room, category='other', details='Over quota')

    def test_private_post_projection_query_count_stays_constant_for_twenty_posts(self):
        self.approve()
        RoomPost.objects.bulk_create([RoomPost(room=self.room, author=self.user, body=f'Post {n}') for n in range(20)])
        with self.assertNumQueries(4):
            rows = [safe_public_post(post, user=self.user) for post in room_posts(room=self.room, user=self.user)]
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]['author_name'], self.user.public_name)

    def test_stale_public_room_cannot_certify_current_private_posts_for_guests(self):
        room = save_room(actor=self.moderator, room=self.room, values={'visibility': 'public'}, reason='Public discussion reviewed.')
        post = RoomPost.objects.create(room=room, author=self.user, body='CURRENT PRIVATE BODY')
        Room.objects.filter(pk=room.pk).update(visibility='members')
        self.assertEqual(list(room_posts(room=room, user=AnonymousUser())), [])
        with self.assertRaises(PermissionDenied):
            safe_public_post(post, user=AnonymousUser())

    def test_post_creation_calls_activity_hook_once_and_edit_does_not_replay(self):
        self.approve()
        with patch('notifications.community_services.record_room_activity') as activity:
            post = self.post()
            save_post(room=self.room, user=self.user, post=post, cleaned_data={'body': 'Edited'})
        activity.assert_called_once()
        self.assertEqual(activity.call_args.args[0].pk, post.pk)


@skipUnlessDBFeature('has_select_for_update')
class LocalRoomConcurrencyTests(RoomFixtures, TransactionTestCase):
    def setUp(self):
        self.build_fixtures()

    def parallel(self, actions):
        barrier = Barrier(len(actions))

        def run(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run, actions))

    def test_simultaneous_join_requests_produce_one_membership_and_one_request_revision(self):
        def request():
            user = get_user_model().objects.get(pk=self.user.pk)
            return request_membership(room=Room.objects.get(pk=self.room.pk), user=user).pk
        results = self.parallel([request, request])
        self.assertEqual(results[0], results[1])
        self.assertEqual(RoomMembership.objects.count(), 1)
        self.assertEqual(RoomMembershipRevision.objects.count(), 1)

    def test_concurrent_post_edits_require_matching_version_and_never_lose_update(self):
        self.approve()
        post = self.post()

        def edit(body):
            try:
                result = save_post(room=Room.objects.get(pk=self.room.pk), user=get_user_model().objects.get(pk=self.user.pk), post=RoomPost.objects.get(pk=post.pk), cleaned_data={'body': body}, expected_version=1)
                return result.body
            except ValidationError:
                return 'conflict'
        results = self.parallel([lambda: edit('First edit'), lambda: edit('Second edit')])
        self.assertEqual(results.count('conflict'), 1)
        post.refresh_from_db()
        self.assertEqual(post.version, 2)
        self.assertEqual(post.revisions.count(), 2)

    def test_approval_racing_leave_never_rejoins_a_member_who_cancelled(self):
        membership = request_membership(room=self.room, user=self.user)

        def approve():
            try:
                return moderate_membership(membership=RoomMembership.objects.get(pk=membership.pk), actor=get_user_model().objects.get(pk=self.moderator.pk), status='approved', reason='Request checked').status
            except ValidationError:
                return 'cancelled'

        def leave():
            return leave_room(room=Room.objects.get(pk=self.room.pk), user=get_user_model().objects.get(pk=self.user.pk)).status
        self.parallel([approve, leave])
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'left')
        self.assertFalse(membership.notifications_enabled)
