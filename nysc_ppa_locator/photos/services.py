"""Private, consented chat photos with bounded reads and cleanup on failed commits."""
from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
import logging
import re

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import connection, transaction
from django.db.models import Sum
from django.http import Http404
from django.utils import timezone

from messenger import services as messaging
from messenger.models import Conversation, Message, MessageReport, MessengerPreference
from .models import PhotoAttachment, PhotoEvidence
from .processing import MAX_OUTPUT_BYTES, normalize_photo


logger = logging.getLogger(__name__)
UNAVAILABLE = 'This private photo is unavailable.'
UPLOAD_UNAVAILABLE = 'Photo sharing is temporarily unavailable. Please try again later.'
PHOTOS_PER_DAY = 10
PHOTOS_PER_FIVE_MINUTES = 3
MAX_MEMBER_STORED_BYTES = 100 * 1024 * 1024
KEY_PATTERN = re.compile(r'^(conversation-photos|reported-photos)/[a-f0-9]{32}\.jpg$')


def _enabled():
    return getattr(settings, 'PPA_PHOTOS_ENABLED', False) is True


def _require_enabled(*, reading=False):
    if not _enabled():
        if reading:
            raise Http404(UNAVAILABLE)
        raise ValidationError('Photo sharing is not enabled on this service.')


def _require_outermost():
    # Filesystem/object storage cannot roll back with a caller's transaction.
    # Django's own TestCase wrappers are isolated fixtures, not application
    # transactions; durable atomic deliberately supports that distinction.
    if connection.in_atomic_block and not all(
        getattr(block, '_from_testcase', False) for block in connection.atomic_blocks
    ):
        raise ValidationError('Photo writes must run outside a caller-owned transaction.')


def _photo_consent(member, other):
    enabled = set(MessengerPreference.objects.filter(
        user_id__in=[member.pk, other.pk], allow_photos=True,
    ).values_list('user_id', flat=True))
    return enabled == {member.pk, other.pk}


def _require_sendable(row, member, other):
    _require_enabled()
    messaging._require_verified_pair(member, other)
    if row.status != Conversation.Status.ACCEPTED:
        raise ValidationError('Photos are available only in accepted conversations.')
    if not _photo_consent(member, other):
        raise ValidationError('Both members must allow photos in their message preferences.')


def photo_send_enabled(user, conversation_id):
    row, member, other = messaging._conversation(user, conversation_id)
    try:
        _require_sendable(row, member, other)
    except (ValidationError, PermissionDenied):
        return False
    return True


def _caption(value):
    value = messaging._body(value)
    if len(value) > 500:
        raise ValidationError({'caption': 'Describe this photo in at most 500 characters.'})
    return value


def _storage():
    return storages['photos']


def _cleanup(keys):
    for key in set(keys):
        try:
            # A lost commit acknowledgement is ambiguous. Never delete an
            # object still referenced by a committed row, and leave an opaque
            # object for operator cleanup if the database cannot be checked.
            if (PhotoAttachment.objects.filter(storage_key=key).exists()
                    or PhotoEvidence.objects.filter(storage_key=key).exists()):
                continue
            _storage().delete(key)
        except Exception:
            # Never put storage keys, provider errors or image data in logs.
            logger.warning('Private photo cleanup could not complete.')


def _write_file(key, data, written):
    written.append(key)  # A backend may write and then fail before returning.
    try:
        actual = _storage().save(key, ContentFile(data))
    except Exception:
        raise ValidationError(UPLOAD_UNAVAILABLE) from None
    if actual != key:
        written.append(actual)
        raise ValidationError(UPLOAD_UNAVAILABLE)


def _check_photo_allowance(member, now, byte_size):
    rows = PhotoAttachment.objects.filter(message__sender=member)
    if rows.filter(created_at__gte=now-timedelta(days=1)).count() >= PHOTOS_PER_DAY:
        raise ValidationError('Your daily photo allowance has been reached. Try again later.')
    if rows.filter(created_at__gte=now-timedelta(minutes=5)).count() >= PHOTOS_PER_FIVE_MINUTES:
        raise ValidationError('Please wait before sending another photo.')
    stored = rows.aggregate(total=Sum('byte_size'))['total'] or 0
    if stored + byte_size > MAX_MEMBER_STORED_BYTES:
        raise ValidationError('Your photo storage allowance has been reached. Contact support for help.')


