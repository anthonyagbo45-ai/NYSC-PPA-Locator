"""Source-labelled public resources, kept apart from private membership claims."""
from datetime import timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, F
from django.utils import timezone

from locations.models import LGA
from service_year.models import ServiceProfile


def group_expiry():
    return timezone.now() + timedelta(days=180)


def notice_expiry():
    return timezone.now() + timedelta(days=30)


def validate_public_url(value):
    if not value:
        return
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValidationError('Use a public HTTP(S) reference without credentials or control characters.')


def is_official_url(value):
    try:
        validate_public_url(value)
        parsed = urlsplit(value)
        hosts = getattr(settings, 'NYSC_OFFICIAL_SOURCE_HOSTS', ('nysc.gov.ng', 'www.nysc.gov.ng'))
        return parsed.scheme == 'https' and parsed.hostname.lower() in hosts and parsed.port in (None, 443)
    except (ValidationError, ValueError, AttributeError):
        return False


def validate_zone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError('Choose a recognised IANA time zone, for example Africa/Lagos or UTC.')


class TrustRecord(models.Model):
    class Source(models.TextChoices):
        MEMBER = 'member', 'Member report'
        ORGANISATION = 'organisation', 'Organisation or representative report'
        EDITORIAL = 'editorial', 'Editorial guidance'
        OFFICIAL = 'official', 'Attributed official NYSC material'

    class Publication(models.TextChoices):
        DRAFT = 'draft', 'Private draft'
        PENDING = 'pending', 'Awaiting review'
        APPROVED = 'approved', 'Approved to publish'
        REJECTED = 'rejected', 'Not approved'
        ARCHIVED = 'archived', 'Archived'

    class Verification(models.TextChoices):
        COMMUNITY = 'community', 'Community report — not independently checked'
        CHECKED = 'checked', 'Independently checked source'
        ORGANISATION = 'organisation', 'Checked with the organisation or representative'
        OFFICIAL = 'official', 'Checked against attributed official NYSC material'

    title = models.CharField(max_length=200)
    state = models.ForeignKey('locations.State', on_delete=models.PROTECT)
    lga = models.ForeignKey('locations.LGA', on_delete=models.PROTECT)
    source_type = models.CharField(max_length=16, choices=Source.choices, default=Source.MEMBER)
    source_url = models.URLField(max_length=500, blank=True, validators=[validate_public_url])
    observed_on = models.DateField()
    checked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=notice_expiry)
    publication_status = models.CharField(max_length=16, choices=Publication.choices, default=Publication.DRAFT)
    verification_status = models.CharField(max_length=16, choices=Verification.choices, default=Verification.COMMUNITY)
    shared_for_review = models.BooleanField(default=False, editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='%(class)s_resources')
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_%(class)s_resources')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        errors = {}
        if self.state_id and self.lga_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            errors['lga'] = 'Choose an LGA in the selected state.'
        now = timezone.now()
        if self.observed_on and self.observed_on > timezone.localdate():
            errors['observed_on'] = 'The observation date cannot be in the future.'
        if self.checked_at:
            if timezone.is_naive(self.checked_at) or self.checked_at > now:
                errors['checked_at'] = 'Use a checked time with a time zone that is not in the future.'
            elif self.observed_on and timezone.localtime(self.checked_at).date() < self.observed_on:
                errors['checked_at'] = 'The source check cannot predate the reported observation.'
        if self.expires_at:
            if timezone.is_naive(self.expires_at):
                errors['expires_at'] = 'Use a reconfirmation time with a time zone.'
            elif self.checked_at and timezone.is_aware(self.checked_at) and self.expires_at <= self.checked_at:
                errors['expires_at'] = 'Reconfirmation must fall after the source was checked.'
            elif self.observed_on:
                maximum = 365 if isinstance(self, CDSGroup) else 180
                if timezone.localtime(self.expires_at).date() > self.observed_on + timedelta(days=maximum):
                    errors['expires_at'] = 'Reconfirm this information within its supported freshness period.'
        if self.source_type == self.Source.OFFICIAL and not is_official_url(self.source_url):
            errors['source_url'] = 'Official material needs an HTTPS reference on an explicitly trusted NYSC source host.'
        if self.verification_status != self.Verification.COMMUNITY:
            if not self.source_url:
                errors['source_url'] = 'A checked source requires a traceable public reference.'
            if not self.checked_at:
                errors['checked_at'] = 'Record when the supporting evidence was checked.'
            if not self.moderator_note.strip():
                errors['moderator_note'] = 'Record the private evidence supporting this verification.'
        if self.verification_status == self.Verification.OFFICIAL:
            if self.source_type != self.Source.OFFICIAL or not is_official_url(self.source_url):
                errors['verification_status'] = 'Official-source confirmation requires attributable material on a trusted NYSC host.'
        if errors:
            raise ValidationError(errors)

    @property
    def is_stale(self):
        return self.expires_at <= timezone.now()

    def __str__(self):
        # Related-object labels must not leak a later unsubmitted private title.
        return f'{self._meta.verbose_name.capitalize()} #{self.pk or "new"}'


