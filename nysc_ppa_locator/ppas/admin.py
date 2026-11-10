from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone
from .models import (
    Bookmark, ClaimRevision, Confirmation, PPA, PPAClaim, PlaceChangeLog, PlaceReport, ReportChangeLog,
    Review, ReviewChangeLog, ReviewRevision,
)
from .profile_services import MODERATOR_CLAIM_FIELDS, moderate_claim, moderate_review


PLACE_AUDIT_FIELDS = (
    'name', 'address', 'state_id', 'lga_id', 'latitude', 'longitude', 'place_id', 'category', 'google_maps_url',
    'status', 'source', 'location_status', 'aliases', 'landmark', 'entrance_instructions',
    'organisation_evidence', 'phone', 'email', 'description',
)

# Both form names and stored FK names are used by the two checks below.
_ORGANISATION_VERIFICATION_FIELDS = frozenset({
    'status', 'organisation_evidence', 'name', 'aliases', 'address', 'state', 'state_id',
    'lga', 'lga_id', 'latitude', 'longitude', 'place_id', 'google_maps_url',
    'landmark', 'entrance_instructions', 'phone', 'email', 'source', 'location_status',
})


_SUBMITTED_CLAIM_STATUSES = (
    PPAClaim.Publication.PENDING,
    PPAClaim.Publication.APPROVED,
    PPAClaim.Publication.REJECTED,
)


def _moderator_visible_claims(queryset):
    # Archiving a private draft never submits its contents to moderators. Infer
    # its last state from existing snapshots without rewriting member history.
    last_state = ClaimRevision.objects.filter(claim_id=OuterRef('pk')).filter(
        Q(snapshot__publication_status__isnull=True)
        | ~Q(snapshot__publication_status=PPAClaim.Publication.ARCHIVED),
    ).order_by('-created_at', '-pk').annotate(
        publication=KeyTextTransform('publication_status', 'snapshot'),
    ).values('publication')[:1]
    return queryset.alias(_last_revision_state=Subquery(last_state)).filter(
        Q(publication_status__in=_SUBMITTED_CLAIM_STATUSES)
        | Q(publication_status=PPAClaim.Publication.ARCHIVED,
            _last_revision_state__in=_SUBMITTED_CLAIM_STATUSES)
    )


class PPAAdminForm(forms.ModelForm):
    moderation_reason = forms.CharField(max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 2}),
                                       help_text='Required when changing an existing destination, provenance or trust status.')

    class Meta:
        model = PPA
        fields = '__all__'

    def clean(self):
        data = super().clean()
        changed = set(self.changed_data) - {'moderation_reason'}
        if self.instance.pk and changed and not data.get('moderation_reason', '').strip():
            self.add_error('moderation_reason', 'Explain the evidence supporting this change.')
        actor = getattr(self, 'actor', None)
        if (actor and data.get('status') == PPA.Status.ORGANISATION_CONFIRMED
                and (not self.instance.pk or _ORGANISATION_VERIFICATION_FIELDS & changed)
                and not actor.has_perm('ppas.can_verify_organisations')):
            self.add_error('status', 'Retaining organisation confirmation after an identity, destination or evidence change requires a separately authorised verifier.')
        return data


@admin.register(PPA)
class PPAAdmin(admin.ModelAdmin):
    form = PPAAdminForm
    list_select_related = ('state', 'lga')
    list_per_page = 50
    show_full_result_count = False
    list_display = ('name', 'state', 'lga', 'status', 'location_status', 'source', 'last_confirmed_at')
    list_filter = ('status', 'location_status', 'source', 'category', 'state')
    search_fields = ('name', 'aliases', 'address', 'place_id')
    readonly_fields = ('submitted_by', 'last_confirmed_at', 'created_at', 'updated_at', 'verified')
    actions = None

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)
        return type('RequestPPAAdminForm', (base_form,), {'actor': request.user})

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        changes = {}
        if change:
            before = PPA.objects.select_for_update().get(pk=obj.pk)
            changes = {field: {'from': getattr(before, field), 'to': getattr(obj, field)}
                       for field in PLACE_AUDIT_FIELDS if getattr(before, field) != getattr(obj, field)}
        if (obj.status == PPA.Status.ORGANISATION_CONFIRMED
                and (not change or _ORGANISATION_VERIFICATION_FIELDS & set(changes))
                and not request.user.has_perm('ppas.can_verify_organisations')):
            raise PermissionDenied('Retaining organisation confirmation after an identity, destination or evidence change requires a separately authorised verifier.')
        super().save_model(request, obj, form, change)
        if not change:
            changes = {'created': {field: getattr(obj, field) for field in PLACE_AUDIT_FIELDS}}
        if changes:
            PlaceChangeLog.objects.create(ppa=obj, actor=request.user, changes=changes,
                                         reason=form.cleaned_data.get('moderation_reason') or 'Created by a moderator.')

    def has_delete_permission(self, request, obj=None):
        return False


