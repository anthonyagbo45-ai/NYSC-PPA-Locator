"""Private participant records; only explicitly reported evidence has a review desk."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from community.models import ProtectedAudit


class MessengerPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='messenger_preference')
    allow_requests = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def __str__(self):
        return f'Private message preference #{self.pk or "new"}'


class Conversation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Message request'
        ACCEPTED = 'accepted', 'Accepted conversation'
        DECLINED = 'declined', 'Declined request'
        CLOSED = 'closed', 'Closed conversation'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    low_member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='low_private_conversations')
    high_member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='high_private_conversations')
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='initiated_private_conversations')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    low_read_through = models.PositiveBigIntegerField(default=0)
    high_read_through = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        ordering = ['-updated_at', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['low_member', 'high_member'], name='msg_unique_member_pair'),
            models.CheckConstraint(condition=Q(low_member__lt=F('high_member')), name='msg_ordered_distinct_pair'),
            models.CheckConstraint(condition=Q(initiated_by=F('low_member')) | Q(initiated_by=F('high_member')), name='msg_initiator_is_participant'),
            models.CheckConstraint(condition=Q(status__in=['pending', 'accepted', 'declined', 'closed']), name='msg_valid_conversation_status'),
        ]
        indexes = [
            models.Index(fields=['low_member', 'status', '-updated_at'], name='msg_low_status_date_idx'),
            models.Index(fields=['high_member', 'status', '-updated_at'], name='msg_high_status_date_idx'),
            models.Index(fields=['initiated_by', '-created_at'], name='msg_request_allowance_idx'),
        ]

    def __str__(self):
        return f'Private conversation #{self.pk}'


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='private_messages')
    body = models.TextField(max_length=2000)
    client_token = models.UUIDField(default=uuid.uuid4, editable=False)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['conversation', 'sender', 'client_token'], name='msg_unique_delivery_token')]
        indexes = [
            models.Index(fields=['conversation', '-id'], name='msg_thread_cursor_idx'),
            models.Index(fields=['sender', '-sent_at'], name='msg_sender_allowance_idx'),
        ]

    def clean(self):
        super().clean()
        if not self.body or not self.body.strip():
            raise ValidationError({'body': 'Write a message.'})
        if self.conversation_id and self.sender_id and self.sender_id not in (self.conversation.low_member_id, self.conversation.high_member_id):
            raise ValidationError('Only conversation participants can send messages.')

    def __str__(self):
        return f'Private message #{self.pk or "new"}'


class MessageReport(models.Model):
    class Category(models.TextChoices):
        SPAM = 'spam', 'Spam'
        ABUSE = 'abuse', 'Abuse or harassment'
        PRIVACY = 'privacy', 'Private or identifying information'
        SCAM = 'scam', 'Scam or suspicious payment request'
        OTHER = 'other', 'Other concern'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    message = models.ForeignKey(Message, null=True, on_delete=models.SET_NULL, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='private_message_reports')
    category = models.CharField(max_length=12, choices=Category.choices)
    details = models.TextField(max_length=2000)
    evidence = models.JSONField(default=dict, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_private_message_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        permissions = [('can_review_message_reports', 'Can review only reported private message evidence')]
        ordering = ['-created_at', '-pk']
        constraints = [
            models.CheckConstraint(condition=Q(category__in=['spam', 'abuse', 'privacy', 'scam', 'other']), name='msg_report_valid_category'),
            models.CheckConstraint(condition=Q(status__in=['open', 'investigating', 'resolved', 'dismissed']), name='msg_report_valid_status'),
        ]
        indexes = [
            models.Index(fields=['status', '-created_at'], name='msg_report_status_date_idx'),
            models.Index(fields=['reporter', '-created_at'], name='msg_report_owner_date_idx'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values('message_id', 'reporter_id', 'category', 'details', 'evidence').first()
            if previous and any(previous[name] != getattr(self, name) for name in previous):
                raise ValidationError('Submitted report evidence cannot be changed.')
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'Private message report #{self.pk or "new"}'


class ReportDecision(ProtectedAudit):
    report = models.ForeignKey(MessageReport, on_delete=models.PROTECT, related_name='decisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_status = models.CharField(max_length=16, choices=MessageReport.Status.choices)
    new_status = models.CharField(max_length=16, choices=MessageReport.Status.choices)
    moderator_note = models.TextField(max_length=2000)

    class Meta(ProtectedAudit.Meta):
        default_permissions = ()
        indexes = [models.Index(fields=['report', '-created_at'], name='msg_report_decision_idx')]

    def __str__(self):
        return f'Private message report decision #{self.pk or "new"}'
