"""Typed, locality-scoped CDS and clearance contributions."""
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.utils import timezone

from locations.models import LGA, State
from service_year.models import ServiceProfile
from .models import CDSGroup, ResourceReport, ServiceNotice, validate_zone


def positive_id(value):
    value = str(value or '')
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 2147483647 else None


class GroupChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.title


class LocalityMixin:
    def configure_locality(self):
        values = self.data if self.is_bound else self.initial
        state_id, lga_id = positive_id(values.get('state')), positive_id(values.get('lga'))
        self.fields['lga'].queryset = LGA.objects.filter(state_id=state_id).select_related('state') if state_id else LGA.objects.none()
        self.fields['state'].widget.attrs['id'] = 'state-select'
        self.fields['lga'].widget.attrs['id'] = 'lga-select'
        if 'group' in self.fields:
            self.fields['group'].queryset = CDSGroup.objects.filter(
                publication_status=CDSGroup.Publication.APPROVED, expires_at__gt=timezone.now(),
                state_id=state_id, lga_id=lga_id,
            ).order_by('title', 'pk') if state_id and lga_id else CDSGroup.objects.none()

    def clean(self):
        data = super().clean()
        state, lga, group = data.get('state'), data.get('lga'), data.get('group')
        if lga and (not state or lga.state_id != state.pk):
            self.add_error('lga', 'Choose an LGA in the selected state.')
        if group and ((state and group.state_id != state.pk) or (lga and group.lga_id != lga.pk)):
            self.add_error('group', 'Choose a published CDS group in this state and LGA.')
        return data


