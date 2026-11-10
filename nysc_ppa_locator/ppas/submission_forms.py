from django import forms
from django.utils import timezone

from locations.models import LGA, State
from .models import PPA, PPAClaim, PPASubmission


class PPASubmissionForm(forms.Form):
    name = forms.CharField(max_length=255, label='Organisation / PPA name',
                           help_text='Include a known branch or campus name when organisations share a name in the same LGA.')
    state = forms.ModelChoiceField(queryset=State.objects.order_by('name'))
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), label='Local government area')
    category = forms.ChoiceField(choices=PPA.CATEGORY_CHOICES)
    address = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={'rows': 2}),
                              help_text='A public organisation address from your own knowledge. No private home or live member location.')
    landmark = forms.CharField(max_length=500, required=False, label='Public landmark (optional)')
    entrance_instructions = forms.CharField(max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 2}))
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False,
                                help_text='Optional destination pin only. Leave blank if uncertain; never enter your live location.')
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    source_type = forms.ChoiceField(choices=[(PPAClaim.Source.MEMBER, 'My personal knowledge'),
                                            (PPAClaim.Source.ORGANISATION, 'Reported by the organisation (unconfirmed)')])
    source_url = forms.URLField(max_length=500, required=False, label='Public supporting page (optional)')
    observed_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), label='When did you know these details?')
    privacy_confirmed = forms.BooleanField(label='These are independently known public details. I have removed private contacts, state codes, posting documents and live locations.')
    action = forms.ChoiceField(choices=[('draft', 'Private draft'), ('submit', 'Submit for review')], widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['state'].widget.attrs['id'] = 'state-select'
        self.fields['lga'].widget.attrs['id'] = 'lga-select'
        state = self.data.get('state') if self.is_bound else self.initial.get('state')
        try:
            state_id = int(str(getattr(state, 'pk', state)))
        except (ValueError, TypeError):
            return
        if 0 < state_id <= 2147483647:
            self.fields['lga'].queryset = LGA.objects.filter(state_id=state_id).select_related('state').order_by('name')

    def clean_observed_on(self):
        value = self.cleaned_data['observed_on']
        if value > timezone.localdate():
            raise forms.ValidationError('The observation date cannot be in the future.')
        return value
