from datetime import timedelta
from urllib.parse import urlsplit

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from locations.models import State, LGA


def claim_expiry():
    return timezone.now() + timedelta(days=180)


class PPA(models.Model):
    class Status(models.TextChoices):
        DISCOVERED = 'discovered', 'Discovered — PPA relationship unconfirmed'
        MEMBER_CONFIRMED = 'member_confirmed', 'Corps member confirmed'
        ORGANISATION_CONFIRMED = 'organisation_confirmed', 'Organisation confirmed'
        DISPUTED = 'disputed', 'Information disputed'
        CLOSED = 'closed', 'Closed or moved'

    class Source(models.TextChoices):
        LEGACY = 'legacy', 'Existing record — source needs review'
        COMMUNITY = 'community', 'Independently contributed by a member'
        GOOGLE_REFERENCE = 'google_reference', 'Google Place ID reference'

    class LocationStatus(models.TextChoices):
        UNCONFIRMED = 'unconfirmed', 'Location assignment not checked'
        ADDRESS_CHECKED = 'address_checked', 'Address components match the selected area'
        MISMATCH = 'mismatch', 'Location may not match selected LGA'

    CATEGORY_CHOICES = [
        ('Education', 'Education'),
        ('Public Sector', 'Public Sector'),
        ('Private Sector', 'Private Sector'),
        ('NGO', 'NGO'),
        ('Healthcare', 'Healthcare'),
    ]

    source = models.CharField(max_length=24, choices=Source.choices, default=Source.LEGACY)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DISCOVERED, db_index=True)
    location_status = models.CharField(max_length=24, choices=LocationStatus.choices, default=LocationStatus.UNCONFIRMED)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='submitted_ppas')
    last_confirmed_at = models.DateTimeField(null=True, blank=True)
    organisation_evidence = models.TextField(max_length=2000, blank=True, help_text='Private moderator evidence for organisation confirmation; do not publish personal contacts.')
    aliases = models.CharField(max_length=500, blank=True, help_text='Independently contributed alternative names and abbreviations.')
    landmark = models.CharField(max_length=500, blank=True)
    entrance_instructions = models.TextField(max_length=1000, blank=True)

    place_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    name = models.CharField(max_length=255, db_index=True)
    state = models.ForeignKey(State, on_delete=models.CASCADE, db_index=True)
    lga = models.ForeignKey(LGA, on_delete=models.CASCADE, db_index=True)
    address = models.TextField()
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Public Sector')
    
    # Geographic & Navigation fields
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    google_maps_url = models.URLField(max_length=500, blank=True, null=True)
    
    # Contact & Verification
    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    description = models.TextField(blank=True, null=True, default='')
    verified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [('can_verify_organisations', 'Can verify organisation and attributable official-source claims')]
        indexes = [
            models.Index(fields=['state', 'lga']),
            models.Index(fields=['name']),
            models.Index(fields=['state', 'lga', 'name', 'id'], name='ppa_location_name_idx'),
            models.Index(fields=['category', 'name', 'id'], name='ppa_category_name_idx'),
        ]

    def clean(self):
        super().clean()
        if self.status == self.Status.ORGANISATION_CONFIRMED and not self.organisation_evidence.strip():
            raise ValidationError({'organisation_evidence': 'Record how the organisation confirmed its PPA relationship.'})
        if self.lga_id and self.state_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            raise ValidationError({'lga': 'Choose an LGA in the selected state.'})
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise ValidationError({'latitude': 'Latitude must be between -90 and 90.'})
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise ValidationError({'longitude': 'Longitude must be between -180 and 180.'})

    def __str__(self):
        return f"{self.name} ({self.lga.name}, {self.state.name})"


