"""Private member records, kept separate from public PPA verification."""
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class ServiceProfile(models.Model):
    class Stage(models.TextChoices):
        UNKNOWN = 'unknown', 'Prefer not to say'
        PREPARING = 'preparing', 'Preparing for service'
        FINDING_PPA = 'finding_ppa', 'Finding my PPA'
        SERVING = 'serving', 'Currently serving'
        COMPLETED = 'completed', 'After POP'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='service_profile')
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.UNKNOWN)
    batch = models.CharField(max_length=50, blank=True, help_text='Optional, for example 2026 Batch A, Stream I. Do not enter your state code.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Private service profile #{self.pk}'


class MemberPPA(models.Model):
    class Status(models.TextChoices):
        YET_TO_REPORT = 'yet_to_report', 'Yet to report'
        REPORTED = 'reported', 'Reported'
        ACCEPTED = 'accepted', 'Accepted by organisation'
        DECLINED = 'declined', 'Organisation declined'
        SEEKING_GUIDANCE = 'seeking_guidance', 'Seeking guidance'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ppa_assignments')
    # Protect saved personal history from an accidental directory deletion.
    ppa = models.ForeignKey('ppas.PPA', on_delete=models.PROTECT, related_name='member_assignments')
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.YET_TO_REPORT)
    personal_notes = models.TextField(max_length=2000, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-started_at', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['user'], condition=Q(ended_at__isnull=True), name='one_current_ppa_per_member'),
            models.CheckConstraint(condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F('started_at')), name='member_ppa_valid_dates'),
            models.CheckConstraint(condition=Q(status__in=['yet_to_report', 'reported', 'accepted', 'declined', 'seeking_guidance']), name='member_ppa_valid_status'),
        ]
        indexes = [models.Index(fields=['user', '-started_at', '-id'], name='member_ppa_history_idx')]

    @property
    def is_current(self):
        return self.ended_at is None

    def __str__(self):
        return f'Private PPA tracker #{self.pk}'


class StatusChange(models.Model):
    """Private progress history; notes are not copied into a second store."""
    assignment = models.ForeignKey(MemberPPA, on_delete=models.CASCADE, related_name='status_history')
    previous_status = models.CharField(max_length=24, choices=MemberPPA.Status.choices)
    new_status = models.CharField(max_length=24, choices=MemberPPA.Status.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'Private tracker update #{self.pk}'
