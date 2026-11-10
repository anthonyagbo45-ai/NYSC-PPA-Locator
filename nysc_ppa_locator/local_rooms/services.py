"""Atomic room writes and deliberately small public/private projections."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import BooleanField, Case, Exists, IntegerField, OuterRef, Prefetch, Q, Value, When
from django.http import Http404
from django.utils import timezone

from .models import (Room, RoomMembership, RoomMembershipRevision, RoomPost,
                     RoomPostRevision, RoomReport, RoomReportRevision, RoomRevision, validate_source_url)


ROOM_FIELDS = ('title', 'description', 'guidelines', 'state', 'lga', 'ppa', 'group', 'visibility', 'status', 'moderator_note')


def _member(user, *, verified=True):
    if not user or not user.is_authenticated or not user.is_active or (verified and not user.email_verified):
        raise PermissionDenied('Sign in and verify your email before participating.')


def _lock_member(user, *, verified=True):
    _member(user, verified=verified)
    fresh = get_user_model().objects.select_for_update().get(pk=user.pk)
    _member(fresh, verified=verified)
    return fresh


def _permission(actor, permission):
    _member(actor, verified=False)
    if not actor.has_perm('local_rooms.' + permission):
        raise PermissionDenied('This action requires a separately authorised room moderator.')


def _snapshot(obj):
    result = {}
    for field in obj._meta.concrete_fields:
        value = getattr(obj, field.attname)
        result[field.attname] = value.isoformat() if hasattr(value, 'isoformat') else value
    return result


def _audit(obj, actor, reason):
    model, field = {
        Room: (RoomRevision, 'room'), RoomMembership: (RoomMembershipRevision, 'membership'), RoomPost: (RoomPostRevision, 'post'),
    }[type(obj)]
    model.objects.create(**{field: obj}, actor=actor, snapshot=_snapshot(obj), reason=reason)


def _reason(reason):
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise ValidationError('Record a reason of up to 1,000 characters for this decision.')
    return reason


def _safe_source(value):
    try:
        validate_source_url(value)
    except ValidationError:
        return ''
    return value


def _active(room):
    if room.status != Room.Status.ACTIVE:
        raise ValidationError('This room is paused or archived and is not accepting new participation.')


def _post_room_locked(post):
    room_id = RoomPost.objects.filter(pk=post.pk).values_list('room_id', flat=True).get()
    return Room.objects.select_for_update().get(pk=room_id)


def _owned_membership(room, user):
    if not user or not user.is_authenticated or not user.is_active:
        return None
    cached = getattr(room, '_viewer_memberships', None)
    if cached is not None:
        return next((row for row in cached if row.user_id == user.pk), None)
    return RoomMembership.objects.filter(room_id=room.pk, user_id=user.pk).first()


def can_read_room(room, user=None):
    if room.status == Room.Status.ARCHIVED:
        return False
    if room.visibility == Room.Visibility.PUBLIC:
        return True
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.has_perm('local_rooms.can_moderate_room_posts'):
        return True
    membership = _owned_membership(room, user)
    return bool(membership and membership.status == RoomMembership.Status.APPROVED)


def can_post_room(room, user=None):
    if room.status != Room.Status.ACTIVE or not user or not user.is_authenticated or not user.is_active or not user.email_verified:
        return False
    membership = _owned_membership(room, user)
    return bool(membership and membership.status == RoomMembership.Status.APPROVED)


def public_rooms(*, user=None):
    """Discovery contains descriptive metadata for both visibility settings."""
    rows = Room.objects.exclude(status=Room.Status.ARCHIVED).select_related('state', 'lga', 'ppa', 'group')
    if user and user.is_authenticated and user.is_active:
        rows = rows.prefetch_related(Prefetch('memberships', queryset=RoomMembership.objects.filter(user_id=user.pk), to_attr='_viewer_memberships'))
    return rows


def get_room(pk):
    room = public_rooms().filter(pk=pk).first()
    if room is None:
        raise Http404('This room is not available.')
    return room


def safe_public_room(room, *, user=None):
    if room.status == Room.Status.ARCHIVED:
        raise PermissionDenied('This room is not publicly available.')
    membership = _owned_membership(room, user)
    # A linked CDS resource can later be withdrawn or become a private draft.
    group_public = bool(room.group_id and room.group.publication_status == 'approved' and room.group.expires_at > timezone.now())
    result = {name: getattr(room, name) for name in ('id', 'title', 'description', 'guidelines', 'state_id', 'lga_id', 'ppa_id', 'visibility', 'status', 'version', 'created_at', 'updated_at')}
    result.update(state_name=room.state.name, lga_name=room.lga.name, ppa_name=room.ppa.name if room.ppa_id else '',
                  group_id=room.group_id if group_public else None, group_title=room.group.title if group_public else '', group_available=group_public,
                  group_unavailable=bool(room.group_id and not group_public),
                  visibility_label=room.get_visibility_display(), status_label=room.get_status_display(),
                  can_read=can_read_room(room, user), can_post=can_post_room(room, user),
                  can_request_join=room.status == Room.Status.ACTIVE and (not membership or membership.status in ('left', 'rejected')),
                  my_membership=safe_owner_membership(membership, user=user) if membership else None)
    return result


@transaction.atomic
def save_room(*, actor, values, reason, room=None, expected_version=None):
    actor = _lock_member(actor, verified=False)
    _permission(actor, 'can_manage_rooms')
    reason = _reason(reason)
    created = room is None
    if created:
        room = Room(created_by=actor)
    else:
        supplied_version = expected_version if expected_version is not None else room.version
        room = Room.objects.select_for_update().get(pk=room.pk)
        if supplied_version != room.version:
            raise ValidationError('This room changed while you were reviewing it. Reload and try again.')
        room.version += 1
    for field in ROOM_FIELDS:
        if field in values:
            setattr(room, field, values[field])
    room.title = ' '.join(room.title.split())
    room.description, room.guidelines = room.description.strip(), room.guidelines.strip()
    if room.group_id:
        from service_resources.models import CDSGroup
        if not CDSGroup.objects.filter(pk=room.group_id, publication_status='approved', expires_at__gt=timezone.now()).exists():
            raise ValidationError({'group': 'Choose a currently published CDS resource, or clear this reference.'})
    room.moderated_by, room.moderated_at = actor, timezone.now()
    room.full_clean()
    room.save()
    _audit(room, actor, reason)
    return room


@transaction.atomic
def request_membership(*, room, user):
    user = _lock_member(user)
    room = Room.objects.select_for_update().get(pk=room.pk)
    _active(room)
    membership = RoomMembership.objects.select_for_update().filter(room=room, user=user).first()
    if membership and membership.status == RoomMembership.Status.BANNED:
        raise PermissionDenied('An authorised moderator must review the participation restriction before you can rejoin.')
    if membership and membership.status in ('requested', 'approved'):
        return membership
    if RoomMembership.objects.filter(user=user, requested_at__gte=timezone.now() - timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily room request limit has been reached. Please try again tomorrow.')
    if membership is None:
        membership = RoomMembership(room=room, user=user)
    else:
        membership.version += 1
    membership.status = RoomMembership.Status.REQUESTED
    membership.requested_at = timezone.now()
    membership.notifications_enabled = False
    membership.notifications_enabled_at = membership.approved_at = membership.reviewed_at = membership.reviewed_by = None
    membership.moderator_note = ''
    membership.full_clean()
    membership.save()
    _audit(membership, user, 'Member explicitly requested community room access. No official membership asserted.')
    return membership


@transaction.atomic
def leave_room(*, room, user):
    user = _lock_member(user, verified=False)
    Room.objects.select_for_update().get(pk=room.pk)
    membership = RoomMembership.objects.select_for_update().filter(room_id=room.pk, user=user).first()
    if membership is None:
        raise PermissionDenied('You have no membership request for this room.')
    # Leaving cannot erase a ban and turn it into a fresh ordinary request.
    if membership.status != RoomMembership.Status.BANNED and membership.status != RoomMembership.Status.LEFT:
        membership.status = RoomMembership.Status.LEFT
        membership.notifications_enabled = False
        membership.notifications_enabled_at = None
        membership.version += 1
        membership.save(update_fields=['status', 'notifications_enabled', 'notifications_enabled_at', 'version', 'updated_at'])
        _audit(membership, user, 'Member left the room or cancelled their request; room notifications disabled.')
    return membership


@transaction.atomic
def set_membership_notifications(*, room, user, enabled):
    user = _lock_member(user, verified=False)
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = RoomMembership.objects.select_for_update().filter(room=room, user=user).first()
    if not isinstance(enabled, bool):
        raise ValidationError('Choose whether to receive notifications.')
    if membership is None or membership.status != RoomMembership.Status.APPROVED:
        raise PermissionDenied('Only an approved member can change their room notifications.')
    if enabled:
        _active(room)
    if membership.notifications_enabled != enabled:
        membership.notifications_enabled = enabled
        membership.notifications_enabled_at = timezone.now() if enabled else None
        membership.version += 1
        membership.full_clean()
        membership.save(update_fields=['notifications_enabled', 'notifications_enabled_at', 'version', 'updated_at'])
        _audit(membership, user, 'Member explicitly enabled room notifications.' if enabled else 'Member disabled room notifications.')
    return membership


@transaction.atomic
def moderate_membership(*, membership, actor, status, reason):
    actor = _lock_member(actor, verified=False)
    _permission(actor, 'can_moderate_room_memberships')
    reason = _reason(reason)
    room_id = RoomMembership.objects.filter(pk=membership.pk).values_list('room_id', flat=True).get()
    room = Room.objects.select_for_update().get(pk=room_id)
    membership = RoomMembership.objects.select_for_update().get(pk=membership.pk)
    if status not in ('approved', 'rejected', 'banned'):
        raise ValidationError('Choose approval, rejection or a participation restriction.')
    if status == 'approved':
        _active(room)
    if membership.status == 'left' and status == 'approved':
        raise ValidationError('A member who left must explicitly request access again before approval.')
    if membership.status != status:
        membership.notifications_enabled = False
        membership.notifications_enabled_at = None
        membership.approved_at = timezone.now() if status == 'approved' else None
    membership.status = status
    membership.moderator_note = reason
    membership.reviewed_by, membership.reviewed_at = actor, timezone.now()
    membership.version += 1
    membership.full_clean()
    membership.save()
    _audit(membership, actor, reason)
    return membership


def my_memberships(user):
    _member(user, verified=False)
    return RoomMembership.objects.filter(user_id=user.pk).select_related('room')


def safe_owner_membership(membership, *, user):
    _member(user, verified=False)
    if membership.user_id != user.pk:
        raise PermissionDenied('Only the member can read their membership preferences.')
    result = {name: getattr(membership, name) for name in ('id', 'room_id', 'status', 'notifications_enabled', 'requested_at', 'reviewed_at', 'created_at', 'updated_at')}
    result.update(room_title=membership.room.title, room_status=membership.room.status,
                  room_status_label=membership.room.get_status_display(), room_available=membership.room.status != 'archived',
                  status_label=membership.get_status_display())
    return result


def _interaction_blocked(user, other):
    from community.services import interaction_blocked
    return bool(user and user.is_authenticated and other and interaction_blocked(user, other))


def room_posts(*, room, user=None):
    if not can_read_room(room, user):
        raise PermissionDenied('Approved membership is required to read this room.')
    rows = RoomPost.objects.filter(room_id=room.pk, status=RoomPost.Status.VISIBLE).exclude(room__status='archived').select_related('author', 'room')
    authenticated = bool(user and user.is_authenticated and user.is_active)
    if authenticated:
        rows = rows.annotate(_room_member_approved=Exists(RoomMembership.objects.filter(room_id=OuterRef('room_id'), user_id=user.pk, status='approved')))
        if not user.has_perm('local_rooms.can_moderate_room_posts'):
            rows = rows.filter(Q(room__visibility='public') | Q(_room_member_approved=True))
    else:
        rows = rows.filter(room__visibility='public').annotate(_room_member_approved=Value(False, output_field=BooleanField()))
    if user and user.is_authenticated:
        from community.models import CommunityBlock
        rows = rows.exclude(author_id__in=CommunityBlock.objects.filter(user_id=user.pk).values('blocked_user_id')).exclude(author_id__in=CommunityBlock.objects.filter(blocked_user_id=user.pk).values('user_id'))
    # These query annotations carry the already-checked viewer gate into each
    # small projection. Private membership and block lookups stay constant per
    # page instead of growing once per displayed discussion post.
    return rows.annotate(
        _room_viewer_id=Value(user.pk if user and user.is_authenticated else 0, output_field=IntegerField()),
        _room_viewer_allowed=Value(True, output_field=BooleanField()),
        _room_viewer_can_post=Case(When(room__status='active', _room_member_approved=True,
                                        then=Value(bool(authenticated and user.email_verified))),
                                    default=Value(False), output_field=BooleanField()),
    )


def safe_public_post(post, *, user=None):
    viewer_id = user.pk if user and user.is_authenticated else 0
    scoped = getattr(post, '_room_viewer_id', None) == viewer_id and getattr(post, '_room_viewer_allowed', False)
    if not scoped:
        post = RoomPost.objects.select_related('room', 'author').get(pk=post.pk)
    current_room = post.room
    if post.status != RoomPost.Status.VISIBLE or (not scoped and (not can_read_room(current_room, user) or _interaction_blocked(user, post.author_id))):
        raise PermissionDenied('This post is not available.')
    result = {name: getattr(post, name) for name in ('id', 'room_id', 'body', 'source_url', 'status', 'version', 'created_at', 'updated_at')}
    result['source_url'] = _safe_source(post.source_url)
    owner = bool(user and user.is_authenticated and user.pk == post.author_id)
    can_post = post._room_viewer_can_post if scoped else can_post_room(current_room, user)
    result.update(author_name=post.author.public_name if post.author_id else 'Former member', status_label=post.get_status_display(), is_owner=owner, can_edit=owner and can_post)
    return result


def _post_edit_limit(user):
    if RoomPostRevision.objects.filter(actor=user, created_at__gte=timezone.now() - timedelta(minutes=5)).count() >= 30:
        raise ValidationError('Too many recent changes. Please wait before making another change.')


@transaction.atomic
def save_post(*, room, user, cleaned_data, post=None, expected_version=None):
    user = _lock_member(user)
    room = Room.objects.select_for_update().get(pk=room.pk)
    _active(room)
    if not can_post_room(room, user):
        raise PermissionDenied('An approved room membership is required to post.')
    created = post is None
    if created:
        recent = RoomPost.objects.filter(author=user)
        if recent.filter(created_at__gte=timezone.now() - timedelta(days=1)).count() >= 20 or recent.filter(created_at__gte=timezone.now() - timedelta(minutes=5)).count() >= 5:
            raise ValidationError('Your room posting limit has been reached. Please wait before posting again.')
        post = RoomPost(room=room, author=user)
    else:
        supplied_version = expected_version if expected_version is not None else post.version
        post = RoomPost.objects.select_for_update().get(pk=post.pk)
        if post.room_id != room.pk or post.author_id != user.pk:
            raise PermissionDenied('Only the contributor can edit their post in this room.')
        if supplied_version != post.version:
            raise ValidationError('This post changed while you were editing. Reload it before saving.')
        if post.status == RoomPost.Status.ARCHIVED:
            raise ValidationError('A withdrawn post cannot be edited or restored.')
        _post_edit_limit(user)
        post.version += 1
    for name in ('body', 'source_url'):
        if name in cleaned_data:
            setattr(post, name, cleaned_data[name].strip())
    # Owner edits preserve hidden status and the moderator's private evidence.
    post.full_clean()
    post.save()
    _audit(post, user, 'Member created a discussion post.' if created else 'Member edited their discussion post; moderation state preserved.')
    if created and post.status == RoomPost.Status.VISIBLE:
        # A late import keeps the delivery engine independent of model startup.
        from notifications.community_services import record_room_activity
        record_room_activity(post)
    return post


@transaction.atomic
def archive_post(*, post, user):
    user = _lock_member(user, verified=False)
    room = _post_room_locked(post)
    post = RoomPost.objects.select_for_update().get(pk=post.pk)
    if post.author_id != user.pk or not can_read_room(room, user):
        raise PermissionDenied('Only the contributor with room access can withdraw their post.')
    if post.status != RoomPost.Status.ARCHIVED:
        _post_edit_limit(user)
        post.status = RoomPost.Status.ARCHIVED
        post.version += 1
        post.save(update_fields=['status', 'version', 'updated_at'])
        _audit(post, user, 'Member withdrew their discussion post.')
    return post


@transaction.atomic
def moderate_post(*, post, actor, status, reason):
    actor = _lock_member(actor, verified=False)
    _permission(actor, 'can_moderate_room_posts')
    reason = _reason(reason)
    room = _post_room_locked(post)
    post = RoomPost.objects.select_for_update().get(pk=post.pk)
    if status not in ('visible', 'hidden'):
        raise ValidationError('Choose visible or hidden moderation status.')
    if post.status == RoomPost.Status.ARCHIVED:
        raise ValidationError('A withdrawn post cannot be restored by moderation.')
    if status == 'visible':
        _active(room)
    post.status, post.moderator_note = status, reason
    post.moderated_by, post.moderated_at = actor, timezone.now()
    post.version += 1
    post.full_clean()
    post.save()
    _audit(post, actor, reason)
    return post


def owner_post_revisions(*, post, user):
    _member(user, verified=False)
    if post.author_id != user.pk or not can_read_room(Room.objects.get(pk=post.room_id), user):
        raise PermissionDenied('Only the contributor with room access can read this history.')
    rows = RoomPostRevision.objects.filter(post=post).values('id', 'created_at', 'snapshot')[:100]
    result = []
    for row in rows:
        snapshot = {name: row['snapshot'].get(name) for name in ('body', 'source_url', 'status', 'version')}
        snapshot['source_url'] = _safe_source(snapshot['source_url'])
        result.append({'id': row['id'], 'created_at': row['created_at'], 'reason': 'A saved post or moderation update.', 'snapshot': snapshot})
    return result


@transaction.atomic
def submit_report(*, user, category, details, room=None, post=None):
    user = _lock_member(user)
    if bool(room) == bool(post):
        raise ValidationError('Report exactly one room or post.')
    if room:
        room = Room.objects.select_for_update().get(pk=room.pk)
        if room.status == Room.Status.ARCHIVED:
            raise PermissionDenied('This room is no longer available to report.')
    else:
        room_row = _post_room_locked(post)
        post = RoomPost.objects.select_for_update().get(pk=post.pk)
        if not can_read_room(room_row, user) or post.status != RoomPost.Status.VISIBLE or _interaction_blocked(user, post.author_id):
            raise PermissionDenied('You can only report posts currently available to you.')
    if RoomReport.objects.filter(reporter=user, created_at__gte=timezone.now() - timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily report limit has been reached. Please try again tomorrow.')
    report = RoomReport(room=room, post=post, reporter=user, category=category, details=details.strip())
    report.full_clean()
    report.save()
    return report


def my_reports(user):
    _member(user, verified=False)
    return RoomReport.objects.filter(reporter_id=user.pk)


def safe_owner_report(report, *, user):
    _member(user, verified=False)
    if report.reporter_id != user.pk:
        raise PermissionDenied('Only the reporter can read their report.')
    result = {name: getattr(report, name) for name in ('id', 'room_id', 'post_id', 'category', 'details', 'status', 'created_at', 'updated_at')}
    result.update(category_label=report.get_category_display(), status_label=report.get_status_display())
    return result


@transaction.atomic
def moderate_report(*, report, actor, status, moderator_note):
    actor = _lock_member(actor, verified=False)
    _permission(actor, 'can_moderate_room_reports')
    if not moderator_note.strip() or len(moderator_note) > 2000:
        raise ValidationError('Record a private outcome note of up to 2,000 characters.')
    report = RoomReport.objects.select_for_update().get(pk=report.pk)
    previous_status = report.status
    report.status, report.moderator_note = status, moderator_note.strip()
    report.moderated_by, report.moderated_at = actor, timezone.now()
    report.full_clean()
    report.save()
    RoomReportRevision.objects.create(report=report, actor=actor, previous_status=previous_status, new_status=status, moderator_note=report.moderator_note)
    return report
