from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from locations.models import State, LGA
from service_year.models import MemberPPA
from .models import PPA, PPAClaim, Review, ReviewRevision
from .profile_services import save_member_claim, moderate_claim


class CheckpointOneIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name='Lagos')
        cls.lga = LGA.objects.create(state=cls.state, name='Ikeja')
        cls.ppa = PPA.objects.create(name='Integration School', state=cls.state, lga=cls.lga, address='School road', source='community')
        cls.member = get_user_model().objects.create_user(state_code='LA/26A/1001', email='member+case@example.invalid', full_name='Private legal name', display_name='Helpful member', email_verified=True)
        cls.other = get_user_model().objects.create_user(state_code='LA/26A/1002', email='other@example.invalid')
        cls.moderator = get_user_model().objects.create_user(state_code='LA/26A/1003', email='moderator@example.invalid', is_staff=True)
        cls.moderator.user_permissions.add(Permission.objects.get(content_type__app_label='ppas', codename='can_moderate_claims'))

    def test_explicit_assignment_reaches_both_dashboard_routes_and_stays_private(self):
        self.client.force_login(self.member)
        detail = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertContains(detail, 'Save as my PPA')
        response = self.client.post(f'/service-year/assign/{self.ppa.pk}/', {'confirm_assignment': 'on'})
        self.assertEqual(response.status_code, 302)
        assignment = MemberPPA.objects.get(user=self.member, ended_at__isnull=True)
        for path in ('/dashboard/', '/service-year/'):
            self.assertContains(self.client.get(path), self.ppa.name)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/service-year/tracker/{assignment.pk}/').status_code, 404)
        self.assertNotContains(self.client.get('/service-year/'), 'Private legal name')

    def test_approved_context_renders_source_and_guarantee_labels_without_private_notes(self):
        claim = save_member_claim(ppa=self.ppa, user=self.member, cleaned_data={
            'topic': PPAClaim.Topic.SUPPORT, 'content': 'Member reports transport support',
            'source_type': PPAClaim.Source.MEMBER, 'source_url': '', 'observed_on': timezone.localdate(),
        }, submit=True)
        self.assertNotContains(self.client.get(f'/ppas/{self.ppa.pk}/'), 'Member reports transport support')
        moderate_claim(claim=claim, actor=self.moderator, values={
            'publication_status': PPAClaim.Publication.APPROVED, 'moderator_note': 'Private moderator evidence marker',
        }, reason='Reviewed for publication only')
        response = self.client.get(f'/ppas/{self.ppa.pk}/')
        self.assertContains(response, 'Member reports transport support')
        self.assertContains(response, 'not independently checked')
        self.assertNotContains(response, 'Private moderator evidence marker')
        self.assertNotContains(response, self.member.email)
        self.assertNotContains(response, self.member.state_code)

    def test_review_edit_uses_revision_service_and_member_can_reach_hidden_history(self):
        self.client.force_login(self.member)
        url = f'/ppas/{self.ppa.pk}/'
        self.assertEqual(self.client.post(url, {'rating': 4, 'comment': 'First experience', 'topic_tags': ['hours']}).status_code, 302)
        review = Review.objects.get(user=self.member, ppa=self.ppa)
        review.is_public = False
        review.save(update_fields=['is_public'])
        self.assertEqual(self.client.post(url, {'rating': 5, 'comment': 'Second experience', 'topic_tags': ['duties']}).status_code, 302)
        review.refresh_from_db()
        self.assertFalse(review.is_public)
        self.assertGreaterEqual(ReviewRevision.objects.filter(review=review).count(), 2)
        self.assertContains(self.client.get(url), f'/reviews/{review.pk}/history/')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/reviews/{review.pk}/history/').status_code, 404)
        self.assertNotContains(self.client.get(url), 'Second experience')


    def test_implicit_admin_labels_do_not_disclose_account_identifiers(self):
        review = Review.objects.create(ppa=self.ppa, user=self.member, rating=4, comment='Test')
        for label in (str(self.member), str(review)):
            self.assertNotIn(self.member.state_code, label)
            self.assertNotIn(self.member.email, label)
            self.assertNotIn(self.member.full_name, label)
