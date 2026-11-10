"""Reported-message evidence only, behind the existing OTP-protected admin."""
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.html import format_html

from .models import MessageReport, ReportDecision
from .services import moderate_report


class MessageReportForm(forms.ModelForm):
    class Meta:
        model = MessageReport
        fields = ('status', 'moderator_note')

    def clean_moderator_note(self):
        note = self.cleaned_data.get('moderator_note', '').strip()
        if not note:
            raise forms.ValidationError('Record what was checked and why this report is changing.')
        return note


class ReportEvidenceAdmin(admin.ModelAdmin):
    actions = None
    list_per_page = 30
    show_full_result_count = False

    def allowed(self, request):
        return request.user.is_active and request.user.has_perm('messenger.can_review_message_reports')

    def has_module_permission(self, request):
        return self.allowed(request)

    def has_view_permission(self, request, obj=None):
        return self.allowed(request)

    def has_change_permission(self, request, obj=None):
        return self.allowed(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        rows = super().get_queryset(request)
        return rows if self.allowed(request) else rows.none()


@admin.register(MessageReport)
class MessageReportAdmin(ReportEvidenceAdmin):
    form = MessageReportForm
    list_display = ('id', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    fields = (
        'category', 'details', 'reported_sender_name', 'reported_sent_at',
        'reported_text', 'reported_photo', 'status', 'moderator_note', 'moderated_at',
        'created_at', 'updated_at',
    )
    readonly_fields = tuple(name for name in fields if name not in ('status', 'moderator_note'))

    @admin.display(description='Sender display name at report time')
    def reported_sender_name(self, obj):
        return obj.evidence.get('sender_name', '')

    @admin.display(description='Reported message sent at')
    def reported_sent_at(self, obj):
        return obj.evidence.get('sent_at', '')

    @admin.display(description='Selected reported message')
    def reported_text(self, obj):
        # Django's readonly-field rendering escapes text. Do not mark it safe,
        # turn links into HTML, or load neighbouring conversation messages.
        return obj.evidence.get('body', '')

    @admin.display(description='Selected reported photo')
    def reported_photo(self, obj):
        from photos.models import PhotoEvidence

        if not PhotoEvidence.objects.filter(report_id=obj.pk).exists():
            return 'No photo included in this report.'
        # This application endpoint checks fresh reviewer rights and OTP. Never
        # publish a storage key, signed object URL or conversation attachment URL.
        return format_html(
            '<a href="{}">View selected reported photo</a>',
            reverse('photos:reported_content', args=[obj.pk]),
        )

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied('Use the participant report workflow to create a message report.')
        moderate_report(
            actor=request.user, report_id=obj.pk, status=obj.status,
            moderator_note=form.cleaned_data['moderator_note'],
        )


@admin.register(ReportDecision)
class ReportDecisionAdmin(ReportEvidenceAdmin):
    list_display = ('id', 'report_reference', 'previous_status', 'new_status', 'created_at')
    fields = ('report_reference', 'previous_status', 'new_status', 'moderator_note', 'created_at')
    readonly_fields = fields

    @admin.display(description='Reported-message case')
    def report_reference(self, obj):
        return f'Message report #{obj.report_id}'

    def has_change_permission(self, request, obj=None):
        return False


# Conversations, messages and preferences are deliberately not registered.
# Staff permissions never grant a general private conversation browser.