class CDSGroup(TrustRecord):
    description = models.TextField(max_length=3000)
    venue_hint = models.CharField(max_length=500, blank=True, help_text='Public venue context only, never a member residence or live location.')
    public_contact_url = models.URLField(max_length=500, blank=True, validators=[validate_public_url], help_text='Public organisation information page only; no private contact details.')
    expires_at = models.DateTimeField(default=group_expiry)

    class Meta:
        ordering = ['title', 'pk']
        permissions = [('can_moderate_resources', 'Can review CDS and clearance resources'), ('can_verify_resources', 'Can independently verify CDS and clearance sources')]
        indexes = [
            models.Index(fields=['state', 'lga', 'publication_status', 'title'], name='cds_group_area_status_idx'),
            models.Index(fields=['author', '-updated_at'], name='cds_group_author_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(publication_status='draft', shared_for_review=True), name='cds_group_private_drafts'),
            models.CheckConstraint(condition=Q(publication_status__in=['draft','pending','approved','rejected','archived']), name='cds_group_valid_publication'),
            models.CheckConstraint(condition=Q(verification_status__in=['community','checked','organisation','official']), name='cds_group_valid_verification'),
            models.CheckConstraint(condition=Q(source_type__in=['member','organisation','editorial','official']), name='cds_group_valid_source'),
            models.CheckConstraint(condition=Q(version__gte=1), name='cds_group_positive_version'),
        ]


class ServiceNotice(TrustRecord):
    class Kind(models.TextChoices):
        CDS = 'cds', 'CDS schedule or update'
        CLEARANCE = 'clearance', 'Clearance resource or notice'

    kind = models.CharField(max_length=12, choices=Kind.choices)
    body = models.TextField(max_length=3000)
    venue_hint = models.CharField(max_length=500, blank=True)
    group = models.ForeignKey(CDSGroup, null=True, blank=True, on_delete=models.PROTECT, related_name='notices')
    event_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    timezone_name = models.CharField(max_length=64, default='Africa/Lagos', validators=[validate_zone])
    batch_label = models.CharField(max_length=50, blank=True, help_text='Optional general batch/stream context; never enter a state code.')
    stage = models.CharField(max_length=20, blank=True, choices=ServiceProfile.Stage.choices)

    class Meta:
        ordering = ['-observed_on', '-pk']
        indexes = [
            models.Index(fields=['state', 'lga', 'kind', 'publication_status', 'expires_at'], name='notice_area_kind_status_idx'),
            models.Index(fields=['group', 'event_at'], name='notice_group_event_idx'),
            models.Index(fields=['publication_status', 'event_at', 'expires_at'], name='notice_dispatch_window_idx'),
            models.Index(fields=['author', '-updated_at'], name='notice_author_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(publication_status='draft', shared_for_review=True), name='notice_private_drafts'),
            models.CheckConstraint(condition=Q(publication_status__in=['draft','pending','approved','rejected','archived']), name='notice_valid_publication'),
            models.CheckConstraint(condition=Q(verification_status__in=['community','checked','organisation','official']), name='notice_valid_verification'),
            models.CheckConstraint(condition=Q(source_type__in=['member','organisation','editorial','official']), name='notice_valid_source'),
            models.CheckConstraint(condition=Q(kind__in=['cds','clearance']), name='notice_valid_kind'),
            models.CheckConstraint(condition=Q(version__gte=1), name='notice_positive_version'),
            models.CheckConstraint(condition=Q(ends_at__isnull=True) | Q(event_at__isnull=False, ends_at__gte=F('event_at')), name='notice_event_valid_end'),
            models.CheckConstraint(condition=Q(kind='cds') | Q(group__isnull=True), name='clearance_no_cds_group'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.group_id:
            group = CDSGroup.objects.filter(pk=self.group_id).first()
            if group is None or (group.state_id, group.lga_id) != (self.state_id, self.lga_id):
                errors['group'] = 'Choose a CDS group in the same selected state and LGA.'
            elif group.publication_status != self.Publication.APPROVED:
                errors['group'] = 'Use a group that is currently published.'
            if self.kind != self.Kind.CDS:
                errors['group'] = 'Clearance information cannot claim a CDS group schedule.'
        for field in ('event_at', 'ends_at'):
            value = getattr(self, field)
            if value and timezone.is_naive(value):
                errors[field] = 'Use an event time with an explicit time zone.'
        if self.ends_at and (not self.event_at or (timezone.is_aware(self.ends_at) and timezone.is_aware(self.event_at) and self.ends_at < self.event_at)):
            errors['ends_at'] = 'The end must follow the event start.'
        if errors:
            raise ValidationError(errors)


class CDSPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='cds_preference')
    group = models.ForeignKey(CDSGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name='private_preferences')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Private CDS preference #{self.pk or "new"}'


class GroupRevision(models.Model):
    group = models.ForeignKey(CDSGroup, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)
    shared_for_review = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['group', '-created_at', '-id'], name='cds_group_revision_idx')]

    def __str__(self):
        return f'Group revision #{self.pk}'


class NoticeRevision(models.Model):
    notice = models.ForeignKey(ServiceNotice, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)
    shared_for_review = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['notice', '-created_at', '-id'], name='service_notice_revision_idx')]

    def __str__(self):
        return f'Notice revision #{self.pk}'


