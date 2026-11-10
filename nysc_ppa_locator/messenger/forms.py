"""Explicit inputs for private conversations; identities come from context."""
from uuid import uuid4

from django import forms

from .models import MessageReport


class PreferenceForm(forms.Form):
    allow_requests = forms.BooleanField(
        required=False,
        label='Allow private message requests from community discussions',
        help_text='Off by default. Turning this off closes pending requests; accepted chats stay open until you close or block them.',
    )
    allow_photos = forms.BooleanField(
        required=False, label='Allow photos in accepted private conversations',
        help_text='Off by default. Both members must enable this. Turning it off stops new photos; earlier photos remain in conversation history.',
    )


class MessageForm(forms.Form):
    body = forms.CharField(
        max_length=2000, label='Message',
        widget=forms.Textarea(attrs={'rows': 4, 'autocomplete': 'off'}),
        help_text='Up to 2,000 characters. Keep passwords, identity documents and exact private locations out of messages.',
    )
    client_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)


class RespondForm(forms.Form):
    action = forms.ChoiceField(choices=[('accept', 'Accept'), ('decline', 'Decline')])


class ReadForm(forms.Form):
    message_id = forms.IntegerField(min_value=1, max_value=9223372036854775807)


class ReportForm(forms.Form):
    category = forms.ChoiceField(choices=MessageReport.Category.choices)
    details = forms.CharField(
        max_length=2000, label='Explain your concern',
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='Only this selected message and your explanation will be shared with authorised safety reviewers. Do not add passwords or identity documents.',
    )
