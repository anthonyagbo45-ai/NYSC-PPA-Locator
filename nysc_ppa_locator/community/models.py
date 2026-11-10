"""Public member suggestions and private safety records, never NYSC decisions."""
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


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
    except (TypeError, ValueError):
        raise ValidationError('Use an HTTP(S) public source without credentials or control characters.')


class ContentStatus(models.TextChoices):
    VISIBLE = 'visible', 'Visible community suggestion'
    HIDDEN = 'hidden', 'Hidden by a moderator'
    ARCHIVED = 'archived', 'Archived'


class Question(models.Model):
    Status = ContentStatus

    class Category(models.TextChoices):
        PPA = 'ppa', 'Finding and settling into a PPA'
        ACCOMMODATION = 'accommodation', 'Accommodation'
        TRANSPORT = 'transport', 'Transport'
        CDS = 'cds', 'CDS'
        SERVICE = 'service', 'Service year guidance'
        WELFARE = 'welfare', 'Welfare and support'
        OTHER = 'other', 'Other community question'

    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='community_questions')
    title = models.CharField(max_length=200)
    body = models.TextField(max_length=5000)
    category = models.CharField(max_length=20, choices=Category.choices)
    state = models.ForeignKey('locations.State', on_delete=models.PROTECT)
    lga = models.ForeignKey('locations.LGA', on_delete=models.PROTECT)
    ppa = models.ForeignKey('ppas.PPA', null=True, blank=True, on_delete=models.PROTECT, related_name='community_questions')
    group = models.ForeignKey('service_resources.CDSGroup', null=True, blank=True, on_delete=models.PROTECT, related_name='community_questions')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VISIBLE)
    resolved = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_community', 'Can moderate community questions and answers')]
        indexes = [
            models.Index(fields=['status', '-created_at', '-id'], name='comm_question_status_date_idx'),
            models.Index(fields=['state', 'lga', 'category', 'status', '-created_at'], name='comm_question_geo_cat_idx'),
            models.Index(fields=['author', '-created_at'], name='comm_question_owner_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=ContentStatus.values), name='comm_question_valid_status'),
            models.CheckConstraint(condition=Q(category__in=['ppa','accommodation','transport','cds','service','welfare','other']), name='comm_question_valid_category'),
            models.CheckConstraint(condition=Q(version__gte=1), name='comm_question_positive_version'),
        ]

    def clean(self):
        super().clean()
        from locations.models import LGA
        from ppas.models import PPA
        from service_resources.models import CDSGroup
        if self.state_id and self.lga_id and not LGA.objects.filter(pk=self.lga_id, state_id=self.state_id).exists():
            raise ValidationError({'lga': 'Choose an LGA in the selected state.'})
        if self.ppa_id and not PPA.objects.filter(pk=self.ppa_id, state_id=self.state_id, lga_id=self.lga_id).exclude(status__in=[PPA.Status.CLOSED, PPA.Status.DISPUTED]).exclude(location_status=PPA.LocationStatus.MISMATCH).exists():
            raise ValidationError({'ppa': 'Choose a current public PPA in this state and LGA, or leave it blank.'})
        if self.group_id and not CDSGroup.objects.filter(pk=self.group_id, state_id=self.state_id, lga_id=self.lga_id, publication_status=CDSGroup.Publication.APPROVED, expires_at__gt=timezone.now()).exists():
            raise ValidationError({'group': 'Choose a current published CDS group in this state and LGA, or leave it blank.'})

    def __str__(self):
        return f'Community question #{self.pk or "new"}'


class Answer(models.Model):
    Status = ContentStatus
    question = models.ForeignKey(Question, on_delete=models.PROTECT, related_name='answers')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='community_answers')
    body = models.TextField(max_length=5000)
    source_url = models.URLField(max_length=500, blank=True, validators=[validate_public_url])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VISIBLE)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'pk']
        indexes = [
            models.Index(fields=['question', 'status', 'created_at', 'id'], name='comm_answer_question_date_idx'),
            models.Index(fields=['author', '-created_at'], name='comm_answer_owner_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=ContentStatus.values), name='comm_answer_valid_status'),
            models.CheckConstraint(condition=Q(version__gte=1), name='comm_answer_positive_version'),
        ]

    def __str__(self):
        return f'Community answer #{self.pk or "new"}'


class ProtectedAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Audit records are append-only.')

    def delete(self):
        raise ValidationError('Audit records are retained.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('Audit records are append-only.')

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get('update_conflicts'):
            raise ValidationError('Audit records are append-only.')
        return super().bulk_create(objs, *args, **kwargs)


class ProtectedAudit(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ProtectedAuditQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ['-created_at', '-pk']
        default_permissions = ('view',)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Audit records are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Audit records are retained.')


class QuestionRevision(ProtectedAudit):
    question = models.ForeignKey(Question, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)

    class Meta(ProtectedAudit.Meta):
        indexes = [models.Index(fields=['question', '-created_at', '-id'], name='comm_question_revision_idx')]

    def __str__(self):
        return f'Community question revision #{self.pk or "new"}'


class AnswerRevision(ProtectedAudit):
    answer = models.ForeignKey(Answer, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    snapshot = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000)

    class Meta(ProtectedAudit.Meta):
        indexes = [models.Index(fields=['answer', '-created_at', '-id'], name='comm_answer_revision_idx')]

    def __str__(self):
        return f'Community answer revision #{self.pk or "new"}'


class HelpfulReaction(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='community_helpful_reactions')
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name='helpful_reactions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'answer'], name='comm_unique_helpful_reaction')]

    def clean(self):
        super().clean()
        if self.answer_id and self.user_id and Answer.objects.filter(pk=self.answer_id, author_id=self.user_id).exists():
            raise ValidationError('You cannot mark your own answer helpful.')


class QuestionFollow(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='community_question_follows')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='follows')
    enabled = models.BooleanField(default=False, help_text='Explicit private opt-in to in-app notifications.')
    enabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=['user', 'question'], name='comm_unique_question_follow')]
        indexes = [models.Index(fields=['user', '-updated_at'], name='comm_follow_owner_date_idx')]

    def __str__(self):
        return f'Private question follow #{self.pk or "new"}'


