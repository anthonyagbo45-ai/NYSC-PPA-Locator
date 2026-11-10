from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.forms import BaseUserCreationForm
from django.contrib.auth.models import Group
from django import forms
from django.core.exceptions import PermissionDenied
from .models import CorpMember, ContributorVerification


class AccountLocationValidation:
    def clean(self):
        data = super().clean()
        state, lga = data.get('state_posted'), data.get('lga_posted')
        if lga and (not state or lga.state_id != state.pk):
            self.add_error('lga_posted', 'Choose an LGA belonging to the selected state.')
        return data


class AccountAdminForm(AccountLocationValidation, forms.ModelForm):
    class Meta:
        model = CorpMember
        exclude = ['password']

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if self.instance.pk and (email or '').strip().lower() == (self.instance.email or '').strip().lower():
            return self.instance.email
        return CorpMember.objects.normalize_email(email)


class AccountCreationForm(AccountLocationValidation, BaseUserCreationForm):
    class Meta:
        model = CorpMember
        fields = ['state_code', 'full_name', 'email', 'display_name', 'state_posted', 'lga_posted', 'is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions']

    def clean_email(self):
        return CorpMember.objects.normalize_email(self.cleaned_data.get('email'))


@admin.register(CorpMember)
class CorpMemberAdmin(admin.ModelAdmin):
    form = AccountAdminForm
    list_display = ['id', 'display_name', 'email_verified', 'is_active', 'is_staff']
    list_filter = ['is_active', 'is_staff', 'email_verified']
    search_fields = ['=id', 'state_code', 'email', 'display_name']
    ordering = ['id']
    filter_horizontal = ['groups', 'user_permissions']
    readonly_fields = ['id', 'email_verified', 'last_login', 'date_joined']
    fields = ['id', 'state_code', 'full_name', 'email', 'display_name', 'state_posted', 'lga_posted', 'email_verified', 'is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions', 'last_login', 'date_joined']

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs['form'] = AccountCreationForm
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return [*AccountCreationForm.Meta.fields, 'password1', 'password2']
        return super().get_fields(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if not request.user.is_superuser:
            return self.fields
        return super().get_readonly_fields(request, obj)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm('accounts.view_corpmember')

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            raise PermissionDenied
        if not change:
            return super().save_model(request, obj, form, change)
        # ModelForms hold a snapshot. Updating only actual changed scalar
        # fields protects concurrent password resets and account operations.
        # Django saves groups/user_permissions separately through save_related.
        changed_fields = {
            field.name for field in obj._meta.concrete_fields
            if not field.primary_key and field.name != 'password'
            and field.name in form.changed_data
            and field.value_from_object(obj) != form.initial.get(field.name)
        }
        obj.save(update_fields=changed_fields)


class SuperuserOnlyAdmin:
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# A delegated change_group permission must never become a route to grant oneself
# broader permissions. Only existing superusers manage role definitions.
if admin.site.is_registered(Group):
    admin.site.unregister(Group)


@admin.register(Group)
class RestrictedGroupAdmin(SuperuserOnlyAdmin, GroupAdmin):
    pass


@admin.register(ContributorVerification)
class ContributorVerificationAdmin(SuperuserOnlyAdmin, admin.ModelAdmin):
    list_display = ['id', 'member_id', 'outcome', 'reviewed_by_id', 'created_at']
    list_filter = ['outcome']
    fields = ['member', 'outcome', 'evidence_summary', 'reviewed_by', 'created_at']
    readonly_fields = ['reviewed_by', 'created_at']
    raw_id_fields = ['member']

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return self.fields if obj else self.readonly_fields

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser or change:
            raise PermissionDenied
        obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)
