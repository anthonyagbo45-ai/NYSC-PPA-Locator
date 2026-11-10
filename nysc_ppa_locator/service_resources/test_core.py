from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone

from locations.models import State, LGA
from .models import CDSGroup, CDSPreference, GroupRevision, NoticeRevision, ResourceReport, ServiceNotice, is_official_url
from .services import (archive_group, archive_notice, moderate_group, moderate_notice, moderate_report,
                       notice_reminder_eligible, owner_revisions, public_groups, public_notices,
                       safe_group, safe_notice, save_group, save_notice, save_report, select_group)


class ResourceCoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Resource State')
        cls.lga = LGA.objects.create(name='Resource LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other Resource State')
        cls.other_lga = LGA.objects.create(name='Other Resource LGA', state=cls.other_state)
        users = get_user_model().objects
        cls.author = users.create_user(state_code='RESOURCE/AUTHOR', email='resource-author@example.com', email_verified=True)
        cls.other = users.create_user(state_code='RESOURCE/OTHER', email='resource-other@example.com', email_verified=True)
        cls.unverified = users.create_user(state_code='RESOURCE/UNVERIFIED', email='unverified-resource@example.com')
        cls.moderator = users.create_user(state_code='RESOURCE/MOD', email='resource-mod@example.com', is_staff=True)
        cls.verifier = users.create_user(state_code='RESOURCE/VERIFY', email='resource-verify@example.com', is_staff=True)
        permission = lambda code: Permission.objects.get(content_type__app_label='service_resources', codename=code)
        cls.moderator.user_permissions.add(permission('can_moderate_resources'), permission('can_moderate_resource_reports'))
        cls.verifier.user_permissions.add(permission('can_moderate_resources'), permission('can_verify_resources'))

    def group_data(self, **changes):
        values = dict(title='Environment CDS', description='Independent group context.', venue_hint='Public civic hall',
                    public_contact_url='', state=self.state, lga=self.lga, source_type='member', source_url='', observed_on=timezone.localdate())
        values.update(changes)
        return values

    def notice_data(self, **changes):
        values = dict(kind='cds', title='Group meeting', body='Community-reported time; confirm with the designated official.', venue_hint='Public civic hall',
                      state=self.state, lga=self.lga, group=None, event_at=timezone.now()+timedelta(days=2), ends_at=None,
                      timezone_name='Africa/Lagos', batch_label='', stage='', source_type='member', source_url='', observed_on=timezone.localdate())
        values.update(changes)
        return values

    def group(self, **changes):
        return save_group(user=self.author, cleaned_data=self.group_data(**changes), submit=True)

    def notice(self, **changes):
        return save_notice(user=self.author, cleaned_data=self.notice_data(**changes), submit=True)

    def approve(self, record, **values):
        service, field = (moderate_group, 'group') if isinstance(record, CDSGroup) else (moderate_notice, 'notice')
        return service(**{field: record}, actor=self.moderator, values={'publication_status':'approved', **values}, reason='Source and locality reviewed for publication, not independent verification.')

    def checked_notice(self):
        notice = self.notice(title='Checked source notice')
        return moderate_notice(notice=notice, actor=self.verifier, values=dict(publication_status='approved', verification_status='checked', source_url='https://example.com/public-notice', checked_at=timezone.now(), moderator_note='Independent source/context checked.'), reason='Independently checked notice evidence.')

    def test_private_draft_submission_and_approval_are_separate(self):
        draft = save_group(user=self.author, cleaned_data=self.group_data(), submit=False)
        self.assertFalse(draft.shared_for_review)
        self.assertFalse(public_groups().exists())
        with self.assertRaises(PermissionDenied):self.approve(draft)
        pending = save_group(user=self.author, cleaned_data=self.group_data(), group=draft, submit=True)
        self.assertEqual(pending.publication_status, 'pending')
        approved = self.approve(pending)
        self.assertEqual(approved.verification_status, 'community')
        self.assertEqual(public_groups().count(), 1)
        self.assertEqual(approved.revisions.count(), 3)

    def test_unverified_or_revoked_author_cannot_contribute(self):
        with self.assertRaises(PermissionDenied):save_notice(user=self.unverified, cleaned_data=self.notice_data())
        get_user_model().objects.filter(pk=self.author.pk).update(email_verified=False)
        with self.assertRaises(PermissionDenied):self.notice()
        self.assertFalse(ServiceNotice.objects.exists())

    def test_member_cannot_forge_publication_or_evidence_fields(self):
        data = self.notice_data(verification_status='official', publication_status='approved', author=self.other, moderator_note='Forged')
        notice = save_notice(user=self.author, cleaned_data=data, submit=True)
        self.assertEqual(notice.author_id, self.author.pk)
        self.assertEqual(notice.publication_status, 'pending')
        self.assertEqual(notice.verification_status, 'community')
        self.assertEqual(notice.moderator_note, '')
        with self.assertRaises(PermissionDenied):self.notice(source_type='official')

    def test_geography_and_group_kind_are_validated(self):
        with self.assertRaises(ValidationError):self.notice(lga=self.other_lga)
        group = self.approve(self.group())
        with self.assertRaises(ValidationError):self.notice(group=group, state=self.other_state,lga=self.other_lga)
        with self.assertRaises(ValidationError):self.notice(group=group, kind='clearance')
        private = save_group(user=self.other, cleaned_data=self.group_data(title='Private CDS'), submit=False)
        with self.assertRaises(ValidationError):self.notice(group=private)
        self.assertFalse(ServiceNotice.objects.exists())

    def test_date_end_and_zone_validation(self):
        now = timezone.now()
        cases = [dict(observed_on=timezone.localdate()+timedelta(days=1)),dict(event_at=now,ends_at=now-timedelta(minutes=1)),dict(event_at=None,ends_at=now),dict(timezone_name='Not/AZone')]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.notice(**changes)
        self.assertFalse(ServiceNotice.objects.exists())

    def test_public_links_reject_credentials_controls_and_nonweb_schemes(self):
        for url in ('https://name:password@example.com/x','javascript:alert(1)','https://example.com/a\x7f','https://example.com:invalid/x'):
            with self.subTest(url=url),self.assertRaises(ValidationError):self.notice(source_url=url)
        self.assertFalse(ServiceNotice.objects.exists())

    @override_settings(NYSC_OFFICIAL_SOURCE_HOSTS=('www.nysc.gov.ng',))
    def test_official_source_requires_exact_https_trusted_host(self):
        self.assertTrue(is_official_url('https://www.nysc.gov.ng/publication'))
        for url in ('http://www.nysc.gov.ng/x','https://www.nysc.gov.ng.evil.example/x','https://evilnysc.gov.ng/x','https://name@www.nysc.gov.ng/x','https://www.nysc.gov.ng:444/x','https://www.nysc.gov.ng:bad/x'):
            self.assertFalse(is_official_url(url))
        notice = self.notice()
        with self.assertRaises(ValidationError):moderate_notice(notice=notice,actor=self.verifier,values={'source_type':'official','source_url':'https://example.com/pretend'},reason='A false source should be rejected.')
        self.assertEqual(ServiceNotice.objects.get(pk=notice.pk).source_type,'member')

    def test_moderator_cannot_grant_stronger_verification(self):
        notice = self.notice()
        with self.assertRaises(PermissionDenied):self.approve(notice,verification_status='checked',source_url='https://example.com/notice',checked_at=timezone.now(),moderator_note='Claimed check')
        with self.assertRaises(ValidationError):moderate_notice(notice=notice,actor=self.verifier,values={'verification_status':'checked'},reason='Missing evidence cannot pass.')
        self.assertEqual(ServiceNotice.objects.get(pk=notice.pk).verification_status,'community')

    def test_self_verification_is_forbidden_even_with_permission(self):
        self.author.user_permissions.set(self.verifier.user_permissions.all())
        notice = self.notice()
        with self.assertRaises(PermissionDenied):moderate_notice(notice=notice,actor=self.author,values=dict(verification_status='checked',source_url='https://example.com/n',checked_at=timezone.now(),moderator_note='Self check'),reason='Self check is not independent.')

    def test_verified_notice_public_projection_omits_private_evidence_and_actors(self):
        notice = self.checked_notice()
        result = safe_notice(notice)
        self.assertEqual(result['verification_status'],'checked')
        self.assertTrue(result['reminder_eligible'])
        for field in ('moderator_note','author_id','moderated_by_id','shared_for_review'):
            self.assertNotIn(field,result)
        self.assertIn('designated NYSC official',result['official_caution'])

    def test_owner_edit_resets_source_check_and_invalidates_versions(self):
        notice = self.checked_notice(); before=notice.version
        edited = save_notice(user=self.author,notice=notice,cleaned_data=self.notice_data(body='Updated private proposed time.'),submit=False)
        self.assertGreater(edited.version,before)
        self.assertFalse(edited.shared_for_review)
        self.assertEqual(edited.verification_status,'community')
        self.assertIsNone(edited.checked_at)
        self.assertEqual(edited.moderator_note,'')
        self.assertFalse(notice_reminder_eligible(edited))
        self.assertEqual(edited.revisions.filter(shared_for_review=True).count(),2)

    def test_cross_author_edit_archive_and_history_denied(self):
        group = self.group()
        for action in (lambda:save_group(user=self.other,group=group,cleaned_data=self.group_data()),lambda:archive_group(group=group,user=self.other),lambda:owner_revisions(resource=group,user=self.other),lambda:safe_group(group,user=self.other)):
            with self.assertRaises(PermissionDenied):action()
        self.assertEqual(GroupRevision.objects.count(),1)

    def test_expired_group_and_notice_are_historical_not_current(self):
        group=self.approve(self.group(observed_on=timezone.localdate()-timedelta(days=181)))
        notice=self.approve(self.notice(observed_on=timezone.localdate()-timedelta(days=31)))
        self.assertFalse(public_groups().exists()); self.assertFalse(public_notices().exists())
        self.assertTrue(safe_group(public_groups(include_expired=True).get())['is_stale'])
        self.assertTrue(safe_notice(public_notices(include_expired=True).get())['is_stale'])

    def test_eligibility_distinguishes_community_and_bounded_late_dispatch_gate(self):
        notice=self.approve(self.notice())
        self.assertFalse(notice_reminder_eligible(notice))
        notice=self.checked_notice()
        after=notice.event_at+timedelta(minutes=5)
        self.assertFalse(notice_reminder_eligible(notice,now=after))
        self.assertTrue(notice_reminder_eligible(notice,now=after,require_future_event=False))
        self.assertFalse(notice_reminder_eligible(notice,now=notice.expires_at,require_future_event=False))
        notice.verification_status='unknown'
        self.assertFalse(notice_reminder_eligible(notice))

    def test_official_host_policy_revocation_blocks_existing_reminder_eligibility(self):
        notice=self.notice()
        notice=moderate_notice(notice=notice,actor=self.verifier,values=dict(publication_status='approved',source_type='official',verification_status='official',source_url='https://www.nysc.gov.ng/source',checked_at=timezone.now(),moderator_note='Exact source and locality checked.'),reason='Attributed source checked.')
        self.assertTrue(notice_reminder_eligible(notice))
        with override_settings(NYSC_OFFICIAL_SOURCE_HOSTS=()):self.assertFalse(notice_reminder_eligible(notice))

    def test_private_group_preferences_are_optional_not_public_membership(self):
        group=self.approve(self.group())
        preference=select_group(user=self.unverified,group=group)
        self.assertEqual(preference.group_id,group.pk)
        self.assertNotIn('private_preferences',safe_group(group))
        select_group(user=self.unverified,group=None)
        preference.refresh_from_db();self.assertIsNone(preference.group_id)
        archive_group(group=group,user=self.author)
        with self.assertRaises(ValidationError):select_group(user=self.other,group=group)

    def test_private_group_title_does_not_leak_through_published_notice(self):
        group=self.approve(self.group())
        notice=self.approve(self.notice(group=group))
        save_group(user=self.author,group=group,cleaned_data=self.group_data(title='PRIVATE changed title'),submit=False)
        notice=ServiceNotice.objects.select_related('group','state','lga').get(pk=notice.pk)
        result=safe_notice(notice)
        self.assertEqual(result['group_title'],'')
        self.assertFalse(notice_reminder_eligible(notice))
        self.assertFalse(public_notices().exists())

    def test_author_history_excludes_staff_evidence_and_actor_identifiers(self):
        notice=self.checked_notice()
        history=owner_revisions(resource=notice,user=self.author)
        self.assertEqual(len(history),2)
        for item in history:
            for field in ('author_id','moderator_note','moderated_by_id','moderated_at'):
                self.assertNotIn(field,item['snapshot'])
            self.assertNotIn('Independent source/context checked.',item['reason'])

    def test_unconfirmed_notice_never_becomes_verified_on_publication(self):
        notice=self.approve(self.notice(kind='clearance'))
        self.assertEqual(notice.verification_status,'community')
        self.assertFalse(notice_reminder_eligible(notice))
        self.assertIn('not independently',safe_notice(notice)['verification_label'])

    def test_report_target_private_drafts_and_staff_notes_are_protected(self):
        group=self.group()
        with self.assertRaises(Http404):save_report(user=self.unverified,resource=group,category='wrong_info',details='A concern')
        group=self.approve(group)
        report=save_report(user=self.unverified,resource=group,category='wrong_info',details='A private concern')
        result=moderate_report(report=report,actor=self.moderator,status='investigating',moderator_note='Private investigation evidence.')
        self.assertEqual(result.revisions.count(),1)
        with self.assertRaises(PermissionDenied):moderate_report(report=report,actor=self.other,status='resolved',moderator_note='Forged')
        with self.assertRaises(ValidationError):moderate_report(report=report,actor=self.moderator,status='resolved',moderator_note='')

    def test_archiving_is_idempotent_and_preserves_history(self):
        notice=self.approve(self.notice());before=notice.version
        archived=archive_notice(notice=notice,user=self.author)
        self.assertEqual(archived.version,before+1)
        again=archive_notice(notice=archived,user=self.author)
        self.assertEqual(again.version,archived.version)
        self.assertFalse(public_notices(include_expired=True).exists())
        with self.assertRaises(ValidationError):self.approve(archived)

    def test_database_constraints_reject_private_review_flags_and_bad_status(self):
        group=self.group()
        for updates in ({'publication_status':'draft','shared_for_review':True},{'verification_status':'unknown'},{'version':0}):
            with self.assertRaises(IntegrityError),transaction.atomic():CDSGroup.objects.filter(pk=group.pk).update(**updates)

    def test_public_projection_query_count_is_bounded(self):
        group=self.approve(self.group())
        ServiceNotice.objects.bulk_create([ServiceNotice(title=f'Notice {number}',body='Reported context',kind='cds',group=group,state=self.state,lga=self.lga,author=self.author,observed_on=timezone.localdate(),publication_status='approved',shared_for_review=True) for number in range(20)])
        with self.assertNumQueries(1):rows=[safe_notice(row) for row in public_notices()]
        self.assertEqual(len(rows),20)
