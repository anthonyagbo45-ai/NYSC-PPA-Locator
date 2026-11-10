"""Opaque, immutable private image references; original uploads are never kept."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ImmutablePhoto(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_key = models.CharField(max_length=200, editable=False)
    width = models.PositiveIntegerField(editable=False)
    height = models.PositiveIntegerField(editable=False)
    byte_size = models.PositiveIntegerField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        default_permissions = ()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Private photo records cannot be changed.')
        return super().save(*args, **kwargs)


class PhotoAttachment(ImmutablePhoto):
    message = models.OneToOneField('messenger.Message', on_delete=models.CASCADE, related_name='photo_attachment')

    class Meta(ImmutablePhoto.Meta):
        indexes = [models.Index(fields=['storage_key'], name='photo_attachment_key_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(width__gte=1, width__lte=1600, height__gte=1, height__lte=1600), name='photo_attachment_valid_size'),
            models.CheckConstraint(condition=Q(byte_size__gte=1, byte_size__lte=2097152), name='photo_attachment_valid_bytes'),
        ]

    def __str__(self):
        return f'Private photo #{self.pk}'


class PhotoEvidence(ImmutablePhoto):
    report = models.OneToOneField('messenger.MessageReport', on_delete=models.CASCADE, related_name='photo_evidence')

    class Meta(ImmutablePhoto.Meta):
        indexes = [models.Index(fields=['storage_key'], name='photo_evidence_key_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(width__gte=1, width__lte=1600, height__gte=1, height__lte=1600), name='photo_evidence_valid_size'),
            models.CheckConstraint(condition=Q(byte_size__gte=1, byte_size__lte=2097152), name='photo_evidence_valid_bytes'),
        ]

    def __str__(self):
        return f'Private reported photo #{self.pk}'
