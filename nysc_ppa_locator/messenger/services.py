"""Participant-scoped private messaging with explicit requests and bounded queries."""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import BigIntegerField, Case, Exists, F, OuterRef, Q, When
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from community.models import Answer, CommunityBlock, Question
from community.services import interaction_blocked
from .models import Conversation, Message, MessageReport, MessengerPreference, ReportDecision


UNAVAILABLE = 'This private conversation is unavailable.'
REQUEST_UNAVAILABLE = 'This member is not currently available for a message request.'
REQUESTS_PER_DAY = 5
REQUESTS_PER_FIVE_MINUTES = 3
MESSAGES_PER_DAY = 100
MESSAGES_PER_MINUTE = 10


def _member(user, *, verified=False):
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'pk', None):
        raise PermissionDenied('Sign in to use private messages.')
    fresh = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    if fresh is None:
        raise PermissionDenied('This account cannot use private messages.')
    if verified and not fresh.email_verified:
        raise PermissionDenied('Verify your email before requesting or sending private messages.')
    return fresh


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Http404(UNAVAILABLE) from None


def _positive_id(value):
    if isinstance(value, bool):
        raise Http404(UNAVAILABLE)
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        raise Http404(UNAVAILABLE) from None
    if number < 1 or number > 9223372036854775807 or str(number) != str(value):
        raise Http404(UNAVAILABLE)
    return number


def _body(value):
    if not isinstance(value, str):
        raise ValidationError({'body': 'Write a message of at most 2000 characters.'})
    value = value.strip()
    if not value or len(value) > 2000 or any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValidationError({'body': 'Write a message of at most 2000 characters without control characters.'})
    return value


