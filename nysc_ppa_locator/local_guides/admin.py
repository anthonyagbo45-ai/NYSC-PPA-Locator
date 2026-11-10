"""Scoped moderation of submitted local information, never private bookmarks."""
from django import forms
from django.contrib import admin
from django.db.models import OuterRef, Q, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.utils.html import format_html_join

from .models import Listing, ListingReport, ListingReportRevision, ListingRevision
from .services import MODERATOR_FIELDS, moderate_listing, moderate_report


_SUBMITTED_STATUSES = (
    Listing.Publication.PENDING,
    Listing.Publication.APPROVED,
    Listing.Publication.REJECTED,
)


def _submitted_listings(queryset):
    """Withdrawing a private draft does not submit it to moderators."""
    last_state = ListingRevision.objects.filter(listing_id=OuterRef('pk')).filter(
        Q(snapshot__publication_status__isnull=True)
        | ~Q(snapshot__publication_status=Listing.Publication.ARCHIVED),
    ).order_by('-created_at', '-pk').annotate(
        publication=KeyTextTransform('publication_status', 'snapshot'),
    ).values('publication')[:1]
    return queryset.alias(_last_revision_state=Subquery(last_state)).filter(
        Q(publication_status__in=_SUBMITTED_STATUSES)
        | Q(publication_status=Listing.Publication.ARCHIVED,
            _last_revision_state__in=_SUBMITTED_STATUSES)
    )


def _may_moderate(user, permission):
    return user.is_active and user.has_perm(permission)


class ListingModerationForm(forms.ModelForm):
    moderation_reason = forms.CharField(
        max_length=1000, label='Reason for this change',
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Record what was reviewed. Publication approval alone does not verify the information.',
    )

    class Meta:
        model = Listing
        fields = '__all__'

    def clean_moderation_reason(self):
        reason = self.cleaned_data['moderation_reason'].strip()
        if not reason:
            raise forms.ValidationError('Record the reason and supporting evidence for this change.')
        return reason

    def _preserve_datetime_precision(self, field):
        value = self.cleaned_data.get(field)
        original = getattr(self.instance, field, None)
        # Django's admin time widget displays seconds. An unchanged round-trip
        # must not turn hidden microseconds into a change to verified evidence.
        if (value is not None and original is not None
                and value.replace(microsecond=0) == original.replace(microsecond=0)):
            return original
        return value

    def clean_checked_at(self):
        return self._preserve_datetime_precision('checked_at')

    def clean_expires_at(self):
        return self._preserve_datetime_precision('expires_at')

    def clean(self):
        values = super().clean()
        actor = getattr(self, 'actor', None)
        evidence_changed = {
            name for name in ('source_type', 'source_url', 'verification_status', 'checked_at', 'expires_at')
            if name in values and values[name] != getattr(self.instance, name, None)
        }
        elevated = (
            values.get('verification_status') not in (None, Listing.Verification.COMMUNITY)
            or values.get('source_type') == 'official'
        )
        if (actor and evidence_changed and elevated
                and not actor.has_perm('local_guides.can_verify_listings')):
            self.add_error(None, 'Stronger verification or changes to verified evidence require a separately authorised verifier.')
        if values.get('verification_status') not in (None, Listing.Verification.COMMUNITY):
            if not values.get('moderator_note', '').strip():
                self.add_error('moderator_note', 'Record how the supporting source was checked.')
        return values


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    form = ListingModerationForm
    list_display = (
        'title', 'kind', 'state', 'lga', 'publication_status',
        'verification_status', 'observed_on', 'expires_at',
    )
    list_filter = ('kind', 'publication_status', 'verification_status', 'source_type', 'state')
    search_fields = ('title', 'area', 'summary')
    list_select_related = ('state', 'lga', 'ppa')
    readonly_fields = (
        'kind', 'title', 'area', 'summary', 'state', 'lga', 'ppa', 'author',
        'observed_on', 'last_reconfirmed_at', 'listing_details',
        'moderated_by', 'moderated_at', 'created_at', 'updated_at',
    )
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related('housing', 'fare', 'service')
        return queryset if request.user.is_superuser else _submitted_listings(queryset)

    def get_form(self, request, obj=None, **kwargs):
        base = super().get_form(request, obj, **kwargs)
        return type('RequestListingModerationForm', (base,), {'actor': request.user})

    @admin.display(description='Submitted details — contributor assertions, not verification')
    def listing_details(self, obj):
        if obj is None:
            return 'No submitted details.'
        detail = getattr(obj, obj.kind, None)
        if detail is None:
            return 'No submitted details.'
        fields = []
        for field in detail._meta.concrete_fields:
            if field.name in ('id', 'listing'):
                continue
            value = getattr(detail, field.name)
            display = getattr(detail, f'get_{field.name}_display', None)
            if display:
                value = display()
            if isinstance(value, (list, tuple)):
                value = ', '.join(str(item) for item in value) or 'Not supplied'
            elif value in (None, ''):
                value = 'Not supplied'
            fields.append((field.verbose_name.capitalize(), value))
        return format_html_join('', '<div><strong>{}:</strong> {}</div>', fields)

    def has_view_permission(self, request, obj=None):
        if not _may_moderate(request.user, 'local_guides.can_moderate_listings'):
            return False
        if obj is not None and not request.user.is_superuser:
            return _submitted_listings(Listing.objects.filter(pk=obj.pk)).exists()
        return True

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.publication_status in (
                Listing.Publication.DRAFT, Listing.Publication.ARCHIVED):
            return False
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        moderate_listing(
            listing=obj, actor=request.user,
            values={name: getattr(obj, name) for name in MODERATOR_FIELDS},
            reason=form.cleaned_data['moderation_reason'],
        )


