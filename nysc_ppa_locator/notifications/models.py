"""Private, voluntary reminders. No model is registered in administration."""
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


REMINDER_CHOICES = [(0, 'At the time'), (15, '15 minutes before'), (60, '1 hour before'), (1440, '1 day before'), (10080, '1 week before')]
REMINDER_VALUES = [value for value, _ in REMINDER_CHOICES]
FIRST_EVENT = datetime(1900, 1, 1, tzinfo=datetime_timezone.utc)
LAST_EVENT = datetime(2101, 1, 1, tzinfo=datetime_timezone.utc)


def validate_timezone_name(value):
    try:
        if not isinstance(value, str) or len(value) > 64 or value.startswith(('/', '.')):
            raise ValueError
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError('Use an IANA time zone, for example Africa/Lagos or UTC.')


class PersonalEvent(models.Model):
    class Kind(models.TextChoices):
        CDS = 'cds', 'My CDS reminder'
        CLEARANCE = 'clearance', 'My clearance reminder'
        PERSONAL = 'personal', 'Personal task'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='personal_events')
    title = models.CharField(max_length=200)
    note = models.TextField(max_length=1000, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PERSONAL)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    timezone_name = models.CharField(max_length=64, default='Africa/Lagos', validators=[validate_timezone_name])
    remind_before = models.PositiveIntegerField(choices=REMINDER_CHOICES, default=60)
    notifications_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['starts_at', 'pk']
        indexes = [
            models.Index(fields=['user', 'status', 'starts_at'], name='event_owner_status_time_idx'),
            models.Index(fields=['status', 'notifications_enabled', 'starts_at'], name='event_dispatch_window_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=['cds', 'clearance', 'personal']), name='personal_event_valid_kind'),
            models.CheckConstraint(condition=Q(status__in=['active', 'completed', 'cancelled']), name='personal_event_valid_status'),
            models.CheckConstraint(condition=Q(remind_before__in=REMINDER_VALUES), name='personal_event_valid_lead'),
            models.CheckConstraint(condition=Q(version__gte=1), name='personal_event_valid_version'),
            models.CheckConstraint(condition=Q(starts_at__gte=FIRST_EVENT, starts_at__lt=LAST_EVENT), name='personal_event_supported_date'),
            models.CheckConstraint(condition=Q(ends_at__isnull=True) | Q(ends_at__gte=F('starts_at'), ends_at__lt=LAST_EVENT), name='personal_event_valid_end'),
        ]

    def clean(self):
        super().clean()
        for field in ('starts_at', 'ends_at'):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, datetime) or timezone.is_naive(value)):
                raise ValidationError({field: 'Choose a date, time and valid time zone.'})
            if value is not None and not FIRST_EVENT <= value < LAST_EVENT:
                raise ValidationError({field: 'Choose a date between 1900 and 2100.'})
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError({'ends_at': 'The end must not be earlier than the start.'})

    def __str__(self):
        return f'Private personal event #{self.pk or "new"}'


class NotificationPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reminder_preferences')
    personal_reminders = models.BooleanField(default=False)
    notice_reminders = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Private reminder preferences #{self.pk or "new"}'


class NoticeSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notice_reminder_subscriptions')
    notice = models.ForeignKey('service_resources.ServiceNotice', on_delete=models.CASCADE, related_name='reminder_subscriptions')
    notice_version = models.PositiveIntegerField()
    group_version = models.PositiveIntegerField(null=True, blank=True)
    remind_before = models.PositiveIntegerField(choices=REMINDER_CHOICES, default=60)
    enabled = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'notice'], name='unique_member_notice_reminder'),
            models.CheckConstraint(condition=Q(remind_before__in=REMINDER_VALUES), name='notice_reminder_valid_lead'),
            models.CheckConstraint(condition=Q(version__gte=1, notice_version__gte=1), name='notice_reminder_valid_version'),
        ]

    def __str__(self):
        return f'Private notice reminder #{self.pk or "new"}'


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='in_app_reminders')
    event = models.ForeignKey(PersonalEvent, null=True, blank=True, on_delete=models.CASCADE, related_name='notifications')
    subscription = models.ForeignKey(NoticeSubscription, null=True, blank=True, on_delete=models.CASCADE, related_name='notifications')
    source_version = models.PositiveIntegerField()
    scheduled_for = models.DateTimeField()
    dedup_key = models.CharField(max_length=96)
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['user', 'dedup_key'], name='unique_member_reminder_delivery'),
            models.CheckConstraint(condition=(Q(event__isnull=False, subscription__isnull=True) | Q(event__isnull=True, subscription__isnull=False)), name='reminder_exactly_one_target'),
            models.CheckConstraint(condition=Q(source_version__gte=1), name='reminder_valid_source_version'),
        ]
        indexes = [models.Index(fields=['user', 'dismissed_at', 'read_at', '-created_at'], name='reminder_owner_unread_idx')]

    def clean(self):
        super().clean()
        if self.event_id and PersonalEvent.objects.filter(pk=self.event_id).exclude(user_id=self.user_id).exists():
            raise ValidationError('The reminder must belong to its event owner.')
        if self.subscription_id and NoticeSubscription.objects.filter(pk=self.subscription_id).exclude(user_id=self.user_id).exists():
            raise ValidationError('The reminder must belong to its subscription owner.')

    def __str__(self):
        return f'Private in-app reminder #{self.pk or "new"}'
