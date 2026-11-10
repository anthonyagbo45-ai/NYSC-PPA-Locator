from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from config.security import rate_limit
from .forms import BudgetForm, DeleteBudgetForm, DeleteExpenseForm, ExpenseForm, MonthFilterForm
from .models import Expense, MonthlyBudget
from .services import month_start, monthly_summary, next_month, remove_budget, remove_expense, save_budget, save_expense


def _month_form(request):
    value = request.GET.get('month', timezone.localdate().strftime('%Y-%m'))
    return MonthFilterForm({'month': value})


def _home_url(month):
    return reverse('budgeting:home') + '?' + urlencode({'month': month.strftime('%Y-%m')})


@login_required
@never_cache
@require_GET
def home(request):
    form = _month_form(request)
    if not form.is_valid():
        return render(request, 'budgeting/home.html', {'month_form': form}, status=400)
    month = form.cleaned_data['month']
    context = monthly_summary(request.user, month)
    page = Paginator(context.pop('expenses'), 20).get_page(request.GET.get('page'))
    previous = month.replace(year=month.year - 1, month=12) if month.month == 1 else month.replace(month=month.month - 1)
    context.update({
        'month_form': form, 'page_obj': page,
        'page_query': urlencode({'month': month.strftime('%Y-%m')}),
        'previous_month': previous.strftime('%Y-%m') if month.year > 1900 or month.month > 1 else None,
        'next_month': next_month(month).strftime('%Y-%m') if month.year < 2100 or month.month < 12 else None,
    })
    return render(request, 'budgeting/home.html', context)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('budget-save', 20)
def budget_edit(request):
    month_form = _month_form(request)
    initial = {}
    existing = None
    if month_form.is_valid():
        month = month_form.cleaned_data['month']
        existing = MonthlyBudget.objects.filter(user=request.user, month=month).first()
        initial = {'month': month, 'limit': existing.limit if existing else ''}
    form = BudgetForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'GET' and not month_form.is_valid():
        form = BudgetForm({'month': request.GET.get('month', ''), 'limit': ''})
        form.is_valid()
    if request.method == 'POST' and form.is_valid():
        try:
            save_budget(request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, 'Your private monthly spending limit was saved.')
            return redirect(_home_url(form.cleaned_data['month']))
    return render(request, 'budgeting/budget_form.html', {
        'form': form, 'existing_budget': existing,
        'delete_form': DeleteBudgetForm(initial={'month': existing.month}) if existing else None,
    }, status=400 if request.method == 'POST' or not month_form.is_valid() else 200)


@login_required
@never_cache
@require_POST
@rate_limit('budget-remove', 20)
def budget_delete(request):
    form = DeleteBudgetForm(request.POST)
    if not form.is_valid():
        return render(request, 'budgeting/budget_delete.html', {'form': form}, status=400)
    remove_budget(request.user, month=form.cleaned_data['month'])
    messages.success(request, 'Your spending limit was removed. Your expenses were kept.')
    return redirect(_home_url(form.cleaned_data['month']))


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('expense-add', 30)
def expense_add(request):
    month_form = _month_form(request)
    initial = {'spent_on': timezone.localdate()}
    if month_form.is_valid():
        initial['spent_on'] = min(month_form.cleaned_data['month'], timezone.localdate()) if month_form.cleaned_data['month'] != month_start(timezone.localdate()) else timezone.localdate()
    elif request.method == 'GET':
        return render(request, 'budgeting/home.html', {'month_form': month_form}, status=400)
    form = ExpenseForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        expense = save_expense(request.user, cleaned_data=form.cleaned_data)
        messages.success(request, 'Your expense was added to your private records.')
        return redirect(_home_url(expense.spent_on.replace(day=1)))
    return render(request, 'budgeting/expense_form.html', {'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('expense-edit', 30)
def expense_edit(request, pk):
    expense = get_object_or_404(Expense, pk=pk, user=request.user)
    form = ExpenseForm(request.POST if request.method == 'POST' else None, instance=expense)
    if request.method == 'POST' and form.is_valid():
        saved = save_expense(request.user, expense_id=pk, cleaned_data=form.cleaned_data)
        messages.success(request, 'Your private expense was updated.')
        return redirect(_home_url(saved.spent_on.replace(day=1)))
    return render(request, 'budgeting/expense_form.html', {'form': form, 'expense': expense}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('expense-remove', 30)
def expense_delete(request, pk):
    expense = get_object_or_404(Expense, pk=pk, user=request.user)
    form = DeleteExpenseForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        month = expense.spent_on.replace(day=1)
        remove_expense(request.user, expense.pk)
        messages.success(request, 'Your private expense was deleted.')
        return redirect(_home_url(month))
    return render(request, 'budgeting/expense_delete.html', {'form': form, 'expense': expense}, status=400 if request.method == 'POST' else 200)
