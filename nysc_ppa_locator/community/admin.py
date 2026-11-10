"""Scoped community moderation through the existing OTP-protected admin site."""
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .models import (
    Answer, AnswerRevision, CommunityReport, Question, QuestionRevision,
    ReportRevision,
)
from .services import moderate_answer, moderate_question, moderate_report


def _permission(request, codename):
    return request.user.is_active and request.user.has_perm('community.' + codename)


class ModerationForm(forms.ModelForm):
    moderation_reason = forms.CharField(
        max_length=1000, widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Explain why this community contribution should be hidden or restored. This does not establish official NYSC guidance.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = [
            (value, label) for value, label in self.fields['status'].choices
            if value in ('visible', 'hidden')
        ]

    def clean_moderation_reason(self):
        value = self.cleaned_data['moderation_reason'].strip()
        if not value:
            raise forms.ValidationError('Explain the moderation decision.')
        return value

    def _post_clean(self):
        # Only status and reason are editable here. Revalidating the readonly
        # source references would prevent hiding a question after its original
        # CDS source becomes private or expires. The locked moderation service
        # validates the transition and records the decision.
        if 'status' in self.cleaned_data:
            self.instance.status = self.cleaned_data['status']


class QuestionModerationForm(ModerationForm):
    class Meta:
        model = Question
        fields = ('status',)


class AnswerModerationForm(ModerationForm):
    class Meta:
        model = Answer
        fields = ('status',)


class CommunityContentAdmin(admin.ModelAdmin):
    list_per_page = 50
    show_full_result_count = False
    actions = None
    list_filter = ('status',)

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_community')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj) and (obj is None or obj.status != 'archived')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if field.name != 'status')

    def get_fields(self, request, obj=None):
        return (*self.get_readonly_fields(request, obj), 'status', 'moderation_reason')

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied('Use the member workflow to create community contributions.')
        service = moderate_question if isinstance(obj, Question) else moderate_answer
        argument = 'question' if isinstance(obj, Question) else 'answer'
        service(**{argument: obj}, actor=request.user, status=obj.status,
                reason=form.cleaned_data['moderation_reason'])


@admin.register(Question)
class QuestionAdmin(CommunityContentAdmin):
    form = QuestionModerationForm
    list_display = ('title', 'state', 'lga', 'status', 'resolved', 'version', 'created_at')
    list_filter = ('status', 'category', 'state')
    list_select_related = ('author', 'state', 'lga', 'ppa', 'group')
    search_fields = ('title',)

    def get_readonly_fields(self, request, obj=None):
        replacements = {'ppa': 'safe_ppa', 'group': 'safe_group'}
        return tuple(replacements.get(name, name) for name in super().get_readonly_fields(request, obj))

    @admin.display(description='PPA context')
    def safe_ppa(self, obj):
        return f'PPA #{obj.ppa_id}' if obj.ppa_id else '—'

    @admin.display(description='CDS group context')
    def safe_group(self, obj):
        # A previously public source can later become a private resource draft.
        return f'CDS group #{obj.group_id}' if obj.group_id else '—'


@admin.register(Answer)
class AnswerAdmin(CommunityContentAdmin):
    form = AnswerModerationForm
    list_display = ('id', 'question', 'status', 'version', 'created_at')
    list_select_related = ('question', 'author')
    search_fields = ('body',)


class AppendOnlyAdmin(admin.ModelAdmin):
    list_per_page = 50
    show_full_result_count = False
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ContentRevisionAdmin(AppendOnlyAdmin):
    list_display = ('id', 'actor', 'reason', 'created_at')
    list_select_related = ('actor',)

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_community')


@admin.register(QuestionRevision)
class QuestionRevisionAdmin(ContentRevisionAdmin):
    pass


@admin.register(AnswerRevision)
class AnswerRevisionAdmin(ContentRevisionAdmin):
    pass


class ReportModerationForm(forms.ModelForm):
    class Meta:
        model = CommunityReport
        fields = ('status', 'moderator_note')

    def clean_moderator_note(self):
        value = self.cleaned_data.get('moderator_note', '').strip()
        if not value:
            raise forms.ValidationError('Explain what was checked and why this report is changing.')
        return value


@admin.register(CommunityReport)
class CommunityReportAdmin(admin.ModelAdmin):
    form = ReportModerationForm
    list_display = ('safe_target', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    list_select_related = ('reporter', 'moderated_by')
    search_fields = ('details',)
    fields = ('safe_target', 'reporter', 'category', 'details', 'private_evidence',
              'status', 'moderator_note', 'moderated_by', 'moderated_at', 'created_at', 'updated_at')
    readonly_fields = ('safe_target', 'reporter', 'category', 'details', 'private_evidence',
                       'moderated_by', 'moderated_at', 'created_at', 'updated_at')
    list_per_page = 50
    show_full_result_count = False
    actions = None

    @admin.display(description='Reported contribution')
    def safe_target(self, obj):
        return f'Question #{obj.question_id}' if obj.question_id else f'Answer #{obj.answer_id}'

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_community_reports')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied('Reports must be created through the member report workflow.')
        moderate_report(report=obj, actor=request.user, status=obj.status,
                        moderator_note=form.cleaned_data['moderator_note'])


@admin.register(ReportRevision)
class ReportRevisionAdmin(AppendOnlyAdmin):
    list_display = ('report', 'previous_status', 'new_status', 'actor', 'created_at')
    list_select_related = ('report', 'actor')

    def has_view_permission(self, request, obj=None):
        return _permission(request, 'can_moderate_community_reports')


# Blocks, follows and helpful votes are deliberately absent: these are private
# member preferences, not a member directory or a moderation data source.
