"""Source-labelled local guidance; private home locations and contacts are not collected."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from locations.models import LGA

MAX_RECONFIRMATION_DAYS = {'housing': 30, 'fare': 180, 'service': 365}


def default_expiry():
    return timezone.now() + timedelta(days=180)


def validate_public_url(value):
    if not value:
        return
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise ValidationError('Use a public HTTP(S) page without credentials, access tokens or private contacts.')


class Listing(models.Model):
    class Kind(models.TextChoices):
        HOUSING = 'housing', 'Accommodation'
        FARE = 'fare', 'Transport fare'
        SERVICE = 'service', 'Local service'

    class Source(models.TextChoices):
        MEMBER = 'member', 'Member experience'
        OWNER = 'owner', 'Reported by an owner or authorised contact'
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
        OFFICIAL = 'official', 'Checked against attributed official material'

    kind = models.CharField(max_length=12, choices=Kind.choices)
    title = models.CharField(max_length=200)
    area = models.CharField(max_length=200, help_text='Town or broad neighbourhood only. Do not enter a private home address.')
    summary = models.TextField(max_length=2000)
    state = models.ForeignKey('locations.State', on_delete=models.PROTECT)
    lga = models.ForeignKey('locations.LGA', on_delete=models.PROTECT)
    ppa = models.ForeignKey('ppas.PPA', null=True, blank=True, on_delete=models.SET_NULL, related_name='local_guides')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='local_listings')
    source_type = models.CharField(max_length=16, choices=Source.choices, default=Source.MEMBER)
    source_url = models.URLField(max_length=500, blank=True, validators=[validate_public_url], help_text='Public supporting information only; no private home/contact links.')
    publication_status = models.CharField(max_length=16, choices=Publication.choices, default=Publication.DRAFT)
    verification_status = models.CharField(max_length=16, choices=Verification.choices, default=Verification.COMMUNITY)
    observed_on = models.DateField()
    checked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=default_expiry)
    last_reconfirmed_at = models.DateTimeField(null=True, blank=True, help_text='Contributor assertion only, separate from independent checking.')
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_local_listings')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-observed_on', '-pk']
        permissions = [
            ('can_moderate_listings', 'Can review and publish local guidance'),
            ('can_verify_listings', 'Can independently verify local guidance sources'),
        ]
        indexes = [
            models.Index(fields=['kind', 'publication_status', 'expires_at'], name='guide_kind_status_expiry_idx'),
            models.Index(fields=['state', 'lga', 'kind', '-observed_on'], name='guide_area_kind_date_idx'),
            models.Index(fields=['author', '-created_at'], name='guide_author_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=['housing', 'fare', 'service']), name='guide_valid_kind'),
            models.CheckConstraint(condition=Q(publication_status__in=['draft', 'pending', 'approved', 'rejected', 'archived']), name='guide_valid_publication'),
            models.CheckConstraint(condition=Q(verification_status__in=['community', 'checked', 'organisation', 'official']), name='guide_valid_verification'),
        ]

    def clean(self):
        super().clean()
        for name in ('checked_at', 'expires_at', 'last_reconfirmed_at'):
            value = getattr(self, name)
            if isinstance(value, datetime) and timezone.is_naive(value):
                setattr(self, name, timezone.make_aware(value))
        if self.state_id and self.lga_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            raise ValidationError({'lga': 'Choose an LGA in the selected state.'})
        if self.ppa_id:
            from ppas.models import PPA
            if not PPA.objects.filter(pk=self.ppa_id, state_id=self.state_id, lga_id=self.lga_id).exists():
                raise ValidationError({'ppa': 'Choose a PPA in this state and LGA, or leave it blank.'})
        if isinstance(self.observed_on, date) and self.observed_on > timezone.localdate():
            raise ValidationError({'observed_on': 'The observation date cannot be in the future.'})
        if isinstance(self.checked_at, datetime) and self.checked_at > timezone.now():
            raise ValidationError({'checked_at': 'A check date cannot be in the future.'})
        if isinstance(self.checked_at, datetime) and isinstance(self.expires_at, datetime) and self.expires_at <= self.checked_at:
            raise ValidationError({'expires_at': 'Reconfirmation must be due after the supporting evidence was checked.'})
        if isinstance(self.observed_on, date) and isinstance(self.expires_at, datetime) and self.kind in MAX_RECONFIRMATION_DAYS:
            latest = self.observed_on + timedelta(days=MAX_RECONFIRMATION_DAYS[self.kind])
            if timezone.localdate(self.expires_at) > latest:
                raise ValidationError({'expires_at': f'Reconfirmation is required within {MAX_RECONFIRMATION_DAYS[self.kind]} days of the observation for this type of information.'})
        if self.source_type == self.Source.OFFICIAL and not self.source_url:
            raise ValidationError({'source_url': 'Attributed official material requires a traceable public source.'})
        if self.verification_status != self.Verification.COMMUNITY:
            if not self.checked_at or not self.source_url or not self.moderator_note.strip():
                raise ValidationError('Independent verification needs a public supporting source, check date and private evidence note.')
        if self.verification_status == self.Verification.OFFICIAL and self.source_type != self.Source.OFFICIAL:
            raise ValidationError({'source_type': 'Official-source verification needs attributed official material.'})

    @property
    def is_stale(self):
        return self.expires_at <= timezone.now()

    @property
    def is_available(self):
        return self.kind != self.Kind.HOUSING or (hasattr(self, 'housing') and self.housing.availability == HousingDetails.Availability.AVAILABLE)

    @property
    def freshness_label(self):
        return 'Outdated — reconfirm before relying on this' if self.is_stale else 'Reconfirmation scheduled'

    def __str__(self):
        return f'{self.get_kind_display()}: {self.title}'


class HousingDetails(models.Model):
    class PricePeriod(models.TextChoices):
        MONTHLY = 'monthly', 'Per month'
        YEARLY = 'yearly', 'Per year'

    class RoomType(models.TextChoices):
        SINGLE = 'single', 'Single room'
        SELF_CONTAINED = 'self_contained', 'Self-contained room'
        SHARED = 'shared', 'Shared accommodation'
        FLAT = 'flat', 'Flat'
        OTHER = 'other', 'Other'

    class Availability(models.TextChoices):
        AVAILABLE = 'available', 'Reported available'
        UNAVAILABLE = 'unavailable', 'Reported unavailable'

    class ContributorRole(models.TextChoices):
        MEMBER = 'member', 'Corps member'
        ALUMNI = 'alumni', 'Alumni (self-described)'
        OWNER = 'owner', 'Property owner (self-described)'
        AGENT = 'agent', 'Authorised agent (self-described)'
        PPA = 'ppa', 'Host organisation contact (self-described)'

    AMENITY_CHOICES = [('water', 'Water'), ('power', 'Power supply'), ('security', 'Security'), ('accessible', 'Accessibility features'), ('furnished', 'Furnished')]
    listing = models.OneToOneField(Listing, on_delete=models.CASCADE, related_name='housing')
    reported_rent = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0'))])
    price_period = models.CharField(max_length=10, choices=PricePeriod.choices, default=PricePeriod.YEARLY)
    room_type = models.CharField(max_length=16, choices=RoomType.choices, default=RoomType.OTHER)
    amenities = models.JSONField(default=list, blank=True)
    availability = models.CharField(max_length=12, choices=Availability.choices, default=Availability.AVAILABLE)
    contributor_role = models.CharField(max_length=12, choices=ContributorRole.choices, default=ContributorRole.MEMBER)
    authorization_attested = models.BooleanField(default=False, help_text='Permission to share these details; an assertion, not verified ownership.')
    authorization_basis = models.TextField(max_length=1500, blank=True, help_text='Private explanation of your authority to share this listing. No IDs, documents, private addresses or personal contacts.')

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(reported_rent__isnull=True) | Q(reported_rent__gte=0), name='housing_rent_nonnegative')]

    def clean(self):
        super().clean()
        if self.listing.kind != Listing.Kind.HOUSING:
            raise ValidationError('Accommodation details require an accommodation listing.')
        allowed = dict(self.AMENITY_CHOICES)
        if (not isinstance(self.amenities, list) or len(self.amenities) > len(allowed)
                or any(not isinstance(x, str) or x not in allowed for x in self.amenities)
                or len(set(self.amenities)) != len(self.amenities)):
            raise ValidationError({'amenities': 'Select each supported amenity at most once.'})
        if not self.authorization_attested:
            raise ValidationError({'authorization_attested': 'Confirm you have permission to share these accommodation details.'})
        if self.contributor_role in (self.ContributorRole.OWNER, self.ContributorRole.AGENT, self.ContributorRole.PPA) and not self.authorization_basis.strip():
            raise ValidationError({'authorization_basis': 'Explain your authority to share this accommodation. A moderator will review this separately; it does not grant verified ownership.'})

    @property
    def amenity_labels(self):
        allowed = dict(self.AMENITY_CHOICES)
        return [allowed[x] for x in self.amenities if x in allowed]

    def __str__(self):
        return f'Accommodation details #{self.listing_id}'


class FareDetails(models.Model):
    class Mode(models.TextChoices):
        BUS = 'bus', 'Bus'
        KEKE = 'keke', 'Keke'
        OKADA = 'okada', 'Okada'
        TAXI = 'taxi', 'Taxi'
        WALK = 'walk', 'Walking'
        OTHER = 'other', 'Other'

    listing = models.OneToOneField(Listing, on_delete=models.CASCADE, related_name='fare')
    origin_area = models.CharField(max_length=200)
    destination_area = models.CharField(max_length=200)
    mode = models.CharField(max_length=12, choices=Mode.choices)
    amount_low = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    amount_high = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount_low__gte=0), name='fare_low_nonnegative'),
            models.CheckConstraint(condition=Q(amount_high__gte=F('amount_low')), name='fare_valid_range'),
        ]

    def clean(self):
        super().clean()
        if self.listing.kind != Listing.Kind.FARE:
            raise ValidationError('Fare details require a transport listing.')
        if (isinstance(self.amount_low, Decimal) and isinstance(self.amount_high, Decimal)
                and self.amount_low.is_finite() and self.amount_high.is_finite()
                and self.amount_high < self.amount_low):
            raise ValidationError({'amount_high': 'The upper estimate must be at least the lower estimate.'})

    def __str__(self):
        return f'Fare details #{self.listing_id}'


class ServiceDetails(models.Model):
    class Category(models.TextChoices):
        ESSENTIALS = 'essentials', 'Everyday essentials'
        PRINTING = 'printing', 'Printing and documents'
        HEALTH = 'health', 'Pharmacy or clinic'
        PHONE = 'phone', 'Phone and data services'
        TRANSPORT = 'transport', 'Transport hub'
        OTHER = 'other', 'Other local service'

    listing = models.OneToOneField(Listing, on_delete=models.CASCADE, related_name='service')
    category = models.CharField(max_length=16, choices=Category.choices)
    opening_hours = models.CharField(max_length=200, blank=True)
    public_business_address = models.CharField(max_length=500, blank=True, help_text='Only a publicly advertised business premise; no private home or member address.')

    def clean(self):
        super().clean()
        if self.listing.kind != Listing.Kind.SERVICE:
            raise ValidationError('Service details require a service listing.')

    def __str__(self):
        return f'Public service details #{self.listing_id}'


class ListingBookmark(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name='bookmarks')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='local_guide_bookmarks')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'listing'], name='unique_member_guide_bookmark')]


class ListingRevision(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['listing', '-created_at', '-id'], name='guide_revision_date_idx')]

    def __str__(self):
        return f'Local guidance revision #{self.pk}'


class ListingReport(models.Model):
    class Category(models.TextChoices):
        WRONG_INFO = 'wrong_info', 'Incorrect information'
        UNAVAILABLE = 'unavailable', 'No longer available'
        DUPLICATE = 'duplicate', 'Duplicate entry'
        SCAM = 'scam', 'Scam or suspicious payment request'
        PRIVACY = 'privacy', 'Private information or unsafe location details'
        OTHER = 'other', 'Other correction'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='local_guide_reports')
    category = models.CharField(max_length=16, choices=Category.choices)
    details = models.TextField(max_length=2000)
    proposed_correction = models.TextField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_local_guide_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_listing_reports', 'Can investigate local guidance reports')]
        indexes = [models.Index(fields=['status', '-created_at'], name='guide_report_status_date_idx')]

    def __str__(self):
        return f'Local guidance report #{self.pk}'


class ListingReportRevision(models.Model):
    report = models.ForeignKey(ListingReport, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_status = models.CharField(max_length=16, choices=ListingReport.Status.choices)
    new_status = models.CharField(max_length=16, choices=ListingReport.Status.choices)
    moderator_note = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'Local report audit #{self.pk}'
