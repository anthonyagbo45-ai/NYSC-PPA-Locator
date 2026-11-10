"""Separate, permission-scoped moderation desks behind the site's OTP admin."""
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from service_resources.models import CDSGroup
from .models import (Room, RoomMembership, RoomMembershipRevision, RoomPost,
                     RoomPostRevision, RoomReport, RoomReportRevision, RoomRevision)
from .services import ROOM_FIELDS, moderate_membership, moderate_post, moderate_report, save_room


class ScopedAdmin(admin.ModelAdmin):
    permission = ''
    actions = None
    list_per_page = 50
    show_full_result_count = False

    def allowed(self, request):
        return request.user.is_active and request.user.has_perm('local_rooms.' + self.permission)

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


class ReasonForm(forms.ModelForm):
    moderation_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 3}), label='Reason for this decision')

    def clean_moderation_reason(self):
        reason = self.cleaned_data['moderation_reason'].strip()
        if not reason:
            raise forms.ValidationError('Record a private reason for this moderation decision.')
        return reason


class RoomForm(ReasonForm):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput, required=False)

    class Meta:
        model = Room
        fields = ROOM_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['expected_version'].required = True
            self.fields['expected_version'].initial = self.instance.version

    def clean(self):
        data = super().clean()
        if self.instance.pk and data.get('expected_version') != self.instance.version:
            self.add_error(None, 'This room changed since you opened it. Reload before saving.')
        return data


@admin.register(Room)
class RoomAdmin(ScopedAdmin):
    permission = 'can_manage_rooms'
    form = RoomForm
    fields = (*ROOM_FIELDS, 'moderation_reason', 'expected_version', 'created_by', 'moderated_by', 'moderated_at', 'version', 'created_at', 'updated_at')
    readonly_fields = ('created_by', 'moderated_by', 'moderated_at', 'version', 'created_at', 'updated_at')
    list_display = ('id', 'title', 'state', 'lga', 'visibility', 'status', 'version')
    list_filter = ('visibility', 'status', 'state')
    list_select_related = ('state', 'lga')
    search_fields = ('title', 'description')

    def has_add_permission(self, request):
        return self.allowed(request)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'group':
            kwargs['queryset'] = CDSGroup.objects.filter(publication_status='approved', expires_at__gt=timezone.now())
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        saved = save_room(actor=request.user, values={name: getattr(obj, name) for name in ROOM_FIELDS},
                          reason=form.cleaned_data['moderation_reason'], room=obj if change else None,
                          expected_version=form.cleaned_data.get('expected_version'))
        obj.pk = saved.pk
        obj._state.adding = False
        obj._state.db = saved._state.db


class MembershipForm(ReasonForm):
    class Meta:
        model = RoomMembership
        fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'status' in self.fields:
            self.fields['status'].choices = [(key, label) for key, label in RoomMembership.Status.choices if key in ('approved', 'rejected', 'banned')]


@admin.register(RoomMembership)
class RoomMembershipAdmin(ScopedAdmin):
    permission = 'can_moderate_room_memberships'
    form = MembershipForm
    fields = ('room', 'user', 'status', 'moderation_reason', 'notifications_enabled', 'notifications_enabled_at', 'requested_at', 'approved_at', 'reviewed_at', 'reviewed_by', 'moderator_note', 'version', 'created_at', 'updated_at')
    readonly_fields = tuple(name for name in fields if name not in ('status', 'moderation_reason'))
    list_display = ('id', 'room', 'user', 'status', 'requested_at', 'reviewed_at')
    list_select_related = ('room', 'user')
    list_filter = ('status',)

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied
        moderate_membership(membership=obj, actor=request.user, status=obj.status, reason=form.cleaned_data['moderation_reason'])


class PostForm(ReasonForm):
    class Meta:
        model = RoomPost
        fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'status' in self.fields:
            self.fields['status'].choices = [(key, label) for key, label in RoomPost.Status.choices if key in ('visible', 'hidden')]


@admin.register(RoomPost)
class RoomPostAdmin(ScopedAdmin):
    permission = 'can_moderate_room_posts'
    form = PostForm
    fields = ('room', 'author', 'body', 'source_url', 'status', 'moderation_reason', 'moderator_note', 'moderated_by', 'moderated_at', 'version', 'created_at', 'updated_at')
    readonly_fields = tuple(name for name in fields if name not in ('status', 'moderation_reason'))
    list_display = ('id', 'room', 'author', 'status', 'created_at')
    list_filter = ('status',)
    list_select_related = ('room', 'author')

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status != 'archived')

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied
        moderate_post(post=obj, actor=request.user, status=obj.status, reason=form.cleaned_data['moderation_reason'])


class ReportForm(forms.ModelForm):
    class Meta:
        model = RoomReport
        fields = ['status', 'moderator_note']

    def clean_moderator_note(self):
        note = self.cleaned_data.get('moderator_note', '').strip()
        if not note:
            raise forms.ValidationError('Record a private outcome note for this decision.')
        return note


@admin.register(RoomReport)
class RoomReportAdmin(ScopedAdmin):
    permission = 'can_moderate_room_reports'
    form = ReportForm
    fields = ('room', 'post', 'reporter', 'category', 'details', 'status', 'moderator_note', 'moderated_by', 'moderated_at', 'created_at', 'updated_at')
    readonly_fields = tuple(name for name in fields if name not in ('status', 'moderator_note'))
    list_display = ('id', 'room', 'post', 'category', 'status', 'created_at')
    list_select_related = ('room', 'post')
    list_filter = ('status', 'category')

    def save_model(self, request, obj, form, change):
        if not change:
            raise PermissionDenied
        moderate_report(report=obj, actor=request.user, status=obj.status, moderator_note=form.cleaned_data['moderator_note'])


class AuditAdmin(ScopedAdmin):
    list_display = ('id', 'actor', 'created_at')
    list_select_related = ('actor',)

    def has_change_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)


@admin.register(RoomRevision)
class RoomRevisionAdmin(AuditAdmin):
    permission = 'can_manage_rooms'


@admin.register(RoomMembershipRevision)
class RoomMembershipRevisionAdmin(AuditAdmin):
    permission = 'can_moderate_room_memberships'


@admin.register(RoomPostRevision)
class RoomPostRevisionAdmin(AuditAdmin):
    permission = 'can_moderate_room_posts'


@admin.register(RoomReportRevision)
class RoomReportRevisionAdmin(AuditAdmin):
    permission = 'can_moderate_room_reports'
