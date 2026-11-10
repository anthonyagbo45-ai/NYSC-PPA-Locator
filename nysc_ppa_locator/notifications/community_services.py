"""Deferred, consent-bound community alerts with current access on every read."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.http import Http404
from django.urls import reverse

from .models import CommunityActivity, CommunityNotification, NotificationPreference
from .services import _lock_member, _member, _now


DELIVERY_GRACE = timedelta(hours=24)


def record_answer_activity(answer):
    """Call inside source creation's transaction. This never delivers anything."""
    if not answer.pk or not answer.author_id or answer.status != 'visible' or answer.question.status != 'visible':
        return None
    return CommunityActivity.objects.get_or_create(answer_id=answer.pk, defaults={
        'actor_id': answer.author_id, 'source_version': answer.version,
        'parent_version': answer.question.version, 'occurred_at': answer.created_at,
    })[0]


def record_room_activity(post):
    """Record one creation, in the source transaction; edits never replay it."""
    if not post.pk or not post.author_id or post.status != 'visible' or post.room.status != 'active':
        return None
    return CommunityActivity.objects.get_or_create(room_post_id=post.pk, defaults={
        'actor_id': post.author_id, 'source_version': post.version,
        'parent_version': post.room.version, 'occurred_at': post.created_at,
    })[0]


def _batch_limit(limit, maximum):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValidationError(f'Choose a community alert limit between 1 and {maximum}.')
    return limit


def _activities(user, now, *, due=False):
    """Filter consent, access, source versions and blocks before any page limit."""
    from community.models import CommunityBlock, QuestionFollow
    from local_rooms.models import RoomMembership
    preference = NotificationPreference.objects.filter(user_id=user.pk, community_replies=True, community_enabled_at__isnull=False).first()
    if preference is None:
        return CommunityActivity.objects.none()
    follows = QuestionFollow.objects.filter(user_id=user.pk, question_id=OuterRef('answer__question_id'),
        enabled=True, enabled_at__isnull=False, enabled_at__lte=OuterRef('occurred_at'))
    memberships = RoomMembership.objects.filter(user_id=user.pk, room_id=OuterRef('room_post__room_id'),
        status='approved', notifications_enabled=True, notifications_enabled_at__isnull=False,
        approved_at__isnull=False, approved_at__lte=OuterRef('occurred_at'), notifications_enabled_at__lte=OuterRef('occurred_at'))
    blocked_actor = CommunityBlock.objects.filter(
        Q(user_id=user.pk, blocked_user_id=OuterRef('actor_id')) | Q(user_id=OuterRef('actor_id'), blocked_user_id=user.pk))
    blocked_parent = CommunityBlock.objects.filter(
        Q(user_id=user.pk, blocked_user_id=OuterRef('answer__question__author_id')) |
        Q(user_id=OuterRef('answer__question__author_id'), blocked_user_id=user.pk))
    answers = Q(answer__status='visible', answer__question__status='visible', source_version=F('answer__version'),
                parent_version=F('answer__question__version'), actor_id=F('answer__author_id'), _followed=True, _parent_blocked=False)
    rooms = Q(room_post__status='visible', room_post__room__status='active', source_version=F('room_post__version'),
              parent_version=F('room_post__room__version'), actor_id=F('room_post__author_id'), _joined=True)
    rows = CommunityActivity.objects.filter(occurred_at__lte=now, occurred_at__gte=preference.community_enabled_at,
        actor__is_active=True).exclude(actor_id=user.pk).annotate(
            _followed=Exists(follows), _joined=Exists(memberships), _actor_blocked=Exists(blocked_actor), _parent_blocked=Exists(blocked_parent),
        ).filter(answers | rooms, _actor_blocked=False)
    if due:
        delivered = CommunityNotification.objects.filter(user_id=user.pk, activity_id=OuterRef('pk'))
        rows = rows.filter(occurred_at__gte=now-DELIVERY_GRACE).annotate(_delivered=Exists(delivered)).filter(_delivered=False)
    return rows


@transaction.atomic
def dispatch_user_community(user, now=None, limit=100, dry_run=False):
    member = _lock_member(user)
    now = _now(now)
    _batch_limit(limit, 200)
    activity_ids = _activities(member, now, due=True).order_by('occurred_at', 'pk').values_list('pk', flat=True)[:limit]
    pending = [CommunityNotification(user=member, activity_id=pk) for pk in activity_ids]
    if pending and not dry_run:
        # Recipient row locking serializes explicit checks and dispatcher runs;
        # the database unique constraint provides the final idempotency boundary.
        CommunityNotification.objects.bulk_create(pending)
    return len(pending)


