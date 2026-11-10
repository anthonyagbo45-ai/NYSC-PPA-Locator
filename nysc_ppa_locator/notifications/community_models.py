"""Private activity references only: never freeze public or private content."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class CommunityActivity(models.Model):
    answer = models.ForeignKey('community.Answer', null=True, blank=True, on_delete=models.CASCADE, related_name='notification_activities')
    room_post = models.ForeignKey('local_rooms.RoomPost', null=True, blank=True, on_delete=models.CASCADE, related_name='notification_activities')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='community_notification_activities')
    source_version = models.PositiveIntegerField()
    parent_version = models.PositiveIntegerField()
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        default_permissions = ()
        ordering = ['occurred_at', 'pk']
        indexes = [models.Index(fields=['occurred_at', 'id'], name='community_activity_due_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(answer__isnull=False, room_post__isnull=True) | Q(answer__isnull=True, room_post__isnull=False), name='community_activity_one_source'),
            models.CheckConstraint(condition=Q(source_version__gte=1, parent_version__gte=1), name='community_activity_versions'),
            models.UniqueConstraint(fields=['answer'], name='community_activity_once_answer'),
            models.UniqueConstraint(fields=['room_post'], name='community_activity_once_post'),
        ]

    def clean(self):
        super().clean()
        if bool(self.answer_id) == bool(self.room_post_id):
            raise ValidationError('Activity must reference exactly one community source.')

    def __str__(self):
        return f'Private community activity #{self.pk or "new"}'


class CommunityNotification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='community_notifications')
    activity = models.ForeignKey(CommunityActivity, on_delete=models.CASCADE, related_name='notifications')
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        ordering = ['-created_at', '-pk']
        constraints = [models.UniqueConstraint(fields=['user', 'activity'], name='unique_member_community_alert')]
        indexes = [models.Index(fields=['user', 'dismissed_at', 'read_at', '-created_at'], name='community_alert_owner_idx')]

    def __str__(self):
        return f'Private community alert #{self.pk or "new"}'
