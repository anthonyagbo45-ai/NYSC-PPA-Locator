"""Explicit, bounded input for community questions and private controls."""
from django import forms
from django.utils import timezone

from locations.models import LGA, State
from ppas.models import PPA
from service_resources.models import CDSGroup
from .models import CommunityReport, Question


def positive_id(value):
    value = str(value or '')
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 2147483647 else None


class PublicPPAChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


class PublicGroupChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.title


class LocalityMixin:
    def configure_locality(self):
        values = self.data if self.is_bound else self.initial
        state_id, lga_id = positive_id(values.get('state')), positive_id(values.get('lga'))
        self.fields['state'].widget.attrs['id'] = 'state-select'
        self.fields['lga'].widget.attrs['id'] = 'lga-select'
        self.fields['lga'].queryset = LGA.objects.filter(state_id=state_id).select_related('state').order_by('name') if state_id else LGA.objects.none()
        self.fields['ppa'].queryset = PPA.objects.filter(state_id=state_id, lga_id=lga_id).exclude(status__in=['closed', 'disputed']).exclude(location_status='mismatch').only('id', 'name', 'state_id', 'lga_id').order_by('name') if state_id and lga_id else PPA.objects.none()
        self.fields['group'].queryset = CDSGroup.objects.filter(state_id=state_id, lga_id=lga_id, publication_status='approved', expires_at__gt=timezone.now()).only('id', 'title', 'state_id', 'lga_id').order_by('title') if state_id and lga_id else CDSGroup.objects.none()

    def clean(self):
        data = super().clean()
        state, lga = data.get('state'), data.get('lga')
        if lga and (not state or lga.state_id != state.pk):
            self.add_error('lga', 'Choose an LGA in the selected state.')
        for name in ('ppa', 'group'):
            obj = data.get(name)
            if obj and (not state or not lga or obj.state_id != state.pk or obj.lga_id != lga.pk):
                self.add_error(name, 'Choose an entry in the selected state and LGA.')
        return data


class QuestionFilterForm(LocalityMixin, forms.Form):
    q = forms.CharField(max_length=120, required=False, label='Search questions')
    category = forms.ChoiceField(choices=[('', 'All topics')] + list(Question.Category.choices), required=False)
    state = forms.ModelChoiceField(queryset=State.objects.order_by('name'), required=False, empty_label='All states')
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), required=False, empty_label='All LGAs in this state')
    ppa = PublicPPAChoiceField(queryset=PPA.objects.none(), required=False, empty_label='All PPAs in this LGA', label='PPA')
    group = PublicGroupChoiceField(queryset=CDSGroup.objects.none(), required=False, empty_label='All current CDS groups in this LGA', label='CDS group')
    sort = forms.ChoiceField(choices=[('newest', 'Newest'), ('helpful', 'Most helpful answers'), ('unanswered', 'Unanswered'), ('resolved', 'Resolved')], required=False, initial='newest', label='Show')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure_locality()


class QuestionForm(LocalityMixin, forms.Form):
    title = forms.CharField(max_length=200, label='Your question')
    body = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 7}), label='Public context')
    category = forms.ChoiceField(choices=Question.Category.choices, label='Topic')
    state = forms.ModelChoiceField(queryset=State.objects.order_by('name'))
    lga = forms.ModelChoiceField(queryset=LGA.objects.none(), label='LGA')
    ppa = PublicPPAChoiceField(queryset=PPA.objects.none(), required=False, empty_label='No particular PPA', label='PPA (optional)')
    group = PublicGroupChoiceField(queryset=CDSGroup.objects.none(), required=False, empty_label='No particular CDS group', label='CDS group (optional)')
    privacy_confirmed = forms.BooleanField(label='I understand this is public and have removed private contacts, state codes, identity documents, private addresses and sensitive personal information.')
    version = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure_locality()


class AnswerForm(forms.Form):
    body = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 7}), label='Your public answer')
    source_url = forms.URLField(max_length=500, required=False, label='Public supporting reference (optional)', help_text='Use a public page without private contacts, invitation tokens or personal documents. A citation does not confer official status.')
    privacy_confirmed = forms.BooleanField(label='I have permission to share this answer and have removed private contacts, state codes, identity documents, private addresses and sensitive personal information.')
    version = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)


class ReportForm(forms.Form):
    category = forms.ChoiceField(choices=CommunityReport.Category.choices)
    details = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 5}), label='What is wrong?')
    private_evidence = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Additional private context (optional)', help_text='You and authorised moderators can review this. Do not provide passwords, identity documents or payment details.')


class FollowForm(forms.Form):
    action = forms.ChoiceField(choices=[('follow', 'Follow'), ('unfollow', 'Unfollow')])
    enabled = forms.BooleanField(required=False)


class BooleanActionForm(forms.Form):
    value = forms.ChoiceField(choices=[('1', 'Yes'), ('0', 'No')])


class BlockForm(forms.Form):
    target_type = forms.ChoiceField(choices=[('question', 'Question'), ('answer', 'Answer'), ('room_post', 'Room post')])
    target_id = forms.IntegerField(min_value=1, max_value=2147483647)
