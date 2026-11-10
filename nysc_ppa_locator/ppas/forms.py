from django import forms
from django.utils import timezone
from locations.models import State, LGA
from .models import PPA, PPAClaim, Review


class SearchForm(forms.Form):
    q = forms.CharField(max_length=200, required=False)
    state = forms.IntegerField(min_value=1, max_value=2147483647, required=False)
    lga = forms.IntegerField(min_value=1, max_value=2147483647, required=False)
    category = forms.ChoiceField(choices=[('', 'All')] + PPA.CATEGORY_CHOICES, required=False)


class ReviewForm(forms.ModelForm):
    comment = forms.CharField(max_length=4000, required=False, widget=forms.Textarea,
                              help_text='Optional. Share your experience without private names, contact details, state codes or posting documents.')
    rating = forms.IntegerField(min_value=1, max_value=5)
    topic_tags = forms.MultipleChoiceField(choices=Review.TOPIC_CHOICES, required=False,
                                          widget=forms.CheckboxSelectMultiple, label='Topics (optional)')
    context_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                                   label='When did you experience this? (optional)')

    class Meta:
        model = Review
        fields = ['rating', 'comment', 'topic_tags', 'context_date']


class PPAClaimForm(forms.Form):
    topic = forms.ChoiceField(choices=PPAClaim.Topic.choices)
    content = forms.CharField(max_length=2000, label='What should the next corps member know?', widget=forms.Textarea(attrs={'rows': 5}))
    source_type = forms.ChoiceField(choices=[
        (PPAClaim.Source.MEMBER, 'My personal experience'),
        (PPAClaim.Source.ORGANISATION, 'Information reported by the organisation (not independently confirmed)'),
    ])
    source_url = forms.URLField(max_length=500, required=False, label='Public supporting page (optional)',
                               help_text='Never include private documents, contacts or links containing access tokens.')
    observed_on = forms.DateField(label='When was this observed or reported?', widget=forms.DateInput(attrs={'type': 'date'}))
    privacy_confirmed = forms.BooleanField(label='I have removed private contacts, state codes, personal documents and other people’s sensitive information.')
    action = forms.ChoiceField(choices=[('draft', 'Save private draft'), ('submit', 'Submit for review')], widget=forms.HiddenInput)

    def clean_observed_on(self):
        value = self.cleaned_data['observed_on']
        if value > timezone.localdate():
            raise forms.ValidationError('The observation date cannot be in the future.')
        return value


class LivePlaceForm(forms.Form):
    name = forms.CharField(max_length=255)
    address = forms.CharField(max_length=2000)
    state = forms.ModelChoiceField(queryset=State.objects.all())
    lga = forms.ModelChoiceField(queryset=LGA.objects.none())
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    place_id = forms.RegexField(r'^[A-Za-z0-9_-]+$', max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state = self.data.get('state', '')
        if state.isascii() and state.isdigit() and len(state) <= 10:
            self.fields['lga'].queryset = LGA.objects.filter(state_id=int(state))


class CommunityPlaceForm(forms.Form):
    """Facts supplied independently by the contributor, not copied from Google."""
    name = forms.CharField(max_length=255, label='PPA name from your posting letter or personal knowledge')
    address = forms.CharField(max_length=2000, required=False, label='Address you know independently', widget=forms.Textarea(attrs={'rows': 2}))
    landmark = forms.CharField(max_length=500, required=False)
    entrance_instructions = forms.CharField(max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 2}))
    state = forms.ModelChoiceField(queryset=State.objects.all())
    lga = forms.ModelChoiceField(queryset=LGA.objects.none())
    community_details_confirmed = forms.BooleanField(label='These details are from my posting information or personal knowledge, and this is the correct LGA.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state = self.data.get('state', '')
        if str(state).isascii() and str(state).isdigit() and len(str(state)) <= 10:
            self.fields['lga'].queryset = LGA.objects.filter(state_id=int(state))
