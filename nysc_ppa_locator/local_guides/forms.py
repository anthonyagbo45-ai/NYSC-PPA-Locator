"""Bounded, typed input for public local guidance and private contributions."""
from django import forms
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from .models import FareDetails, HousingDetails, Listing, ListingReport, ServiceDetails


def _positive_identifier(value):
    value = str(value or '')
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 2147483647 else None


class LocalityFieldsMixin:
    def configure_locality(self):
        values = self.data if self.is_bound else self.initial
        state_id = _positive_identifier(values.get('state'))
        self.fields['lga'].queryset = LGA.objects.filter(state_id=state_id).select_related('state') if state_id else LGA.objects.none()
        self.fields['state'].widget.attrs['id'] = 'state-select'
        self.fields['lga'].widget.attrs['id'] = 'lga-select'
        if 'ppa' in self.fields:
            ppa_id = _positive_identifier(values.get('ppa'))
            self.fields['ppa'].queryset = PPA.objects.filter(pk=ppa_id).select_related('state', 'lga') if ppa_id else PPA.objects.none()

    def clean(self):
        data = super().clean()
        ppa, state, lga = data.get('ppa'), data.get('state'), data.get('lga')
        if lga and state and lga.state_id != state.pk:
            self.add_error('lga', 'Choose an LGA in the selected state.')
        if ppa and ((state and ppa.state_id != state.pk) or (lga and ppa.lga_id != lga.pk)):
            self.add_error('ppa', 'This PPA belongs to a different selected state or LGA. Open its local guide link again or clear the PPA filter.')
        return data


class DirectoryFilterForm(LocalityFieldsMixin, forms.Form):
    kind = forms.ChoiceField(choices=[('', 'All local guidance')] + list(Listing.Kind.choices), required=False, label='What do you need?')
    q = forms.CharField(max_length=120, required=False, label='Search names or areas')
    state = forms.ModelChoiceField(queryset=State.objects.all().order_by('name'), required=False, empty_label='All states')
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), required=False, empty_label='All LGAs in this state')
    ppa = forms.ModelChoiceField(queryset=PPA.objects.none(), required=False, widget=forms.HiddenInput)
    max_rent = forms.DecimalField(min_value=0, max_value=9999999999, max_digits=12, decimal_places=2, required=False, label='Maximum reported rent (₦)', widget=forms.NumberInput(attrs={'min': '0', 'step': '0.01'}))
    price_period = forms.ChoiceField(choices=[('', 'Any rent period')] + list(HousingDetails.PricePeriod.choices), required=False, label='Rent period')
    room_type = forms.ChoiceField(choices=[('', 'Any room type')] + list(HousingDetails.RoomType.choices), required=False)
    amenity = forms.ChoiceField(choices=[('', 'Any amenity')] + list(HousingDetails.AMENITY_CHOICES), required=False)
    mode = forms.ChoiceField(choices=[('', 'Any transport mode')] + list(FareDetails.Mode.choices), required=False, label='Transport mode')
    category = forms.ChoiceField(choices=[('', 'Any service category')] + list(ServiceDetails.Category.choices), required=False, label='Service category')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure_locality()

    def clean(self):
        data = super().clean()
        if data.get('lga') and not data.get('state'):
            self.add_error('state', 'Choose the state for this LGA.')
        if data.get('max_rent') is not None and not data.get('price_period'):
            self.add_error('price_period', 'Choose monthly or yearly so rent estimates can be compared fairly.')
        return data


