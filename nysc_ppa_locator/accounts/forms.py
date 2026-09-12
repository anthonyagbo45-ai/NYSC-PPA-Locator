from django import forms
from django.contrib.auth import get_user_model
from locations.models import State, LGA

User = get_user_model()

class RegistrationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(attrs={'placeholder': 'Create a password'}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={'placeholder': 'Confirm your password'}))
    
    class Meta:
        model = User
        fields = ['full_name', 'email', 'state_code', 'state_posted', 'lga_posted']
        widgets = {
            'full_name': forms.TextInput(attrs={'placeholder': 'Enter your full name'}),
            'email': forms.EmailInput(attrs={'placeholder': 'Enter your email address'}),
            'state_code': forms.TextInput(attrs={'placeholder': 'e.g. LA/25A/1234'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lga_posted'].queryset = LGA.objects.none()

        if 'state_posted' in self.data:
            try:
                state_id = int(self.data.get('state_posted'))
                self.fields['lga_posted'].queryset = LGA.objects.filter(state_id=state_id).order_by('name')
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.state_posted:
            self.fields['lga_posted'].queryset = self.instance.state_posted.lgas.order_by('name')

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match.")
            
        return cleaned_data