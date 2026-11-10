"""Typed input for voluntary private reminders; no account or source grants."""
from django import forms

from .models import PersonalEvent, REMINDER_CHOICES
from .services import make_local_datetime


class EventFilterForm(forms.Form):
    period = forms.ChoiceField(choices=[('upcoming', 'Upcoming'), ('past', 'Past'), ('all', 'All dates')], required=False)
    status = forms.ChoiceField(choices=[('', 'All statuses')] + list(PersonalEvent.Status.choices), required=False)


class EventForm(forms.Form):
    title = forms.CharField(max_length=200, label='What do you want to remember?')
    kind = forms.ChoiceField(choices=PersonalEvent.Kind.choices, label='Category')
    event_date = forms.DateField(label='Date', widget=forms.DateInput(attrs={'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31'}))
    event_time = forms.TimeField(label='Time', widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'))
    timezone_name = forms.CharField(max_length=64, initial='Africa/Lagos', label='Time zone', help_text='Use Africa/Lagos for Nigerian local time, or an IANA name such as UTC or Europe/London.', widget=forms.TextInput(attrs={'list': 'reminder-timezones', 'autocomplete': 'off'}))
    end_date = forms.DateField(required=False, label='End date (optional)', widget=forms.DateInput(attrs={'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31'}))
    end_time = forms.TimeField(required=False, label='End time (optional)', widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'))
    note = forms.CharField(max_length=1000, required=False, label='Private note (optional)', help_text='Avoid passwords, bank details, personal identifiers and other people’s private information.', widget=forms.Textarea(attrs={'rows': 3}))
    status = forms.ChoiceField(choices=PersonalEvent.Status.choices, initial=PersonalEvent.Status.ACTIVE, label='My task status')
    remind_before = forms.TypedChoiceField(choices=REMINDER_CHOICES, coerce=int, initial=60, label='When should I be reminded?')
    notifications_enabled = forms.BooleanField(required=False, label='Enable an in-app reminder for this event.', help_text='Your personal-reminder preference must also be enabled. No email, text message or browser push is sent.')

    def clean(self):
        data = super().clean()
        if data.get('event_date') and not 1900 <= data['event_date'].year <= 2100:
            self.add_error('event_date', 'Choose a date between 1900 and 2100.')
        if data.get('end_date') and not 1900 <= data['end_date'].year <= 2100:
            self.add_error('end_date', 'Choose a date between 1900 and 2100.')
        if bool(data.get('end_date')) != bool(data.get('end_time')):
            self.add_error('end_date', 'Supply both the end date and end time, or leave both blank.')
        if data.get('event_date') and data.get('event_time') and data.get('timezone_name'):
            try:
                data['starts_at'] = make_local_datetime(data['event_date'], data['event_time'], data['timezone_name'])
            except forms.ValidationError as error:
                self.add_error('event_time', error)
        data['ends_at'] = None
        if data.get('end_date') and data.get('end_time') and data.get('timezone_name'):
            try:
                data['ends_at'] = make_local_datetime(data['end_date'], data['end_time'], data['timezone_name'])
            except forms.ValidationError as error:
                self.add_error('end_time', error)
        if data.get('starts_at') and data.get('ends_at') and data['ends_at'] < data['starts_at']:
            self.add_error('end_date', 'The end must be at or after the starting time.')
        return data


class PreferenceForm(forms.Form):
    personal_reminders = forms.BooleanField(required=False, label='Show in-app reminders for my own events.', help_text='Only events you separately enable are included.')
    notice_reminders = forms.BooleanField(required=False, label='Show in-app reminders for confirmed notices I explicitly follow.', help_text='Unconfirmed community reports never create notice reminders. New or changed notices require your choice.')


class ConfirmEventDeleteForm(forms.Form):
    confirm_delete = forms.BooleanField(label='Delete this private event and its reminders.')


class NoticeSubscriptionForm(forms.Form):
    remind_before = forms.TypedChoiceField(choices=REMINDER_CHOICES, coerce=int, initial=60, label='When should I be reminded?')
    consent = forms.BooleanField(label='Follow this confirmed notice with an in-app reminder. I will still confirm arrangements with my designated NYSC official.')


class ConfirmUnsubscribeForm(forms.Form):
    confirm = forms.BooleanField(label='Stop reminders for this followed notice.')


class InboxFilterForm(forms.Form):
    status = forms.ChoiceField(choices=[('all', 'All current reminders'), ('unread', 'Unread'), ('read', 'Read')], required=False)
