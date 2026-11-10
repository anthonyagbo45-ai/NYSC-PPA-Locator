"""Owner-scoped writes and bounded monthly aggregates, using decimal naira."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404

from .models import Expense, MonthlyBudget, validate_supported_date


def month_start(value):
    validate_supported_date(value)
    return value.replace(day=1)


def next_month(value):
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def _require_member(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied('Sign in to use your private budget.')


def _lock_member(user):
    _require_member(user)
    return get_user_model().objects.select_for_update().get(pk=user.pk, is_active=True)


@transaction.atomic
def save_budget(user, *, month, limit):
    member = _lock_member(user)
    validate_supported_date(month)
    if month.day != 1:
        raise ValidationError({'month': 'Choose a calendar month.'})
    budget = MonthlyBudget.objects.select_for_update().filter(user=member, month=month).first()
    if budget is None:
        budget = MonthlyBudget(user=member, month=month)
    budget.limit = limit
    budget.full_clean()
    budget.save()
    return budget


@transaction.atomic
def remove_budget(user, *, month):
    _lock_member(user)
    validate_supported_date(month)
    if month.day != 1:
        raise ValidationError({'month': 'Choose a calendar month.'})
    MonthlyBudget.objects.filter(user=user, month=month).delete()


@transaction.atomic
def save_expense(user, *, cleaned_data, expense_id=None):
    member = _lock_member(user)
    if expense_id is None:
        expense = Expense(user=member)
    else:
        expense = get_object_or_404(Expense.objects.select_for_update(), pk=expense_id, user=member)
    # Never accept owner, timestamps or arbitrary model attributes from input.
    for field in ('amount', 'category', 'spent_on', 'note'):
        setattr(expense, field, cleaned_data.get(field, '') if field == 'note' else cleaned_data[field])
    expense.note = expense.note.strip()
    expense.full_clean()
    expense.save()
    return expense


@transaction.atomic
def remove_expense(user, expense_id):
    _lock_member(user)
    expense = get_object_or_404(Expense.objects.select_for_update(), pk=expense_id, user=user)
    expense.delete()


def monthly_summary(user, month):
    _require_member(user)
    month = month_start(month)
    budget = MonthlyBudget.objects.filter(user=user, month=month).first()
    expenses = Expense.objects.filter(user=user, spent_on__gte=month, spent_on__lt=next_month(month))
    grouped = {row['category']: row for row in expenses.order_by().values('category').annotate(total=Sum('amount'), count=Count('pk'))}
    categories = []
    total = Decimal('0.00')
    count = 0
    for value, label in Expense.Category.choices:
        row = grouped.get(value, {})
        amount = (row.get('total') or Decimal('0')).quantize(Decimal('0.01'))
        categories.append({'category': value, 'label': label, 'amount': amount, 'count': row.get('count', 0)})
        total += amount
        count += row.get('count', 0)
    remaining = budget.limit - total if budget is not None else None
    return {
        'month': month,
        'budget': budget,
        'total_spent': total,
        'expense_count': count,
        'remaining': remaining,
        'overspent': -remaining if remaining is not None and remaining < 0 else Decimal('0.00'),
        'categories': categories,
        'expenses': expenses,
    }