def send_photo(user, conversation_id, upload, caption, client_token, rights_confirmed):
    # Check access before decoding anything or touching storage. Repeat these
    # checks after decoding while holding the same ordered locks as messaging.
    row, member, other = messaging._conversation(user, conversation_id)
    _require_sendable(row, member, other)
    _require_outermost()
    if rights_confirmed is not True:
        raise ValidationError('Confirm that you have permission to share this photo.')
    caption, token = _caption(caption), messaging._token(client_token)
    normalized = normalize_photo(upload)
    written = []
    try:
        with transaction.atomic(durable=True):
            row, member, other = messaging._conversation(user, conversation_id, lock=True)
            _require_sendable(row, member, other)
            existing = Message.objects.filter(
                conversation=row, sender=member, client_token=token,
            ).select_related('sender', 'photo_attachment').first()
            if existing:
                try:
                    photo = existing.photo_attachment
                except Message.photo_attachment.RelatedObjectDoesNotExist:
                    photo = None
                if (photo is None or existing.body != caption
                        or photo.sha256 != normalized['sha256']):
                    raise ValidationError('This send token has already been used. Reload the form.')
                return messaging._safe_message(existing, member)
            now = timezone.now()
            messaging._allow_message(member, now)
            _check_photo_allowance(member, now, len(normalized['data']))
            message = Message.objects.create(
                conversation=row, sender=member, body=caption, client_token=token,
            )
            photo = PhotoAttachment(
                message=message, width=normalized['width'], height=normalized['height'],
                byte_size=len(normalized['data']), sha256=normalized['sha256'],
            )
            photo.storage_key = f'conversation-photos/{photo.id.hex}.jpg'
            _write_file(photo.storage_key, normalized['data'], written)
            photo.save()
            row.updated_at = now
            row.save(update_fields=['updated_at'])
            # Creating a reverse OneToOne binds it to message's relation cache.
            result = messaging._safe_message(message, member)
        return result
    except Exception:
        _cleanup(written)
        raise


def _read_file(photo, prefix):
    expected = f'{prefix}/{photo.id.hex}.jpg'
    if (photo.storage_key != expected or not KEY_PATTERN.fullmatch(photo.storage_key)
            or not 1 <= photo.byte_size <= MAX_OUTPUT_BYTES):
        raise Http404(UNAVAILABLE)
    try:
        storage = _storage()
        if storage.size(photo.storage_key) != photo.byte_size:
            raise ValueError
        with storage.open(photo.storage_key, 'rb') as stream:
            data = stream.read(MAX_OUTPUT_BYTES + 1)
        if (not isinstance(data, bytes) or len(data) != photo.byte_size
                or not compare_digest(sha256(data).hexdigest(), photo.sha256)):
            raise ValueError
    except Exception:
        raise Http404(UNAVAILABLE) from None
    return {
        'file': BytesIO(data), 'width': photo.width, 'height': photo.height,
        'byte_size': photo.byte_size, 'sha256': photo.sha256,
    }


def open_photo(user, photo_id):
    _require_enabled(reading=True)
    photo = PhotoAttachment.objects.filter(pk=messaging._uuid(photo_id)).select_related('message').first()
    if photo is None:
        raise Http404(UNAVAILABLE)
    messaging._conversation(user, photo.message.conversation_id)
    return _read_file(photo, 'conversation-photos')


def report_photo_message(user, conversation_id, message_id, category, details):
    _require_outermost()
    # Participant scoping precedes storage access. This selected image is
    # copied independently, without granting reviewers any thread access.
    row, _, _ = messaging._conversation(user, conversation_id)
    message = Message.objects.filter(pk=messaging._positive_id(message_id), conversation=row).select_related('photo_attachment').first()
    if message is None:
        raise Http404(UNAVAILABLE)
    try:
        attachment = message.photo_attachment
    except Message.photo_attachment.RelatedObjectDoesNotExist:
        raise Http404(UNAVAILABLE) from None
    source = _read_file(attachment, 'conversation-photos')
    data = source['file'].getvalue()
    written = []
    try:
        with transaction.atomic(durable=True):
            report_id = messaging._create_message_report(user, conversation_id, message_id, category, details)
            # _create_message_report retains participant/thread locks until
            # this outermost commit, coordinating close/block/identity edits.
            fresh = PhotoAttachment.objects.filter(pk=attachment.pk, message_id=message_id).first()
            if fresh is None or fresh.sha256 != attachment.sha256 or fresh.storage_key != attachment.storage_key:
                raise Http404(UNAVAILABLE)
            evidence = PhotoEvidence(
                report_id=report_id, width=attachment.width, height=attachment.height,
                byte_size=attachment.byte_size, sha256=attachment.sha256,
            )
            evidence.storage_key = f'reported-photos/{evidence.id.hex}.jpg'
            _write_file(evidence.storage_key, data, written)
            evidence.save()
        return report_id
    except Exception:
        _cleanup(written)
        raise


def open_report_evidence_file(user, report_id):
    _require_enabled(reading=True)
    reviewer = messaging._member(user)
    if (not reviewer.is_staff or not reviewer.has_perm('messenger.can_review_message_reports')
            or not callable(getattr(user, 'is_verified', None)) or not user.is_verified()):
        raise PermissionDenied('Verify an authorised reviewer account to open reported photos.')
    # A stale in-memory device or forged OTP method is insufficient: the
    # current device must still be confirmed and belong to this reviewer.
    from django_otp.models import Device
    device = getattr(user, 'otp_device', None)
    persistent_id = getattr(device, 'persistent_id', '')
    fresh_device = Device.from_persistent_id(persistent_id) if persistent_id else None
    if fresh_device is None or not fresh_device.confirmed or fresh_device.user_id != reviewer.pk:
        raise PermissionDenied('Verify an authorised reviewer account to open reported photos.')
    photo = PhotoEvidence.objects.filter(report_id=messaging._positive_id(report_id)).first()
    if photo is None or not MessageReport.objects.filter(pk=photo.report_id).exists():
        raise Http404(UNAVAILABLE)
    return _read_file(photo, 'reported-photos')