class AuditAdmin(admin.ModelAdmin):
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ListingRevision)
class ListingRevisionAdmin(AuditAdmin):
    list_display = ('listing', 'actor', 'reason', 'created_at')
    list_select_related = ('listing', 'actor')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        visible = _submitted_listings(Listing.objects.all()).values('pk')
        previous_state = ListingRevision.objects.filter(listing_id=OuterRef('listing_id')).filter(
            Q(created_at__lt=OuterRef('created_at'))
            | Q(created_at=OuterRef('created_at'), pk__lt=OuterRef('pk')),
        ).filter(
            Q(snapshot__publication_status__isnull=True)
            | ~Q(snapshot__publication_status=Listing.Publication.ARCHIVED),
        ).order_by('-created_at', '-pk').annotate(
            publication=KeyTextTransform('publication_status', 'snapshot'),
        ).values('publication')[:1]
        return queryset.filter(listing_id__in=visible).alias(
            _previous_revision_state=Subquery(previous_state),
        ).filter(
            Q(snapshot__publication_status__in=_SUBMITTED_STATUSES)
            | Q(snapshot__publication_status=Listing.Publication.ARCHIVED,
                _previous_revision_state__in=_SUBMITTED_STATUSES)
        )

    def has_view_permission(self, request, obj=None):
        return _may_moderate(request.user, 'local_guides.can_moderate_listings')


class ListingReportModerationForm(forms.ModelForm):
    class Meta:
        model = ListingReport
        fields = '__all__'

    def clean_moderator_note(self):
        note = self.cleaned_data.get('moderator_note', '').strip()
        if not note:
            raise forms.ValidationError('Explain what was checked and the reason for this moderation action.')
        return note


@admin.register(ListingReport)
class ListingReportAdmin(admin.ModelAdmin):
    form = ListingReportModerationForm
    list_display = ('safe_listing', 'category', 'status', 'created_at', 'moderated_by')
    list_filter = ('status', 'category')
    search_fields = ('details',)
    list_select_related = ('listing', 'reporter', 'moderated_by')
    exclude = ('listing',)
    readonly_fields = (
        'safe_listing', 'reporter', 'category', 'details', 'proposed_correction',
        'created_at', 'updated_at', 'moderated_by', 'moderated_at',
    )
    list_per_page = 50
    show_full_result_count = False
    actions = None

    @admin.display(description='Reported entry')
    def safe_listing(self, obj):
        # A public entry can later become a private author draft. Report access
        # must not disclose its new unsubmitted title through a foreign key.
        if obj.listing.publication_status == Listing.Publication.APPROVED:
            return str(obj.listing)
        return f'Local entry #{obj.listing_id} (currently not public)'

    def has_view_permission(self, request, obj=None):
        return _may_moderate(request.user, 'local_guides.can_moderate_listing_reports')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        moderate_report(
            report=obj, actor=request.user, status=obj.status,
            moderator_note=form.cleaned_data['moderator_note'],
        )


@admin.register(ListingReportRevision)
class ListingReportRevisionAdmin(AuditAdmin):
    list_display = ('report', 'previous_status', 'new_status', 'actor', 'created_at')
    list_select_related = ('report__listing', 'actor')

    def has_view_permission(self, request, obj=None):
        return _may_moderate(request.user, 'local_guides.can_moderate_listing_reports')
