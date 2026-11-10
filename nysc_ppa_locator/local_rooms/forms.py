"""Small, explicit inputs for moderated local discussions."""
from django import forms
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from service_resources.models import CDSGroup
from .models import validate_source_url


def positive_id(value):
    value = str(value or '')
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 2147483647 else None


class RoomFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=120, label='Search room names or public descriptions')
    state = forms.ModelChoiceField(queryset=State.objects.order_by('name'), required=False, empty_label='All states')
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), required=False, empty_label='All LGAs in this state', label='Local government (LGA)')
    ppa = forms.TypedChoiceField(choices=[('', 'All PPAs in this LGA')], coerce=int, empty_value=None, required=False, label='PPA context')
    group = forms.TypedChoiceField(choices=[('', 'All published CDS groups in this LGA')], coerce=int, empty_value=None, required=False, label='CDS group context')
    visibility = forms.ChoiceField(choices=[('', 'All rooms'), ('public', 'Public discussions'), ('members', 'Approved members only')], required=False, label='Reading access')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        values = self.data if self.is_bound else self.initial
        state_id, lga_id = positive_id(values.get('state')), positive_id(values.get('lga'))
        self.fields['state'].widget.attrs['id'] = 'state-select'
        self.fields['lga'].widget.attrs['id'] = 'lga-select'
        self.fields['lga'].queryset = LGA.objects.filter(state_id=state_id).order_by('name') if state_id else LGA.objects.none()
        if state_id and lga_id:
            self.fields['ppa'].choices = [('', 'All PPAs in this LGA')] + list(PPA.objects.filter(state_id=state_id, lga_id=lga_id).order_by('name', 'pk').values_list('pk', 'name'))
            self.fields['group'].choices = [('', 'All published CDS groups in this LGA')] + list(CDSGroup.objects.filter(
                state_id=state_id, lga_id=lga_id, publication_status=CDSGroup.Publication.APPROVED, expires_at__gt=timezone.now(),
            ).order_by('title', 'pk').values_list('pk', 'title'))

    def clean(self):
        values = super().clean()
        state, lga = values.get('state'), values.get('lga')
        if lga and (not state or lga.state_id != state.pk):
            self.add_error('lga', 'Choose an LGA in the selected state.')
        return values


class JoinRoomForm(forms.Form):
    confirmed = forms.BooleanField(label='I have read the room guidelines and request moderator approval. My profile or CDS preference does not establish membership.')


class LeaveRoomForm(forms.Form):
    confirmed = forms.BooleanField(label='Leave this room or cancel my pending request, and turn off its notifications immediately.')


class RoomPreferenceForm(forms.Form):
    notifications_enabled = forms.BooleanField(required=False, label='Notify me in the app about new room discussions', help_text='Off by default. Requires approved room membership and community notifications enabled in My reminders. No email or text messages are sent.')


class RoomPostForm(forms.Form):
    body = forms.CharField(max_length=4000, label='Discussion text', widget=forms.Textarea(attrs={'rows': 6}))
    source_url = forms.URLField(max_length=500, required=False, validators=[validate_source_url], label='Public supporting reference (optional)', help_text='Only a public HTTP(S) page without credentials, personal contacts, private invitations or access tokens.')
    privacy_confirmed = forms.BooleanField(label='I have removed private contacts, state codes, identity documents and private locations, and have permission to share this text.')
    expected_version = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expected_version'].required = editing


class ArchivePostForm(forms.Form):
    confirmed = forms.BooleanField(label='Withdraw my post from this discussion. My private edit history will be retained while I have access to this room.')


class RoomReportForm(forms.Form):
    category = forms.ChoiceField(choices=[('abuse', 'Abuse or harassment'), ('spam', 'Spam'), ('unsafe', 'Unsafe or private information'), ('other', 'Other concern')])
    details = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 5}), label='Explain the concern privately', help_text='Moderators can review this report. Do not include passwords, identity documents, state codes or payment information.')
