"""Scoped source review through the existing OTP-protected administration site."""
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .models import CDSGroup, GroupRevision, NoticeRevision, ResourceReport, ResourceReportRevision, ServiceNotice
from .services import MODERATOR_FIELDS, moderate_group, moderate_notice, moderate_report


def _permission(request, codename):
    return request.user.is_active and request.user.has_perm('service_resources.' + codename)


class ModerationForm(forms.ModelForm):
    moderation_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 3}), help_text='Record the exact source page and statement supporting this locality/date. A homepage alone cannot confirm an individual clearance schedule. Publication does not grant NYSC approval.')

    def clean_moderation_reason(self):
        value = self.cleaned_data['moderation_reason'].strip()
        if not value:
            raise forms.ValidationError('Record why this information is being changed.')
        return value

    def _precision(self, field):
        value, old = self.cleaned_data.get(field), getattr(self.instance, field, None)
        if value is not None and old is not None and value.replace(microsecond=0) == old.replace(microsecond=0):
            return old
        return value

    def clean_checked_at(self):
        return self._precision('checked_at')

    def clean_expires_at(self):
        return self._precision('expires_at')

    def clean(self):
        data = super().clean()
        actor = getattr(self, 'actor', None)
        strong = data.get('verification_status') not in (None, 'community') or data.get('source_type') == 'official'
        changed = any(name in data and data[name] != getattr(self.instance, name, None) for name in MODERATOR_FIELDS if name != 'moderator_note')
        if actor and strong and changed and not actor.has_perm('service_resources.can_verify_resources'):
            self.add_error(None, 'Independent or official-source verification requires a separately authorised verifier.')
        return data


class GroupModerationForm(ModerationForm):
    class Meta:
        model = CDSGroup
        fields = '__all__'


class NoticeModerationForm(ModerationForm):
    class Meta:
        model = ServiceNotice
        fields = '__all__'


class ResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'state', 'lga', 'publication_status', 'verification_status', 'observed_on', 'expires_at', 'version')
    list_filter = ('publication_status', 'verification_status', 'source_type', 'state')
    search_fields = ('title',)
    list_select_related = ('state', 'lga', 'author')
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else queryset.filter(shared_for_review=True).exclude(publication_status='draft')

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        return type('ActorResourceModerationForm', (form,), {'actor': request.user})

    def has_view_permission(self, request, obj=None):
        return (_permission(request, 'can_moderate_resources')
                and (obj is None or request.user.is_superuser or (obj.shared_for_review and obj.publication_status != 'draft')))

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj) and (obj is None or obj.publication_status not in ('draft', 'archived'))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied('Resource creation requires the member contribution workflow.')
        service = moderate_group if isinstance(obj, CDSGroup) else moderate_notice
        argument = 'group' if isinstance(obj, CDSGroup) else 'notice'
        service(**{argument: obj}, actor=request.user,
                values={name: getattr(obj, name) for name in MODERATOR_FIELDS}, reason=form.cleaned_data['moderation_reason'])


@admin.register(CDSGroup)
class CDSGroupAdmin(ResourceAdmin):
    form = GroupModerationForm
    readonly_fields = ('title', 'description', 'venue_hint', 'public_contact_url', 'state', 'lga', 'author', 'observed_on', 'shared_for_review', 'version', 'moderated_by', 'moderated_at', 'created_at', 'updated_at')


@admin.register(ServiceNotice)
class ServiceNoticeAdmin(ResourceAdmin):
    form = NoticeModerationForm
    readonly_fields = ('kind', 'title', 'body', 'venue_hint', 'state', 'lga', 'group', 'event_at', 'ends_at', 'timezone_name', 'batch_label', 'stage', 'author', 'observed_on', 'shared_for_review', 'version', 'moderated_by', 'moderated_at', 'created_at', 'updated_at')


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


class ResourceRevisionAdmin(AuditAdmin):
    list_display = ('id', 'actor', 'reason', 'created_at')
    list_select_related = ('actor',)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else queryset.filter(shared_for_review=True)

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_resources') and (obj is None or request.user.is_superuser or obj.shared_for_review)


@admin.register(GroupRevision)
class GroupRevisionAdmin(ResourceRevisionAdmin):
    pass


@admin.register(NoticeRevision)
class NoticeRevisionAdmin(ResourceRevisionAdmin):
    pass


class ReportModerationForm(forms.ModelForm):
    class Meta:
        model = ResourceReport
        fields = '__all__'

    def clean_moderator_note(self):
        value = self.cleaned_data.get('moderator_note', '').strip()
        if not value:
            raise forms.ValidationError('Explain what was checked and why this report is changing.')
        return value


@admin.register(ResourceReport)
class ResourceReportAdmin(admin.ModelAdmin):
    form = ReportModerationForm
    list_display = ('safe_target', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    list_select_related = ('group', 'notice', 'reporter')
    search_fields = ('details',)
    exclude = ('group', 'notice')
    readonly_fields = ('safe_target', 'reporter', 'category', 'details', 'proposed_correction', 'created_at', 'updated_at', 'moderated_by', 'moderated_at')
    list_per_page = 50
    show_full_result_count = False
    actions = None

    @admin.display(description='Reported resource')
    def safe_target(self, obj):
        # Generic labels do not expose a later unsubmitted private title.
        return f'CDS group #{obj.group_id}' if obj.group_id else f'Service notice #{obj.notice_id}'

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_resource_reports')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied('Reports must be submitted through the member report workflow.')
        moderate_report(report=obj, actor=request.user, status=obj.status, moderator_note=form.cleaned_data['moderator_note'])


@admin.register(ResourceReportRevision)
class ResourceReportRevisionAdmin(AuditAdmin):
    list_display = ('report', 'previous_status', 'new_status', 'actor', 'created_at')
    list_select_related = ('report', 'actor')

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_resource_reports')