class Review(models.Model):
    TOPIC_CHOICES = [
        ('work_environment', 'Working environment'), ('hours', 'Working hours'),
        ('duties', 'Typical duties'), ('accessibility', 'Accessibility'),
        ('transport', 'Transport'), ('accommodation', 'Accommodation support'),
        ('support', 'Reported stipend or transport support'), ('acceptance', 'Reported acceptance'),
    ]
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey('accounts.CorpMember', on_delete=models.CASCADE)
    rating = models.PositiveIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(max_length=4000, blank=True)
    topic_tags = models.JSONField(default=list, blank=True)
    context_date = models.DateField(null=True, blank=True, help_text='Optional date of the experience; never a posting identifier.')
    is_public = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        permissions = [('can_moderate_reviews', 'Can hide or restore community reviews')]
        constraints = [
            models.UniqueConstraint(fields=['ppa', 'user'], name='unique_user_ppa_review'),
            models.CheckConstraint(condition=models.Q(rating__gte=1, rating__lte=5), name='review_rating_range'),
        ]
        indexes = [models.Index(fields=['ppa', '-created_at', '-id'], name='review_ppa_created_idx')]

    def clean(self):
        super().clean()
        allowed = dict(self.TOPIC_CHOICES)
        if (not isinstance(self.topic_tags, list) or len(self.topic_tags) > len(allowed)
                or any(not isinstance(tag, str) or tag not in allowed for tag in self.topic_tags)
                or len(set(self.topic_tags)) != len(self.topic_tags)):
            raise ValidationError({'topic_tags': 'Choose each review topic at most once.'})
        if self.context_date and self.context_date > timezone.localdate():
            raise ValidationError({'context_date': 'The experience date cannot be in the future.'})

    @property
    def topic_labels(self):
        labels = dict(self.TOPIC_CHOICES)
        return [labels[tag] for tag in self.topic_tags if tag in labels]

    def __str__(self):
        return f"Review #{self.pk or 'new'} for {self.ppa.name}"

class Confirmation(models.Model):
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='confirmations')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ppa_confirmations')
    confirmed_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['ppa', 'user'], name='unique_member_confirmation')]
        indexes = [models.Index(fields=['ppa', '-confirmed_at'], name='confirmation_ppa_date_idx')]

    def __str__(self):
        return f'Confirmation for {self.ppa.name}'


class Bookmark(models.Model):
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='bookmarks')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ppa_bookmarks')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['ppa', 'user'], name='unique_member_bookmark')]


class PlaceReport(models.Model):
    class Category(models.TextChoices):
        WRONG_PIN = 'wrong_pin', 'Wrong location pin'
        WRONG_LGA = 'wrong_lga', 'Wrong state or LGA'
        WRONG_BRANCH = 'wrong_branch', 'Wrong branch or entrance'
        DUPLICATE = 'duplicate', 'Duplicate place'
        CLOSED = 'closed', 'Closed or moved'
        MISLEADING = 'misleading', 'PPA relationship is incorrect'
        SCAM = 'scam', 'Scam or suspicious payment request'
        REVIEW_ABUSE = 'review_abuse', 'Abusive or misleading review'
        OTHER = 'other', 'Other correction'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='place_reports')
    review = models.ForeignKey(Review, null=True, blank=True, on_delete=models.SET_NULL, related_name='reports')
    category = models.CharField(max_length=24, choices=Category.choices)
    details = models.TextField(max_length=2000)
    proposed_correction = models.TextField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_place_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['status', '-created_at'], name='report_status_created_idx')]

    def clean(self):
        super().clean()
        if self.review_id and self.review.ppa_id != self.ppa_id:
            raise ValidationError({'review': 'The review must belong to this place.'})

    def __str__(self):
        return f'{self.get_category_display()}: {self.ppa.name}'