def _token(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Reload this form before sending.') from None


def _pair_ids(conversation):
    return (conversation.low_member_id, conversation.high_member_id)


def _lock_pair(user, pair):
    if not getattr(user, 'is_authenticated', False) or user.pk not in pair:
        raise Http404(UNAVAILABLE)
    members = list(get_user_model().objects.select_for_update().filter(pk__in=pair).order_by('pk'))
    lookup = {member.pk: member for member in members}
    if len(lookup) != 2 or user.pk not in lookup or not lookup[user.pk].is_active:
        raise PermissionDenied('This account cannot use private messages.')
    return lookup[user.pk], lookup[pair[1] if pair[0] == user.pk else pair[0]]


def _conversation(user, conversation_id, *, lock=False):
    # Scope before retrieving: guessed UUIDs, staff and superusers get no bypass.
    if not getattr(user, 'is_authenticated', False):
        raise Http404(UNAVAILABLE)
    rows = Conversation.objects.filter(Q(low_member_id=user.pk) | Q(high_member_id=user.pk), pk=_uuid(conversation_id))
    row = rows.first()
    if row is None:
        raise Http404(UNAVAILABLE)
    if lock:
        member, other = _lock_pair(user, _pair_ids(row))
        row = Conversation.objects.select_for_update().get(pk=row.pk)
    else:
        member = _member(user)
        other = get_user_model().objects.filter(pk=row.high_member_id if row.low_member_id == member.pk else row.low_member_id).first()
        if other is None:
            raise Http404(UNAVAILABLE)
    return row, member, other


def _require_verified_pair(member, other):
    if not member.email_verified or not other.email_verified or not other.is_active:
        raise PermissionDenied(REQUEST_UNAVAILABLE)
    if interaction_blocked(member.pk, other.pk):
        raise PermissionDenied(UNAVAILABLE)


def _resolve_context(context_type, context_id, *, lock=False):
    pk = _positive_id(context_id)
    if context_type not in ('question', 'answer'):
        raise Http404(REQUEST_UNAVAILABLE)
    if context_type == 'question':
        rows = Question.objects.filter(pk=pk, status='visible')
        if lock:
            rows = rows.select_for_update(of=('self',))
        context = rows.select_related('author').first()
        question = context
    else:
        parent_id = Answer.objects.filter(pk=pk, status='visible', question__status='visible').values_list('question_id', flat=True).first()
        if parent_id is None:
            raise Http404(REQUEST_UNAVAILABLE)
        questions = Question.objects.filter(pk=parent_id, status='visible')
        if lock:
            questions = questions.select_for_update(of=('self',))
        question = questions.first()
        rows = Answer.objects.filter(pk=pk, status='visible', question__status='visible')
        if lock:
            rows = rows.select_for_update(of=('self',))
        context = rows.select_related('author').first()
    if context is None or question is None or not context.author_id:
        raise Http404(REQUEST_UNAVAILABLE)
    return context, question


def _request_available(member, other):
    if member.pk == other.pk:
        raise ValidationError(REQUEST_UNAVAILABLE)
    _require_verified_pair(member, other)
    # Both directions need explicit opt-in; there is no searchable member list.
    enabled = set(MessengerPreference.objects.filter(user_id__in=[member.pk, other.pk], allow_requests=True).values_list('user_id', flat=True))
    if enabled != {member.pk, other.pk}:
        raise ValidationError(REQUEST_UNAVAILABLE)


def get_preferences(user):
    user = _member(user)
    row = MessengerPreference.objects.filter(user=user).values('allow_requests', 'allow_photos').first()
    return row or {'allow_requests': False, 'allow_photos': False}


@transaction.atomic
def save_preferences(user, allow_requests, allow_photos=None):
    if not isinstance(allow_requests, bool):
        raise ValidationError('Choose a valid message-request preference.')
    if allow_photos is not None and not isinstance(allow_photos, bool):
        raise ValidationError('Choose a valid photo-sharing preference.')
    fresh = _member(user)
    member = get_user_model().objects.select_for_update().get(pk=fresh.pk)
    if not member.is_active or ((allow_requests or allow_photos is True) and not member.email_verified):
        raise PermissionDenied('Verify your email before enabling message requests.')
    preference, _ = MessengerPreference.objects.get_or_create(user=member)
    preference.allow_requests = allow_requests
    if allow_photos is not None:
        preference.allow_photos = allow_photos
    preference.save(update_fields=['allow_requests', 'allow_photos', 'updated_at'])
    if not allow_requests:
        # No participant lock is acquired after these thread locks. This keeps
        # the lock order compatible with send/request actions and block writes.
        ids = list(Conversation.objects.select_for_update().filter(
            Q(low_member=member) | Q(high_member=member), status=Conversation.Status.PENDING,
        ).order_by('pk').values_list('pk', flat=True))
        Conversation.objects.filter(pk__in=ids).update(status=Conversation.Status.CLOSED, updated_at=timezone.now())
    return {'allow_requests': preference.allow_requests, 'allow_photos': preference.allow_photos}


def get_request_context(user, context_type, context_id):
    member = _member(user, verified=True)
    context, question = _resolve_context(context_type, context_id)
    _request_available(member, context.author)
    return {
        'context_type': context_type, 'context_id': context.pk,
        'context_title': question.title, 'recipient_name': context.author.public_name,
        'context_url': reverse('community:question_detail', args=[question.pk]),
    }


def _allow_message(member, now):
    sent = Message.objects.filter(sender=member)
    if sent.filter(sent_at__gte=now-timedelta(days=1)).count() >= MESSAGES_PER_DAY:
        raise ValidationError('Your daily private-message allowance has been reached. Try again later.')
    if sent.filter(sent_at__gte=now-timedelta(minutes=1)).count() >= MESSAGES_PER_MINUTE:
        raise ValidationError('Please wait before sending another private message.')


def _safe_message(message, user):
    result = {
        'id': message.pk, 'body': message.body,
        'sender_name': message.sender.public_name if message.sender else 'Former member',
        'is_mine': message.sender_id == user.pk, 'sent_at': message.sent_at,
    }
    try:
        photo = message.photo_attachment
    except Message.photo_attachment.RelatedObjectDoesNotExist:
        photo = None
    if photo is not None:
        result['photo'] = {
            'id': photo.pk, 'width': photo.width, 'height': photo.height,
            'url': reverse('photos:content', args=[photo.pk]),
        }
    return result


@transaction.atomic
def request_chat(user, context_type, context_id, body, client_token):
    # Resolve privately, then recheck the original public context under lock.
    fresh = _member(user, verified=True)
    context, _ = _resolve_context(context_type, context_id)
    if context.author_id == fresh.pk:
        raise ValidationError(REQUEST_UNAVAILABLE)
    pair = sorted([fresh.pk, context.author_id])
    member, other = _lock_pair(fresh, pair)
    context, _ = _resolve_context(context_type, context_id, lock=True)
    if context.author_id != other.pk:
        raise ValidationError(REQUEST_UNAVAILABLE)
    _request_available(member, other)
    body, token = _body(body), _token(client_token)
    existing = Conversation.objects.select_for_update().filter(low_member_id=pair[0], high_member_id=pair[1]).first()
    if existing is not None:
        repeated = Message.objects.filter(conversation=existing, sender=member, client_token=token, body=body).exists()
        if existing.status == Conversation.Status.PENDING and existing.initiated_by_id == member.pk and repeated:
            return existing.pk
        raise ValidationError('A message request or conversation already exists. Open your inbox instead.')
    now = timezone.now()
    requests = Conversation.objects.filter(initiated_by=member)
    if requests.filter(created_at__gte=now-timedelta(days=1)).count() >= REQUESTS_PER_DAY:
        raise ValidationError('Your daily message-request allowance has been reached. Try again later.')
    if requests.filter(created_at__gte=now-timedelta(minutes=5)).count() >= REQUESTS_PER_FIVE_MINUTES:
        raise ValidationError('Please wait before sending another message request.')
    _allow_message(member, now)
    row = Conversation.objects.create(low_member_id=pair[0], high_member_id=pair[1], initiated_by=member)
    Message.objects.create(conversation=row, sender=member, body=body, client_token=token)
    return row.pk


@transaction.atomic
def respond_request(user, conversation_id, accept):
    if not isinstance(accept, bool):
        raise ValidationError('Choose whether to accept or decline this request.')
    row, member, other = _conversation(user, conversation_id, lock=True)
    if row.initiated_by_id == member.pk:
        raise PermissionDenied('Only the recipient can answer a message request.')
    if row.status != Conversation.Status.PENDING:
        raise ValidationError('This request has already been answered or closed.')
    if accept:
        _request_available(member, other)
    row.status = Conversation.Status.ACCEPTED if accept else Conversation.Status.DECLINED
    row.save(update_fields=['status', 'updated_at'])
    return row.pk


@transaction.atomic
def close_conversation(user, conversation_id):
    row, _, _ = _conversation(user, conversation_id, lock=True)
    if row.status not in (Conversation.Status.CLOSED, Conversation.Status.DECLINED):
        row.status = Conversation.Status.CLOSED
        row.save(update_fields=['status', 'updated_at'])
    return row.pk


@transaction.atomic
def block_peer(user, conversation_id):
    _, member, other = _conversation(user, conversation_id, lock=True)
    CommunityBlock.objects.get_or_create(user=member, blocked_user=other)


@transaction.atomic
def send_message(user, conversation_id, body, client_token):
    row, member, other = _conversation(user, conversation_id, lock=True)
    _require_verified_pair(member, other)
    if row.status != Conversation.Status.ACCEPTED:
        raise ValidationError('Messages are available only after the recipient accepts the request.')
    body, token = _body(body), _token(client_token)
    existing = Message.objects.filter(conversation=row, sender=member, client_token=token).select_related('sender', 'photo_attachment').first()
    if existing:
        if existing.body != body:
            raise ValidationError('This send token has already been used. Reload the form.')
        return _safe_message(existing, member)
    now = timezone.now()
    _allow_message(member, now)
    message = Message.objects.create(conversation=row, sender=member, body=body, client_token=token)
    row.updated_at = now
    row.save(update_fields=['updated_at'])
    # Newly created text messages have no attachment; avoid a projection query.
    message._state.fields_cache['photo_attachment'] = None
    return _safe_message(message, member)


@transaction.atomic
def mark_read(user, conversation_id, message_id):
    row, member, _ = _conversation(user, conversation_id, lock=True)
    message_id = _positive_id(message_id)
    if not Message.objects.filter(pk=message_id, conversation=row).exists():
        raise Http404(UNAVAILABLE)
    field = 'low_read_through' if member.pk == row.low_member_id else 'high_read_through'
    if message_id > getattr(row, field):
        setattr(row, field, message_id)
        # Read state is private and does not reorder the other member's inbox.
        row.save(update_fields=[field])


def _blocked_ids(user):
    rows = CommunityBlock.objects.filter(Q(user=user) | Q(blocked_user=user)).values_list('user_id', 'blocked_user_id')
    return {target if actor == user.pk else actor for actor, target in rows}


def _safe_conversation(row, member, other, blocked):
    available = other.is_active and other.email_verified and member.email_verified and not blocked
    return {
        'id': row.pk, 'other_name': other.public_name,
        'status': row.status, 'status_label': row.get_status_display(),
        'is_initiator': row.initiated_by_id == member.pk,
        'is_incoming': row.initiated_by_id != member.pk,
        'blocked': blocked, 'can_accept': row.status == Conversation.Status.PENDING and row.initiated_by_id != member.pk and available,
        'can_send': row.status == Conversation.Status.ACCEPTED and available,
    }


def get_inbox(user, folder='all', page=1, per_page=20):
    member = _member(user)
    if folder not in ('all', 'pending', 'accepted', 'closed'):
        raise ValidationError('Choose a valid inbox folder.')
    per_page = min(max(int(per_page), 1), 30)
    blocked = _blocked_ids(member)
    rows = Conversation.objects.filter(Q(low_member=member) | Q(high_member=member))
    if folder == 'closed':
        rows = rows.filter(status__in=[Conversation.Status.CLOSED, Conversation.Status.DECLINED])
    elif folder != 'all':
        rows = rows.filter(status=folder)
    rows = rows.annotate(_read_through=Case(When(low_member=member, then=F('low_read_through')), default=F('high_read_through'), output_field=BigIntegerField()))
    unread = Message.objects.filter(conversation_id=OuterRef('pk'), pk__gt=OuterRef('_read_through')).exclude(sender=member)
    rows = rows.annotate(_unread=Exists(unread)).select_related('low_member', 'high_member').order_by('-updated_at', '-pk')
    result = Paginator(rows, per_page).get_page(page)
    projected = []
    for row in result.object_list:
        other = row.high_member if row.low_member_id == member.pk else row.low_member
        data = _safe_conversation(row, member, other, other.pk in blocked)
        data.update(unread=bool(row._unread and not data['blocked'] and other.is_active), updated_at=row.updated_at)
        projected.append(data)
    result.object_list = projected
    result.paginator.object_list = projected
    return result


def get_thread(user, conversation_id, before=None, limit=50):
    row, member, other = _conversation(user, conversation_id)
    limit = min(max(int(limit), 1), 50)
    query = Message.objects.filter(conversation=row)
    if before is not None:
        query = query.filter(pk__lt=_positive_id(before))
    messages = list(query.select_related('sender', 'photo_attachment').order_by('-pk')[:limit+1])
    has_older = len(messages) > limit
    messages = messages[:limit]
    next_before = messages[-1].pk if has_older and messages else None
    messages.reverse()
    return {
        'conversation': _safe_conversation(row, member, other, interaction_blocked(member.pk, other.pk)),
        'messages': [_safe_message(message, member) for message in messages],
        'next_before': next_before,
        'last_message_id': messages[-1].pk if messages else None,
    }


def get_selected_message(user, conversation_id, message_id):
    row, member, _ = _conversation(user, conversation_id)
    message = Message.objects.filter(pk=_positive_id(message_id), conversation=row).select_related('sender', 'photo_attachment').first()
    if message is None:
        raise Http404(UNAVAILABLE)
    return _safe_message(message, member)


def report_message(user, conversation_id, message_id, category, details):
    # Photo copies need an outermost commit so failures can clean up storage.
    row, _, _ = _conversation(user, conversation_id)
    message = Message.objects.filter(pk=_positive_id(message_id), conversation=row).select_related('photo_attachment').first()
    if message is None:
        raise Http404(UNAVAILABLE)
    try:
        message.photo_attachment
    except Message.photo_attachment.RelatedObjectDoesNotExist:
        return _create_message_report(user, conversation_id, message_id, category, details)
    from photos.services import report_photo_message
    return report_photo_message(user, conversation_id, message_id, category, details)


@transaction.atomic
def _create_message_report(user, conversation_id, message_id, category, details):
    row, member, _ = _conversation(user, conversation_id, lock=True)
    message = Message.objects.filter(pk=_positive_id(message_id), conversation=row).select_related('sender').first()
    if message is None:
        raise Http404(UNAVAILABLE)
    if message.sender_id == member.pk:
        raise ValidationError('Report the other participant\'s message.')
    if category not in MessageReport.Category.values or not isinstance(details, str) or not details.strip() or len(details.strip()) > 2000:
        raise ValidationError('Choose a report category and explain your concern in at most 2000 characters.')
    if MessageReport.objects.filter(reporter=member, message=message).exists():
        raise ValidationError('You already reported this message.')
    if MessageReport.objects.filter(reporter=member, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily report allowance has been reached. Try again later.')
    report = MessageReport.objects.create(
        message=message, reporter=member, category=category, details=details.strip(),
        evidence={'body': message.body, 'sent_at': message.sent_at.isoformat(), 'sender_name': message.sender.public_name if message.sender else 'Former member'},
    )
    return report.pk


@transaction.atomic
def moderate_report(actor, report_id, status, moderator_note):
    fresh = _member(actor)
    reviewer = get_user_model().objects.select_for_update().get(pk=fresh.pk)
    if not reviewer.is_active or not reviewer.has_perm('messenger.can_review_message_reports'):
        raise PermissionDenied('You cannot review private-message evidence.')
    if status not in MessageReport.Status.values or not isinstance(moderator_note, str) or not moderator_note.strip() or len(moderator_note.strip()) > 2000:
        raise ValidationError('Choose a valid decision and explain it in at most 2000 characters.')
    report = MessageReport.objects.select_for_update().filter(pk=_positive_id(report_id)).first()
    if report is None:
        raise Http404('Report unavailable.')
    previous = report.status
    report.status, report.moderator_note = status, moderator_note.strip()
    report.moderated_by, report.moderated_at = reviewer, timezone.now()
    report.save(update_fields=['status', 'moderator_note', 'moderated_by', 'moderated_at', 'updated_at'])
    ReportDecision.objects.create(report=report, actor=reviewer, previous_status=previous, new_status=status, moderator_note=report.moderator_note)
    return report
