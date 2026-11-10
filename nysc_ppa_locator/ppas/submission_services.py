"""Reviewed independent PPA proposals; Google results are not required."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from locations.models import LGA
from .models import PPA, PPAClaim, PPASubmission, PlaceChangeLog, SubmissionRevision
from .submission_models import normalize_submission_name


MEMBER_FIELDS = ('name', 'state', 'lga', 'category', 'address', 'landmark', 'entrance_instructions',
                 'latitude', 'longitude', 'source_type', 'source_url', 'observed_on')
TRUST_FIELDS = ('source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at', 'moderator_note')


def duplicate_candidates(*, name, state_id, lga_id):
    # New proposals use canonical whitespace. A bounded, exact-name candidate
    # query is a review aid, never permission to merge two organisations.
    canonical_name = ' '.join(name.split())
    published_ids = SubmissionRevision.objects.filter(published_as_new=True,
        publication_identity=normalize_submission_name(name)).values('published_ppa_id')
    return PPA.objects.filter(state_id=state_id, lga_id=lga_id).filter(
        Q(name__iexact=canonical_name) | Q(pk__in=published_ids),
    ).order_by('pk')


def _require_member(user):
    if not user.is_authenticated or not user.is_active or not user.email_verified:
        raise PermissionDenied('Verify your email before proposing a PPA.')


def _snapshot(submission):
    result = {}
    fields = ('name', 'state_id', 'lga_id', 'category', 'address', 'landmark', 'entrance_instructions',
              'latitude', 'longitude', 'source_type', 'source_url', 'observed_on', 'checked_at',
              'expires_at', 'verification_status', 'publication_status', 'published_ppa_id',
              'moderator_note', 'moderated_by_id', 'moderated_at')
    for field in fields:
        value = getattr(submission, field)
        result[field] = value.isoformat() if hasattr(value, 'isoformat') else value
    return result


def _record(submission, actor, reason, *, published_as_new=False):
    SubmissionRevision.objects.create(submission=submission, actor=actor, snapshot=_snapshot(submission),
                                      published_ppa=submission.published_ppa, published_as_new=published_as_new,
                                      publication_identity=submission.normalized_name if published_as_new else '',
                                      shared_for_review=submission.shared_for_review, reason=reason)


@transaction.atomic
def save_submission(*, user, cleaned_data, submission=None, submit=False):
    _require_member(user)
    get_user_model().objects.select_for_update().get(pk=user.pk)
    if submission is not None:
        submission = PPASubmission.objects.select_for_update().get(pk=submission.pk)
        if submission.author_id != user.pk:
            raise PermissionDenied('Only the author may edit this proposal.')
        if submission.publication_status == PPASubmission.Publication.WITHDRAWN:
            raise ValidationError('Withdrawn proposals are retained as history. Create a new proposal instead.')
    else:
        if PPASubmission.objects.filter(author=user, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 10:
            raise ValidationError('Your daily PPA proposal limit has been reached. Please try tomorrow.')
        if PPASubmission.objects.filter(author=user, publication_status__in=['draft', 'pending']).count() >= 20:
            raise ValidationError('Please finish or withdraw one of your existing proposals first.')
        submission = PPASubmission(author=user)
    if cleaned_data.get('source_type') not in (PPAClaim.Source.MEMBER, PPAClaim.Source.ORGANISATION):
        raise PermissionDenied('Members cannot assign editorial or official status.')
    for field in MEMBER_FIELDS:
        setattr(submission, field, cleaned_data.get(field))
    submission.name = ' '.join(submission.name.split())
    submission.normalized_name = normalize_submission_name(submission.name)
    for field in ('address', 'landmark', 'entrance_instructions', 'source_url'):
        setattr(submission, field, (getattr(submission, field) or '').strip())
    submission.publication_status = PPASubmission.Publication.PENDING if submit else PPASubmission.Publication.DRAFT
    submission.shared_for_review = bool(submit)
    submission.verification_status = PPAClaim.Verification.COMMUNITY
    submission.checked_at = None
    submission.moderator_note = ''
    submission.moderated_by = None
    submission.moderated_at = None
    # Retain the old published directory row unchanged; its earlier association
    # remains in the append-only revision history.
    submission.published_ppa = None
    submission.expires_at = timezone.now() + timedelta(days=180 - (timezone.localdate()-submission.observed_on).days)
    submission.full_clean()
    submission.save()
    _record(submission, user, 'Submitted for review.' if submit else 'Saved as a private draft.')
    return submission


@transaction.atomic
def withdraw_submission(*, submission, user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    submission = PPASubmission.objects.select_for_update().get(pk=submission.pk)
    if submission.author_id != user.pk:
        raise PermissionDenied('Only the author may withdraw this proposal.')
    if submission.publication_status != PPASubmission.Publication.WITHDRAWN:
        submission.publication_status = PPASubmission.Publication.WITHDRAWN
        submission.save(update_fields=['publication_status', 'updated_at'])
        _record(submission, user, 'Withdrawn by the author. Existing directory records are unchanged.')
    return submission


@transaction.atomic
def moderate_submission(*, submission, actor, decision, reason, existing_ppa=None, values=None):
    if not actor.is_authenticated or not actor.is_active or not actor.has_perm('ppas.can_moderate_submissions'):
        raise PermissionDenied('Independent PPA review permission is required.')
    if decision not in ('approve', 'reject'):
        raise ValidationError('Choose approve or reject.')
    if not reason.strip() or len(reason) > 1000:
        raise ValidationError('Record a review reason of at most 1000 characters.')
    submission = PPASubmission.objects.select_for_update().get(pk=submission.pk)
    if actor.pk == submission.author_id:
        raise PermissionDenied('A different reviewer must review your own proposal.')
    if submission.publication_status == PPASubmission.Publication.APPROVED and decision == 'approve':
        if existing_ppa is None or existing_ppa.pk == submission.published_ppa_id:
            return submission
        raise ValidationError('This proposal is already linked to a published PPA.')
    if submission.publication_status != PPASubmission.Publication.PENDING or not submission.shared_for_review:
        raise ValidationError('Only a proposal submitted by its author may be reviewed.')
    values = values or {}
    for field in TRUST_FIELDS:
        if field in values:
            setattr(submission, field, values[field])
    if (submission.verification_status != PPAClaim.Verification.COMMUNITY
            or submission.source_type == PPAClaim.Source.OFFICIAL):
        if not actor.has_perm('ppas.can_verify_organisations'):
            raise PermissionDenied('Independent/source verification requires a separately authorised verifier.')
    submission.full_clean()
    created_new = False
    if decision == 'approve':
        if submission.expires_at <= timezone.now():
            raise ValidationError('This proposal is outdated. Ask the author to reconfirm it before publishing.')
        # Lock a stable shared parent before checking/creating destinations. Two
        # independent proposals for the same new place cannot race past review.
        LGA.objects.select_for_update().get(pk=submission.lga_id)
        if existing_ppa is not None:
            existing_ppa = PPA.objects.filter(pk=existing_ppa.pk).first()
            if existing_ppa is None:
                raise ValidationError('The selected directory place no longer exists. Review the proposal again.')
            if existing_ppa.state_id != submission.state_id or existing_ppa.lga_id != submission.lga_id:
                raise ValidationError('The linked directory place must be in the selected state and LGA.')
            ppa = existing_ppa
        else:
            if duplicate_candidates(name=submission.name, state_id=submission.state_id, lga_id=submission.lga_id).exists():
                raise ValidationError('A directory place with this name already exists in this LGA. Review and explicitly link the correct existing PPA. For a genuinely different campus or branch, ask the author to include its distinguishing branch name; do not link the wrong place. No records were merged.')
            ppa = PPA(name=submission.name, state=submission.state, lga=submission.lga,
                      category=submission.category, address=submission.address, landmark=submission.landmark,
                      entrance_instructions=submission.entrance_instructions, latitude=submission.latitude,
                      longitude=submission.longitude, source=PPA.Source.COMMUNITY,
                      status=PPA.Status.DISCOVERED, location_status=PPA.LocationStatus.UNCONFIRMED,
                      submitted_by=submission.author, verified=False)
            ppa.full_clean()
            ppa.save()
            created_new = True
            PlaceChangeLog.objects.create(ppa=ppa, actor=actor, changes={'created_from_independent_submission': submission.pk},
                                          reason='Reviewed independent proposal. Publication does not establish NYSC approval or organisation acceptance. ' + reason.strip()[:700])
        submission.published_ppa = ppa
        submission.publication_status = PPASubmission.Publication.APPROVED
    else:
        submission.publication_status = PPASubmission.Publication.REJECTED
    submission.moderated_by = actor
    submission.moderated_at = timezone.now()
    submission.save()
    _record(submission, actor, reason.strip(), published_as_new=created_new)
    return submission


def get_public_submission_provenance(ppa):
    """Immutable provenance of the actual newly published directory facts.

    Later private proposal edits or links to existing places must neither leak
    nor lend new verification badges to an unchanged directory record.
    """
    if ppa.source != PPA.Source.COMMUNITY:
        return None
    names = ('source_type', 'source_url', 'observed_on', 'checked_at', 'expires_at', 'verification_status')
    identity_fields = ('name', 'state_id', 'lga_id', 'category', 'address', 'landmark', 'entrance_instructions', 'latitude', 'longitude')
    row = SubmissionRevision.objects.filter(published_ppa=ppa, published_as_new=True, shared_for_review=True).order_by(
        '-created_at', '-pk',
    ).values('created_at', *('snapshot__'+name for name in names + identity_fields)).first()
    if row is None:
        return None
    result = {name: row['snapshot__'+name] for name in names}
    result['observed_on'] = parse_date(result['observed_on']) if result['observed_on'] else None
    result['checked_at'] = parse_datetime(result['checked_at']) if result['checked_at'] else None
    result['expires_at'] = parse_datetime(result['expires_at']) if result['expires_at'] else None
    result['source_label'] = dict(PPAClaim.Source.choices).get(result['source_type'], 'Community-reported')
    result['verification_label'] = dict(PPAClaim.Verification.choices).get(result['verification_status'], 'Needs review')
    result['is_stale'] = not result['expires_at'] or result['expires_at'] <= timezone.now()
    result['recorded_at'] = row['created_at']
    result['details_changed_since'] = bool(ppa.place_id) or any(row['snapshot__'+field] != getattr(ppa, field) for field in identity_fields)
    if result['details_changed_since']:
        result['verification_label'] = 'Historical evidence — current details need reconfirmation'
    return result