class ResourceReport(models.Model):
    class Category(models.TextChoices):
        WRONG_INFO = 'wrong_info', 'Incorrect information'
        OUTDATED = 'outdated', 'Outdated schedule or source'
        MISLEADING = 'misleading', 'Misleading or fake official claim'
        PRIVACY = 'privacy', 'Private information or unsafe location'
        OTHER = 'other', 'Other correction'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    group = models.ForeignKey(CDSGroup, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    notice = models.ForeignKey(ServiceNotice, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='service_resource_reports')
    category = models.CharField(max_length=16, choices=Category.choices)
    details = models.TextField(max_length=2000)
    proposed_correction = models.TextField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_service_resource_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_resource_reports', 'Can investigate CDS and clearance reports')]
        indexes = [models.Index(fields=['status', '-created_at'], name='service_resource_reports_idx')]
        constraints = [models.CheckConstraint(condition=Q(group__isnull=False, notice__isnull=True) | Q(group__isnull=True, notice__isnull=False), name='resource_report_one_target')]

    def __str__(self):
        return f'Service resource report #{self.pk}'


class ResourceReportRevision(models.Model):
    report = models.ForeignKey(ResourceReport, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_status = models.CharField(max_length=16, choices=ResourceReport.Status.choices)
    new_status = models.CharField(max_length=16, choices=ResourceReport.Status.choices)
    moderator_note = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'Service report revision #{self.pk}'
