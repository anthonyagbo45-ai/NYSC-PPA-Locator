from django import forms
from django.utils import timezone

from .models import Expense, MonthlyBudget, validate_supported_date


class MonthField(forms.DateField):
    def __init__(self, **kwargs):
        super().__init__(
            input_formats=['%Y-%m'],
            widget=forms.DateInput(format='%Y-%m', attrs={'type': 'month', 'min': '1900-01', 'max': '2100-12'}),
            **kwargs,
        )

    def validate(self, value):
        super().validate(value)
        if value:
            validate_supported_date(value)
            if value.day != 1:
                raise forms.ValidationError('Choose a calendar month.')


class MonthFilterForm(forms.Form):
    month = MonthField(label='Month')


class BudgetForm(forms.ModelForm):
    month = MonthField(label='Budget month')

    class Meta:
        model = MonthlyBudget
        fields = ['month', 'limit']
        labels = {'limit': 'My spending limit (NGN)'}
        help_texts = {'limit': 'Optional planning limit in Nigerian naira. A zero limit is allowed. No income or bank details are needed.'}
        widgets = {'limit': forms.NumberInput(attrs={'inputmode': 'decimal', 'min': '0', 'max': '9999999999.99', 'step': '0.01'})}


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ['amount', 'category', 'spent_on', 'note']
        labels = {'amount': 'Amount spent (NGN)', 'spent_on': 'Date spent', 'note': 'Private note (optional)'}
        help_texts = {'note': 'Avoid bank details, passwords, identity documents or other people’s personal information.'}
        widgets = {
            'amount': forms.NumberInput(attrs={'inputmode': 'decimal', 'min': '0.01', 'max': '9999999999.99', 'step': '0.01'}),
            'spent_on': forms.DateInput(attrs={'type': 'date', 'min': '1900-01-01'}),
            'note': forms.Textarea(attrs={'rows': 3, 'maxlength': 500}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['spent_on'].widget.attrs['max'] = timezone.localdate().isoformat()


class DeleteExpenseForm(forms.Form):
    confirm_delete = forms.BooleanField(label='Delete this expense from my private records.')


class DeleteBudgetForm(forms.Form):
    month = MonthField(label='Budget month')
    confirm_delete = forms.BooleanField(label='Remove this month’s spending limit. Keep my expenses.')
