from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from locations.models import LGA

User = get_user_model()


class ProfileForm(forms.ModelForm):
    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ['full_name', 'display_name', 'email', 'state_code', 'state_posted', 'lga_posted']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].help_text = 'Used privately for verification and password recovery. Changing it requires your current password.' if self.instance.pk else 'Receive your verification link and password recovery emails. Your email is never published.'
        self.fields['display_name'].label = 'Public name (optional)'
        self.fields['email'].widget.attrs['autocomplete'] = 'email'
        if self.instance.pk:
            self.fields['current_password'] = forms.CharField(required=False, max_length=128, widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}), help_text='Required when you change your state code, or change or remove your email address.')
        self.fields['lga_posted'].queryset = LGA.objects.none()
        state_id = self.data.get('state_posted') if self.is_bound else self.instance.state_posted_id
        try:
            if state_id and 0 < int(state_id) <= 2147483647:
                self.fields['lga_posted'].queryset = LGA.objects.filter(state_id=int(state_id))
        except (ValueError, TypeError):
            pass

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('An account already uses this email address.')
        if self.instance.pk and email == (self.instance.email or '').strip().lower():
            # Do not silently rewrite historical addresses on unrelated edits.
            return self.instance.email
        return email or None

    def clean(self):
        data = super().clean()
        sensitive_change = ('email' in data and data['email'] != self.instance.email) or ('state_code' in data and data['state_code'] != self.instance.state_code)
        if self.instance.pk and sensitive_change:
            if not self.instance.check_password(data.get('current_password', '')):
                self.add_error('current_password', 'Confirm your current password to change your email address or state code.')
        state, lga = data.get('state_posted'), data.get('lga_posted')
        if lga and (not state or lga.state_id != state.pk):
            self.add_error('lga_posted', 'Choose an LGA belonging to the selected state.')
        return data


class RegistrationForm(ProfileForm):
    email = forms.EmailField(required=True)

    password = forms.CharField(max_length=128, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}), help_text=password_validation.password_validators_help_text_html())
    confirm_password = forms.CharField(max_length=128, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    class Meta(ProfileForm.Meta):
        fields = ['full_name', 'display_name', 'email', 'state_code', 'state_posted', 'lga_posted']

    def clean(self):
        data = super().clean()
        if data.get('password') != data.get('confirm_password'):
            self.add_error('confirm_password', 'Passwords do not match.')
        user = User(**{key: data[key] for key in ('full_name', 'email', 'state_code') if key in data})
        try:
            password_validation.validate_password(data.get('password', ''), user)
        except ValidationError as exc:
            self.add_error('password', exc)
        return data
