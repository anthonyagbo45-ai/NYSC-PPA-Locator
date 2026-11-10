"""Voluntary personal records; intentionally not registered in administration."""
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


MAX_AMOUNT = Decimal('9999999999.99')
FIRST_DATE = date(1900, 1, 1)
LAST_DATE = date(2100, 12, 31)


def validate_supported_date(value):
    if not FIRST_DATE <= value <= LAST_DATE:
        raise ValidationError('Choose a date between 1900 and 2100.')


class MonthlyBudget(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='monthly_budgets')
    month = models.DateField(validators=[validate_supported_date])
    limit = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0')), MaxValueValidator(MAX_AMOUNT)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-month']
        constraints = [
            models.UniqueConstraint(fields=['user', 'month'], name='unique_member_budget_month'),
            models.CheckConstraint(condition=models.Q(month__day=1), name='budget_month_is_first_day'),
            models.CheckConstraint(condition=models.Q(month__gte=FIRST_DATE, month__lte=date(2100, 12, 1)), name='budget_month_supported'),
            models.CheckConstraint(condition=models.Q(limit__gte=0, limit__lte=MAX_AMOUNT), name='budget_limit_valid_range'),
        ]

    def clean(self):
        super().clean()
        if self.month and self.month.day != 1:
            raise ValidationError({'month': 'Choose a calendar month.'})

    def __str__(self):
        return f'Private monthly budget #{self.pk or "new"}'


class Expense(models.Model):
    class Category(models.TextChoices):
        TRANSPORT = 'transport', 'Transport'
        RENT = 'rent', 'Rent and housing'
        FOOD = 'food', 'Food'
        DATA = 'data', 'Phone and data'
        ESSENTIALS = 'essentials', 'Other essentials'
        OTHER = 'other', 'Other'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='private_expenses')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(MAX_AMOUNT)])
    category = models.CharField(max_length=16, choices=Category.choices)
    spent_on = models.DateField(default=timezone.localdate, validators=[validate_supported_date])
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-spent_on', '-pk']
        indexes = [models.Index(fields=['user', '-spent_on', '-id'], name='expense_member_date_idx')]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=Decimal('0.01'), amount__lte=MAX_AMOUNT), name='expense_amount_valid_range'),
            models.CheckConstraint(condition=models.Q(category__in=['transport', 'rent', 'food', 'data', 'essentials', 'other']), name='expense_category_valid'),
            models.CheckConstraint(condition=models.Q(spent_on__gte=FIRST_DATE, spent_on__lte=LAST_DATE), name='expense_date_supported'),
        ]

    def clean(self):
        super().clean()
        if self.spent_on and self.spent_on > timezone.localdate():
            raise ValidationError({'spent_on': 'Record spending that has already happened. Use your monthly budget for planning.'})

    def __str__(self):
        return f'Private expense #{self.pk or "new"}'