class ReviewAdminForm(forms.ModelForm):
    moderation_note = forms.CharField(max_length=1000, required=False)

    class Meta:
        model = Review
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if 'is_public' in self.changed_data and not data.get('moderation_note', '').strip():
            self.add_error('moderation_note', 'Record why the review is being hidden or restored.')
        return data


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    form = ReviewAdminForm
    list_select_related = ('ppa', 'user')
    raw_id_fields = ('ppa', 'user')
    list_per_page = 50
    show_full_result_count = False
    list_display = ('ppa', 'user', 'rating', 'is_public', 'created_at')
    list_filter = ('is_public', 'rating', 'created_at')
    readonly_fields = ('ppa', 'user', 'rating', 'comment', 'topic_tags', 'context_date', 'created_at', 'updated_at')
    actions = None

    def save_model(self, request, obj, form, change):
        if change:
            moderate_review(review=obj, actor=request.user, is_public=obj.is_public,
                            note=form.cleaned_data.get('moderation_note') or 'Review visibility unchanged.')

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_reviews')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PlaceReportAdminForm(forms.ModelForm):
    class Meta:
        model = PlaceReport
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if {'status', 'moderator_note'} & set(self.changed_data) and not data.get('moderator_note', '').strip():
            self.add_error('moderator_note', 'Explain what was checked and how this report was resolved.')
        return data


@admin.register(PlaceReport)
class PlaceReportAdmin(admin.ModelAdmin):
    form = PlaceReportAdminForm
    list_select_related = ('ppa', 'reporter', 'moderated_by')
    list_display = ('ppa', 'category', 'status', 'created_at', 'moderated_by')
    list_filter = ('status', 'category')
    search_fields = ('ppa__name', 'details')
    readonly_fields = ('ppa', 'reporter', 'review', 'category', 'details', 'proposed_correction',
                       'created_at', 'updated_at', 'moderated_by', 'moderated_at')
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def save_model(self, request, obj, form, change):
        before = PlaceReport.objects.get(pk=obj.pk) if change else None
        if before and (before.status != obj.status or before.moderator_note != obj.moderator_note):
            obj.moderated_by = request.user
            obj.moderated_at = timezone.now()
        super().save_model(request, obj, form, change)
        if before and (before.status != obj.status or before.moderator_note != obj.moderator_note):
            ReportChangeLog.objects.create(report=obj, actor=request.user, previous_status=before.status,
                                          new_status=obj.status, note=obj.moderator_note)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AuditAdmin(admin.ModelAdmin):
    list_per_page = 50
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlaceChangeLog)
class PlaceChangeLogAdmin(AuditAdmin):
    list_display = ('ppa', 'actor', 'created_at')
    list_select_related = ('ppa', 'actor')


@admin.register(ReportChangeLog)
class ReportChangeLogAdmin(AuditAdmin):
    list_display = ('report', 'previous_status', 'new_status', 'actor', 'created_at')
    list_select_related = ('report', 'actor')


@admin.register(ReviewChangeLog)
class ReviewChangeLogAdmin(AuditAdmin):
    list_display = ('review', 'previous_visibility', 'new_visibility', 'actor', 'created_at')
    list_select_related = ('review', 'actor')


@admin.register(Confirmation)
class ConfirmationAdmin(AuditAdmin):
    list_display = ('ppa', 'user', 'confirmed_at')
    list_select_related = ('ppa', 'user')


