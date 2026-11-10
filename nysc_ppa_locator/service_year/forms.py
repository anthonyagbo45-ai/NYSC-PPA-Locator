from django import forms

from .models import MemberPPA, ServiceProfile


class AssignmentConfirmationForm(forms.Form):
    confirm_assignment = forms.BooleanField(
        label='Use this place as my assigned PPA in my private tracker.',
        help_text='This records your choice. It does not verify this organisation or change your NYSC posting.',
    )


class ProgressForm(forms.ModelForm):
    class Meta:
        model = MemberPPA
        fields = ['status', 'personal_notes']
        labels = {'status': 'My progress', 'personal_notes': 'Private notes (optional)'}
        help_texts = {'personal_notes': 'Only your account can see these notes. Avoid passwords, identity documents and other people’s contact details.'}
        widgets = {'personal_notes': forms.Textarea(attrs={'rows': 5, 'maxlength': 2000})}


class EndAssignmentForm(forms.Form):
    confirm_end = forms.BooleanField(label='Remove this place from my current PPA and keep it in my private history.')


class ServiceProfileForm(forms.ModelForm):
    stage = forms.ChoiceField(choices=ServiceProfile.Stage.choices, required=False, label='Service stage (optional)')

    def clean_stage(self):
        return self.cleaned_data['stage'] or ServiceProfile.Stage.UNKNOWN

    class Meta:
        model = ServiceProfile
        fields = ['stage', 'batch']
        labels = {'stage': 'Service stage (optional)', 'batch': 'Batch and stream (optional)'}