class PlaceChangeLog(models.Model):
    """Append-only in the admin: destination and moderation edits remain reviewable."""
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='change_history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    changes = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class ReportChangeLog(models.Model):
    report = models.ForeignKey(PlaceReport, on_delete=models.CASCADE, related_name='change_history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_status = models.CharField(max_length=16)
    new_status = models.CharField(max_length=16)
    note = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class ReviewChangeLog(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='moderation_history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_visibility = models.BooleanField()
    new_visibility = models.BooleanField()
    note = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class PPAClaim(models.Model):
    """One attributed contextual report. Publication never establishes verification."""

    class Topic(models.TextChoices):
        HOURS = 'hours', 'Typical working hours'
        DUTIES = 'duties', 'Typical duties'
        ENVIRONMENT = 'environment', 'Working environment'
        ACCESSIBILITY = 'accessibility', 'Accessibility'
        ACCOMMODATION = 'accommodation', 'Accommodation support'
        TRANSPORT = 'transport', 'Transport and getting around'
        ACCEPTANCE = 'acceptance', 'Reported corps-member acceptance'
        SUPPORT = 'support', 'Reported stipend or transport support'

    class Source(models.TextChoices):
        MEMBER = 'member', 'Member experience'
        ORGANISATION = 'organisation', 'Organisation-reported'
        EDITORIAL = 'editorial', 'Editorial note'
        OFFICIAL = 'official', 'Attributed official material'

    class Publication(models.TextChoices):
        DRAFT = 'draft', 'Private draft'
        PENDING = 'pending', 'Awaiting moderation'
        APPROVED = 'approved', 'Approved to publish'
        REJECTED = 'rejected', 'Not approved to publish'
        ARCHIVED = 'archived', 'Archived'

    class Verification(models.TextChoices):
        COMMUNITY = 'community', 'Community-reported — not independently checked'
        CHECKED = 'checked', 'Independently checked'
        ORGANISATION = 'organisation', 'Confirmed with the organisation'
        OFFICIAL = 'official', 'Confirmed against attributed official material'

    ppa = models.ForeignKey(PPA, on_delete=models.PROTECT, related_name='profile_claims')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='ppa_claims')
    topic = models.CharField(max_length=24, choices=Topic.choices)
    content = models.TextField(max_length=2000)
    source_type = models.CharField(max_length=24, choices=Source.choices, default=Source.MEMBER)
    source_url = models.URLField(max_length=500, blank=True, help_text='Optional public supporting page. Never enter private documents, personal contact details or links containing access tokens.')
    observed_on = models.DateField(help_text='When this information was personally observed or reported.')
    checked_at = models.DateTimeField(null=True, blank=True, help_text='When a reviewer last checked supporting evidence, not when publication was approved.')
    expires_at = models.DateTimeField(default=claim_expiry, help_text='Reconfirmation is needed after this time.')
    publication_status = models.CharField(max_length=16, choices=Publication.choices, default=Publication.DRAFT, db_index=True)
    verification_status = models.CharField(max_length=24, choices=Verification.choices, default=Verification.COMMUNITY)
    moderator_note = models.TextField(max_length=2000, blank=True, help_text='Private moderation/evidence notes; never shown to members or in public profiles.')
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_ppa_claims')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['topic', '-created_at', '-pk']
        permissions = [('can_moderate_claims', 'Can review and publish contextual PPA claims')]
        indexes = [
            models.Index(fields=['ppa', 'publication_status', 'topic'], name='claim_ppa_status_topic_idx'),
            models.Index(fields=['publication_status', 'expires_at'], name='claim_status_expiry_idx'),
            models.Index(fields=['author', '-created_at'], name='claim_author_created_idx'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.content.strip():
            errors['content'] = 'Describe the information being reported.'
        if self.source_url:
            try:
                source = urlsplit(self.source_url)
            except ValueError:
                errors['source_url'] = 'Use a valid public HTTP or HTTPS supporting page.'
            else:
                if source.scheme.lower() not in ('http', 'https'):
                    errors['source_url'] = 'Use a public HTTP or HTTPS supporting page.'
                if source.username is not None or source.password is not None:
                    errors['source_url'] = 'Public supporting links must not contain usernames or passwords.'
                if any(ord(character) < 32 or ord(character) == 127 for character in self.source_url):
                    errors['source_url'] = 'Public supporting links must not contain control characters.'
        if self.observed_on and self.observed_on > timezone.localdate():
            errors['observed_on'] = 'The observation date cannot be in the future.'
        if self.checked_at and self.checked_at > timezone.now():
            errors['checked_at'] = 'The checked time cannot be in the future.'
        if self.checked_at and self.expires_at and self.expires_at <= self.checked_at:
            errors['expires_at'] = 'Reconfirmation must fall after the evidence was checked.'
        if self.verification_status != self.Verification.COMMUNITY:
            if not self.source_url:
                errors['source_url'] = 'Record an attributable public supporting source before independently confirming a claim.'
            if not self.checked_at:
                errors['checked_at'] = 'Record when the evidence was checked.'
        if self.source_type == self.Source.OFFICIAL and not self.source_url:
            errors['source_url'] = 'Attributed official material requires a traceable public source.'
        if self.verification_status == self.Verification.OFFICIAL and self.source_type != self.Source.OFFICIAL:
            errors['source_type'] = 'Official-source confirmation requires attributable official material.'
        if self.verification_status == self.Verification.ORGANISATION and self.source_type != self.Source.ORGANISATION:
            errors['source_type'] = 'Organisation confirmation must identify an organisation-reported source.'
        if errors:
            raise ValidationError(errors)

    @property
    def is_stale(self):
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f'{self.get_topic_display()}: {self.ppa.name}'


class ClaimRevision(models.Model):
    """Private append-only snapshots, including the reason for operator edits."""
    claim = models.ForeignKey(PPAClaim, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class ReviewRevision(models.Model):
    """Private historical text is never included in public review queries."""
    review = models.ForeignKey(Review, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    rating = models.PositiveIntegerField()
    comment = models.TextField(blank=True)
    topic_tags = models.JSONField(default=list, blank=True)
    context_date = models.DateField(null=True, blank=True)
    is_public = models.BooleanField()
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


# Keep independent directory proposals separate from contextual PPA claims.
from .submission_models import PPASubmission, SubmissionRevision  # noqa: E402,F401
