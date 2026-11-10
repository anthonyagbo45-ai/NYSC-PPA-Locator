"""Permission-checked, atomic writes for contextual claims and review histories."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ClaimRevision, PPAClaim, Review, ReviewChangeLog, ReviewRevision


MEMBER_CLAIM_FIELDS = ('topic', 'content', 'source_type', 'source_url', 'observed_on')
MODERATOR_CLAIM_FIELDS = MEMBER_CLAIM_FIELDS + (
    'publication_status', 'verification_status', 'checked_at', 'expires_at', 'moderator_note',
)


def _require_member(user):
    if not user.is_authenticated or not user.is_active or not user.email_verified:
        raise PermissionDenied('Verify your email before contributing.')


def _require_permission(user, permission):
    if not user.is_authenticated or not user.is_active or not user.has_perm(permission):
        raise PermissionDenied('You do not have permission to perform this moderation action.')


def _claim_snapshot(claim):
    fields = MODERATOR_CLAIM_FIELDS + ('ppa_id', 'author_id', 'moderated_by_id', 'moderated_at')
    result = {}
    for name in fields:
        value = getattr(claim, name)
        result[name] = value.isoformat() if hasattr(value, 'isoformat') else value
    return result


def _log_claim(claim, actor, reason):
    ClaimRevision.objects.create(claim=claim, actor=actor, snapshot=_claim_snapshot(claim), reason=reason)


@transaction.atomic
def save_member_claim(*, ppa, user, cleaned_data, claim=None, submit=False):
    _require_member(user)
    # Serialize this contributor's quota and duplicate checks on shared databases.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    if claim is not None:
        claim = PPAClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.author_id != user.pk or claim.ppa_id != ppa.pk:
            raise PermissionDenied('Only the author can edit this contribution.')
        if claim.publication_status == PPAClaim.Publication.ARCHIVED:
            raise ValidationError('Archived contributions cannot be edited. Submit a new report if circumstances change.')
    else:
        recent = PPAClaim.objects.filter(author=user, created_at__gte=timezone.now() - timedelta(days=1))
        if recent.count() >= 20:
            raise ValidationError('Your daily contribution limit has been reached. Please try again tomorrow.')
        active = PPAClaim.objects.filter(ppa=ppa, author=user, publication_status__in=[PPAClaim.Publication.DRAFT, PPAClaim.Publication.PENDING])
        if active.count() >= 5:
            raise ValidationError('You already have five drafts or pending contributions for this PPA. Update one of those first.')
        claim = PPAClaim(ppa=ppa, author=user)
    if cleaned_data.get('source_type') not in (PPAClaim.Source.MEMBER, PPAClaim.Source.ORGANISATION):
        raise PermissionDenied('Members cannot assign editorial or official-source status.')
    for name in MEMBER_CLAIM_FIELDS:
        setattr(claim, name, cleaned_data.get(name, ''))
    claim.content = claim.content.strip()
    duplicates = PPAClaim.objects.filter(ppa=ppa, author=user, topic=claim.topic, content__iexact=claim.content).exclude(
        publication_status=PPAClaim.Publication.ARCHIVED,
    )
    if claim.pk:
        duplicates = duplicates.exclude(pk=claim.pk)
    if duplicates.exists():
        raise ValidationError('This information is already in your contributions. Edit the existing report instead.')
    claim.publication_status = PPAClaim.Publication.PENDING if submit else PPAClaim.Publication.DRAFT
    # An author edit is a new proposal, never a way to inherit a prior verification.
    claim.verification_status = PPAClaim.Verification.COMMUNITY
    claim.checked_at = None
    if claim.observed_on:
        age = (timezone.localdate() - claim.observed_on).days
        claim.expires_at = timezone.now() + timedelta(days=180 - age)
    claim.moderated_by = None
    claim.moderated_at = None
    claim.full_clean()
    created = claim.pk is None
    claim.save()
    _log_claim(claim, user, ('Created' if created else 'Updated') + (' and submitted for moderation.' if submit else ' as a private draft.'))
    return claim


@transaction.atomic
def archive_member_claim(*, claim, user):
    _require_member(user)
    claim = PPAClaim.objects.select_for_update().get(pk=claim.pk)
    if claim.author_id != user.pk:
        raise PermissionDenied('Only the author can withdraw this contribution.')
    if claim.publication_status != PPAClaim.Publication.ARCHIVED:
        claim.publication_status = PPAClaim.Publication.ARCHIVED
        claim.save(update_fields=['publication_status', 'updated_at'])
        _log_claim(claim, user, 'Archived by the author; removed from public profiles.')
    return claim


@transaction.atomic
def moderate_claim(*, claim, actor, values, reason):
    _require_permission(actor, 'ppas.can_moderate_claims')
    if not reason.strip():
        raise ValidationError('Record the reason and evidence supporting this moderation action.')
    if claim.pk:
        claim = PPAClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.publication_status == PPAClaim.Publication.DRAFT:
            raise PermissionDenied('Private drafts must be submitted by their author before moderation.')
        if (claim.publication_status == PPAClaim.Publication.ARCHIVED
                and values.get('publication_status', claim.publication_status) != PPAClaim.Publication.ARCHIVED):
            raise ValidationError('An archived contribution cannot be republished. Ask for a new submission.')
    before = _claim_snapshot(claim)
    for field in MODERATOR_CLAIM_FIELDS:
        if field in values:
            setattr(claim, field, values[field])
    after = _claim_snapshot(claim)
    verification_fields = ('source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at')
    verification_changed = any(before[field] != after[field] for field in verification_fields)
    content_changed = any(before[field] != after[field] for field in ('topic', 'content', 'observed_on'))
    restricted = claim.source_type == PPAClaim.Source.OFFICIAL or claim.verification_status != PPAClaim.Verification.COMMUNITY
    # A moderator cannot edit previously verified material and retain its badge.
    if restricted and (verification_changed or content_changed or not claim.pk):
        _require_permission(actor, 'ppas.can_verify_organisations')
        if claim.author_id == actor.pk and claim.verification_status != PPAClaim.Verification.COMMUNITY:
            raise PermissionDenied('Independent verification must be performed by someone other than the contributor.')
    if claim.verification_status != PPAClaim.Verification.COMMUNITY:
        if not claim.moderator_note.strip():
            raise ValidationError({'moderator_note': 'Record how the supporting source was checked.'})
    claim.moderated_by = actor
    claim.moderated_at = timezone.now()
    claim.full_clean()
    claim.save()
    _log_claim(claim, actor, reason.strip())
    return claim


def get_public_profile_claims(ppa):
    """A bounded safe projection: no author identifiers or private moderation notes."""
    claims = PPAClaim.objects.filter(ppa=ppa, publication_status=PPAClaim.Publication.APPROVED).order_by('topic', '-observed_on', '-pk').values(
        'id', 'topic', 'content', 'source_type', 'source_url', 'observed_on', 'checked_at', 'expires_at', 'verification_status',
    )[:50]
    now = timezone.now()
    rows = []
    for claim in claims:
        stale = claim['expires_at'] <= now
        disclaimers = []
        if claim['source_type'] == PPAClaim.Source.OFFICIAL:
            disclaimers.append('The attributed official material provides context only; it does not establish NYSC posting approval for this organisation.')
        if claim['topic'] in (PPAClaim.Topic.SUPPORT, PPAClaim.Topic.ACCEPTANCE):
            disclaimers.append('Reports of acceptance or financial support are not guarantees. Confirm current terms directly with the organisation.')
        claim.update(
            topic_label=dict(PPAClaim.Topic.choices)[claim['topic']],
            source_label=dict(PPAClaim.Source.choices)[claim['source_type']],
            verification_label=dict(PPAClaim.Verification.choices)[claim['verification_status']],
            is_stale=stale,
            freshness_label='Outdated — reconfirm before relying on this' if stale else 'Reconfirmation scheduled',
            disclaimer=' '.join(disclaimers),
        )
        if stale:
            claim['verification_label'] += ' (historical; reconfirmation needed)'
        rows.append(claim)
    return rows


def _log_review(review, actor, reason):
    return ReviewRevision.objects.create(
        review=review, actor=actor, rating=review.rating, comment=review.comment,
        topic_tags=review.topic_tags, context_date=review.context_date,
        is_public=review.is_public, reason=reason,
    )


@transaction.atomic
def save_member_review(*, ppa, user, cleaned_data):
    _require_member(user)
    # Lock the contributor to serialize concurrent submissions while retaining the
    # existing database uniqueness constraint for one review per member and PPA.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    review = Review.objects.select_for_update().filter(ppa=ppa, user=user).first()
    created = review is None
    if created:
        review = Review(ppa=ppa, user=user)
    elif not review.revisions.exists():
        _log_review(review, user, 'Original review preserved before the first recorded edit.')
    previous = (review.rating, review.comment, review.topic_tags, review.context_date)
    review.rating = cleaned_data['rating']
    review.comment = cleaned_data.get('comment', '').strip()
    review.topic_tags = list(cleaned_data.get('topic_tags') or [])
    review.context_date = cleaned_data.get('context_date')
    review.full_clean()
    # is_public is deliberately never accepted from member input. Moderation
    # persists even if the author submits a replacement text.
    if created or previous != (review.rating, review.comment, review.topic_tags, review.context_date):
        review.save()
        _log_review(review, user, 'Review submitted.' if created else 'Review edited by its author.')
    return review


@transaction.atomic
def moderate_review(*, review, actor, is_public, note):
    _require_permission(actor, 'ppas.can_moderate_reviews')
    if not note.strip():
        raise ValidationError('Explain why the review is being hidden or restored.')
    review = Review.objects.select_for_update().get(pk=review.pk)
    if review.is_public != is_public:
        if not review.revisions.exists():
            _log_review(review, actor, 'Original review preserved before the first recorded moderation action.')
        before = review.is_public
        review.is_public = is_public
        review.save(update_fields=['is_public', 'updated_at'])
        ReviewChangeLog.objects.create(review=review, actor=actor, previous_visibility=before, new_visibility=is_public, note=note.strip())
        _log_review(review, actor, 'Review restored after moderation.' if is_public else 'Review hidden after moderation.')
    return review