class CommunityBlock(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='community_blocks')
    blocked_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='community_blocked_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=['user', 'blocked_user'], name='comm_unique_member_block'),
            models.CheckConstraint(condition=~Q(user=F('blocked_user')), name='comm_no_self_block'),
        ]

    def clean(self):
        super().clean()
        if self.user_id and self.user_id == self.blocked_user_id:
            raise ValidationError('You cannot block yourself.')

    def __str__(self):
        return f'Private member block #{self.pk or "new"}'


class CommunityReport(models.Model):
    class Category(models.TextChoices):
        SPAM = 'spam', 'Spam'
        ABUSE = 'abuse', 'Abuse or harassment'
        MISLEADING = 'misleading', 'Misleading information'
        PRIVACY = 'privacy', 'Private or identifying information'
        SCAM = 'scam', 'Scam or suspicious payment request'
        OTHER = 'other', 'Other concern'

    class Status(models.TextChoices):
        OPEN = 'open', 'Awaiting review'
        INVESTIGATING = 'investigating', 'Being investigated'
        RESOLVED = 'resolved', 'Resolved'
        DISMISSED = 'dismissed', 'Dismissed'

    question = models.ForeignKey(Question, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    answer = models.ForeignKey(Answer, null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='community_reports')
    category = models.CharField(max_length=16, choices=Category.choices)
    details = models.TextField(max_length=2000)
    private_evidence = models.TextField(max_length=2000, blank=True, help_text='Private explanation for the report reviewer. Do not upload identity documents or credentials.')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    moderator_note = models.TextField(max_length=2000, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='moderated_community_reports')
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        permissions = [('can_moderate_community_reports', 'Can review private community reports')]
        indexes = [models.Index(fields=['status', '-created_at'], name='comm_report_status_date_idx'), models.Index(fields=['reporter', '-created_at'], name='comm_report_owner_date_idx')]
        constraints = [
            models.CheckConstraint(condition=(Q(question__isnull=False, answer__isnull=True) | Q(question__isnull=True, answer__isnull=False)), name='comm_report_exactly_one_target'),
            models.CheckConstraint(condition=Q(status__in=['open','investigating','resolved','dismissed']), name='comm_report_valid_status'),
        ]

    def clean(self):
        super().clean()
        if bool(self.question_id) == bool(self.answer_id):
            raise ValidationError('Report exactly one question or answer.')

    def __str__(self):
        return f'Private community report #{self.pk or "new"}'


class ReportRevision(ProtectedAudit):
    report = models.ForeignKey(CommunityReport, on_delete=models.PROTECT, related_name='revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    previous_status = models.CharField(max_length=16, choices=CommunityReport.Status.choices)
    new_status = models.CharField(max_length=16, choices=CommunityReport.Status.choices)
    moderator_note = models.TextField(max_length=2000)

    def __str__(self):
        return f'Private community report audit #{self.pk or "new"}'
