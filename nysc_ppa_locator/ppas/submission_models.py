"""Independent PPA proposals; review never overwrites the existing directory."""
import math
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from locations.models import LGA

from .models import PPA, PPAClaim, claim_expiry


def normalize_submission_name(value):
    return ' '.join(value.casefold().split())


class PPASubmission(models.Model):
    class Publication(models.TextChoices):
        DRAFT = 'draft', 'Private draft'
        PENDING = 'pending', 'Awaiting review'
        APPROVED = 'approved', 'Approved for the directory'
        REJECTED = 'rejected', 'Not approved'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='independent_ppa_submissions')
    state = models.ForeignKey('locations.State', on_delete=models.PROTECT)
    lga = models.ForeignKey('locations.LGA', on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=510, editable=False)
    address = models.TextField(max_length=2000, blank=True)
    landmark = models.CharField(max_length=500, blank=True)
    entrance_instructions = models.TextField(max_length=1000, blank=True)
    category = models.CharField(max_length=50, choices=PPA.CATEGORY_CHOICES)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    source_type = models.CharField(max_length=24, choices=PPAClaim.Source.choices, default=PPAClaim.Source.MEMBER)
    source_url = models.URLField(max_length=500, blank=True)
    observed_on = models.DateField()
    checked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=claim_expiry)
    verification_status = models.CharField(max_length=24, choices=PPAClaim.Verification.choices, default=PPAClaim.Verification.COMMUNITY)
    publication_status = models.CharField(max_length=16, choices=Publication.choices, default=Publication.DRAFT)
    # Explicit typed visibility metadata keeps draft-only/withdrawn history out
    # of moderation, including a private edit of a previously submitted item.
    shared_for_review = models.BooleanField(default=False, editable=False)
    published_ppa = models.ForeignKey(PPA, null=True, blank=True, on_delete=models.PROTECT, related_name='independent_proposals')
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_ppa_submissions')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-pk']
        permissions = [('can_moderate_submissions', 'Can review independent PPA proposals')]
        indexes = [
            models.Index(fields=['publication_status', '-created_at'], name='submission_review_queue_idx'),
            models.Index(fields=['author', '-updated_at'], name='submission_author_date_idx'),
            models.Index(fields=['state', 'lga', 'normalized_name'], name='submission_location_name_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['author', 'state', 'lga', 'normalized_name'],
                                    condition=models.Q(publication_status__in=['draft', 'pending']),
                                    name='one_active_member_ppa_proposal'),
            models.CheckConstraint(condition=(models.Q(latitude__isnull=True, longitude__isnull=True)
                                              | models.Q(latitude__isnull=False, longitude__isnull=False,
                                                         latitude__gte=-90, latitude__lte=90,
                                                         longitude__gte=-180, longitude__lte=180)),
                                   name='submission_coordinate_pair'),
            models.CheckConstraint(condition=~models.Q(publication_status='draft', shared_for_review=True),
                                   name='submission_drafts_private'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.name = ' '.join(self.name.split())
        self.normalized_name = normalize_submission_name(self.name)
        if not self.name:
            errors['name'] = 'Enter the organisation name you know independently.'
        if not (self.address.strip() or self.landmark.strip()):
            errors['address'] = 'Provide an independently known address or public landmark.'
        if self.state_id and self.lga_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            errors['lga'] = 'Choose an LGA in the selected state.'
        if (self.latitude is None) != (self.longitude is None):
            errors['latitude'] = 'Supply both destination coordinates, or leave both blank.'
        for name, limit in [('latitude', 90), ('longitude', 180)]:
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or not -limit <= value <= limit):
                errors[name] = 'Use a finite destination coordinate within the valid range.'
        if self.observed_on and self.observed_on > timezone.localdate():
            errors['observed_on'] = 'The observation date cannot be in the future.'
        if self.checked_at and self.checked_at > timezone.now():
            errors['checked_at'] = 'The checked time cannot be in the future.'
        if self.checked_at and self.expires_at and self.expires_at <= self.checked_at:
            errors['expires_at'] = 'Reconfirmation must fall after the supporting evidence was checked.'
        if (self.verification_status == PPAClaim.Verification.COMMUNITY and self.observed_on and self.expires_at
                and timezone.localtime(self.expires_at).date() > self.observed_on + timedelta(days=180)):
            errors['expires_at'] = 'An unverified observation needs author reconfirmation within 180 days; do not extend its freshness without new evidence.'
        if self.source_url:
            try:
                url = urlsplit(self.source_url)
                if (url.scheme.lower() not in ('http', 'https') or not url.hostname
                        or url.username is not None or url.password is not None
                        or any(ord(c) < 32 or ord(c) == 127 for c in self.source_url)):
                    raise ValueError
            except ValueError:
                errors['source_url'] = 'Use a public HTTP(S) page without credentials or control characters.'
        if self.verification_status != PPAClaim.Verification.COMMUNITY:
            if not self.source_url:
                errors['source_url'] = 'Independent verification needs a traceable supporting page.'
            if not self.checked_at:
                errors['checked_at'] = 'Record when the evidence was checked.'
            if not self.moderator_note.strip():
                errors['moderator_note'] = 'Record the private evidence supporting independent verification.'
        if self.source_type == PPAClaim.Source.OFFICIAL and not self.source_url:
            errors['source_url'] = 'Attributed official material requires a traceable source.'
        if self.verification_status == PPAClaim.Verification.OFFICIAL and self.source_type != PPAClaim.Source.OFFICIAL:
            errors['source_type'] = 'Official-source confirmation needs attributable official material.'
        if self.verification_status == PPAClaim.Verification.ORGANISATION and self.source_type != PPAClaim.Source.ORGANISATION:
            errors['source_type'] = 'Organisation confirmation must identify an organisation-reported source.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'Private PPA proposal #{self.pk or "new"}'


class SubmissionRevision(models.Model):
    submission = models.ForeignKey(PPASubmission, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    published_ppa = models.ForeignKey(PPA, null=True, blank=True, on_delete=models.PROTECT, related_name='independent_publication_revisions')
    published_as_new = models.BooleanField(default=False)
    publication_identity = models.CharField(max_length=510, blank=True, default='', editable=False, db_index=True)
    snapshot = models.JSONField(default=dict)
    shared_for_review = models.BooleanField(default=False)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'Private PPA proposal revision #{self.pk}'
