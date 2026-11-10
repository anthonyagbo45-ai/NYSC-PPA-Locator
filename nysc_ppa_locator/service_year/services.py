"""All assignment mutations serialize on the member, including the first save."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from ppas.models import Bookmark, PlaceReport, Review
from .models import MemberPPA, ServiceProfile, StatusChange


def _lock_member(user):
    # Lock the stable parent row: locking only assignments misses the first-save
    # race when neither transaction has an assignment row to lock yet.
    return get_user_model().objects.select_for_update().get(pk=user.pk)


@transaction.atomic
def assign_ppa(user, ppa):
    member = _lock_member(user)
    current = MemberPPA.objects.filter(user=member, ended_at__isnull=True).first()
    if current and current.ppa_id == ppa.pk:
        return current, False
    changed_at = timezone.now()
    if current:
        current.ended_at = max(changed_at, current.started_at)
        current.save(update_fields=['ended_at', 'updated_at'])
    assignment = MemberPPA.objects.create(user=member, ppa=ppa, started_at=changed_at)
    return assignment, True


@transaction.atomic
def update_progress(user, assignment_id, *, status, personal_notes):
    _lock_member(user)
    assignment = get_object_or_404(MemberPPA.objects.select_for_update(), pk=assignment_id, user=user)
    if not assignment.is_current:
        raise ValidationError('Previous assignments are kept as history. Update your current PPA instead.')
    previous_status = assignment.status
    assignment.status = status
    assignment.personal_notes = personal_notes
    assignment.full_clean()
    assignment.save(update_fields=['status', 'personal_notes', 'updated_at'])
    if previous_status != status:
        StatusChange.objects.create(assignment=assignment, previous_status=previous_status, new_status=status)
    return assignment


@transaction.atomic
def end_assignment(user, assignment_id):
    _lock_member(user)
    assignment = get_object_or_404(MemberPPA.objects.select_for_update(), pk=assignment_id, user=user)
    if assignment.is_current:
        assignment.ended_at = max(timezone.now(), assignment.started_at)
        assignment.save(update_fields=['ended_at', 'updated_at'])
    return assignment


@transaction.atomic
def save_service_profile(user, *, stage, batch):
    _lock_member(user)
    profile, _ = ServiceProfile.objects.update_or_create(user=user, defaults={'stage': stage, 'batch': batch})
    return profile


def dashboard_context(user):
    """Six bounded queries, independent of how many records a member has."""
    current = MemberPPA.objects.filter(user=user, ended_at__isnull=True).select_related('ppa__state', 'ppa__lga').first()
    profile = ServiceProfile.objects.filter(user=user).first()
    bookmarks = Bookmark.objects.filter(user=user)
    bookmark_count = bookmarks.count()
    report_counts = PlaceReport.objects.filter(reporter=user).aggregate(
        total=Count('pk'),
        pending=Count('pk', filter=Q(status__in=[PlaceReport.Status.OPEN, PlaceReport.Status.INVESTIGATING])),
    )
    review_count = Review.objects.filter(user=user).count()
    recent_bookmarks = list(bookmarks.select_related('ppa__state', 'ppa__lga').order_by('-created_at', '-pk')[:3])
    return {
        'current_assignment': current,
        'service_profile': profile,
        'bookmark_count': bookmark_count,
        'report_count': report_counts['total'],
        'pending_report_count': report_counts['pending'],
        'review_count': review_count,
        'recent_bookmarks': recent_bookmarks,
    }
