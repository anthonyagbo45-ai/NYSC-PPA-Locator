from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from locations.models import LGA, State
from .models import PPA, PPAClaim, PPASubmission, PlaceChangeLog, SubmissionRevision
from .submission_admin import PPASubmissionAdminForm
from .submission_forms import PPASubmissionForm
from .submission_services import (
    duplicate_candidates, get_public_submission_provenance, moderate_submission,
    save_submission, withdraw_submission,
)


class IndependentSubmissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Independent State')
        cls.lga = LGA.objects.create(name='Independent LGA', state=cls.state)
        cls.other_state = State.objects.create(name='Other Independent State')
        cls.other_lga = LGA.objects.create(name='Other Independent LGA', state=cls.other_state)
        users = get_user_model().objects
        cls.author = users.create_user(state_code='SUGGEST/AUTHOR', email='proposal-author@example.com', email_verified=True)
        cls.other = users.create_user(state_code='SUGGEST/OTHER', email='proposal-other@example.com', email_verified=True)
        cls.unverified = users.create_user(state_code='SUGGEST/UNVERIFIED', email='proposal-unverified@example.com')
        cls.moderator = users.create_user(state_code='SUGGEST/MODERATOR', email='proposal-moderator@example.com', is_staff=True)
        cls.verifier = users.create_user(state_code='SUGGEST/VERIFIER', email='proposal-verifier@example.com', is_staff=True)
        permission = Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_submissions')
        cls.moderator.user_permissions.add(permission)
        cls.verifier.user_permissions.add(permission, Permission.objects.get(content_type__app_label='ppas', codename='can_verify_organisations'))

    def setUp(self):
        cache.clear()
        self.client.force_login(self.author)

    def data(self, **changes):
        return {'name': 'Independent Learning Centre', 'state': self.state, 'lga': self.lga,
                'category': 'Education', 'address': 'Public organisation address', 'landmark': '',
                'entrance_instructions': '', 'latitude': None, 'longitude': None,
                'source_type': 'member', 'source_url': '', 'observed_on': timezone.localdate(), **changes}

    def form_data(self, **changes):
        values = self.data(**changes)
        return {**values, 'state': getattr(values['state'], 'pk', values['state']),
                'lga': getattr(values['lga'], 'pk', values['lga']), 'latitude': values['latitude'] or '',
                'longitude': values['longitude'] or '', 'privacy_confirmed': 'on', 'action': 'submit'}

    def proposal(self, submit=True, **changes):
        return save_submission(user=self.author, cleaned_data=self.data(**changes), submit=submit)

    def approve(self, proposal, actor=None, **kwargs):
        return moderate_submission(submission=proposal, actor=actor or self.moderator, decision='approve',
                                    reason='Checked source, locality, relevance and privacy. No NYSC approval is implied.', **kwargs)

    def moderator_client(self, user=None):
        user = user or self.moderator
        client = Client()
        client.force_login(user)
        device = TOTPDevice.objects.create(user=user, confirmed=True)
        session = client.session
        session['otp_device_id'] = device.persistent_id
        session.save()
        return client

    def test_private_draft_and_pending_proposal_do_not_create_directory_place(self):
        draft = self.proposal(submit=False)
        self.assertEqual(draft.publication_status, 'draft')
        self.assertFalse(draft.shared_for_review)
        self.assertEqual(PPA.objects.count(), 0)
        pending = save_submission(user=self.author, submission=draft, cleaned_data=self.data(), submit=True)
        self.assertTrue(pending.shared_for_review)
        self.assertEqual(pending.publication_status, 'pending')
        self.assertEqual(PPA.objects.count(), 0)
        self.assertEqual(pending.revisions.count(), 2)

    def test_review_publishes_independent_record_without_google_or_official_badge(self):
        proposal = self.approve(self.proposal(latitude=6.52, longitude=3.38))
        ppa = proposal.published_ppa
        self.assertEqual(PPA.objects.count(), 1)
        self.assertEqual(ppa.source, PPA.Source.COMMUNITY)
        self.assertEqual(ppa.status, PPA.Status.DISCOVERED)
        self.assertEqual(ppa.location_status, PPA.LocationStatus.UNCONFIRMED)
        self.assertFalse(ppa.verified)
        self.assertIsNone(ppa.place_id)
        self.assertEqual(ppa.submitted_by_id, self.author.pk)
        self.assertEqual(PlaceChangeLog.objects.filter(ppa=ppa).count(), 1)
        self.assertEqual(proposal.verification_status, PPAClaim.Verification.COMMUNITY)

    def test_invalid_state_lga_cannot_be_saved(self):
        with self.assertRaises(ValidationError):
            self.proposal(lga=self.other_lga)
        form = PPASubmissionForm(self.form_data(lga=self.other_lga))
        self.assertFalse(form.is_valid())
        self.assertIn('lga', form.errors)
        self.assertFalse(PPASubmission.objects.exists())

    def test_lga_choice_rendering_uses_bounded_queries(self):
        LGA.objects.bulk_create([LGA(name=f'Additional LGA {index}', state=self.state) for index in range(12)])
        form = PPASubmissionForm(initial={'state': self.state.pk, 'lga': self.lga.pk})
        with self.assertNumQueries(2):
            html = form.as_p()
        self.assertIn('Additional LGA 11', html)

    def test_optional_pin_must_be_a_finite_coordinate_pair(self):
        for values in [{'latitude': 6.5}, {'latitude': float('nan'), 'longitude': 3.3},
                       {'latitude': 6.5, 'longitude': float('inf')}, {'latitude': 91, 'longitude': 3.3}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.proposal(**values)
        self.assertEqual(PPASubmission.objects.count(), 0)
        self.assertIsNone(self.proposal().latitude)

    def test_missing_destination_context_and_future_observation_are_invalid(self):
        for values in [{'address': '', 'landmark': ''}, {'observed_on': timezone.localdate()+timedelta(days=1)}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.proposal(**values)

    def test_supporting_url_rejects_credentials_controls_and_unsafe_scheme(self):
        for url in ['https://name:password@example.com/page', 'javascript:alert(1)', 'https://example.com/a\x7fb']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.proposal(source_url=url)

    def test_active_duplicate_proposals_cannot_be_created_for_same_member(self):
        self.proposal(name='Independent   Learning Centre')
        with self.assertRaises(ValidationError):
            self.proposal(name='  independent learning centre ')
        self.assertEqual(PPASubmission.objects.count(), 1)

    def test_daily_and_active_quotas_are_bounded(self):
        for index in range(10):
            self.proposal(name=f'School {index}')
        with self.assertRaises(ValidationError):
            self.proposal(name='Over quota school')

    def test_unverified_member_cannot_publish_or_save_a_proposal(self):
        with self.assertRaises(PermissionDenied):
            save_submission(user=self.unverified, cleaned_data=self.data(), submit=True)
        self.client.force_login(self.unverified)
        response = self.client.post(reverse('ppa_submissions:submit'), self.form_data())
        self.assertRedirects(response, reverse('email_verification'))
        self.assertFalse(PPASubmission.objects.exists())

    def test_forged_publication_and_role_fields_are_ignored(self):
        response = self.client.post(reverse('ppa_submissions:submit'), {**self.form_data(),
            'publication_status': 'approved', 'verification_status': 'official',
            'moderator_note': 'FAKE PRIVATE EVIDENCE', 'published_ppa': '12', 'author': str(self.other.pk)})
        self.assertEqual(response.status_code, 302)
        proposal = PPASubmission.objects.get()
        self.assertEqual(proposal.author_id, self.author.pk)
        self.assertEqual(proposal.publication_status, 'pending')
        self.assertEqual(proposal.verification_status, 'community')
        self.assertEqual(proposal.moderator_note, '')
        self.assertIsNone(proposal.published_ppa_id)

    def test_duplicate_directory_place_requires_explicit_reviewed_link_and_is_unchanged(self):
        existing = PPA.objects.create(name='Independent Learning Centre', state=self.state, lga=self.lga,
                                      address='Original protected address', description='Original independent text')
        before = PPA.objects.filter(pk=existing.pk).values().get()
        proposal = self.proposal(address='A proposed different address')
        self.assertEqual(list(duplicate_candidates(name=proposal.name, state_id=self.state.pk, lga_id=self.lga.pk)), [existing])
        with self.assertRaises(ValidationError):
            self.approve(proposal)
        proposal.refresh_from_db()
        self.assertEqual(proposal.publication_status, 'pending')
        self.approve(proposal, existing_ppa=existing)
        self.assertEqual(PPA.objects.count(), 1)
        self.assertEqual(PPA.objects.filter(pk=existing.pk).values().get(), before)
        self.assertIsNone(get_public_submission_provenance(existing))

    def test_existing_ppa_link_cannot_cross_state_or_lga(self):
        existing = PPA.objects.create(name='Other Area School', state=self.other_state, lga=self.other_lga, address='Public address')
        proposal = self.proposal()
        with self.assertRaises(ValidationError):
            self.approve(proposal, existing_ppa=existing)
        proposal.refresh_from_db()
        self.assertEqual(proposal.publication_status, 'pending')
        self.assertIsNone(proposal.published_ppa_id)

    def test_normalized_published_identity_survives_private_edits_and_unicode_casefold(self):
        original = self.approve(self.proposal(name='Straße Training Centre'))
        ppa = original.published_ppa
        save_submission(user=self.author, submission=original, cleaned_data=self.data(name='Private later edit'), submit=False)
        proposal = save_submission(user=self.other, cleaned_data=self.data(name='STRASSE  TRAINING CENTRE'), submit=True)
        with self.assertRaises(ValidationError):
            self.approve(proposal)
        self.assertEqual(list(duplicate_candidates(name=proposal.name, state_id=self.state.pk, lga_id=self.lga.pk)), [ppa])
        self.assertEqual(PPA.objects.count(), 1)

    def test_only_independent_reviewer_can_review_submitted_proposals(self):
        proposal = self.proposal()
        for actor in [self.author, self.other]:
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.approve(proposal, actor=actor)
        self.author.user_permissions.add(Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_submissions'))
        actor = get_user_model().objects.get(pk=self.author.pk)
        with self.assertRaises(PermissionDenied):
            self.approve(proposal, actor=actor)
        with self.assertRaises(ValidationError):
            self.approve(self.proposal(submit=False, name='Private draft'))

    def test_independent_verification_requires_permission_evidence_and_different_reviewer(self):
        proposal = self.proposal(source_url='https://example.com/public-school-info')
        values = {'verification_status': 'checked', 'checked_at': timezone.now()-timedelta(minutes=1),
                  'moderator_note': 'PRIVATE OPERATOR EVIDENCE'}
        with self.assertRaises(PermissionDenied):
            self.approve(proposal, values=values)
        published = self.approve(proposal, actor=self.verifier, values=values)
        self.assertEqual(published.verification_status, 'checked')
        self.assertEqual(published.published_ppa.status, PPA.Status.DISCOVERED)
        self.assertFalse(published.published_ppa.verified)
        with self.assertRaises(ValidationError):
            self.approve(self.proposal(name='Missing evidence'), actor=self.verifier, values=values)

    def test_author_edit_resets_review_without_mutating_published_place_or_original_provenance(self):
        proposal = self.approve(self.proposal(source_url='https://example.com/original'), actor=self.verifier,
                                values={'verification_status': 'checked', 'checked_at': timezone.now()-timedelta(minutes=1),
                                        'moderator_note': 'PRIVATE EVIDENCE'})
        ppa = proposal.published_ppa
        original = PPA.objects.filter(pk=ppa.pk).values().get()
        edited = save_submission(user=self.author, submission=proposal,
                                  cleaned_data=self.data(name='NEVER PUBLISH PRIVATE EDIT'), submit=False)
        self.assertEqual(edited.publication_status, 'draft')
        self.assertFalse(edited.shared_for_review)
        self.assertEqual(edited.verification_status, 'community')
        self.assertIsNone(edited.checked_at)
        self.assertIsNone(edited.published_ppa_id)
        self.assertEqual(PPA.objects.filter(pk=ppa.pk).values().get(), original)
        provenance = get_public_submission_provenance(ppa)
        self.assertEqual(provenance['source_url'], 'https://example.com/original')
        self.assertEqual(provenance['verification_status'], 'checked')
        self.assertNotIn('moderator_note', provenance)
        self.assertNotIn('author_id', provenance)
        self.assertNotIn('NEVER PUBLISH PRIVATE EDIT', str(provenance))

    def test_current_destination_edit_cannot_inherit_historical_verification_badge(self):
        published = self.approve(self.proposal(source_url='https://example.com/info'), actor=self.verifier,
                                 values={'verification_status': 'checked', 'checked_at': timezone.now()-timedelta(minutes=1),
                                         'moderator_note': 'PRIVATE EVIDENCE'})
        ppa = published.published_ppa
        before = get_public_submission_provenance(ppa)
        self.assertFalse(before['details_changed_since'])
        PPA.objects.filter(pk=ppa.pk).update(latitude=6.51, longitude=3.39)
        ppa.refresh_from_db()
        after = get_public_submission_provenance(ppa)
        self.assertTrue(after['details_changed_since'])
        self.assertEqual(after['verification_label'], 'Historical evidence — current details need reconfirmation')
        self.assertEqual(after['recorded_at'], before['recorded_at'])

    def test_legacy_directory_profile_does_not_query_manual_provenance(self):
        legacy = PPA.objects.create(name='Preserved legacy school', state=self.state, lga=self.lga, address='Public address')
        self.assertEqual(legacy.source, PPA.Source.LEGACY)
        with self.assertNumQueries(0):
            self.assertIsNone(get_public_submission_provenance(legacy))

    def test_outdated_proposal_needs_reconfirmation_before_approval(self):
        proposal = self.proposal(observed_on=timezone.localdate()-timedelta(days=190))
        with self.assertRaises(ValidationError):
            self.approve(proposal)
        with self.assertRaises(ValidationError):
            self.approve(proposal, values={'expires_at': timezone.now()+timedelta(days=180)})
        proposal.refresh_from_db()
        self.assertEqual(proposal.publication_status, 'pending')

    def test_withdrawal_preserves_directory_records_and_history_and_cannot_be_republished(self):
        proposal = self.approve(self.proposal())
        ppa_id = proposal.published_ppa_id
        withdrawn = withdraw_submission(submission=proposal, user=self.author)
        self.assertEqual(withdrawn.publication_status, 'withdrawn')
        self.assertTrue(PPA.objects.filter(pk=ppa_id).exists())
        self.assertEqual(withdrawn.revisions.count(), 3)
        with self.assertRaises(ValidationError):
            self.approve(withdrawn)
        with self.assertRaises(ValidationError):
            save_submission(user=self.author, submission=withdrawn, cleaned_data=self.data(), submit=True)

    def test_owner_views_and_history_exclude_private_moderator_notes_and_reasons(self):
        proposal = self.proposal()
        moderate_submission(submission=proposal, actor=self.moderator, decision='reject', reason='PRIVATE INVESTIGATION REASON',
                            values={'moderator_note': 'PRIVATE INVESTIGATION NOTE'})
        for name in ['mine', 'history']:
            url = reverse(f'ppa_submissions:{name}', args=[proposal.pk] if name == 'history' else [])
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'PRIVATE INVESTIGATION REASON')
            self.assertNotContains(response, 'PRIVATE INVESTIGATION NOTE')
            self.assertNotContains(response, self.author.state_code)
            self.assertNotContains(response, self.moderator.email)

    def test_guest_and_other_member_cannot_read_edit_withdraw_or_list_private_proposal(self):
        proposal = self.proposal(submit=False, name='AUTHOR ONLY SECRET DRAFT')
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('ppa_submissions:mine')), 'AUTHOR ONLY SECRET DRAFT')
        for name in ['edit', 'history']:
            self.assertEqual(self.client.get(reverse(f'ppa_submissions:{name}', args=[proposal.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('ppa_submissions:withdraw', args=[proposal.pk])).status_code, 404)
        guest = Client()
        for name in ['edit', 'history']:
            self.assertEqual(guest.get(reverse(f'ppa_submissions:{name}', args=[proposal.pk])).status_code, 302)
        self.assertEqual(guest.get(reverse('ppa_submissions:mine')).status_code, 302)

    def test_csrf_and_post_only_withdrawal_and_privacy_consent(self):
        proposal = self.proposal(submit=False)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post(reverse('ppa_submissions:submit'), self.form_data(name='Other school')).status_code, 403)
        self.assertEqual(client.post(reverse('ppa_submissions:withdraw', args=[proposal.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('ppa_submissions:withdraw', args=[proposal.pk])).status_code, 405)
        response = self.client.post(reverse('ppa_submissions:submit'), {**self.form_data(name='Other school'), 'privacy_confirmed': ''})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PPASubmission.objects.count(), 1)

    def test_public_overview_is_free_and_member_pages_are_no_store(self):
        self.assertEqual(Client().get(reverse('ppa_submissions:overview')).status_code, 200)
        response = self.client.get(reverse('ppa_submissions:mine'))
        self.assertIn('no-store', response['Cache-Control'])

    def test_ordinary_moderator_cannot_read_draft_or_draft_only_revision_after_submit_or_withdraw(self):
        draft = self.proposal(submit=False, name='PRIVATE DRAFT SNAPSHOT')
        draft_revision = draft.revisions.get()
        client = self.moderator_client()
        for model, item in [('ppasubmission', draft), ('submissionrevision', draft_revision)]:
            response = client.get(reverse(f'admin:ppas_{model}_changelist'))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(item.pk, set(response.context['cl'].queryset.values_list('pk', flat=True)))
            detail = client.get(reverse(f'admin:ppas_{model}_change', args=[item.pk]), follow=True)
            self.assertNotContains(detail, 'PRIVATE DRAFT SNAPSHOT')
        submitted = save_submission(user=self.author, submission=draft, cleaned_data=self.data(), submit=True)
        response = client.get(reverse('admin:ppas_submissionrevision_changelist'))
        self.assertNotIn(draft_revision.pk, set(response.context['cl'].queryset.values_list('pk', flat=True)))
        self.assertIn(submitted.revisions.latest('pk').pk, set(response.context['cl'].queryset.values_list('pk', flat=True)))
        private = save_submission(user=self.author, submission=submitted, cleaned_data=self.data(name='PRIVATE LATER EDIT'), submit=False)
        withdrawn = withdraw_submission(submission=private, user=self.author)
        response = client.get(reverse('admin:ppas_ppasubmission_changelist'))
        self.assertNotIn(withdrawn.pk, set(response.context['cl'].queryset.values_list('pk', flat=True)))
        response = client.get(reverse('admin:ppas_submissionrevision_changelist'))
        self.assertEqual(response.context['cl'].queryset.filter(submission=withdrawn).count(), 0)
        self.assertEqual(withdrawn.revisions.count(), 4)

    def test_admin_form_catches_invalid_duplicate_link_instead_of_server_error(self):
        proposal = self.proposal()
        PPA.objects.create(name=proposal.name, state=self.state, lga=self.lga, address='Public address')
        base = {field: getattr(proposal, field) or '' for field in ('source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at', 'moderator_note')}
        form = PPASubmissionAdminForm({**base, 'decision': 'approve', 'review_reason': 'Reviewed', 'existing_ppa_id': ''}, instance=proposal)
        self.assertFalse(form.is_valid())
        self.assertIn('existing_ppa_id', form.errors)

        form = PPASubmissionAdminForm({**base, 'decision': 'approve', 'review_reason': 'Reviewed', 'existing_ppa_id': 999999}, instance=proposal)
        self.assertFalse(form.is_valid())
        self.assertIn('existing_ppa_id', form.errors)

    def test_real_otp_admin_approval_publishes_once_and_audits_the_proposal(self):
        proposal = self.proposal()
        client = self.moderator_client()
        expires = timezone.localtime(proposal.expires_at)
        response = client.post(reverse('admin:ppas_ppasubmission_change', args=[proposal.pk]), {
            'source_type': 'member', 'source_url': '', 'verification_status': 'community',
            'checked_at_0': '', 'checked_at_1': '', 'expires_at_0': expires.date().isoformat(),
            'expires_at_1': expires.strftime('%H:%M:%S'), 'moderator_note': '', 'decision': 'approve',
            'existing_ppa_id': '', 'review_reason': 'Public source and locality checked; no NYSC endorsement.', '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.publication_status, 'approved')
        self.assertEqual(PPA.objects.count(), 1)
        self.assertEqual(proposal.revisions.count(), 2)
        self.assertFalse(proposal.published_ppa.verified)


class SubmissionApprovalConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_two_proposals_cannot_concurrently_publish_duplicate_new_places(self):
        state = State.objects.create(name='Approval State')
        lga = LGA.objects.create(name='Approval LGA', state=state)
        users = get_user_model().objects
        first = users.create_user(state_code='RACE/PROPOSER1', email='first-proposer@example.com', email_verified=True)
        second = users.create_user(state_code='RACE/PROPOSER2', email='second-proposer@example.com', email_verified=True)
        moderator = users.create_user(state_code='RACE/REVIEWER', email='reviewer@example.com', is_staff=True)
        moderator.user_permissions.add(Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_submissions'))
        values = {'name': 'Concurrent New School', 'state': state, 'lga': lga, 'category': 'Education',
                  'address': 'Public organisation address', 'landmark': '', 'entrance_instructions': '',
                  'latitude': None, 'longitude': None, 'source_type': 'member', 'source_url': '',
                  'observed_on': timezone.localdate()}
        proposals = [save_submission(user=user, cleaned_data=values, submit=True) for user in [first, second]]
        barrier = Barrier(2)

        def approve(pk):
            close_old_connections()
            try:
                actor = users.get(pk=moderator.pk)
                proposal = PPASubmission.objects.get(pk=pk)
                barrier.wait(timeout=10)
                try:
                    moderate_submission(submission=proposal, actor=actor, decision='approve', reason='Independent privacy and locality review.')
                    return 'published'
                except ValidationError:
                    return 'review_duplicate'
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(approve, [item.pk for item in proposals]))
        self.assertCountEqual(outcomes, ['published', 'review_duplicate'])
        self.assertEqual(PPA.objects.filter(name='Concurrent New School').count(), 1)
        self.assertEqual(PPASubmission.objects.filter(publication_status='approved').count(), 1)
