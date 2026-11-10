"""Atomic author proposals, source checks and bounded private/public projections."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from .models import (
    FareDetails, HousingDetails, Listing, ListingBookmark, ListingReport,
    ListingReportRevision, ListingRevision, ServiceDetails,
)


MEMBER_FIELDS = ('kind', 'title', 'area', 'summary', 'state', 'lga', 'ppa', 'source_type', 'source_url', 'observed_on')
MODERATOR_FIELDS = ('source_type', 'source_url', 'publication_status', 'verification_status', 'checked_at', 'expires_at', 'moderator_note')
DETAIL_MODELS = {Listing.Kind.HOUSING: HousingDetails, Listing.Kind.FARE: FareDetails, Listing.Kind.SERVICE: ServiceDetails}
DETAIL_FIELDS = {
    Listing.Kind.HOUSING: ('reported_rent', 'price_period', 'room_type', 'amenities', 'availability', 'contributor_role', 'authorization_attested', 'authorization_basis'),
    Listing.Kind.FARE: ('origin_area', 'destination_area', 'mode', 'amount_low', 'amount_high'),
    Listing.Kind.SERVICE: ('category', 'opening_hours', 'public_business_address'),
}
FRESHNESS_DAYS = {Listing.Kind.HOUSING: 14, Listing.Kind.FARE: 90, Listing.Kind.SERVICE: 180}


def _require_member(user, *, verified=True):
    if not user.is_authenticated or not user.is_active or (verified and not user.email_verified):
        raise PermissionDenied('Sign in and verify your email before contributing to shared information.')


def _require_permission(user, permission):
    if not user.is_authenticated or not user.is_active or not user.has_perm(permission):
        raise PermissionDenied('You do not have permission for this moderation action.')


def _lock_member(user):
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def _json(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value) if isinstance(value, Decimal) else value


def _snapshot(listing):
    fields = ('kind', 'title', 'area', 'summary', 'state_id', 'lga_id', 'ppa_id', 'author_id', 'source_type', 'source_url', 'publication_status', 'verification_status', 'observed_on', 'checked_at', 'expires_at', 'last_reconfirmed_at', 'moderator_note', 'moderated_by_id', 'moderated_at')
    snapshot = {name: _json(getattr(listing, name)) for name in fields}
    detail = getattr(listing, listing.kind)
    snapshot['details'] = {name: _json(getattr(detail, name)) for name in DETAIL_FIELDS[listing.kind]}
    return snapshot


def _log(listing, actor, reason):
    ListingRevision.objects.create(listing=listing, actor=actor, snapshot=_snapshot(listing), reason=reason)


def _expiry(kind, observed_on):
    age = (timezone.localdate() - observed_on).days
    return timezone.now() + timedelta(days=FRESHNESS_DAYS[kind] - age)


def _reset_proposal(listing, *, submit):
    listing.publication_status = Listing.Publication.PENDING if submit else Listing.Publication.DRAFT
    listing.verification_status = Listing.Verification.COMMUNITY
    listing.checked_at = None
    listing.moderated_by = None
    listing.moderated_at = None
    # Prior private evidence remains in immutable revisions, but cannot approve
    # a changed permission-to-share assertion without a fresh review.
    listing.moderator_note = ''
    listing.expires_at = _expiry(listing.kind, listing.observed_on)


@transaction.atomic
def save_listing(*, user, cleaned_data, details, listing=None, submit=False):
    _require_member(user)
    _require_member(_lock_member(user))
    created = listing is None
    if not created:
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        if listing.author_id != user.pk:
            raise PermissionDenied('Only the contributor can edit this information.')
        if listing.publication_status == Listing.Publication.ARCHIVED:
            raise ValidationError('Archived entries are kept as history. Submit a new entry if circumstances change.')
        if cleaned_data.get('kind', listing.kind) != listing.kind:
            raise ValidationError({'kind': 'An existing entry cannot change its type.'})
    else:
        if Listing.objects.filter(author=user, created_at__gte=timezone.now() - timedelta(days=1)).count() >= 20:
            raise ValidationError('Your daily contribution limit has been reached. Please try again tomorrow.')
        listing = Listing(author=user)
    source = cleaned_data.get('source_type', Listing.Source.MEMBER)
    if source not in (Listing.Source.MEMBER, Listing.Source.OWNER):
        raise PermissionDenied('Members cannot assign editorial or official-source status.')
    for name in MEMBER_FIELDS:
        if name in cleaned_data:
            setattr(listing, name, cleaned_data[name])
    listing.source_type = source
    for name in ('title', 'area', 'summary'):
        setattr(listing, name, ' '.join(getattr(listing, name).split()) if name != 'summary' else getattr(listing, name).strip())
    if listing.kind not in DETAIL_MODELS:
        raise ValidationError({'kind': 'Choose a supported entry type.'})
    # Validate the date before age-dependent expiry, then drop old verification
    # before checking the author's replacement source/content.
    listing.observed_on = Listing._meta.get_field('observed_on').clean(listing.observed_on, listing)
    if listing.observed_on > timezone.localdate():
        raise ValidationError({'observed_on': 'The observation date cannot be in the future.'})
    _reset_proposal(listing, submit=submit)
    duplicates = Listing.objects.filter(author=user, kind=listing.kind, lga=listing.lga, title__iexact=listing.title, area__iexact=listing.area).exclude(publication_status=Listing.Publication.ARCHIVED)
    if listing.pk:
        duplicates = duplicates.exclude(pk=listing.pk)
    if duplicates.exists():
        raise ValidationError('You already contributed this entry in this area. Update your existing entry instead.')
    listing.full_clean()
    listing.save()
    detail_model = DETAIL_MODELS[listing.kind]
    detail = detail_model.objects.select_for_update().filter(listing=listing).first() or detail_model(listing=listing)
    for name in DETAIL_FIELDS[listing.kind]:
        if name in details:
            setattr(detail, name, details[name])
    detail.full_clean()
    detail.save()
    # Clear the related cache in case this object came from a joined queryset.
    listing._state.fields_cache.pop(listing.kind, None)
    _log(listing, user, ('Created' if created else 'Updated') + (' and submitted for moderation.' if submit else ' as a private draft.'))
    return listing


@transaction.atomic
def archive_listing(*, listing, user):
    _require_member(user)
    _require_member(_lock_member(user))
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if listing.author_id != user.pk:
        raise PermissionDenied('Only the contributor can withdraw this information.')
    if listing.publication_status != Listing.Publication.ARCHIVED:
        listing.publication_status = Listing.Publication.ARCHIVED
        listing.save(update_fields=['publication_status', 'updated_at'])
        _log(listing, user, 'Archived by the contributor; removed from public listings.')
    return listing


@transaction.atomic
def reconfirm_listing(*, listing, user, observed_on, available=None):
    _require_member(user)
    _require_member(_lock_member(user))
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if listing.author_id != user.pk:
        raise PermissionDenied('Only the contributor can reconfirm this entry.')
    if listing.publication_status == Listing.Publication.ARCHIVED:
        raise ValidationError('Archived entries cannot be reconfirmed. Submit a new report.')
    observed_on = Listing._meta.get_field('observed_on').clean(observed_on, listing)
    if observed_on < listing.observed_on or observed_on > timezone.localdate():
        raise ValidationError({'observed_on': 'Choose a new observation date between the previous observation and today.'})
    if listing.kind == Listing.Kind.HOUSING:
        if not isinstance(available, bool):
            raise ValidationError('Confirm whether the accommodation is still available.')
        housing = HousingDetails.objects.select_for_update().get(listing=listing)
        housing.availability = HousingDetails.Availability.AVAILABLE if available else HousingDetails.Availability.UNAVAILABLE
        housing.full_clean()
        housing.save(update_fields=['availability'])
    listing.observed_on = observed_on
    listing.last_reconfirmed_at = timezone.now()
    _reset_proposal(listing, submit=True)
    listing.full_clean()
    listing.save()
    _log(listing, user, 'Contributor reconfirmed the information and resubmitted it for review; independent verification reset.')
    return listing


@transaction.atomic
def moderate_listing(*, listing, actor, values, reason):
    _require_permission(actor, 'local_guides.can_moderate_listings')
    if not reason.strip():
        raise ValidationError('Record a reason and the evidence supporting this decision.')
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if listing.publication_status == Listing.Publication.DRAFT:
        raise PermissionDenied('Private drafts must be submitted before moderation.')
    if listing.publication_status == Listing.Publication.ARCHIVED:
        raise ValidationError('Archived information cannot be republished. Ask for a new contribution.')
    before = {name: getattr(listing, name) for name in MODERATOR_FIELDS}
    for name in MODERATOR_FIELDS:
        if name in values:
            setattr(listing, name, values[name])
    if listing.publication_status == Listing.Publication.DRAFT:
        raise ValidationError('Moderators cannot turn submitted information into a private author draft.')
    strong = listing.verification_status != Listing.Verification.COMMUNITY or listing.source_type == Listing.Source.OFFICIAL
    sensitive = ('source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at')
    if strong and any(before[name] != getattr(listing, name) for name in sensitive):
        _require_permission(actor, 'local_guides.can_verify_listings')
    if listing.verification_status != Listing.Verification.COMMUNITY and listing.author_id == actor.pk:
        raise PermissionDenied('Independent verification must be performed by another person.')
    detail = getattr(listing, listing.kind, None)
    if detail is None:
        raise ValidationError('This entry has no complete typed details. Ask the contributor for a new submission.')
    detail.full_clean()
    # Permission-to-share assertions are not proof of property ownership.
    if listing.kind == Listing.Kind.HOUSING and listing.publication_status == Listing.Publication.APPROVED:
        housing = detail
        if housing.contributor_role in (HousingDetails.ContributorRole.OWNER, HousingDetails.ContributorRole.AGENT, HousingDetails.ContributorRole.PPA) and not listing.moderator_note.strip():
            raise ValidationError({'moderator_note': 'Review and record the authority to share this accommodation before publication. Do not record IDs or private contacts.'})
    listing.moderated_by = actor
    listing.moderated_at = timezone.now()
    listing.full_clean()
    listing.save()
    _log(listing, actor, reason.strip())
    return listing


def public_listings(*, include_expired=False):
    queryset = Listing.objects.filter(publication_status=Listing.Publication.APPROVED).filter(
        Q(kind=Listing.Kind.HOUSING, housing__isnull=False)
        | Q(kind=Listing.Kind.FARE, fare__isnull=False)
        | Q(kind=Listing.Kind.SERVICE, service__isnull=False)
    ).select_related('state', 'lga', 'ppa', 'housing', 'fare', 'service')
    if not include_expired:
        queryset = queryset.filter(expires_at__gt=timezone.now()).exclude(kind=Listing.Kind.HOUSING, housing__availability=HousingDetails.Availability.UNAVAILABLE)
    return queryset


def get_public_listing(pk):
    listing = public_listings(include_expired=True).filter(pk=pk).first()
    if listing is None:
        raise Http404('This entry is not publicly available.')
    return listing


def safe_public_listing(listing, *, user=None):
    """Only this projection should enter anonymous list/detail template contexts."""
    if listing.publication_status != Listing.Publication.APPROVED and not (user and user.is_authenticated and user.pk == listing.author_id):
        raise PermissionDenied('This entry is not publicly available.')
    fields = ('id', 'kind', 'title', 'area', 'summary', 'state_id', 'lga_id', 'ppa_id', 'source_type', 'source_url', 'publication_status', 'verification_status', 'observed_on', 'checked_at', 'expires_at', 'last_reconfirmed_at', 'created_at', 'updated_at')
    result = {name: getattr(listing, name) for name in fields}
    detail = getattr(listing, listing.kind)
    detail_fields = [name for name in DETAIL_FIELDS[listing.kind] if name not in ('authorization_attested', 'authorization_basis')]
    result[listing.kind] = {name: getattr(detail, name) for name in detail_fields}
    for field in detail_fields:
        display = getattr(detail, 'get_' + field + '_display', None)
        if display:
            result[listing.kind][field + '_label'] = display()
    if listing.kind == Listing.Kind.HOUSING:
        result['housing']['amenity_labels'] = detail.amenity_labels
    result.update(
        kind_label=listing.get_kind_display(), source_label=listing.get_source_type_display(), publication_label=listing.get_publication_status_display(),
        verification_label=listing.get_verification_status_display() + (' (historical; reconfirmation needed)' if listing.is_stale else ''),
        state_name=listing.state.name, lga_name=listing.lga.name,
        ppa_name=listing.ppa.name if listing.ppa_id else '',
        is_stale=listing.is_stale, is_available=listing.is_available,
        freshness_label=listing.freshness_label,
    )
    return result


def owner_revisions(*, listing, user):
    if not user.is_authenticated or listing.author_id != user.pk:
        raise PermissionDenied('Only the contributor can read this history.')
    rows = ListingRevision.objects.filter(listing=listing).order_by('-created_at', '-pk').values('id', 'snapshot', 'created_at')[:100]
    safe_fields = ('kind', 'title', 'area', 'summary', 'source_type', 'source_url', 'publication_status', 'verification_status', 'observed_on', 'checked_at', 'expires_at', 'last_reconfirmed_at')
    result = []
    for row in rows:
        snapshot = row['snapshot'] if isinstance(row['snapshot'], dict) else {}
        item = {name: snapshot.get(name) for name in safe_fields}
        details = snapshot.get('details', {})
        if not isinstance(details, dict):
            details = {}
        item['details'] = {name: details.get(name) for name in DETAIL_FIELDS.get(snapshot.get('kind'), ()) if name not in ('authorization_basis', 'authorization_attested')}
        item.update(id=row['id'], created_at=row['created_at'], reason='A saved contribution or moderation update.')
        result.append(item)
    return result


@transaction.atomic
def save_report(*, user, listing, category, details, proposed_correction=''):
    _require_member(user, verified=False)
    _require_member(_lock_member(user), verified=False)
    # Private draft IDs are never valid report targets.
    listing = get_public_listing(listing.pk)
    if ListingReport.objects.filter(reporter=user, created_at__gte=timezone.now() - timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily report limit has been reached. Please try again tomorrow.')
    report = ListingReport(listing=listing, reporter=user, category=category, details=details.strip(), proposed_correction=proposed_correction.strip())
    report.full_clean()
    report.save()
    return report


@transaction.atomic
def moderate_report(*, report, actor, status, moderator_note):
    _require_permission(actor, 'local_guides.can_moderate_listing_reports')
    if not moderator_note.strip():
        raise ValidationError('Record what was checked and the reason for this outcome.')
    report = ListingReport.objects.select_for_update().get(pk=report.pk)
    previous = report.status
    if report.status != status or report.moderator_note != moderator_note.strip():
        report.status = status
        report.moderator_note = moderator_note.strip()
        report.moderated_by = actor
        report.moderated_at = timezone.now()
        report.full_clean()
        report.save()
        ListingReportRevision.objects.create(report=report, actor=actor, previous_status=previous, new_status=report.status, moderator_note=report.moderator_note)
    return report


@transaction.atomic
def set_bookmark(*, user, listing, save=True):
    _require_member(user, verified=False)
    if save:
        listing = get_public_listing(listing.pk)
        return ListingBookmark.objects.get_or_create(user=user, listing=listing)[0]
    ListingBookmark.objects.filter(user=user, listing=listing).delete()
    return None
