"""OTP-protected, reasoned review of submitted independent place proposals."""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import PPA, PPASubmission, SubmissionRevision
from .submission_services import TRUST_FIELDS, duplicate_candidates, moderate_submission


class PPASubmissionAdminForm(forms.ModelForm):
    decision = forms.ChoiceField(choices=[('', 'Choose a review outcome'), ('approve', 'Approve for the directory'), ('reject', 'Reject proposal')])
    existing_ppa_id = forms.IntegerField(min_value=1, required=False,
                                         help_text='Optional existing PPA ID after reviewing duplicates. Linking never edits the existing record.')
    review_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))

    class Meta:
        model = PPASubmission
        fields = list(TRUST_FIELDS)

    def clean(self):
        data = super().clean()
        actor = getattr(self, 'actor', None)
        if actor and not actor.has_perm('ppas.can_verify_organisations'):
            if data.get('verification_status') not in (None, 'community') or data.get('source_type') == 'official':
                self.add_error(None, 'Independent or attributed official verification requires a separately authorised verifier.')
        existing_id = data.get('existing_ppa_id')
        if existing_id:
            existing = PPA.objects.filter(pk=existing_id, state_id=self.instance.state_id, lga_id=self.instance.lga_id).first()
            if existing is None:
                self.add_error('existing_ppa_id', 'Choose an existing PPA in this proposal’s state and LGA.')
        if data.get('decision') == 'approve':
            if not existing_id and duplicate_candidates(name=self.instance.name, state_id=self.instance.state_id, lga_id=self.instance.lga_id).exists():
                self.add_error('existing_ppa_id', 'Review the exact-name candidates and explicitly link the correct existing PPA.')
            if data.get('expires_at') and data['expires_at'] <= timezone.now():
                self.add_error('expires_at', 'Ask the author to reconfirm outdated details before publication.')
        return data


@admin.register(PPASubmission)
class PPASubmissionAdmin(admin.ModelAdmin):
    form = PPASubmissionAdminForm
    list_display = ('id', 'name', 'state', 'lga', 'publication_status', 'source_type', 'observed_on', 'expires_at')
    list_filter = ('publication_status', 'source_type', 'state')
    list_select_related = ('state', 'lga')
    list_per_page = 40
    show_full_result_count = False
    search_fields = ('name', 'address', 'landmark')
    readonly_fields = ('author', 'name', 'state', 'lga', 'category', 'address', 'landmark', 'entrance_instructions',
                       'latitude', 'longitude', 'normalized_name', 'observed_on', 'publication_status', 'shared_for_review',
                       'published_ppa', 'moderated_by', 'moderated_at', 'created_at', 'updated_at', 'candidate_places')
    actions = None

    @admin.display(description='Possible existing directory entries')
    def candidate_places(self, obj):
        if not obj:
            return 'No proposal selected.'
        rows = list(duplicate_candidates(name=obj.name, state_id=obj.state_id, lga_id=obj.lga_id).values('id', 'name')[:20])
        if not rows:
            return 'No exact-name candidates in this LGA. Also check spelling/branches before creating a new entry.'
        return format_html('<ul>{}</ul>', format_html_join('', '<li><a href="{}">{} — PPA ID {}</a></li>',
                           ((reverse('ppa_detail', args=[row['id']]), row['name'], row['id']) for row in rows)))

    def get_queryset(self, request):
        rows = super().get_queryset(request)
        return rows if request.user.is_superuser else rows.filter(shared_for_review=True).exclude(publication_status='draft')

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)
        return type('RequestPPASubmissionAdminForm', (base_form,), {'actor': request.user})

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_submissions')

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj) and (obj is None or obj.publication_status == 'pending')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        existing = None
        if form.cleaned_data.get('existing_ppa_id'):
            existing = PPA.objects.filter(pk=form.cleaned_data['existing_ppa_id']).first()
            if existing is None:
                raise ValidationError('The selected directory place no longer exists. Review the proposal again.')
        moderate_submission(submission=obj, actor=request.user, decision=form.cleaned_data['decision'],
                            reason=form.cleaned_data['review_reason'], existing_ppa=existing,
                            values={name: form.cleaned_data[name] for name in TRUST_FIELDS})

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError:
            # A concurrent submission/approval can invalidate an otherwise valid
            # form. The admin transaction has rolled back before this redirect.
            self.message_user(request, 'The proposal changed during review or now conflicts with another entry. Reload it and check the current status and duplicate candidates.', level=messages.ERROR)
            return redirect('admin:ppas_ppasubmission_change', object_id=object_id)


@admin.register(SubmissionRevision)
class SubmissionRevisionAdmin(admin.ModelAdmin):
    list_display = ('submission', 'created_at', 'shared_for_review', 'published_as_new')
    list_select_related = ('submission',)
    list_per_page = 40
    show_full_result_count = False
    readonly_fields = ('submission', 'actor', 'snapshot', 'published_ppa', 'published_as_new', 'publication_identity', 'shared_for_review', 'reason', 'created_at')
    actions = None

    def get_queryset(self, request):
        rows = super().get_queryset(request)
        if request.user.is_superuser:
            return rows
        return rows.filter(shared_for_review=True, submission__shared_for_review=True).exclude(submission__publication_status='draft')

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.has_perm('ppas.can_moderate_submissions')

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