class ResourceFilterForm(LocalityMixin, forms.Form):
    view = forms.ChoiceField(choices=[('notices', 'Notices and resources'), ('groups', 'CDS groups')], required=False, label='Browse')
    kind = forms.ChoiceField(choices=[('', 'CDS and clearance')] + list(ServiceNotice.Kind.choices), required=False, label='Topic')
    q = forms.CharField(max_length=120, required=False, label='Search titles or public context')
    state = forms.ModelChoiceField(queryset=State.objects.all().order_by('name'), required=False, empty_label='All states')
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), required=False, empty_label='All LGAs in this state', label='Local government')
    group = GroupChoiceField(queryset=CDSGroup.objects.none(), required=False, empty_label='All published groups in this LGA', label='CDS group')
    batch = forms.CharField(max_length=50, required=False, label='Batch or stream context (optional)')
    stage = forms.ChoiceField(choices=[('', 'Any service stage')] + list(ServiceProfile.Stage.choices), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure_locality()

    def clean(self):
        data = super().clean()
        if data.get('group') and data.get('kind') == ServiceNotice.Kind.CLEARANCE:
            self.add_error('group', 'A CDS group does not establish a clearance schedule. Clear this group filter.')
        return data


class ContributionForm(LocalityMixin, forms.Form):
    title = forms.CharField(max_length=200)
    state = forms.ModelChoiceField(queryset=State.objects.all().order_by('name'))
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), label='Local government')
    source_type = forms.ChoiceField(choices=[
        (CDSGroup.Source.MEMBER, 'My observed or reported experience'),
        (CDSGroup.Source.ORGANISATION, 'Reported by an organisation or representative (not independently confirmed)'),
    ])
    source_url = forms.URLField(max_length=500, required=False, label='Public supporting reference', help_text='Use an attributable public page, not private documents, personal contacts or a link containing an access token.')
    observed_on = forms.DateField(label='When did you observe or receive this information?', widget=forms.DateInput(attrs={'type': 'date'}))
    privacy_confirmed = forms.BooleanField(label='I have removed private contacts, state codes, identity documents and private member locations, and understand publication is not NYSC approval.')
    action = forms.ChoiceField(choices=[('submit', 'Submit for review'), ('draft', 'Save private draft')], widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure_locality()

    def clean_observed_on(self):
        value = self.cleaned_data['observed_on']
        if value > timezone.localdate():
            raise forms.ValidationError('The observation date cannot be in the future.')
        return value


class GroupForm(ContributionForm):
    description = forms.CharField(max_length=3000, widget=forms.Textarea(attrs={'rows': 5}), label='Public group context')
    venue_hint = forms.CharField(max_length=500, required=False, label='Public venue context (optional)', help_text='Use a public venue description, not a member residence or live location.')
    public_contact_url = forms.URLField(max_length=500, required=False, label='Public organisation information page (optional)', help_text='No personal phone numbers, private invitation links or documents.')


class LocalDateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'


def local_time_to_utc(value, zone_name):
    """Reject ambiguous/nonexistent wall times instead of silently shifting them."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
        if 'T' not in value or naive.tzinfo is not None or not 1900 <= naive.year <= 2100:
            raise ValueError
        zone = ZoneInfo(zone_name)
        first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise forms.ValidationError('This local time is ambiguous or does not exist because of a clock change. Choose another time, or enter the equivalent time in UTC.')
        if first.astimezone(datetime_timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            raise ValueError
        return first.astimezone(datetime_timezone.utc)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise forms.ValidationError('Enter a valid local date and time between 1900 and 2100.')


class NoticeForm(ContributionForm):
    kind = forms.ChoiceField(choices=ServiceNotice.Kind.choices, label='Notice topic')
    body = forms.CharField(max_length=3000, widget=forms.Textarea(attrs={'rows': 5}), label='Reported resource or update')
    venue_hint = forms.CharField(max_length=500, required=False, label='Public venue context (optional)', help_text='No exact private homes or member locations.')
    group = GroupChoiceField(queryset=CDSGroup.objects.none(), required=False, empty_label='No particular CDS group', label='CDS group (optional)', help_text='Choose only a published group in this selected LGA. Different groups can have different schedules.')
    timezone_name = forms.CharField(max_length=64, initial='Africa/Lagos', label='Time zone (IANA name)', validators=[validate_zone], widget=forms.TextInput(attrs={'list': 'resource-timezones'}), help_text='Nigeria uses Africa/Lagos. A date is a reported schedule, not approval to participate or clear.')
    event_local = forms.CharField(max_length=32, required=False, label='Reported start date and time (optional)', widget=LocalDateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'step': '60'}))
    ends_local = forms.CharField(max_length=32, required=False, label='Reported end date and time (optional)', widget=LocalDateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'step': '60'}))
    batch_label = forms.CharField(max_length=50, required=False, label='General batch or stream context (optional)', help_text='Do not enter a state code or claim this applies to every batch.')
    stage = forms.ChoiceField(choices=[('', 'Not limited to a service stage')] + list(ServiceProfile.Stage.choices), required=False)

    def clean(self):
        data = super().clean()
        zone = data.get('timezone_name')
        if zone:
            for form_name, model_name in [('event_local', 'event_at'), ('ends_local', 'ends_at')]:
                try:
                    data[model_name] = local_time_to_utc(data.get(form_name), zone)
                except forms.ValidationError as error:
                    self.add_error(form_name, error)
        start, end = data.get('event_at'), data.get('ends_at')
        if end and (not start or end < start):
            self.add_error('ends_local', 'The end must follow the reported start.')
        if data.get('kind') == ServiceNotice.Kind.CLEARANCE and data.get('group'):
            self.add_error('group', 'Clearance information cannot claim a CDS group schedule.')
        return data


class GroupSelectionForm(forms.Form):
    group = GroupChoiceField(queryset=CDSGroup.objects.none(), required=False, widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label='This is my private preference only. It does not verify membership, attendance or official NYSC approval.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        values = self.data if self.is_bound else self.initial
        selected = positive_id(values.get('group'))
        self.fields['group'].queryset = CDSGroup.objects.filter(pk=selected, publication_status=CDSGroup.Publication.APPROVED, expires_at__gt=timezone.now()) if selected else CDSGroup.objects.none()


class ResourceReportForm(forms.Form):
    category = forms.ChoiceField(choices=ResourceReport.Category.choices)
    details = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 4}), help_text='Describe the issue privately. Do not enter passwords, state codes, payment details or identity documents.')
    proposed_correction = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Suggested correction (optional)')
