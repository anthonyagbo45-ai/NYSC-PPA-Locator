"""Opt-in in-app delivery; no email, push provider or private title snapshots."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import DateTimeField, DurationField, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Value
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .models import (
    FIRST_EVENT, LAST_EVENT, REMINDER_VALUES, NoticeSubscription, Notification,
    NotificationPreference, PersonalEvent, validate_timezone_name,
)


DELIVERY_GRACE = timedelta(hours=24)
MAX_ACTIVE_EVENTS = 100
MAX_SUBSCRIPTIONS = 100
EVENT_FIELDS = ('title', 'note', 'kind', 'starts_at', 'ends_at', 'timezone_name', 'remind_before', 'notifications_enabled', 'status')


def _member(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied('Sign in to use your private reminders.')


def _lock_member(user):
    _member(user)
    member = get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).first()
    if member is None:
        raise PermissionDenied('This account cannot use reminders.')
    return member


def _now(value):
    value = value or timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError('Use an aware date and time.')
    return value


def make_local_datetime(date, time, timezone_name):
    """Reject DST gaps/folds rather than silently choosing the wrong instant."""
    validate_timezone_name(timezone_name)
    try:
        naive = datetime.combine(date, time).replace(tzinfo=None)
        zone = ZoneInfo(timezone_name)
        candidates = []
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=fold)
            utc = candidate.astimezone(datetime_timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.append(utc)
        unique = set(candidates)
        if len(unique) != 1:
            raise ValidationError('This local time is missing or ambiguous because of a clock change. Choose another time, or enter the exact time using UTC.')
        result = unique.pop()
        if not FIRST_EVENT <= result < LAST_EVENT:
            raise ValidationError('Choose a date between 1900 and 2100.')
        return result
    except (TypeError, ValueError, OverflowError):
        raise ValidationError('Choose a valid date, time and time zone.')


def _validate_reminder_time(starts_at, lead, now):
    if lead not in REMINDER_VALUES:
        raise ValidationError({'remind_before': 'Choose a supported reminder interval.'})
    scheduled = starts_at - timedelta(minutes=lead)
    if scheduled < now - DELIVERY_GRACE:
        raise ValidationError({'remind_before': 'That reminder time is more than a day in the past. Choose a later date or a shorter interval.'})
    return scheduled


@transaction.atomic
def save_event(user, cleaned_data, event_id=None):
    member = _lock_member(user)
    event = get_object_or_404(PersonalEvent.objects.select_for_update(), pk=event_id, user=member) if event_id is not None else PersonalEvent(user=member)
    new = event.pk is None
    if new and PersonalEvent.objects.filter(user=member, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily personal-event limit has been reached. Please update an existing event or try tomorrow.')
    before = {name: getattr(event, name) for name in EVENT_FIELDS}
    for name in EVENT_FIELDS:
        if name in cleaned_data:
            setattr(event, name, cleaned_data[name])
    event.title = event.title.strip()
    event.note = event.note.strip()
    changed = new or any(before[name] != getattr(event, name) for name in EVENT_FIELDS)
    if not new and changed:
        event.version += 1
    event.full_clean()
    if event.status == PersonalEvent.Status.ACTIVE:
        active = PersonalEvent.objects.filter(user=member, status=PersonalEvent.Status.ACTIVE).exclude(pk=event.pk)
        if active.count() >= MAX_ACTIVE_EVENTS:
            raise ValidationError('Complete or cancel an existing event before adding another active one.')
    if event.notifications_enabled and event.status == PersonalEvent.Status.ACTIVE:
        _validate_reminder_time(event.starts_at, event.remind_before, timezone.now())
    if changed:
        event.save()
    return event


@transaction.atomic
def delete_event(user, event_id):
    _lock_member(user)
    get_object_or_404(PersonalEvent.objects.select_for_update(), pk=event_id, user=user).delete()


@transaction.atomic
def save_preferences(user, personal_reminders=False, notice_reminders=False, community_replies=None):
    member = _lock_member(user)
    if not isinstance(personal_reminders, bool) or not isinstance(notice_reminders, bool):
        raise ValidationError('Choose valid reminder preferences.')
    if community_replies is not None and not isinstance(community_replies, bool):
        raise ValidationError('Choose valid community notification preferences.')
    preference = NotificationPreference.objects.select_for_update().filter(user=member).first() or NotificationPreference(user=member)
    now = timezone.now()
    if not personal_reminders:
        Notification.objects.filter(user=member, event__isnull=False, dismissed_at__isnull=True).update(dismissed_at=now)
    if not notice_reminders:
        Notification.objects.filter(user=member, subscription__isnull=False, dismissed_at__isnull=True).update(dismissed_at=now)
    preference.personal_reminders = personal_reminders
    preference.notice_reminders = notice_reminders
    # Legacy calendar callers do not change community consent. A genuine
    # opt-in starts a new window, so enabling never backfills old replies.
    if community_replies is not None:
        if community_replies and not preference.community_replies:
            preference.community_enabled_at = now
        elif not community_replies:
            from .models import CommunityNotification
            CommunityNotification.objects.filter(user=member, dismissed_at__isnull=True).update(dismissed_at=now)
            preference.community_enabled_at = None
        preference.community_replies = community_replies
    preference.full_clean()
    preference.save()
    return preference


def _eligible_notice(notice, now, *, future=False):
    from service_resources.services import notice_reminder_eligible
    return notice_reminder_eligible(notice, now=now, require_future_event=future)


def _notice_query():
    from service_resources.models import ServiceNotice
    return ServiceNotice.objects.select_related('group', 'state', 'lga')


def _subscription_current(subscription, now):
    notice = subscription.notice
    return (
        subscription.enabled and subscription.notice_version == notice.version
        and subscription.group_version == (notice.group.version if notice.group_id else None)
        and _eligible_notice(notice, now)
    )


@transaction.atomic
def subscribe_notice(user, notice, remind_before=60):
    member = _lock_member(user)
    # A subscription is bound to the exact current public version, not merely
    # a previously displayed notice ID or the member's claimed affiliation.
    notice = get_object_or_404(_notice_query(), pk=notice.pk)
    now = timezone.now()
    if not _eligible_notice(notice, now, future=True):
        raise ValidationError('This notice does not currently qualify for a confirmed-source reminder. Confirm the date with your designated NYSC official or create a clearly personal event.')
    _validate_reminder_time(notice.event_at, remind_before, now)
    subscription = NoticeSubscription.objects.select_for_update().filter(user=member, notice=notice).first()
    group_version = notice.group.version if notice.group_id else None
    if subscription and subscription.enabled and subscription.notice_version == notice.version and subscription.group_version == group_version and subscription.remind_before == remind_before:
        return subscription
    if NoticeSubscription.objects.filter(user=member, enabled=True).exclude(pk=subscription.pk if subscription else None).count() >= MAX_SUBSCRIPTIONS:
        raise ValidationError('Remove an existing notice reminder before subscribing to another one.')
    if subscription is None:
        subscription = NoticeSubscription(user=member, notice=notice)
    else:
        subscription.version += 1
    subscription.enabled = True
    subscription.notice_version = notice.version
    subscription.group_version = group_version
    subscription.remind_before = remind_before
    subscription.full_clean()
    subscription.save()
    return subscription


@transaction.atomic
def unsubscribe_notice(user, subscription_id):
    _lock_member(user)
    subscription = get_object_or_404(NoticeSubscription.objects.select_for_update(), pk=subscription_id, user=user)
    if subscription.enabled:
        subscription.enabled = False
        subscription.version += 1
        subscription.save(update_fields=['enabled', 'version', 'updated_at'])
    Notification.objects.filter(user=user, subscription=subscription, dismissed_at__isnull=True).update(dismissed_at=timezone.now())
    return subscription


def _scheduled(expression, lead):
    # SQLite's duration expression requires an IntegerField operand, rather
    # than the PositiveIntegerField used to constrain a reminder's lead time.
    interval = ExpressionWrapper(Cast(F(lead), IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField())
    return ExpressionWrapper(F(expression) - interval, output_field=DateTimeField())


def _notice_filters(prefix, now):
    base = Q(**{
        prefix+'publication_status': 'approved', prefix+'expires_at__gt': now,
        prefix+'verification_status__in': ['checked', 'organisation', 'official'],
        prefix+'checked_at__isnull': False, prefix+'checked_at__lte': now,
        prefix+'event_at__isnull': False, prefix+'event_at__lte': F(prefix+'expires_at'),
    }) & ~Q(**{prefix+'source_url': ''})
    return base & (Q(**{prefix+'group__isnull': True}) | Q(**{prefix+'group__publication_status': 'approved', prefix+'group__expires_at__gt': now}))


def _due_events(now):
    existing = Notification.objects.filter(event_id=OuterRef('pk'), source_version=OuterRef('version'), scheduled_for=OuterRef('_reminder_at'))
    return PersonalEvent.objects.filter(user__is_active=True, user__reminder_preferences__personal_reminders=True,
        notifications_enabled=True, status=PersonalEvent.Status.ACTIVE,
        starts_at__gte=now-DELIVERY_GRACE,
        starts_at__lte=now+timedelta(minutes=max(REMINDER_VALUES))).annotate(
        _reminder_at=_scheduled('starts_at', 'remind_before'),
    ).filter(_reminder_at__lte=now, _reminder_at__gte=now-DELIVERY_GRACE).annotate(_delivered=Exists(existing)).filter(_delivered=False)


def _due_subscriptions(now):
    existing = Notification.objects.filter(subscription_id=OuterRef('pk'), source_version=OuterRef('version'), scheduled_for=OuterRef('_reminder_at'))
    current_group = Q(notice__group__isnull=True, group_version__isnull=True) | Q(group_version=F('notice__group__version'))
    return NoticeSubscription.objects.filter(user__is_active=True, user__reminder_preferences__notice_reminders=True,
        enabled=True, notice_version=F('notice__version'),
        notice__event_at__gte=now-DELIVERY_GRACE,
        notice__event_at__lte=now+timedelta(minutes=max(REMINDER_VALUES))).filter(_notice_filters('notice__', now), current_group).annotate(
        _reminder_at=_scheduled('notice__event_at', 'remind_before'),
    ).filter(_reminder_at__lte=now, _reminder_at__gte=now-DELIVERY_GRACE).annotate(_delivered=Exists(existing)).filter(_delivered=False)


def _key(kind, pk, version, scheduled):
    instant = scheduled.astimezone(datetime_timezone.utc).isoformat()
    return f'{kind}:{pk}:{version}:' + sha256(instant.encode()).hexdigest()[:24]


@transaction.atomic
def dispatch_user_reminders(user, now=None, limit=100, dry_run=False):
    member = _lock_member(user)
    now = _now(now)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValidationError('Choose a reminder batch size between 1 and 200.')
    pending = []
    for event in _due_events(now).filter(user=member).order_by('_reminder_at', 'pk')[:limit]:
        pending.append(Notification(user=member, event=event, source_version=event.version, scheduled_for=event._reminder_at,
                                    dedup_key=_key('event', event.pk, event.version, event._reminder_at)))
    remaining = limit - len(pending)
    if remaining:
        subscriptions = _due_subscriptions(now).filter(user=member).select_related('notice__group', 'notice__state', 'notice__lga').order_by('_reminder_at', 'pk')[:remaining]
        for subscription in subscriptions:
            if _subscription_current(subscription, now):
                pending.append(Notification(user=member, subscription=subscription, source_version=subscription.version, scheduled_for=subscription._reminder_at,
                                            dedup_key=_key('notice', subscription.pk, subscription.version, subscription._reminder_at)))
    if not dry_run and pending:
        Notification.objects.bulk_create(pending)
    return len(pending)


def dispatch_reminders(now=None, limit=1000, dry_run=False):
    now = _now(now)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise ValidationError('Choose a delivery limit between 1 and 5000.')
    # Excluding already delivered targets prevents a permanently repeated first
    # batch from starving later members when a scheduled job is bounded.
    event_users = _due_events(now).order_by().values_list('user_id', flat=True)
    notice_users = _due_subscriptions(now).order_by().values_list('user_id', flat=True)
    candidates = event_users.union(notice_users).order_by('user_id')[:limit]
    total = 0
    examined = 0
    for user in get_user_model().objects.filter(pk__in=candidates, is_active=True).order_by('pk'):
        examined += 1
        try:
            total += dispatch_user_reminders(user, now=now, limit=min(200, limit-total), dry_run=dry_run)
        except PermissionDenied:
            # Account deactivation/deletion after candidate selection revokes
            # its delivery without stopping unrelated members' reminders.
            continue
        if total >= limit:
            break
    return {'created': 0 if dry_run else total, 'would_create': total if dry_run else 0, 'examined_users': examined, 'dry_run': dry_run}


def _visible_queryset(user, now):
    _member(user)
    events = Q(event__user=user, event__notifications_enabled=True, event__status=PersonalEvent.Status.ACTIVE,
               source_version=F('event__version'), user__reminder_preferences__personal_reminders=True)
    groups = Q(subscription__notice__group__isnull=True, subscription__group_version__isnull=True) | Q(subscription__group_version=F('subscription__notice__group__version'))
    notices = Q(subscription__user=user, subscription__enabled=True, source_version=F('subscription__version'),
                subscription__notice_version=F('subscription__notice__version'), user__reminder_preferences__notice_reminders=True)
    notices &= groups & _notice_filters('subscription__notice__', now)
    return Notification.objects.filter(user=user, user__is_active=True, dismissed_at__isnull=True, scheduled_for__lte=now).filter(events | notices).select_related('event', 'subscription__notice__group', 'subscription__notice__state', 'subscription__notice__lga')


def _row_current(row, now):
    return row.event_id is not None or _subscription_current(row.subscription, now)


def visible_notifications(user, now=None, limit=200):
    now = _now(now)
    limit = min(max(int(limit), 1), 200)
    result = []
    for row in _visible_queryset(user, now).order_by('-created_at', '-pk')[:limit]:
        if not _row_current(row, now):
            continue
        target = row.event if row.event_id else row.subscription.notice
        result.append({
            'id': row.pk, 'title': target.title, 'kind': 'personal' if row.event_id else 'notice',
            'label': 'Your personal reminder — confirm any official date separately' if row.event_id else 'Confirmed-source notice reminder — confirm with your designated NYSC official',
            'event_id': row.event_id, 'subscription_id': row.subscription_id,
            'notice_id': row.subscription.notice_id if row.subscription_id else None,
            'starts_at': target.starts_at if row.event_id else target.event_at,
            'scheduled_for': row.scheduled_for, 'timezone_name': target.timezone_name,
            'is_read': row.read_at is not None, 'created_at': row.created_at,
        })
    return result


def unread_count(user, now=None):
    # Service caps allow at most 100 active events + 100 enabled subscriptions,
    # each with one current-version delivery. No private source title is read
    # for a stale/ineligible notice and no per-row database query is required.
    return sum(not row['is_read'] for row in visible_notifications(user, now=now))


def dashboard_summary(user, now=None):
    _member(user)
    now = _now(now)
    upcoming = PersonalEvent.objects.filter(user=user, status=PersonalEvent.Status.ACTIVE, starts_at__gte=now).order_by('starts_at', 'pk').values('id', 'title', 'starts_at', 'timezone_name', 'kind')[:3]
    events = list(upcoming)
    for event in events:
        event['kind_label'] = dict(PersonalEvent.Kind.choices)[event['kind']]
    return {'upcoming_events': events, 'unread_count': unread_count(user, now=now)}


def subscription_rows(user, now=None):
    _member(user)
    now = _now(now)
    result = []
    rows = NoticeSubscription.objects.filter(user=user, enabled=True).select_related('notice__group').order_by('-updated_at', '-pk')[:100]
    for subscription in rows:
        current = _subscription_current(subscription, now)
        result.append({'id': subscription.pk, 'notice_id': subscription.notice_id,
                       'title': subscription.notice.title if current else 'Notice needs renewed confirmation',
                       'is_current': current, 'enabled': subscription.enabled,
                       'remind_before': subscription.remind_before, 'notice_version': subscription.notice_version})
    return result


@transaction.atomic
def mark_read(user, notification_id):
    _lock_member(user)
    now = timezone.now()
    row = get_object_or_404(_visible_queryset(user, now), pk=notification_id)
    if not _row_current(row, now):
        from django.http import Http404
        raise Http404
    if row.read_at is None:
        Notification.objects.filter(pk=row.pk, user=user).update(read_at=now)
    return True


@transaction.atomic
def dismiss_notification(user, notification_id):
    _lock_member(user)
    now = timezone.now()
    row = get_object_or_404(_visible_queryset(user, now), pk=notification_id)
    if not _row_current(row, now):
        from django.http import Http404
        raise Http404
    Notification.objects.filter(pk=row.pk, user=user).update(dismissed_at=now)
    return True
