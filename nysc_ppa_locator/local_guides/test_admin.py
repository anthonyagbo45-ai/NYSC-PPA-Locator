"""Real OTP administration regressions for private drafts and scoped moderation."""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from locations.models import LGA, State
from .admin import ListingAdmin, ListingReportAdmin, ListingRevisionAdmin
from .models import Listing, ListingBookmark, ListingReport, ListingReportRevision, ListingRevision
from .services import archive_listing, moderate_listing, moderate_report, save_listing, save_report


class LocalGuideAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Local Guide Admin State')
        cls.lga = LGA.objects.create(name='Local Guide Admin LGA', state=cls.state)
        users = get_user_model().objects
        cls.author = users.create_user(state_code='GUIDE/AUTHOR', email='guide-author@example.com', email_verified=True)
        cls.moderator = users.create_user(state_code='GUIDE/MODERATOR', email='guide-mod@example.com', is_staff=True, email_verified=True)
        cls.report_moderator = users.create_user(state_code='GUIDE/REPORT', email='guide-report@example.com', is_staff=True, email_verified=True)
        cls.verifier = users.create_user(state_code='GUIDE/VERIFIER', email='guide-verifier@example.com', is_staff=True, email_verified=True)
        cls.supervisor = users.create_superuser(state_code='GUIDE/SUPERVISOR', email='guide-supervisor@example.com')
        permission = lambda code: Permission.objects.get(content_type__app_label='local_guides', codename=code)
        cls.moderator.user_permissions.add(permission('can_moderate_listings'))
        cls.report_moderator.user_permissions.add(permission('can_moderate_listing_reports'))
        cls.verifier.user_permissions.add(permission('can_moderate_listings'), permission('can_verify_listings'))

    def setUp(self):
        cache.clear()

    def listing_data(self, **changes):
        return {
            'kind': Listing.Kind.SERVICE, 'title': 'Public document service',
            'area': 'Town centre', 'summary': 'Printing guidance from personal experience.',
            'state': self.state, 'lga': self.lga, 'ppa': None,
            'source_type': Listing.Source.MEMBER, 'source_url': '',
            'observed_on': timezone.localdate(), **changes,
        }

    def contribute(self, *, submit=True, **changes):
        return save_listing(user=self.author, cleaned_data=self.listing_data(**changes),
                            details={'category': 'printing', 'opening_hours': 'Daytime', 'public_business_address': ''}, submit=submit)

    def approve(self, listing, **changes):
        return moderate_listing(listing=listing, actor=self.moderator,
                                values={'publication_status': Listing.Publication.APPROVED, **changes},
                                reason='Reviewed relevance and privacy; publication is not verification.')

    def otp_client(self, user=None):
        user = user or self.moderator
        client = Client()
        client.force_login(user)
        device = TOTPDevice.objects.create(user=user, name='Local guide privacy regression', confirmed=True)
        session = client.session
        session['otp_device_id'] = device.persistent_id
        session.save()
        return client

    def listed_ids(self, client, model):
        response = client.get(reverse(f'admin:local_guides_{model}_changelist'))
        self.assertEqual(response.status_code, 200)
        return set(response.context['cl'].queryset.values_list('pk', flat=True))

    def assert_hidden(self, client, listing, marker):
        self.assertNotIn(listing.pk, self.listed_ids(client, 'listing'))
        revisions = list(listing.revisions.all())
        self.assertFalse(set(r.pk for r in revisions) & self.listed_ids(client, 'listingrevision'))
        for model, records in [('listing', [listing]), ('listingrevision', revisions)]:
            for record in records:
                response = client.get(reverse(f'admin:local_guides_{model}_change', args=[record.pk]), follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, marker)

    def test_otp_is_required_before_a_staff_moderator_can_read_the_queue(self):
        pending = self.contribute(summary='Submitted moderation context')
        client = Client()
        client.force_login(self.moderator)
        response = client.get(reverse('admin:local_guides_listing_change', args=[pending.pk]))
        self.assertNotEqual(response.status_code, 200)
        self.assertNotContains(response, pending.summary, status_code=response.status_code)
        self.assertContains(self.otp_client().get(reverse('admin:local_guides_listing_change', args=[pending.pk])), pending.summary)

    def test_current_and_archived_private_drafts_stay_private_and_history_is_preserved(self):
        marker = 'PRIVATE NEVER SUBMITTED GUIDE'
        listing = self.contribute(submit=False, summary=marker)
        original = listing.revisions.get()
        before = dict(original.snapshot)
        client = self.otp_client()
        self.assert_hidden(client, listing, marker)
        listing = archive_listing(listing=listing, user=self.author)
        self.assert_hidden(client, listing, marker)
        original.refresh_from_db()
        self.assertEqual(original.snapshot, before)
        self.assertEqual(listing.revisions.count(), 2)

    def test_later_submission_does_not_share_earlier_private_or_unknown_snapshots(self):
        marker = 'EARLIER PRIVATE GUIDE WORDING'
        listing = self.contribute(submit=False, summary=marker)
        private = listing.revisions.get()
        listing = save_listing(user=self.author, listing=listing,
                               cleaned_data=self.listing_data(summary='Submitted public guidance'),
                               details={}, submit=True)
        submitted = listing.revisions.first()
        unknown = ListingRevision.objects.create(listing=listing, actor=self.author,
                                                 snapshot={'summary': 'UNKNOWN PRIVATE LEGACY TEXT'}, reason='Retained legacy snapshot.')
        client = self.otp_client()
        self.assertEqual(self.listed_ids(client, 'listingrevision'), {submitted.pk})
        for revision in (private, unknown):
            response = client.get(reverse('admin:local_guides_listingrevision_change', args=[revision.pk]), follow=True)
            self.assertNotContains(response, marker)
            self.assertNotContains(response, 'UNKNOWN PRIVATE LEGACY TEXT')
        self.assertContains(client.get(reverse('admin:local_guides_listingrevision_change', args=[submitted.pk])), 'Submitted public guidance')

    def test_archiving_an_unsubmitted_replacement_does_not_share_its_contents(self):
        listing = self.contribute(summary='Previously submitted public guidance')
        listing = save_listing(user=self.author, listing=listing,
                               cleaned_data=self.listing_data(summary='PRIVATE UNSUBMITTED REPLACEMENT'),
                               details={}, submit=False)
        listing = archive_listing(listing=listing, user=self.author)
        self.assert_hidden(self.otp_client(), listing, 'PRIVATE UNSUBMITTED REPLACEMENT')
        self.assertEqual(listing.revisions.count(), 3)

    def test_unknown_latest_visibility_does_not_expose_an_archived_snapshot(self):
        listing = self.contribute()
        submitted = listing.revisions.get()
        listing.summary = 'UNKNOWN PRIVATE HISTORICAL GUIDE'
        listing.save(update_fields=['summary'])
        ListingRevision.objects.create(listing=listing, actor=self.author, snapshot={'summary': listing.summary}, reason='Unknown historical visibility.')
        listing = archive_listing(listing=listing, user=self.author)
        client = self.otp_client()
        self.assert_hidden(client, listing, listing.summary)
        # Retained uncertain archive snapshots stay private after a later explicit
        # submitted state; the prior safely submitted snapshot remains reviewable.
        Listing.objects.filter(pk=listing.pk).update(publication_status=Listing.Publication.PENDING)
        self.assertEqual(self.listed_ids(client, 'listingrevision'), {submitted.pk})

    def test_submitted_archives_keep_audits_and_superuser_access_to_drafts_is_readonly(self):
        listing = self.approve(self.contribute())
        listing = archive_listing(listing=listing, user=self.author)
        client = self.otp_client()
        self.assertIn(listing.pk, self.listed_ids(client, 'listing'))
        self.assertEqual(self.listed_ids(client, 'listingrevision'), set(listing.revisions.values_list('pk', flat=True)))
        private = self.contribute(submit=False, title='Private second draft', summary='PRIVATE SUPERVISOR CONTEXT')
        supervisor = self.otp_client(self.supervisor)
        for model, pk in [('listing', private.pk), ('listingrevision', private.revisions.get().pk)]:
            response = supervisor.get(reverse(f'admin:local_guides_{model}_change', args=[pk]))
            self.assertContains(response, private.summary)
            self.assertFalse(response.context.get('has_change_permission', True))

    def moderation_data(self, listing, **changes):
        expiry = timezone.localtime(listing.expires_at)
        return {
            'source_type': listing.source_type, 'source_url': listing.source_url,
            'publication_status': Listing.Publication.APPROVED,
            'verification_status': Listing.Verification.COMMUNITY,
            'checked_at_0': '', 'checked_at_1': '',
            'expires_at_0': expiry.strftime('%Y-%m-%d'), 'expires_at_1': expiry.strftime('%H:%M:%S'),
            'moderator_note': '', 'moderation_reason': 'Reviewed the submitted information and privacy.', **changes,
        }

    def test_moderation_form_ignores_forged_author_content_geography_and_details(self):
        listing = self.contribute()
        request = RequestFactory().post('/admin/')
        request.user = self.moderator
        model_admin = ListingAdmin(Listing, admin.site)
        form_class = model_admin.get_form(request, listing)
        for field in ('author', 'state', 'lga', 'ppa', 'kind', 'title', 'summary', 'listing_details'):
            self.assertNotIn(field, form_class.base_fields)
        form = form_class(self.moderation_data(listing, title='FORGED TITLE', author=self.moderator.pk,
                                              summary='FORGED SUMMARY', kind='housing'), instance=listing)
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.instance, form, change=True)
        listing.refresh_from_db()
        self.assertEqual(listing.title, 'Public document service')
        self.assertEqual(listing.author_id, self.author.pk)
        self.assertEqual(listing.kind, Listing.Kind.SERVICE)
        self.assertEqual(listing.publication_status, Listing.Publication.APPROVED)
        self.assertEqual(listing.revisions.count(), 2)

    def test_moderation_form_and_save_hook_both_require_separate_verifier(self):
        listing = self.contribute()
        request = RequestFactory().post('/admin/')
        request.user = self.moderator
        model_admin = ListingAdmin(Listing, admin.site)
        now = timezone.localtime()
        form = model_admin.get_form(request, listing)(self.moderation_data(
            listing, verification_status=Listing.Verification.CHECKED,
            source_url='https://example.org/public-directory', moderator_note='Compared public source information.',
            checked_at_0=now.strftime('%Y-%m-%d'), checked_at_1=now.strftime('%H:%M:%S'),
        ), instance=listing)
        self.assertFalse(form.is_valid())
        listing.verification_status = Listing.Verification.CHECKED
        listing.source_url = 'https://example.org/public-directory'
        listing.checked_at = timezone.now()
        listing.moderator_note = 'Compared source.'
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, listing, SimpleNamespace(cleaned_data={'moderation_reason': 'Attempted privilege escalation.'}), change=True)
        listing.refresh_from_db()
        self.assertEqual(listing.verification_status, Listing.Verification.COMMUNITY)
        self.assertEqual(listing.revisions.count(), 1)

    def test_whitespace_moderation_reason_is_rejected_without_saving(self):
        listing = self.contribute()
        request = RequestFactory().post('/admin/')
        request.user = self.moderator
        form = ListingAdmin(Listing, admin.site).get_form(request, listing)(
            self.moderation_data(listing, moderation_reason='   '), instance=listing,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('moderation_reason', form.errors)
        self.assertEqual(listing.revisions.count(), 1)

    def test_publication_only_review_preserves_verified_datetime_precision(self):
        listing = moderate_listing(listing=self.contribute(), actor=self.verifier, values={
            'publication_status': Listing.Publication.APPROVED,
            'verification_status': Listing.Verification.CHECKED,
            'source_url': 'https://example.org/public-directory',
            'checked_at': (timezone.now() - timedelta(seconds=1)).replace(microsecond=123456),
            'moderator_note': 'Compared the public source without establishing NYSC endorsement.',
        }, reason='Independent supporting source checked.')
        checked, expiry = listing.checked_at, listing.expires_at
        request = RequestFactory().post('/admin/')
        request.user = self.moderator
        checked_local = timezone.localtime(checked)
        model_admin = ListingAdmin(Listing, admin.site)
        form = model_admin.get_form(request, listing)(self.moderation_data(
            listing, publication_status=Listing.Publication.REJECTED,
            verification_status=Listing.Verification.CHECKED,
            moderator_note=listing.moderator_note,
            checked_at_0=checked_local.strftime('%Y-%m-%d'),
            checked_at_1=checked_local.strftime('%H:%M:%S'),
        ), instance=listing)
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.instance, form, change=True)
        listing.refresh_from_db()
        self.assertEqual(listing.publication_status, Listing.Publication.REJECTED)
        self.assertEqual(listing.checked_at, checked)
        self.assertEqual(listing.expires_at, expiry)
        self.assertEqual(listing.verification_status, Listing.Verification.CHECKED)

    def test_report_moderation_is_a_separate_permission_and_audits_are_readonly(self):
        listing = self.approve(self.contribute())
        report = save_report(user=self.author, listing=listing, category=ListingReport.Category.PRIVACY,
                             details='PRIVATE SAFETY REPORT', proposed_correction='Check publication consent.')
        report = moderate_report(report=report, actor=self.report_moderator, status=ListingReport.Status.INVESTIGATING,
                                 moderator_note='PRIVATE INVESTIGATION CONTEXT')
        revision = report.revisions.get()
        listing_mod = self.otp_client()
        for model, pk in [('listingreport', report.pk), ('listingreportrevision', revision.pk)]:
            response = listing_mod.get(reverse(f'admin:local_guides_{model}_change', args=[pk]))
            self.assertEqual(response.status_code, 403)
            self.assertNotContains(response, 'PRIVATE', status_code=403)
        report_mod = self.otp_client(self.report_moderator)
        response = report_mod.get(reverse('admin:local_guides_listingreport_change', args=[report.pk]))
        self.assertContains(response, report.details)
        self.assertContains(response, report.moderator_note)
        response = report_mod.get(reverse('admin:local_guides_listingreportrevision_change', args=[revision.pk]))
        self.assertContains(response, 'PRIVATE INVESTIGATION CONTEXT')
        self.assertFalse(response.context.get('has_change_permission', True))
        self.assertEqual(report_mod.get(reverse('admin:local_guides_listing_change', args=[listing.pk])).status_code, 403)
        self.assertEqual(ListingReportRevision.objects.count(), 1)

    def test_reports_do_not_disclose_a_later_private_author_title(self):
        listing = self.approve(self.contribute())
        report = save_report(user=self.author, listing=listing, category=ListingReport.Category.WRONG_INFO,
                             details='Please check the originally published opening hours.')
        marker = 'PRIVATE FUTURE BUSINESS TITLE'
        listing = save_listing(user=self.author, listing=listing,
                               cleaned_data=self.listing_data(title=marker, summary='Unsubmitted replacement details.'),
                               details={}, submit=False)
        client = self.otp_client(self.report_moderator)
        list_url = reverse('admin:local_guides_listingreport_changelist')
        for response in (
                client.get(list_url),
                client.get(reverse('admin:local_guides_listingreport_change', args=[report.pk]))):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, marker)
            self.assertContains(response, f'Local entry #{listing.pk}')
        searched = client.get(list_url, {'q': marker})
        self.assertEqual(searched.status_code, 200)
        self.assertFalse(searched.context['cl'].queryset.filter(pk=report.pk).exists())
        listing.refresh_from_db()
        self.assertEqual(listing.title, marker)
        self.assertEqual(listing.publication_status, Listing.Publication.DRAFT)

    def test_no_operator_add_delete_or_bookmark_browsing_routes_are_granted(self):
        listing = self.contribute()
        request = RequestFactory().get('/admin/')
        request.user = self.supervisor
        for model, model_admin in [
            (Listing, ListingAdmin(Listing, admin.site)),
            (ListingRevision, ListingRevisionAdmin(ListingRevision, admin.site)),
            (ListingReport, ListingReportAdmin(ListingReport, admin.site)),
        ]:
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request, listing if model is Listing else None))
        self.assertNotIn(ListingBookmark, admin.site._registry)
