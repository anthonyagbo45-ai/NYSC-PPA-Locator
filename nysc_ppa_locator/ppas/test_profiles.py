from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from locations.models import LGA, State
from .admin import ClaimRevisionAdmin, PPAAdmin, PPAClaimAdmin, ReviewAdmin, ReviewRevisionAdmin
from .forms import ReviewForm
from .models import ClaimRevision, PPA, PPAClaim, PlaceChangeLog, Review, ReviewChangeLog, ReviewRevision
from .profile_services import (
    archive_member_claim, get_public_profile_claims, moderate_claim,
    moderate_review, save_member_claim, save_member_review,
)


class ProfileTrustTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Profile State')
        cls.lga = LGA.objects.create(name='Profile LGA', state=cls.state)
        cls.ppa = PPA.objects.create(name='Community Learning Centre', state=cls.state, lga=cls.lga, address='Public address')
        users = get_user_model().objects
        cls.author = users.create_user(state_code='PRIVATE/AUTHOR', email='author@example.com', email_verified=True)
        cls.other = users.create_user(state_code='PRIVATE/OTHER', email='other@example.com', email_verified=True)
        cls.unverified = users.create_user(state_code='PRIVATE/UNVERIFIED', email='unverified@example.com')
        cls.moderator = users.create_user(state_code='PRIVATE/MODERATOR', email='moderator@example.com', email_verified=True, is_staff=True)
        cls.verifier = users.create_user(state_code='PRIVATE/VERIFIER', email='verifier@example.com', email_verified=True, is_staff=True)
        moderation = Permission.objects.filter(content_type__app_label='ppas', codename__in=['can_moderate_claims', 'can_moderate_reviews'])
        cls.moderator.user_permissions.add(*moderation)
        cls.verifier.user_permissions.add(*moderation, Permission.objects.get(content_type__app_label='ppas', codename='can_verify_organisations'))

    def setUp(self):
        cache.clear()
        self.client.force_login(self.author)

    def data(self, **changes):
        return {'topic': PPAClaim.Topic.HOURS, 'content': 'I usually worked from 8am to 2pm.',
                'source_type': PPAClaim.Source.MEMBER, 'source_url': '',
                'observed_on': timezone.localdate(), **changes}

    def form_data(self, **changes):
        return {**self.data(), 'privacy_confirmed': 'on', 'action': 'submit', **changes}

    def claim(self, **changes):
        return save_member_claim(ppa=self.ppa, user=self.author, cleaned_data=self.data(**changes), submit=True)

    def approve(self, claim, **values):
        return moderate_claim(claim=claim, actor=self.moderator,
                              values={'publication_status': PPAClaim.Publication.APPROVED, **values},
                              reason='Reviewed for public relevance and privacy; this does not independently verify it.')

    def test_verified_contribution_stays_pending_and_records_private_revision(self):
        claim = self.claim()
        self.assertEqual(claim.publication_status, PPAClaim.Publication.PENDING)
        self.assertEqual(claim.verification_status, PPAClaim.Verification.COMMUNITY)
        self.assertEqual(claim.revisions.count(), 1)
        self.assertEqual(get_public_profile_claims(self.ppa), [])
        self.approve(claim, moderator_note='PRIVATE INVESTIGATION NOTE')
        rows = get_public_profile_claims(self.ppa)
        self.assertEqual(len(rows), 1)
        self.assertIn('not independently checked', rows[0]['verification_label'])
        self.assertEqual(rows[0]['source_label'], 'Member experience')
        self.assertNotIn('moderator_note', rows[0])
        self.assertNotIn('author_id', rows[0])
        self.assertNotIn('PRIVATE', str(rows))
        self.assertEqual(claim.revisions.count(), 2)

    def test_submission_requires_email_login_post_csrf_and_privacy_acknowledgement(self):
        url = reverse('ppa_profiles:submit_claim', args=[self.ppa.pk])
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.post(url, self.form_data()).status_code, 302)
        self.assertFalse(PPAClaim.objects.exists())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post(url, self.form_data()).status_code, 403)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url, self.form_data(privacy_confirmed='')).status_code, 400)
        self.assertFalse(PPAClaim.objects.exists())

    def test_forged_publication_or_verification_fields_never_grant_trust(self):
        url = reverse('ppa_profiles:submit_claim', args=[self.ppa.pk])
        response = self.client.post(url, self.form_data(publication_status='approved', verification_status='official', checked_at=timezone.now()))
        self.assertEqual(response.status_code, 302)
        claim = PPAClaim.objects.get()
        self.assertEqual(claim.publication_status, PPAClaim.Publication.PENDING)
        self.assertEqual(claim.verification_status, PPAClaim.Verification.COMMUNITY)
        self.assertIsNone(claim.checked_at)
        self.assertEqual(self.client.post(url, self.form_data(source_type='official')).status_code, 400)
        self.assertEqual(PPAClaim.objects.count(), 1)

    def test_private_drafts_are_not_readable_by_other_members_or_moderation_queue(self):
        claim = save_member_claim(ppa=self.ppa, user=self.author, cleaned_data=self.data(), submit=False)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('ppa_profiles:edit_claim', args=[self.ppa.pk, claim.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('ppa_profiles:my_claims')), claim.content)
        request = RequestFactory().get('/admin/')
        request.user = self.moderator
        self.assertFalse(PPAClaimAdmin(PPAClaim, admin.site).get_queryset(request).filter(pk=claim.pk).exists())
        with self.assertRaises(PermissionDenied):
            self.approve(claim)
        self.assertEqual(get_public_profile_claims(self.ppa), [])

    def moderator_client(self, user=None):
        user = user or self.moderator
        client = Client()
        client.force_login(user)
        device = TOTPDevice.objects.create(user=user, name='Privacy regression', confirmed=True)
        session = client.session
        session['otp_device_id'] = device.persistent_id
        session.save()
        return client

    def assert_hidden_from_moderator(self, client, claim, revisions, marker):
        for model, records in [('ppaclaim', [claim]), ('claimrevision', revisions)]:
            response = client.get(reverse(f'admin:ppas_{model}_changelist'))
            self.assertEqual(response.status_code, 200)
            listed = set(response.context['cl'].queryset.values_list('pk', flat=True))
            self.assertFalse(listed.intersection(record.pk for record in records))
            for record in records:
                response = client.get(reverse(f'admin:ppas_{model}_change', args=[record.pk]), follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, marker)

    def test_revision_admin_keeps_current_and_archived_private_drafts_private(self):
        marker = 'NEVER SUBMITTED PRIVATE DRAFT'
        claim = save_member_claim(ppa=self.ppa, user=self.author, cleaned_data=self.data(content=marker), submit=False)
        original = claim.revisions.get()
        snapshot = dict(original.snapshot)
        client = self.moderator_client()
        self.assert_hidden_from_moderator(client, claim, [original], marker)
        archive_member_claim(claim=claim, user=self.author)
        self.assert_hidden_from_moderator(client, claim, list(claim.revisions.all()), marker)
        original.refresh_from_db()
        self.assertEqual(original.snapshot, snapshot)
        self.assertEqual(claim.revisions.count(), 2)
        self.assertContains(self.client.get(reverse('ppa_profiles:my_claims')), marker)

    def test_submitting_a_claim_does_not_share_its_earlier_private_snapshots(self):
        marker = 'EARLIER PRIVATE DRAFT TEXT'
        claim = save_member_claim(ppa=self.ppa, user=self.author, cleaned_data=self.data(content=marker), submit=False)
        draft = claim.revisions.get()
        claim = save_member_claim(ppa=self.ppa, user=self.author, claim=claim,
                                  cleaned_data=self.data(content='Submitted public context'), submit=True)
        submitted = claim.revisions.first()
        unknown = ClaimRevision.objects.create(claim=claim, actor=self.author,
                                               snapshot={'content': 'Legacy private text'}, reason='Historical snapshot without visibility metadata.')
        client = self.moderator_client()
        response = client.get(reverse('admin:ppas_claimrevision_changelist'))
        self.assertEqual(set(response.context['cl'].queryset.values_list('pk', flat=True)), {submitted.pk})
        for revision in (draft, unknown):
            response = client.get(reverse('admin:ppas_claimrevision_change', args=[revision.pk]), follow=True)
            self.assertNotContains(response, marker)
            self.assertNotContains(response, 'Legacy private text')
        response = client.get(reverse('admin:ppas_claimrevision_change', args=[submitted.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Submitted public context')
        self.assertEqual(claim.revisions.count(), 3)

    def test_archiving_a_private_edit_does_not_share_it_with_previous_moderators(self):
        claim = self.claim(content='Previously submitted context')
        claim = save_member_claim(ppa=self.ppa, user=self.author, claim=claim,
                                  cleaned_data=self.data(content='PRIVATE UNSUBMITTED REPLACEMENT'), submit=False)
        archive_member_claim(claim=claim, user=self.author)
        self.assert_hidden_from_moderator(self.moderator_client(), claim, list(claim.revisions.all()),
                                         'PRIVATE UNSUBMITTED REPLACEMENT')
        self.assertEqual(claim.revisions.count(), 3)

    def test_legacy_unknown_state_does_not_make_an_archived_private_edit_visible(self):
        claim = self.claim(content='Previously submitted context')
        submitted = claim.revisions.get()
        # Simulate retained history with missing visibility metadata, without
        # inventing a public state for this earlier private text.
        claim.content = 'UNKNOWN LEGACY PRIVATE CONTENT'
        claim.save(update_fields=['content'])
        unknown = ClaimRevision.objects.create(claim=claim, actor=self.author,
                                               snapshot={'content': claim.content}, reason='Legacy revision.')
        archive_member_claim(claim=claim, user=self.author)
        archived = claim.revisions.first()
        client = self.moderator_client()
        self.assert_hidden_from_moderator(client, claim, list(claim.revisions.all()), claim.content)
        # A later submitted state must not expose the ambiguous archive either.
        PPAClaim.objects.filter(pk=claim.pk).update(publication_status=PPAClaim.Publication.PENDING)
        response = client.get(reverse('admin:ppas_claimrevision_changelist'))
        self.assertEqual(set(response.context['cl'].queryset.values_list('pk', flat=True)), {submitted.pk})
        self.assertEqual(ClaimRevision.objects.filter(pk__in=[unknown.pk, archived.pk]).count(), 2)

    def test_moderators_keep_submitted_archive_history_and_superusers_keep_private_access(self):
        claim = self.approve(self.claim())
        archive_member_claim(claim=claim, user=self.author)
        client = self.moderator_client()
        response = client.get(reverse('admin:ppas_claimrevision_changelist'))
        self.assertEqual(set(response.context['cl'].queryset.values_list('pk', flat=True)),
                         set(claim.revisions.values_list('pk', flat=True)))
        response = client.get(reverse('admin:ppas_ppaclaim_change', args=[claim.pk]))
        self.assertEqual(response.status_code, 200)
        private = save_member_claim(ppa=self.ppa, user=self.author,
                                    cleaned_data=self.data(content='PRIVATE SUPERUSER REVIEW'), submit=False)
        supervisor = get_user_model().objects.create_superuser(state_code='PRIVATE/SUPERVISOR', email='supervisor@example.com')
        supervisor_client = self.moderator_client(supervisor)
        for model, pk in [('ppaclaim', private.pk), ('claimrevision', private.revisions.get().pk)]:
            response = supervisor_client.get(reverse(f'admin:ppas_{model}_change', args=[pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'PRIVATE SUPERUSER REVIEW')

    def test_cross_ppa_edit_and_cross_member_archive_are_rejected(self):
        claim = self.claim()
        another = PPA.objects.create(name='Another Centre', state=self.state, lga=self.lga, address='Other address')
        self.assertEqual(self.client.get(reverse('ppa_profiles:edit_claim', args=[another.pk, claim.pk])).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('ppa_profiles:archive_claim', args=[claim.pk])).status_code, 404)
        with self.assertRaises(PermissionDenied):
            archive_member_claim(claim=claim, user=self.other)
        claim.refresh_from_db()
        self.assertEqual(claim.publication_status, PPAClaim.Publication.PENDING)

    def test_member_pages_exclude_private_moderator_notes(self):
        claim = self.approve(self.claim(), moderator_note='PRIVATE INVESTIGATION 12345')
        for url in [reverse('ppa_profiles:my_claims'), reverse('ppa_profiles:edit_claim', args=[self.ppa.pk, claim.pk])]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'PRIVATE INVESTIGATION')
            if 'form' in response.context:
                self.assertFalse(hasattr(response.context['form'], 'instance'))

    def test_author_edit_is_a_new_unverified_proposal(self):
        claim = self.claim(source_type=PPAClaim.Source.ORGANISATION)
        claim = moderate_claim(claim=claim, actor=self.verifier, values={
            'publication_status': PPAClaim.Publication.APPROVED,
            'verification_status': PPAClaim.Verification.ORGANISATION,
            'source_url': 'https://example.org/placements', 'checked_at': timezone.now(),
            'moderator_note': 'Compared the published organisation placement information.',
        }, reason='Organisation source checked.')
        self.assertEqual(len(get_public_profile_claims(self.ppa)), 1)
        edited = save_member_claim(ppa=self.ppa, user=self.author, claim=claim,
                                  cleaned_data=self.data(content='Working hours have changed.'), submit=True)
        self.assertEqual(edited.publication_status, PPAClaim.Publication.PENDING)
        self.assertEqual(edited.verification_status, PPAClaim.Verification.COMMUNITY)
        self.assertIsNone(edited.checked_at)
        self.assertEqual(get_public_profile_claims(self.ppa), [])
        self.assertEqual(edited.revisions.count(), 3)

    def test_moderation_permission_does_not_grant_independent_verification(self):
        claim = self.claim()
        with self.assertRaises(PermissionDenied):
            moderate_claim(claim=claim, actor=self.moderator, values={
                'verification_status': PPAClaim.Verification.CHECKED, 'checked_at': timezone.now(),
                'source_url': 'https://example.org/reference', 'moderator_note': 'Checked source.',
            }, reason='Attempting to verify.')
        claim.refresh_from_db()
        self.assertEqual(claim.verification_status, PPAClaim.Verification.COMMUNITY)
        self.assertEqual(claim.revisions.count(), 1)
        with self.assertRaises(PermissionDenied):
            moderate_claim(claim=claim, actor=self.other, values={'publication_status': 'approved'}, reason='I would like to publish it.')

    def test_official_source_requires_evidence_check_date_and_separate_verifier(self):
        claim = self.claim()
        values = {'source_type': PPAClaim.Source.OFFICIAL, 'verification_status': PPAClaim.Verification.OFFICIAL,
                  'publication_status': PPAClaim.Publication.APPROVED, 'moderator_note': 'Read attributable published information.'}
        with self.assertRaises(ValidationError):
            moderate_claim(claim=claim, actor=self.verifier, values=values, reason='Missing evidence.')
        result = moderate_claim(claim=claim, actor=self.verifier, values={
            **values, 'source_url': 'https://example.org/public-information', 'checked_at': timezone.now(),
        }, reason='Source material checked with attribution.')
        row = get_public_profile_claims(self.ppa)[0]
        self.assertEqual(row['source_label'], 'Attributed official material')
        self.assertNotIn('NYSC', row['verification_label'])
        self.assertIn('does not establish NYSC posting approval', row['disclaimer'])
        self.assertEqual(result.verification_status, PPAClaim.Verification.OFFICIAL)
        self.author.user_permissions.add(Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_claims'),
                                         Permission.objects.get(content_type__app_label='ppas', codename='can_verify_organisations'))
        with self.assertRaises(PermissionDenied):
            moderate_claim(claim=result, actor=self.author, values={'content': 'I confirm my own report.'}, reason='Self-verification is disallowed.')

    def test_outdated_reports_show_reconfirmation_and_financial_disclaimer(self):
        claim = self.claim(topic=PPAClaim.Topic.SUPPORT, content='Some members reported transport support.',
                           observed_on=timezone.localdate() - timedelta(days=200))
        self.approve(claim)
        row = get_public_profile_claims(self.ppa)[0]
        self.assertTrue(row['is_stale'])
        self.assertIn('Outdated', row['freshness_label'])
        self.assertIn('not guarantees', row['disclaimer'])
        self.assertIn('reconfirmation needed', row['verification_label'])

    def test_future_dates_and_non_web_supporting_links_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.claim(observed_on=timezone.localdate() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.claim(source_url='ftp://example.org/reference')
        self.assertFalse(PPAClaim.objects.exists())

    def test_supporting_links_reject_embedded_credentials_controls_and_malformed_hosts(self):
        for url in ('https://user:password@example.org/reference', 'https://user@example.org/reference',
                    'https://example.org/ref\nerence', 'https://example.org/ref\x00erence',
                    'https://example.org/ref\x7ference', 'https://[bad-host/reference'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.claim(source_url=url)
        self.assertFalse(PPAClaim.objects.exists())

    def test_spam_controls_cover_duplicates_pending_quota_and_daily_quota(self):
        self.claim()
        with self.assertRaisesMessage(ValidationError, 'already in your contributions'):
            self.claim(content='I usually worked from 8am to 2pm.')
        for number in range(4):
            self.claim(content=f'Distinct report {number}')
        with self.assertRaisesMessage(ValidationError, 'five drafts or pending'):
            self.claim(content='Another pending report')
        PPAClaim.objects.update(publication_status=PPAClaim.Publication.ARCHIVED)
        PPAClaim.objects.bulk_create([PPAClaim(ppa=self.ppa, author=self.author, topic='duties', content=f'Archived history {i}',
                                               observed_on=timezone.localdate(), publication_status='archived') for i in range(15)])
        with self.assertRaisesMessage(ValidationError, 'daily contribution limit'):
            self.claim(content='Beyond the daily limit')

    def test_shared_request_limit_prevents_repeated_submission_attempts(self):
        url = reverse('ppa_profiles:submit_claim', args=[self.ppa.pk])
        for _ in range(10):
            self.assertEqual(self.client.post(url, {}).status_code, 400)
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)

    def test_archive_withdraws_publication_and_preserves_audit(self):
        claim = self.approve(self.claim())
        archive_member_claim(claim=claim, user=self.author)
        self.assertEqual(get_public_profile_claims(self.ppa), [])
        self.assertEqual(claim.revisions.count(), 3)
        with self.assertRaises(ValidationError):
            self.approve(claim)
        with self.assertRaises(ProtectedError):
            PPAClaim.objects.get(pk=claim.pk).delete()

    def test_claim_write_and_revision_are_one_transaction(self):
        with patch('ppas.profile_services.ClaimRevision.objects.create', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.claim()
        self.assertFalse(PPAClaim.objects.exists())
        self.assertFalse(ClaimRevision.objects.exists())

    def test_review_edit_preserves_legacy_text_and_hidden_moderation(self):
        review = Review.objects.create(ppa=self.ppa, user=self.author, rating=4, comment='Original private details', is_public=False)
        result = save_member_review(ppa=self.ppa, user=self.author, cleaned_data={
            'rating': 3, 'comment': 'Revised useful experience', 'topic_tags': ['hours'],
            'context_date': timezone.localdate(), 'is_public': True,
        })
        self.assertEqual(result.pk, review.pk)
        self.assertFalse(result.is_public)
        self.assertEqual(result.revisions.count(), 2)
        self.assertEqual(result.revisions.order_by('pk').first().comment, 'Original private details')
        self.assertEqual(result.revisions.first().comment, 'Revised useful experience')
        self.assertEqual(result.topic_labels, ['Working hours'])
        self.assertEqual(Review.objects.count(), 1)

    def test_review_history_is_author_only_and_never_appears_in_public_detail(self):
        review = save_member_review(ppa=self.ppa, user=self.author, cleaned_data={'rating': 4, 'comment': 'OLD PRIVATE TEXT'})
        save_member_review(ppa=self.ppa, user=self.author, cleaned_data={'rating': 5, 'comment': 'Current public text'})
        url = reverse('ppa_profiles:review_history', args=[review.pk])
        self.assertContains(self.client.get(url), 'OLD PRIVATE TEXT')
        self.assertNotContains(self.client.get(reverse('ppa_detail', args=[self.ppa.pk])), 'OLD PRIVATE TEXT')
        for user in (self.other, self.moderator):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_review_rating_only_topics_validation_and_unique_member_review(self):
        form = ReviewForm({'rating': 5, 'comment': '', 'topic_tags': ['transport', 'hours'], 'context_date': timezone.localdate()})
        self.assertTrue(form.is_valid(), form.errors)
        review = save_member_review(ppa=self.ppa, user=self.author, cleaned_data=form.cleaned_data)
        save_member_review(ppa=self.ppa, user=self.author, cleaned_data=form.cleaned_data)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(review.revisions.count(), 1)
        self.assertFalse(ReviewForm({'rating': 4, 'topic_tags': ['official_nysc']}).is_valid())
        self.assertFalse(ReviewForm({'rating': 4, 'context_date': timezone.localdate() + timedelta(days=1)}).is_valid())
        with self.assertRaises(PermissionDenied):
            save_member_review(ppa=self.ppa, user=self.unverified, cleaned_data=form.cleaned_data)

    def test_review_moderation_and_revision_are_audited_and_permission_scoped(self):
        review = save_member_review(ppa=self.ppa, user=self.author, cleaned_data={'rating': 4, 'comment': 'Useful experience'})
        with self.assertRaises(PermissionDenied):
            moderate_review(review=review, actor=self.other, is_public=False, note='No moderation permission')
        with self.assertRaises(ValidationError):
            moderate_review(review=review, actor=self.moderator, is_public=False, note='')
        moderate_review(review=review, actor=self.moderator, is_public=False, note='Contains identifying information.')
        self.assertEqual(ReviewChangeLog.objects.get().note, 'Contains identifying information.')
        self.assertFalse(review.revisions.first().is_public)
        self.assertNotIn('identifying', review.revisions.first().reason)
        request = RequestFactory().get('/admin/')
        request.user = self.other
        self.assertFalse(ReviewAdmin(Review, admin.site).has_change_permission(request))
        self.assertFalse(ReviewRevisionAdmin(ReviewRevision, admin.site).has_view_permission(request))
        request.user = self.moderator
        self.assertTrue(ReviewAdmin(Review, admin.site).has_change_permission(request))

    def test_review_changes_roll_back_when_history_fails(self):
        review = save_member_review(ppa=self.ppa, user=self.author, cleaned_data={'rating': 4, 'comment': 'Keep this original'})
        with patch('ppas.profile_services.ReviewRevision.objects.create', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                save_member_review(ppa=self.ppa, user=self.author, cleaned_data={'rating': 1, 'comment': 'Failed edit'})
        review.refresh_from_db()
        self.assertEqual(review.comment, 'Keep this original')
        self.assertEqual(review.revisions.count(), 1)


class DirectoryVerificationPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Verified State')
        cls.lga = LGA.objects.create(name='Verified LGA', state=cls.state)
        cls.other_lga = LGA.objects.create(name='Other LGA', state=cls.state)
        cls.ppa = PPA.objects.create(
            name='Checked organisation', address='Checked address', state=cls.state, lga=cls.lga,
            latitude=6.5, longitude=3.4, status=PPA.Status.ORGANISATION_CONFIRMED,
            organisation_evidence='Checked the organisation through its published contact channel.',
        )
        users = get_user_model().objects
        cls.editor = users.create_user(state_code='DIRECTORY/EDITOR', is_staff=True)
        cls.verifier = users.create_user(state_code='DIRECTORY/VERIFIER', is_staff=True)
        change = Permission.objects.get(content_type__app_label='ppas', codename='change_ppa')
        cls.editor.user_permissions.add(change)
        cls.verifier.user_permissions.add(change, Permission.objects.get(
            content_type__app_label='ppas', codename='can_verify_organisations',
        ))

    def request(self, user):
        request = RequestFactory().post('/admin/')
        request.user = user
        return request

    def data(self, **changes):
        return {
            'name': self.ppa.name, 'address': self.ppa.address, 'state': self.state.pk,
            'lga': self.lga.pk, 'category': self.ppa.category, 'source': self.ppa.source,
            'status': self.ppa.status, 'location_status': self.ppa.location_status,
            'organisation_evidence': self.ppa.organisation_evidence,
            'latitude': self.ppa.latitude, 'longitude': self.ppa.longitude,
            'moderation_reason': 'A directory correction was requested.', **changes,
        }

    def test_directory_editor_cannot_retain_confirmation_when_changing_identity_or_destination(self):
        request = self.request(self.editor)
        model_admin = PPAAdmin(PPA, admin.site)
        self.assertTrue(model_admin.has_change_permission(request, self.ppa))
        self.assertFalse(self.editor.has_perm('ppas.can_verify_organisations'))
        changes = {
            'name': 'Different organisation', 'address': 'Different address',
            'latitude': 7.5, 'longitude': 4.4, 'place_id': 'different-provider-id',
            'google_maps_url': 'https://www.google.com/maps?q=Other',
            'aliases': 'A different name', 'landmark': 'Different landmark',
            'entrance_instructions': 'Use a different entrance',
            'phone': '+2341234567890', 'email': 'other@example.invalid',
            'source': PPA.Source.COMMUNITY, 'location_status': PPA.LocationStatus.ADDRESS_CHECKED,
            'organisation_evidence': 'Different claimed evidence.', 'lga': self.other_lga.pk,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                original = PPA.objects.get(pk=self.ppa.pk)
                form = model_admin.get_form(request, original)(self.data(**{field: value}), instance=original)
                self.assertFalse(form.is_valid())
                self.assertIn('status', form.errors)
                # The save hook must also reject callers bypassing form checks.
                original = PPA.objects.get(pk=self.ppa.pk)
                setattr(original, 'lga_id' if field == 'lga' else field, value)
                with self.assertRaises(PermissionDenied):
                    model_admin.save_model(request, original, SimpleNamespace(cleaned_data={
                        'moderation_reason': 'Attempting an unauthorised retained badge.',
                    }), True)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.address, 'Checked address')
        self.assertEqual(self.ppa.name, 'Checked organisation')
        self.assertEqual(self.ppa.status, PPA.Status.ORGANISATION_CONFIRMED)
        self.assertFalse(PlaceChangeLog.objects.exists())

    def test_authorised_verifier_can_update_confirmed_destination_with_an_audit(self):
        request = self.request(self.verifier)
        model_admin = PPAAdmin(PPA, admin.site)
        form = model_admin.get_form(request, self.ppa)(self.data(address='Rechecked address'), instance=self.ppa)
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, True)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.address, 'Rechecked address')
        self.assertEqual(self.ppa.status, PPA.Status.ORGANISATION_CONFIRMED)
        self.assertEqual(PlaceChangeLog.objects.get().changes['address'], {
            'from': 'Checked address', 'to': 'Rechecked address',
        })

    def test_editor_can_explicitly_downgrade_confirmation_before_changing_destination(self):
        request = self.request(self.editor)
        model_admin = PPAAdmin(PPA, admin.site)
        form = model_admin.get_form(request, self.ppa)(self.data(
            address='Address awaiting verification', status=PPA.Status.DISPUTED,
        ), instance=self.ppa)
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, True)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.status, PPA.Status.DISPUTED)
        self.assertEqual(PlaceChangeLog.objects.get().changes['status'], {
            'from': PPA.Status.ORGANISATION_CONFIRMED, 'to': PPA.Status.DISPUTED,
        })
