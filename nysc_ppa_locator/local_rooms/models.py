"""Explicit, moderated community membership; unrelated to official NYSC records."""
import ipaddress
from urllib.parse import parse_qsl, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def validate_source_url(value):
    """Validate a link only. Room links are never fetched by the application."""
    if not value:
        return
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme not in ('http', 'https') or not host or '.' not in host
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal'))
                or any(key.lower() in {'token', 'access_token', 'password', 'secret', 'auth'} for key, _ in parse_qsl(parsed.query))):
            raise ValueError
        parsed.port
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError('Use a public HTTP(S) reference without credentials, private addresses or access tokens.')


class Room(models.Model):
    class Visibility(models.TextChoices):
        PUBLIC = 'public', 'Public discussion'
        MEMBERS = 'members', 'Approved members only'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Open'
        PAUSED = 'paused', 'Paused'
        ARCHIVED = 'archived', 'Archived'

    title = models.CharField(max_length=200, help_text='Public discovery title, including for members-only rooms.')
    description = models.TextField(max_length=3000, help_text='Public descriptive information. Do not include private membership or contact details.')
    guidelines = models.TextField(max_length=3000, blank=True, help_text='Public participation guidelines.')
    state = models.ForeignKey('locations.State', on_delete=models.PROTECT)
    lga = models.ForeignKey('locations.LGA', on_delete=models.PROTECT)
    ppa = models.ForeignKey('ppas.PPA', null=True, blank=True, on_delete=models.PROTECT, related_name='local_rooms')
    group = models.ForeignKey('service_resources.CDSGroup', null=True, blank=True, on_delete=models.PROTECT, related_name='local_rooms')
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.MEMBERS)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='created_local_rooms')
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_local_rooms')
    moderated_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title', 'pk']
        permissions = [('can_manage_rooms', 'Can create and manage local rooms')]
        indexes = [models.Index(fields=['status', 'state', 'lga', 'title'], name='rooms_discovery_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(visibility__in=['public', 'members']), name='room_valid_visibility'),
            models.CheckConstraint(condition=Q(status__in=['active', 'paused', 'archived']), name='room_valid_status'),
            models.CheckConstraint(condition=Q(version__gte=1), name='room_positive_version'),
        ]

    def clean(self):
        super().clean()
        from locations.models import LGA
        if self.state_id and self.lga_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            raise ValidationError({'lga': 'Choose an LGA in the selected state.'})
        if self.ppa_id:
            from ppas.models import PPA
            if not PPA.objects.filter(pk=self.ppa_id, state_id=self.state_id, lga_id=self.lga_id).exists():
                raise ValidationError({'ppa': 'Choose a PPA in this state and LGA.'})
        if self.group_id:
            from service_resources.models import CDSGroup
            if not CDSGroup.objects.filter(pk=self.group_id, state_id=self.state_id, lga_id=self.lga_id).exists():
                raise ValidationError({'group': 'Choose a CDS resource in this state and LGA.'})

    def __str__(self):
        return f'Local room #{self.pk or "new"}'


class RoomMembership(models.Model):
    class Status(models.TextChoices):
        REQUESTED = 'requested', 'Awaiting review'
        APPROVED = 'approved', 'Approved community member'
        REJECTED = 'rejected', 'Request declined'
        LEFT = 'left', 'Left or cancelled'
        BANNED = 'banned', 'Participation restricted'

    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='room_memberships')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    notifications_enabled = models.BooleanField(default=False)
    notifications_enabled_at = models.DateTimeField(null=True, blank=True, editable=False)
    requested_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_room_memberships')
    moderator_note = models.TextField(max_length=2000, blank=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-pk']
        permissions = [('can_moderate_room_memberships', 'Can review and restrict room membership')]
        constraints = [
            models.UniqueConstraint(fields=['room', 'user'], name='one_room_membership_per_user'),
            models.CheckConstraint(condition=Q(status__in=['requested', 'approved', 'rejected', 'left', 'banned']), name='room_member_valid_status'),
            models.CheckConstraint(condition=Q(notifications_enabled=False, notifications_enabled_at__isnull=True) | Q(status='approved', notifications_enabled=True, notifications_enabled_at__isnull=False), name='room_member_explicit_alerts'),
            models.CheckConstraint(condition=Q(version__gte=1), name='room_member_positive_version'),
        ]
        indexes = [models.Index(fields=['room', 'status', 'notifications_enabled'], name='room_member_delivery_idx'), models.Index(fields=['user', '-updated_at'], name='room_member_owner_idx')]

    def __str__(self):
        return f'Private room membership #{self.pk or "new"}'


class RoomPost(models.Model):
    class Status(models.TextChoices):
        VISIBLE = 'visible', 'Visible'
        HIDDEN = 'hidden', 'Hidden by a moderator'
        ARCHIVED = 'archived', 'Withdrawn'

    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name='posts')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='room_posts')
    body = models.TextField(max_length=4000)
    source_url = models.URLField(max_length=500, blank=True, validators=[validate_source_url])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VISIBLE)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_room_posts')
    moderated_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_room_posts', 'Can hide and review room posts')]
        indexes = [models.Index(fields=['room', 'status', '-created_at', '-id'], name='room_post_visible_idx'), models.Index(fields=['author', '-created_at'], name='room_post_author_idx')]
        constraints = [models.CheckConstraint(condition=Q(status__in=['visible', 'hidden', 'archived']), name='room_post_valid_status'), models.CheckConstraint(condition=Q(version__gte=1), name='room_post_positive_version')]

    def __str__(self):
        return f'Room post #{self.pk or "new"}'


class ImmutableAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Audit history is append-only.')

    def delete(self):
        raise ValidationError('Audit history is protected.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('Audit history is append-only.')

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
        if update_conflicts or ignore_conflicts:
            raise ValidationError('Audit history cannot replace or silently ignore existing records.')
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                                   update_conflicts=update_conflicts, update_fields=update_fields, unique_fields=unique_fields)


class ImmutableAudit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ImmutableAuditQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ['-created_at', '-pk']
        default_permissions = ('view',)

    def save(self, *args, **kwargs):
        if not self._state.adding or self.pk is not None:
            raise ValidationError('Audit history is append-only.')
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Audit history is protected.')

    def __str__(self):
        return f'Private room audit #{self.pk or "new"}'


class RoomRevision(ImmutableAudit):
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name='revisions')
    snapshot = models.JSONField(default=dict)
    reason = models.TextField(max_length=1000)


class RoomMembershipRevision(ImmutableAudit):
    membership = models.ForeignKey(RoomMembership, on_delete=models.PROTECT, related_name='revisions')
    snapshot = models.JSONField(default=dict)
    reason = models.TextField(max_length=1000)


class RoomPostRevision(ImmutableAudit):
    post = models.ForeignKey(RoomPost, on_delete=models.PROTECT, related_name='revisions')
    snapshot = models.JSONField(default=dict)
    reason = models.TextField(max_length=1000)


class RoomReport(models.Model):
    class Category(models.TextChoices):
        ABUSE = 'abuse', 'Abuse or harassment'
        SPAM = 'spam', 'Spam or misleading information'
        UNSAFE = 'unsafe', 'Unsafe or private information'
        OTHER = 'other', 'Other concern'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    post = models.ForeignKey(RoomPost, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='room_reports')
    category = models.CharField(max_length=12, choices=Category.choices)
    details = models.TextField(max_length=2000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_room_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_room_reports', 'Can investigate private room reports')]
        constraints = [models.CheckConstraint(condition=Q(room__isnull=False, post__isnull=True) | Q(room__isnull=True, post__isnull=False), name='room_report_exactly_one_target'), models.CheckConstraint(condition=Q(status__in=['open', 'investigating', 'resolved', 'dismissed']), name='room_report_valid_status'), models.CheckConstraint(condition=Q(category__in=['abuse', 'spam', 'unsafe', 'other']), name='room_report_valid_category')]
        indexes = [models.Index(fields=['status', '-created_at'], name='room_report_review_idx'), models.Index(fields=['reporter', '-created_at'], name='room_report_owner_idx')]

    def __str__(self):
        return f'Private room report #{self.pk or "new"}'


class RoomReportRevision(ImmutableAudit):
    report = models.ForeignKey(RoomReport, on_delete=models.PROTECT, related_name='revisions')
    previous_status = models.CharField(max_length=16, choices=RoomReport.Status.choices)
    new_status = models.CharField(max_length=16, choices=RoomReport.Status.choices)
    moderator_note = models.TextField(max_length=2000)