@admin.register(Bookmark)
class BookmarkAdmin(AuditAdmin):
    list_display = ('ppa', 'user', 'created_at')
    list_select_related = ('ppa', 'user')


class PPAClaimAdminForm(forms.ModelForm):
    moderation_reason = forms.CharField(max_length=1000, label='Reason for this change', widget=forms.Textarea(attrs={'rows': 2}))

    class Meta:
        model = PPAClaim
        fields = '__all__'

    def clean(self):
        data = super().clean()
        actor = getattr(self, 'actor', None)
        if actor and not actor.has_perm('ppas.can_verify_organisations'):
            elevated = data.get('verification_status') != PPAClaim.Verification.COMMUNITY or data.get('source_type') == PPAClaim.Source.OFFICIAL
            changed = set(self.changed_data)
            if elevated and changed & {'topic', 'content', 'observed_on', 'source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at'}:
                self.add_error(None, 'Independent verification and edits to verified evidence require a separately authorised verifier. Publication approval alone does not verify a claim.')
        if data.get('verification_status') not in (None, PPAClaim.Verification.COMMUNITY) and not data.get('moderator_note', '').strip():
            self.add_error('moderator_note', 'Record how the supporting source was checked.')
        return data


@admin.register(PPAClaim)
class PPAClaimAdmin(admin.ModelAdmin):
    form = PPAClaimAdminForm
    list_display = ('ppa', 'topic', 'publication_status', 'source_type', 'verification_status', 'observed_on', 'expires_at')
    list_filter = ('publication_status', 'verification_status', 'source_type', 'topic')
    list_select_related = ('ppa', 'author', 'moderated_by')
    search_fields = ('ppa__name', 'content')
    readonly_fields = ('ppa', 'author', 'created_at', 'updated_at', 'moderated_by', 'moderated_at')
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def get_queryset(self, request):
        # A private draft is not a submission to the moderation team.
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else _moderator_visible_claims(queryset)

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)
        return type('RequestPPAClaimAdminForm', (base_form,), {'actor': request.user})

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_claims')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        moderate_claim(claim=obj, actor=request.user,
                       values={name: getattr(obj, name) for name in MODERATOR_CLAIM_FIELDS},
                       reason=form.cleaned_data['moderation_reason'])


@admin.register(ClaimRevision)
class ClaimRevisionAdmin(AuditAdmin):
    list_display = ('claim', 'actor', 'reason', 'created_at')
    list_select_related = ('claim__ppa', 'actor')
    actions = None

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        visible_claims = _moderator_visible_claims(PPAClaim.objects.all()).values('pk')
        # A later submission must not expose earlier draft-only snapshots. An
        # archived snapshot is shared only if its preceding non-archived state
        # was submitted; unknown legacy states remain private conservatively.
        previous_state = ClaimRevision.objects.filter(claim_id=OuterRef('claim_id')).filter(
            Q(created_at__lt=OuterRef('created_at'))
            | Q(created_at=OuterRef('created_at'), pk__lt=OuterRef('pk'))
        ).filter(
            Q(snapshot__publication_status__isnull=True)
            | ~Q(snapshot__publication_status=PPAClaim.Publication.ARCHIVED),
        ).order_by(
            '-created_at', '-pk',
        ).annotate(publication=KeyTextTransform('publication_status', 'snapshot')).values('publication')[:1]
        return queryset.filter(claim_id__in=visible_claims).alias(
            _previous_revision_state=Subquery(previous_state),
        ).filter(
            Q(snapshot__publication_status__in=_SUBMITTED_CLAIM_STATUSES)
            | Q(snapshot__publication_status=PPAClaim.Publication.ARCHIVED,
                _previous_revision_state__in=_SUBMITTED_CLAIM_STATUSES)
        )

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_claims')


@admin.register(ReviewRevision)
class ReviewRevisionAdmin(AuditAdmin):
    list_display = ('review', 'rating', 'is_public', 'created_at')
    list_select_related = ('review__ppa', 'review__user', 'actor')
    actions = None

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_reviews')


# Keep independent PPA proposal moderation in its own module.
from . import submission_admin  # noqa: E402,F401