def _candidate_users(now):
    # Exclude deliveries in the candidate query itself, so later members are not
    # starved by a repeated first batch. Detailed access is rechecked under lock.
    from community.models import CommunityBlock
    pending = CommunityActivity.objects.filter(occurred_at__gte=now-DELIVERY_GRACE, occurred_at__lte=now,
        actor__is_active=True).filter(occurred_at__gte=OuterRef('reminder_preferences__community_enabled_at')).exclude(actor_id=OuterRef('pk'))
    delivered = CommunityNotification.objects.filter(user_id=OuterRef(OuterRef('pk')), activity_id=OuterRef('pk'))
    # Nested OuterRef targets the candidate member; the inner reference targets
    # this activity. This is the same either-direction boundary as
    # community.services.interaction_blocked, expressed in one database query.
    blocked_actor = CommunityBlock.objects.filter(
        Q(user_id=OuterRef(OuterRef('pk')), blocked_user_id=OuterRef('actor_id')) |
        Q(user_id=OuterRef('actor_id'), blocked_user_id=OuterRef(OuterRef('pk'))))
    blocked_parent = CommunityBlock.objects.filter(
        Q(user_id=OuterRef(OuterRef('pk')), blocked_user_id=OuterRef('answer__question__author_id')) |
        Q(user_id=OuterRef('answer__question__author_id'), blocked_user_id=OuterRef(OuterRef('pk'))))
    pending = pending.annotate(_delivered=Exists(delivered), _actor_blocked=Exists(blocked_actor), _parent_blocked=Exists(blocked_parent)).filter(_delivered=False, _actor_blocked=False).filter(
        Q(answer__question__follows__user_id=OuterRef('pk'), answer__question__follows__enabled=True,
          answer__question__follows__enabled_at__lte=F('occurred_at'), answer__status='visible', answer__question__status='visible',
          source_version=F('answer__version'), parent_version=F('answer__question__version'), actor_id=F('answer__author_id'), _parent_blocked=False) |
        Q(room_post__room__memberships__user_id=OuterRef('pk'), room_post__room__memberships__status='approved',
          room_post__room__memberships__notifications_enabled=True, room_post__room__memberships__approved_at__lte=F('occurred_at'),
          room_post__room__memberships__notifications_enabled_at__lte=F('occurred_at'), room_post__status='visible',
          room_post__room__status='active', source_version=F('room_post__version'), parent_version=F('room_post__room__version'), actor_id=F('room_post__author_id')))
    return get_user_model().objects.filter(is_active=True, reminder_preferences__community_replies=True,
        reminder_preferences__community_enabled_at__isnull=False).annotate(_pending=Exists(pending)).filter(_pending=True).order_by('pk')


def dispatch_community(now=None, limit=1000, dry_run=False):
    now = _now(now)
    _batch_limit(limit, 5000)
    total = examined = 0
    for user in _candidate_users(now).iterator(chunk_size=200):
        examined += 1
        try:
            total += dispatch_user_community(user, now=now, limit=min(200, limit-total), dry_run=dry_run)
        except PermissionDenied:
            continue
        if total >= limit:
            break
    return {'created': 0 if dry_run else total, 'would_create': total if dry_run else 0,
            'examined_users': examined, 'dry_run': dry_run}


def _visible_rows(user, now):
    _member(user)
    return CommunityNotification.objects.filter(user_id=user.pk, user__is_active=True, dismissed_at__isnull=True,
        activity_id__in=_activities(user, now).values('pk')).select_related('activity__answer__question', 'activity__room_post__room').only(
            'id', 'activity_id', 'read_at', 'created_at', 'activity__id', 'activity__answer_id', 'activity__room_post_id', 'activity__occurred_at',
            'activity__answer__id', 'activity__answer__question_id', 'activity__answer__question__id', 'activity__answer__question__title',
            'activity__room_post__id', 'activity__room_post__room_id', 'activity__room_post__room__id', 'activity__room_post__room__title')


def community_items(user, now=None, limit=200):
    now = _now(now)
    _batch_limit(limit, 200)
    result = []
    for row in _visible_rows(user, now).order_by('-created_at', '-pk')[:limit]:
        activity = row.activity
        if activity.answer_id:
            question = activity.answer.question
            kind, title = 'answer', question.title
            url = reverse('community:question_detail', args=[question.pk]) + f'#answer-{activity.answer_id}'
        else:
            room = activity.room_post.room
            kind, title = 'room', room.title
            url = reverse('local_rooms:room_detail', args=[room.pk]) + f'#post-{activity.room_post_id}'
        result.append({'id': row.pk, 'kind': kind, 'title': title, 'url': url,
            'label': 'New reply to a question you follow' if kind == 'answer' else 'New post in a room you follow',
            'is_read': row.read_at is not None, 'created_at': row.created_at, 'occurred_at': activity.occurred_at})
    return result


def community_unread_count(user, now=None):
    now = _now(now)
    return _visible_rows(user, now).filter(read_at__isnull=True).count()


@transaction.atomic
def mark_community_read(user, notification_id):
    member = _lock_member(user)
    now = _now(None)
    row = _visible_rows(member, now).filter(pk=notification_id).first()
    if row is None:
        raise Http404('This community alert is unavailable.')
    CommunityNotification.objects.filter(pk=row.pk, user=member, read_at__isnull=True).update(read_at=now)
    return True


@transaction.atomic
def dismiss_community_notification(user, notification_id):
    member = _lock_member(user)
    now = _now(None)
    row = _visible_rows(member, now).filter(pk=notification_id).first()
    if row is None:
        raise Http404('This community alert is unavailable.')
    CommunityNotification.objects.filter(pk=row.pk, user=member).update(dismissed_at=now)
    return True