class ListingForm(LocalityFieldsMixin, forms.Form):
    title = forms.CharField(max_length=200)
    area = forms.CharField(max_length=200, label='General area or neighbourhood', help_text='Use a broad area, not an exact private home address or live location.')
    summary = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 4}), label='What should the next corps member know?')
    state = forms.ModelChoiceField(queryset=State.objects.all().order_by('name'))
    lga = forms.ModelChoiceField(queryset=LGA.objects.none())
    ppa = forms.ModelChoiceField(queryset=PPA.objects.none(), required=False, widget=forms.HiddenInput)
    source_type = forms.ChoiceField(choices=[(Listing.Source.MEMBER, 'My own experience'), (Listing.Source.OWNER, 'Reported by the owner or authorised contact')])
    source_url = forms.URLField(required=False, max_length=500, label='Public supporting page (optional)', help_text='Only a public reference suitable for everyone. Do not include private home addresses, personal contacts or links containing access tokens.')
    observed_on = forms.DateField(label='When did you observe or check this?', widget=forms.DateInput(attrs={'type': 'date'}))
    privacy_confirmed = forms.BooleanField(label='I have permission to share this information and have removed private addresses, personal contacts, state codes and other sensitive details.')
    action = forms.ChoiceField(choices=[('submit', 'Submit for review'), ('draft', 'Save private draft')], widget=forms.HiddenInput)

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.configure_locality()
        if kind == Listing.Kind.HOUSING:
            self.fields.update({
                'reported_rent': forms.DecimalField(required=False, min_value=0, max_value=9999999999, max_digits=12, decimal_places=2, label='Reported rent (₦, optional)', widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0'})),
                'price_period': forms.ChoiceField(choices=HousingDetails.PricePeriod.choices, label='Rent period'),
                'room_type': forms.ChoiceField(choices=HousingDetails.RoomType.choices),
                'amenities': forms.MultipleChoiceField(choices=HousingDetails.AMENITY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple),
                'availability': forms.ChoiceField(choices=HousingDetails.Availability.choices),
                'contributor_role': forms.ChoiceField(choices=HousingDetails.ContributorRole.choices, label='How do you know this housing?'),
                'authorization_attested': forms.BooleanField(label='I am authorised to share this housing information.'),
                'authorization_basis': forms.CharField(max_length=1500, required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Private authorisation explanation', help_text='Required for owners, agents and host organisation contacts. Briefly explain your authority or a reference moderators can check. Do not upload or enter identity documents, access tokens or private contacts. This explanation is not published.'),
            })
        elif kind == Listing.Kind.FARE:
            self.fields.update({
                'origin_area': forms.CharField(max_length=200, label='Starting area or public transport stop'),
                'destination_area': forms.CharField(max_length=200, label='Destination area or public transport stop'),
                'mode': forms.ChoiceField(choices=FareDetails.Mode.choices),
                'amount_low': forms.DecimalField(min_value=0, max_value=9999999999, max_digits=12, decimal_places=2, label='Lower estimate per person (₦)', widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0'})),
                'amount_high': forms.DecimalField(min_value=0, max_value=9999999999, max_digits=12, decimal_places=2, label='Upper estimate per person (₦)', widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0'})),
            })
        elif kind == Listing.Kind.SERVICE:
            self.fields.update({
                'category': forms.ChoiceField(choices=ServiceDetails.Category.choices),
                'opening_hours': forms.CharField(max_length=200, required=False, label='Reported opening hours (optional)'),
                'public_business_address': forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 2}), label='Public business address (optional)', help_text='Only a legitimate public business premise. Leave out private homes and personal locations.'),
            })

    def clean_observed_on(self):
        value = self.cleaned_data['observed_on']
        if value > timezone.localdate():
            raise forms.ValidationError('The observation date cannot be in the future.')
        return value

    def clean(self):
        data = super().clean()
        if self.kind == Listing.Kind.FARE and data.get('amount_low') is not None and data.get('amount_high') is not None and data['amount_high'] < data['amount_low']:
            self.add_error('amount_high', 'The upper estimate must be at least the lower estimate.')
        if self.kind == Listing.Kind.HOUSING and data.get('contributor_role') in ('owner', 'agent', 'ppa') and not data.get('authorization_basis', '').strip():
            self.add_error('authorization_basis', 'Explain your authority to share this housing so a moderator can review it privately.')
        return data


class ReconfirmationForm(forms.Form):
    observed_on = forms.DateField(label='When did you check this information again?', widget=forms.DateInput(attrs={'type': 'date'}))
    confirmed = forms.BooleanField(label='I checked this information again and understand it will return for moderation without inheriting an old verification badge.')

    def __init__(self, *args, housing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if housing:
            self.fields['available'] = forms.ChoiceField(choices=[('yes', 'Available when I checked'), ('no', 'No longer available')], label='Current availability')

    def clean_observed_on(self):
        value = self.cleaned_data['observed_on']
        if value > timezone.localdate():
            raise forms.ValidationError('The checked date cannot be in the future.')
        return value


class ListingReportForm(forms.Form):
    category = forms.ChoiceField(choices=ListingReport.Category.choices)
    details = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 4}), help_text='Explain the problem privately. Do not include passwords, state codes or payment details.')
    proposed_correction = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Suggested correction (optional)')
