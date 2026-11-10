from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from locations.models import LGA, State
from .admin import PPAAdmin, PPAAdminForm, PlaceReportAdmin, PlaceReportAdminForm, ReviewAdmin, ReviewAdminForm
from .models import Bookmark, Confirmation, PPA, PlaceChangeLog, PlaceReport, ReportChangeLog, Review, ReviewChangeLog

@override_settings(ROOT_URLCONF='config.urls')
class CommunityTrustTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.user = get_user_model().objects.create_user(state_code='LA/COMMUNITY', email='member@example.com', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='LA/OTHER', email='other@example.com')
        cls.ppa = PPA.objects.create(name='Community School', address='Independently contributed address', state=cls.state, lga=cls.lga, source=PPA.Source.COMMUNITY)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_confirmation_requires_login_post_csrf_and_verified_email(self):
        url = reverse('confirm_ppa', args=[self.ppa.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(url, {'reported_here': 'on'}).status_code, 403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, {'reported_here': 'on'}).status_code, 302)
        self.assertEqual(Confirmation.objects.count(), 0)
        self.client.logout()
        self.assertEqual(self.client.post(url, {'reported_here': 'on'}).status_code, 302)

    def test_confirmation_is_unique_tracks_history_and_updates_status(self):
        url = reverse('confirm_ppa', args=[self.ppa.pk])
        self.client.post(url, {'reported_here': 'on', 'note': 'I reported at this entrance.'})
        self.client.post(url, {'reported_here': 'on', 'note': 'Still current.'})
        self.assertEqual(Confirmation.objects.filter(ppa=self.ppa).count(), 1)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.status, PPA.Status.MEMBER_CONFIRMED)
        self.assertIsNotNone(self.ppa.last_confirmed_at)
        self.assertEqual(PlaceChangeLog.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Confirmation.objects.create(ppa=self.ppa, user=self.user)

    def test_confirmation_does_not_erase_disputed_or_closed_status(self):
        for status in (PPA.Status.DISPUTED, PPA.Status.CLOSED):
            self.ppa.status = status
            self.ppa.save()
            self.client.post(reverse('confirm_ppa', args=[self.ppa.pk]), {'reported_here': 'on'})
            self.ppa.refresh_from_db()
            self.assertEqual(self.ppa.status, status)

    def test_confirmation_requires_explicit_reported_here_declaration(self):
        self.client.post(reverse('confirm_ppa', args=[self.ppa.pk]), {'note': 'A school exists here.'})
        self.assertFalse(Confirmation.objects.exists())

    def test_reports_allow_unverified_accounts_without_overwriting_destination(self):
        self.client.force_login(self.other)
        url = reverse('report_ppa', args=[self.ppa.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        result = self.client.post(url, {'category': 'wrong_pin', 'details': 'The entrance is on the other road.', 'proposed_correction': 'Use the north gate.'})
        self.assertEqual(result.status_code, 302)
        report = PlaceReport.objects.get()
        self.assertEqual(report.status, PlaceReport.Status.OPEN)
        self.assertEqual(report.reporter, self.other)
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.address, 'Independently contributed address')
        self.assertEqual(self.ppa.status, PPA.Status.DISCOVERED)

    def test_reports_validate_content_and_review_ownership(self):
        url = reverse('report_ppa', args=[self.ppa.pk])
        self.assertEqual(self.client.post(url, {'category': 'not-a-category', 'details': 'x'}).status_code, 400)
        self.assertEqual(self.client.post(url, {'category': 'wrong_pin', 'details': 'x' * 2001}).status_code, 400)
        self.assertEqual(self.client.post(url, {'category': 'review_abuse', 'details': 'Abusive'}).status_code, 400)
        another = PPA.objects.create(name='Other', address='Address', state=self.state, lga=self.lga)
        review = Review.objects.create(ppa=another, user=self.user, rating=4, comment='Review')
        self.assertEqual(self.client.post(url, {'category': 'review_abuse', 'details': 'Abusive', 'review_id': review.pk}).status_code, 404)
        self.assertFalse(PlaceReport.objects.exists())

    def test_report_form_prefills_and_validates_review_link(self):
        review = Review.objects.create(ppa=self.ppa, user=self.user, rating=4, comment='Review under investigation')
        url = reverse('report_ppa', args=[self.ppa.pk])
        response = self.client.get(url, {'category': 'review_abuse', 'review': review.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['form'].initial['review_id'], review.pk)
        self.assertEqual(response.context['form'].initial['category'], 'review_abuse')
        self.assertContains(response, 'Review under investigation')
        self.assertEqual(self.client.get(url, {'review': 'not-an-id'}).status_code, 400)
        self.assertEqual(self.client.get(url, {'category': 'invalid'}).status_code, 400)
        review.is_public = False
        review.save()
        self.assertEqual(self.client.get(url, {'review': review.pk}).status_code, 404)
        self.assertEqual(self.client.post(url, {'category': 'review_abuse', 'details': 'Hidden review', 'review_id': review.pk}).status_code, 404)
        another = PPA.objects.create(name='Other school', address='Other address', state=self.state, lga=self.lga)
        self.assertEqual(self.client.get(reverse('report_ppa', args=[another.pk]), {'review': review.pk}).status_code, 404)

    def test_submitted_report_history_is_private_and_excludes_moderator_notes(self):
        PlaceReport.objects.create(ppa=self.ppa, reporter=self.user, category='wrong_pin', details='My submitted correction',
                                   status='investigating', moderator_note='Private investigation contact')
        PlaceReport.objects.create(ppa=self.ppa, reporter=self.other, category='other', details='Another member private report')
        response = self.client.get(reverse('my_reports'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My submitted correction')
        self.assertContains(response, 'Being investigated')
        self.assertNotContains(response, 'Private investigation contact')
        self.assertNotContains(response, 'Another member private report')
        self.assertNotIn('moderator_note', response.context['reports'][0])
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('my_reports')).status_code, 302)

    def test_report_throttling(self):
        url = reverse('report_ppa', args=[self.ppa.pk])
        for _ in range(10):
            self.assertEqual(self.client.post(url, {'category': 'other', 'details': 'Please check.'}).status_code, 302)
        self.assertEqual(self.client.post(url, {'category': 'other', 'details': 'Please check.'}).status_code, 429)

    def test_bookmarks_are_private_and_idempotent(self):
        url = reverse('bookmark_ppa', args=[self.ppa.pk])
        for _ in range(2):
            self.client.post(url, {'action': 'save'})
        self.assertEqual(Bookmark.objects.count(), 1)
        self.assertContains(self.client.get(reverse('saved_ppas')), 'Community School')
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('saved_ppas')), 'Community School')
        self.client.post(url, {'action': 'remove'})
        self.assertEqual(Bookmark.objects.count(), 1)
        self.client.force_login(self.user)
        self.client.post(url, {'action': 'remove'})
        self.assertFalse(Bookmark.objects.exists())


class ModerationAuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(name='Ikeja', state=cls.state)
        cls.user = get_user_model().objects.create_user(state_code='MODERATOR', email='moderator@example.com', is_staff=True)
        cls.user.user_permissions.add(Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_reviews'))
        cls.ppa = PPA.objects.create(name='School', address='Address', state=cls.state, lga=cls.lga)

    def request(self):
        request = RequestFactory().post('/admin/')
        request.user = self.user
        return request

    def place_data(self, **changes):
        return {'name': self.ppa.name, 'address': self.ppa.address, 'state': self.state.pk, 'lga': self.lga.pk,
                'category': 'Public Sector', 'status': self.ppa.status, 'location_status': self.ppa.location_status,
                'source': self.ppa.source, **changes}

    def test_destination_change_requires_reason_and_records_diff(self):
        form = PPAAdminForm(self.place_data(address='Corrected entrance'), instance=self.ppa)
        self.assertFalse(form.is_valid())
        self.assertIn('moderation_reason', form.errors)
        self.ppa.refresh_from_db()
        form = PPAAdminForm(self.place_data(address='Corrected entrance', moderation_reason='Checked with organisation.'), instance=self.ppa)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        PPAAdmin(PPA, admin.site).save_model(self.request(), obj, form, True)
        log = PlaceChangeLog.objects.get()
        self.assertEqual(log.changes['address'], {'from': 'Address', 'to': 'Corrected entrance'})
        self.assertEqual(log.actor, self.user)

    def test_category_change_is_included_in_moderation_history(self):
        form = PPAAdminForm(self.place_data(category='Healthcare', moderation_reason='Checked the organisation category.'), instance=self.ppa)
        self.assertTrue(form.is_valid(), form.errors)
        PPAAdmin(PPA, admin.site).save_model(self.request(), form.save(commit=False), form, True)
        self.assertEqual(PlaceChangeLog.objects.get().changes['category']['to'], 'Healthcare')

    def test_organisation_confirmation_requires_evidence(self):
        form = PPAAdminForm(self.place_data(status=PPA.Status.ORGANISATION_CONFIRMED, moderation_reason='Promotion requested.'), instance=self.ppa)
        self.assertFalse(form.is_valid())
        self.assertIn('organisation_evidence', form.errors)

    def test_report_resolution_is_audited(self):
        report = PlaceReport.objects.create(ppa=self.ppa, reporter=self.user, category='wrong_pin', details='Please check')
        data = {'ppa': self.ppa.pk, 'reporter': self.user.pk, 'category': 'wrong_pin', 'details': 'Please check', 'status': 'resolved'}
        form = PlaceReportAdminForm(data, instance=report)
        self.assertFalse(form.is_valid())
        self.assertIn('moderator_note', form.errors)
        report.refresh_from_db()
        form = PlaceReportAdminForm({**data, 'moderator_note': 'Entrance checked; corrected in place history.'}, instance=report)
        self.assertTrue(form.is_valid(), form.errors)
        PlaceReportAdmin(PlaceReport, admin.site).save_model(self.request(), form.save(commit=False), form, True)
        self.assertEqual(ReportChangeLog.objects.get().new_status, 'resolved')
        self.ppa.refresh_from_db()
        self.assertEqual(self.ppa.address, 'Address')

    def test_moderation_admin_has_no_list_edit_or_delete_bypass(self):
        for model, admin_class in ((PPA, PPAAdmin), (PlaceReport, PlaceReportAdmin), (Review, ReviewAdmin)):
            model_admin = admin_class(model, admin.site)
            self.assertEqual(model_admin.list_editable, ())
            self.assertIsNone(model_admin.actions)
            self.assertFalse(model_admin.has_delete_permission(self.request()))
        self.assertIn('details', PlaceReportAdmin.readonly_fields)
        self.assertIn('proposed_correction', PlaceReportAdmin.readonly_fields)
        self.assertIn('comment', ReviewAdmin.readonly_fields)

    def test_hiding_review_requires_reason_and_audit(self):
        review = Review.objects.create(ppa=self.ppa, user=self.user, rating=4, comment='Content')
        data = {'ppa': self.ppa.pk, 'user': self.user.pk, 'rating': 4, 'comment': 'Content', 'is_public': False}
        form = ReviewAdminForm(data, instance=review)
        self.assertFalse(form.is_valid())
        review.refresh_from_db()
        form = ReviewAdminForm({**data, 'moderation_note': 'Contains private contact details.'}, instance=review)
        self.assertTrue(form.is_valid(), form.errors)
        ReviewAdmin(Review, admin.site).save_model(self.request(), form.save(commit=False), form, True)
        self.assertFalse(ReviewChangeLog.objects.get().new_visibility)
