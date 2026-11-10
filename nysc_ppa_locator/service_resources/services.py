"""Atomic contributions and explicit projections for source-labelled service guidance."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from .models import CDSGroup, CDSPreference, GroupRevision, NoticeRevision, ResourceReport, ResourceReportRevision, ServiceNotice, is_official_url, validate_public_url


GROUP_FIELDS = ('title', 'description', 'venue_hint', 'public_contact_url', 'state', 'lga', 'source_type', 'source_url', 'observed_on')
NOTICE_FIELDS = ('kind', 'title', 'body', 'venue_hint', 'state', 'lga', 'group', 'event_at', 'ends_at', 'timezone_name', 'batch_label', 'stage', 'source_type', 'source_url', 'observed_on')
MODERATOR_FIELDS = ('source_type', 'source_url', 'publication_status', 'verification_status', 'checked_at', 'expires_at', 'moderator_note')
PUBLIC_COMMON = ('id', 'title', 'state_id', 'lga_id', 'source_type', 'source_url', 'observed_on', 'checked_at', 'expires_at', 'publication_status', 'verification_status', 'version', 'created_at', 'updated_at')


def _require_member(user, *, verified=True):
    if not user.is_authenticated or not user.is_active or (verified and not user.email_verified):
        raise PermissionDenied('Sign in and verify your email before contributing to public resources.')


def _lock_member(user, *, verified=True):
    _require_member(user, verified=verified)
    member = get_user_model().objects.select_for_update().get(pk=user.pk)
    _require_member(member, verified=verified)
    return member


def _permission(actor, codename):
    if not actor.is_authenticated or not actor.is_active or not actor.has_perm('service_resources.' + codename):
        raise PermissionDenied('You do not have permission for this moderation action.')


def _revision_model(resource):
    return (GroupRevision, 'group') if isinstance(resource, CDSGroup) else (NoticeRevision, 'notice')


def _snapshot(resource):
    own = GROUP_FIELDS if isinstance(resource, CDSGroup) else NOTICE_FIELDS
    fields = set(own) | set(MODERATOR_FIELDS) | {'author_id', 'version', 'shared_for_review', 'moderated_by_id', 'moderated_at'}
    result = {}
    for name in fields:
        field = resource._meta.get_field(name)
        key = field.attname
        value = getattr(resource, key)
        result[key] = value.isoformat() if hasattr(value, 'isoformat') else value
    return result


def _log(resource, actor, reason):
    model, field = _revision_model(resource)
    model.objects.create(**{field: resource}, actor=actor, snapshot=_snapshot(resource), shared_for_review=resource.shared_for_review, reason=reason)


def _default_expiry(resource):
    days = 180 if isinstance(resource, CDSGroup) else 30
    age = (timezone.localdate() - resource.observed_on).days
    return timezone.now() + timedelta(days=days-age)


@transaction.atomic
def _save(model, *, user, cleaned_data, resource=None, submit=False):
    _lock_member(user)
    created = resource is None
    if created:
        since = timezone.now() - timedelta(days=1)
        count = CDSGroup.objects.filter(author=user, created_at__gte=since).count() + ServiceNotice.objects.filter(author=user, created_at__gte=since).count()
        if count >= 20:
            raise ValidationError('Your daily resource contribution allowance has been reached.')
        resource = model(author=user)
    else:
        resource = model.objects.select_for_update().get(pk=resource.pk)
        if resource.author_id != user.pk:
            raise PermissionDenied('Only the author can edit this contribution.')
        if resource.publication_status == model.Publication.ARCHIVED:
            raise ValidationError('Archived contributions are retained as history. Create a new report if needed.')
    source = cleaned_data.get('source_type', model.Source.MEMBER)
    if source not in (model.Source.MEMBER, model.Source.ORGANISATION):
        raise PermissionDenied('Members cannot assign official or editorial source labels.')
    fields = GROUP_FIELDS if model is CDSGroup else NOTICE_FIELDS
    for name in fields:
        if name in cleaned_data:
            setattr(resource, name, cleaned_data[name])
    resource.title = ' '.join(resource.title.split())
    resource.source_type = source
    resource.observed_on = model._meta.get_field('observed_on').clean(resource.observed_on, resource)
    if resource.observed_on > timezone.localdate():
        raise ValidationError({'observed_on': 'The observation date cannot be in the future.'})
    duplicates = model.objects.filter(author=user, lga=resource.lga, title__iexact=resource.title).exclude(publication_status=model.Publication.ARCHIVED)
    if resource.pk:
        duplicates = duplicates.exclude(pk=resource.pk)
    if duplicates.exists():
        raise ValidationError('You already contributed this title in this area. Edit that contribution instead.')
    resource.publication_status = model.Publication.PENDING if submit else model.Publication.DRAFT
    resource.shared_for_review = bool(submit)
    resource.verification_status = model.Verification.COMMUNITY
    resource.checked_at = None
    resource.moderator_note = ''
    resource.moderated_by = None
    resource.moderated_at = None
    resource.expires_at = _default_expiry(resource)
    if not created:
        resource.version += 1
    resource.full_clean()
    resource.save()
    _log(resource, user, ('Created' if created else 'Updated') + (' and submitted for review.' if submit else ' as a private draft.'))
    return resource


def save_group(*, user, cleaned_data, group=None, submit=False):
    return _save(CDSGroup, user=user, cleaned_data=cleaned_data, resource=group, submit=submit)


def save_notice(*, user, cleaned_data, notice=None, submit=False):
    return _save(ServiceNotice, user=user, cleaned_data=cleaned_data, resource=notice, submit=submit)


@transaction.atomic
def _archive(*, resource, user):
    _lock_member(user)
    model = type(resource)
    resource = model.objects.select_for_update().get(pk=resource.pk)
    if resource.author_id != user.pk:
        raise PermissionDenied('Only the author can withdraw this contribution.')
    if resource.publication_status != model.Publication.ARCHIVED:
        resource.publication_status = model.Publication.ARCHIVED
        resource.version += 1
        resource.save(update_fields=['publication_status', 'version', 'updated_at'])
        _log(resource, user, 'Withdrawn by the author; removed from public browsing.')
    return resource


def archive_group(*, group, user):
    return _archive(resource=group, user=user)


def archive_notice(*, notice, user):
    return _archive(resource=notice, user=user)


@transaction.atomic
def _moderate(*, resource, actor, values, reason):
    _permission(actor, 'can_moderate_resources')
    if not reason.strip():
        raise ValidationError('Record what was checked and the reason for the decision.')
    resource = type(resource).objects.select_for_update().get(pk=resource.pk)
    if not resource.shared_for_review or resource.publication_status == resource.Publication.DRAFT:
        raise PermissionDenied('Private drafts must be submitted before moderation.')
    if resource.publication_status == resource.Publication.ARCHIVED:
        raise ValidationError('Archived contributions cannot be republished. Request a new submission.')
    before = {name: getattr(resource, name) for name in MODERATOR_FIELDS}
    for name in MODERATOR_FIELDS:
        if name in values:
            setattr(resource, name, values[name])
    if resource.publication_status not in (resource.Publication.PENDING, resource.Publication.APPROVED, resource.Publication.REJECTED):
        raise ValidationError({'publication_status': 'Choose a submitted review outcome, not a private draft or archive.'})
    strong = resource.verification_status != resource.Verification.COMMUNITY or resource.source_type == resource.Source.OFFICIAL
    changed = any(before[name] != getattr(resource, name) for name in MODERATOR_FIELDS if name != 'moderator_note')
    if strong and changed:
        _permission(actor, 'can_verify_resources')
    if resource.verification_status != resource.Verification.COMMUNITY and resource.author_id == actor.pk:
        raise PermissionDenied('A different person must independently verify this contribution.')
    resource.full_clean()
    if changed or before['moderator_note'] != resource.moderator_note:
        resource.version += 1
        resource.moderated_by = actor
        resource.moderated_at = timezone.now()
        resource.save()
        _log(resource, actor, reason.strip())
    return resource


def moderate_group(*, group, actor, values, reason):
    return _moderate(resource=group, actor=actor, values=values, reason=reason)


def moderate_notice(*, notice, actor, values, reason):
    return _moderate(resource=notice, actor=actor, values=values, reason=reason)


def public_groups(*, include_expired=False):
    rows = CDSGroup.objects.filter(publication_status=CDSGroup.Publication.APPROVED).select_related('state', 'lga')
    return rows if include_expired else rows.filter(expires_at__gt=timezone.now())


def public_notices(*, include_expired=False):
    rows = ServiceNotice.objects.filter(publication_status=ServiceNotice.Publication.APPROVED).select_related('state', 'lga', 'group')
    if not include_expired:
        now = timezone.now()
        rows = rows.filter(expires_at__gt=now).filter(Q(group__isnull=True) | Q(group__publication_status=CDSGroup.Publication.APPROVED, group__expires_at__gt=now))
    return rows


def _safe_common(resource, user=None):
    if resource.publication_status != resource.Publication.APPROVED and not (user and user.is_authenticated and resource.author_id == user.pk):
        raise PermissionDenied('This contribution is not public.')
    result = {name: getattr(resource, name) for name in PUBLIC_COMMON}
    result.update(state_name=resource.state.name, lga_name=resource.lga.name,
                  source_label=resource.get_source_type_display(), verification_label=resource.get_verification_status_display(),
                  publication_label=resource.get_publication_status_display(), is_stale=resource.is_stale)
    if resource.is_stale:
        result['verification_label'] += ' (historical; reconfirm before relying on it)'
    result['official_caution'] = 'Please confirm with your designated NYSC official. This platform cannot grant posting, participation or clearance approval.'
    return result


def safe_group(group, *, user=None):
    result = _safe_common(group, user)
    result.update(description=group.description, venue_hint=group.venue_hint, public_contact_url=group.public_contact_url)
    return result


def safe_notice(notice, *, user=None):
    result = _safe_common(notice, user)
    fields = ('kind', 'body', 'venue_hint', 'event_at', 'ends_at', 'timezone_name', 'batch_label', 'stage', 'group_id')
    result.update({name: getattr(notice, name) for name in fields})
    group_public = (notice.group_id and notice.group.publication_status == CDSGroup.Publication.APPROVED
                    and notice.group.expires_at > timezone.now())
    result.update(kind_label=notice.get_kind_display(), stage_label=notice.get_stage_display(),
                  group_title=notice.group.title if group_public else '', group_available=bool(group_public),
                  reminder_eligible=notice_reminder_eligible(notice))
    return result


def owner_revisions(*, resource, user):
    if not user.is_authenticated or resource.author_id != user.pk:
        raise PermissionDenied('Only the author can read this history.')
    model, field = _revision_model(resource)
    rows = model.objects.filter(**{field: resource}).order_by('-created_at', '-pk').values('id', 'created_at', 'snapshot')[:100]
    excluded = {'author_id', 'moderator_note', 'moderated_by_id', 'moderated_at', 'shared_for_review'}
    return [{
        'id': row['id'], 'created_at': row['created_at'], 'reason': 'A saved contribution or review update.',
        'snapshot': {name: value for name, value in row['snapshot'].items() if name not in excluded},
    } for row in rows]


def notice_reminder_eligible(notice, now=None, require_future_event=True):
    """Pure source gate; dispatch adds its bounded lateness and version checks."""
    now = now or timezone.now()
    if (notice.publication_status != ServiceNotice.Publication.APPROVED or notice.expires_at <= now
            or notice.verification_status not in (ServiceNotice.Verification.CHECKED, ServiceNotice.Verification.ORGANISATION, ServiceNotice.Verification.OFFICIAL)
            or not notice.checked_at or not notice.source_url or not notice.event_at
            or notice.event_at > notice.expires_at
            or (require_future_event and notice.event_at <= now)):
        return False
    try:
        validate_public_url(notice.source_url)
    except ValidationError:
        return False
    if (notice.checked_at > now or notice.observed_on > timezone.localdate(now)
            or (notice.source_type == ServiceNotice.Source.OFFICIAL and not is_official_url(notice.source_url))
            or (notice.verification_status == ServiceNotice.Verification.OFFICIAL
                and (notice.source_type != ServiceNotice.Source.OFFICIAL or not is_official_url(notice.source_url)))):
        return False
    if notice.group_id and (notice.group.publication_status != CDSGroup.Publication.APPROVED or notice.group.expires_at <= now):
        return False
    return True


@transaction.atomic
def select_group(*, user, group=None):
    _lock_member(user, verified=False)
    if group is not None:
        group = public_groups().filter(pk=group.pk).first()
        if group is None:
            raise ValidationError('Choose a currently published group. Selection is a private preference, not proof of membership.')
    preference, _ = CDSPreference.objects.update_or_create(user=user, defaults={'group': group})
    return preference


@transaction.atomic
def save_report(*, user, resource, category, details, proposed_correction=''):
    _lock_member(user, verified=False)
    resource = type(resource).objects.select_for_update().get(pk=resource.pk)
    if resource.publication_status != resource.Publication.APPROVED:
        raise Http404('Only public resources can be reported.')
    if ResourceReport.objects.filter(reporter=user, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily report allowance has been reached.')
    target = {'group': resource} if isinstance(resource, CDSGroup) else {'notice': resource}
    report = ResourceReport(reporter=user, category=category, details=details.strip(), proposed_correction=proposed_correction.strip(), **target)
    report.full_clean()
    report.save()
    return report


@transaction.atomic
def moderate_report(*, report, actor, status, moderator_note):
    _permission(actor, 'can_moderate_resource_reports')
    if not moderator_note.strip():
        raise ValidationError('Record what was checked and why this report is changing.')
    report = ResourceReport.objects.select_for_update().get(pk=report.pk)
    previous = report.status
    report.status, report.moderator_note = status, moderator_note.strip()
    report.moderated_by, report.moderated_at = actor, timezone.now()
    report.full_clean()
    report.save()
    ResourceReportRevision.objects.create(report=report, actor=actor, previous_status=previous, new_status=report.status, moderator_note=report.moderator_note)
    return report
