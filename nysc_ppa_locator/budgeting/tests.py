from datetime import date, timedelta
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import BudgetForm, ExpenseForm, MonthFilterForm
from .models import Expense, MonthlyBudget
from .services import monthly_summary, remove_budget, remove_expense, save_budget, save_expense


class BudgetDataMixin:
    month = date(2026, 10, 1)

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            state_code='BG/PRIVATE/001', email='private-budget@example.com',
            full_name='Private Budget Member', email_verified=False,
        )
        cls.other = get_user_model().objects.create_user(
            state_code='BG/PRIVATE/002', email='other-budget@example.com',
            full_name='Other Private Member', is_staff=True,
        )

    def setUp(self):
        cache.clear()

    def record(self, user=None, amount='5.00', category=Expense.Category.FOOD, spent_on=date(2026, 10, 5), note='My private expense'):
        return save_expense(user or self.user, cleaned_data={
            'amount': Decimal(amount), 'category': category, 'spent_on': spent_on, 'note': note,
        })


class BudgetServiceTests(BudgetDataMixin, TestCase):
    def test_monthly_totals_use_decimal_money_and_exclude_other_members_and_months(self):
        self.record(amount='0.10', category=Expense.Category.TRANSPORT)
        self.record(amount='0.20', category=Expense.Category.TRANSPORT)
        self.record(amount='50.00', spent_on=date(2026, 9, 30))
        self.record(amount='60.00', spent_on=date(2026, 11, 1))
        self.record(self.other, amount='999.00')
        save_budget(self.user, month=self.month, limit=Decimal('0.20'))
        result = monthly_summary(self.user, self.month)
        self.assertEqual(result['total_spent'], Decimal('0.30'))
        self.assertEqual(result['expense_count'], 2)
        self.assertEqual(result['remaining'], Decimal('-0.10'))
        self.assertEqual(result['overspent'], Decimal('0.10'))
        transport = next(row for row in result['categories'] if row['category'] == Expense.Category.TRANSPORT)
        self.assertEqual(transport['amount'], Decimal('0.30'))
        self.assertEqual(transport['count'], 2)

    def test_no_limit_is_required_and_zero_is_a_real_limit(self):
        result = monthly_summary(self.user, self.month)
        self.assertIsNone(result['budget'])
        self.assertIsNone(result['remaining'])
        self.assertEqual(result['total_spent'], Decimal('0.00'))
        self.record(amount='1.00')
        save_budget(self.user, month=self.month, limit=Decimal('0'))
        self.assertEqual(monthly_summary(self.user, self.month)['overspent'], Decimal('1.00'))

    def test_updating_limit_preserves_existing_expenses_and_other_member_budget(self):
        expense = self.record()
        budget = save_budget(self.user, month=self.month, limit=Decimal('500.00'))
        other_budget = save_budget(self.other, month=self.month, limit=Decimal('800.00'))
        updated = save_budget(self.user, month=self.month, limit=Decimal('600.00'))
        self.assertEqual(updated.pk, budget.pk)
        self.assertEqual(Expense.objects.get(pk=expense.pk).note, 'My private expense')
        other_budget.refresh_from_db()
        self.assertEqual(other_budget.limit, Decimal('800.00'))
        self.assertEqual(MonthlyBudget.objects.filter(user=self.user).count(), 1)

    def test_removing_limit_does_not_remove_expenses_or_another_members_limit(self):
        expense = self.record()
        save_budget(self.user, month=self.month, limit=Decimal('500'))
        other = save_budget(self.other, month=self.month, limit=Decimal('800'))
        remove_budget(self.user, month=self.month)
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())
        self.assertTrue(MonthlyBudget.objects.filter(pk=other.pk).exists())
        self.assertFalse(MonthlyBudget.objects.filter(user=self.user).exists())

    def test_expense_services_reject_cross_account_mutations_and_forged_owner(self):
        expense = self.record(self.other)
        values = {'amount': Decimal('1.00'), 'category': 'food', 'spent_on': date(2026, 10, 1), 'note': 'Replacement'}
        with self.assertRaises(Http404):
            save_expense(self.user, expense_id=expense.pk, cleaned_data=values)
        with self.assertRaises(Http404):
            remove_expense(self.user, expense.pk)
        created = save_expense(self.user, cleaned_data={**values, 'user': self.other, 'user_id': self.other.pk})
        self.assertEqual(created.user_id, self.user.pk)
        expense.refresh_from_db()
        self.assertEqual(expense.note, 'My private expense')

    def test_invalid_fields_cannot_partially_replace_an_existing_expense(self):
        expense = self.record(amount='25.45')
        with self.assertRaises(ValidationError):
            save_expense(self.user, expense_id=expense.pk, cleaned_data={
                'amount': Decimal('-3'), 'category': 'food', 'spent_on': date(2026, 10, 1), 'note': 'Must not be saved',
            })
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('25.45'))
        self.assertEqual(expense.note, 'My private expense')

    def test_service_requires_an_active_member(self):
        self.user.is_active = False
        with self.assertRaises(PermissionDenied):
            monthly_summary(self.user, self.month)
        with self.assertRaises(PermissionDenied):
            save_budget(self.user, month=self.month, limit=Decimal('100'))

    def test_december_and_leap_day_period_boundaries(self):
        self.record(spent_on=date(2024, 2, 29), amount='29.00')
        self.record(spent_on=date(2024, 3, 1), amount='1.00')
        self.record(spent_on=date(2025, 12, 31), amount='31.00')
        self.record(spent_on=date(2026, 1, 1), amount='100.00')
        self.assertEqual(monthly_summary(self.user, date(2024, 2, 1))['total_spent'], Decimal('29.00'))
        self.assertEqual(monthly_summary(self.user, date(2025, 12, 1))['total_spent'], Decimal('31.00'))

    def test_database_rejects_noncanonical_month_duplicate_and_bad_money(self):
        save_budget(self.user, month=self.month, limit=Decimal('0'))
        cases = [
            lambda: MonthlyBudget.objects.create(user=self.user, month=self.month, limit=1),
            lambda: MonthlyBudget.objects.create(user=self.user, month=date(2026, 10, 2), limit=1),
            lambda: MonthlyBudget.objects.create(user=self.user, month=date(2026, 9, 1), limit=-1),
            lambda: Expense.objects.create(user=self.user, amount=0, category='food', spent_on=self.month),
            lambda: Expense.objects.create(user=self.user, amount=-1, category='food', spent_on=self.month),
            lambda: Expense.objects.create(user=self.user, amount=1, category='forged', spent_on=self.month),
            lambda: Expense.objects.create(user=self.user, amount=1, category='food', spent_on=date(1899, 1, 1)),
        ]
        for action in cases:
            with self.subTest(action=cases.index(action)), self.assertRaises(IntegrityError), transaction.atomic():
                action()

    def test_queries_remain_bounded_with_large_private_history(self):
        Expense.objects.bulk_create([
            Expense(user=self.user, amount=Decimal('1.25'), category='food', spent_on=self.month)
            for _ in range(250)
        ])
        with self.assertNumQueries(2):
            result = monthly_summary(self.user, self.month)
        self.assertEqual(result['expense_count'], 250)
        self.assertEqual(result['total_spent'], Decimal('312.50'))

    def test_budget_and_expenses_have_no_administration_registry_or_identifying_labels(self):
        expense = self.record()
        budget = save_budget(self.user, month=self.month, limit=Decimal('50'))
        self.assertNotIn(Expense, admin.site._registry)
        self.assertNotIn(MonthlyBudget, admin.site._registry)
        for value in (str(expense), str(budget)):
            self.assertNotIn(self.user.state_code, value)
            self.assertNotIn(self.user.email, value)
            self.assertNotIn(expense.note, value)


class BudgetFormTests(BudgetDataMixin, TestCase):
    def test_decimal_fields_reject_nonfinite_negative_overprecision_and_oversized_values(self):
        for value in ('NaN', 'Infinity', '-Infinity', '-1', '1.001', '10000000000.00', '1e9999'):
            with self.subTest(value=value):
                form = ExpenseForm({'amount': value, 'category': 'food', 'spent_on': '2026-10-01', 'note': ''})
                self.assertFalse(form.is_valid())
                self.assertIn('amount', form.errors)
        self.assertFalse(ExpenseForm({'amount': '0', 'category': 'food', 'spent_on': '2026-10-01'}).is_valid())
        self.assertTrue(BudgetForm({'limit': '0', 'month': '2026-10'}).is_valid())

    def test_invalid_future_dates_and_long_private_notes_fail_without_writes(self):
        for spent_on in ('2026-02-30', '1899-12-31', (timezone.localdate() + timedelta(days=1)).isoformat()):
            with self.subTest(spent_on=spent_on):
                self.assertFalse(ExpenseForm({'amount': '1.00', 'category': 'food', 'spent_on': spent_on}).is_valid())
        form = ExpenseForm({'amount': '1.00', 'category': 'food', 'spent_on': '2026-10-01', 'note': 'x' * 501})
        self.assertFalse(form.is_valid())
        self.assertIn('note', form.errors)
        self.assertFalse(Expense.objects.exists())

    def test_month_input_rejects_unbounded_or_malformed_periods(self):
        for value in ('999999999999-01', '1899-12', '2101-01', '2026-13', '2026-00', '2026-10-02', '../2026-10'):
            with self.subTest(value=value):
                self.assertFalse(MonthFilterForm({'month': value}).is_valid())
        self.assertTrue(MonthFilterForm({'month': '1900-01'}).is_valid())
        self.assertTrue(MonthFilterForm({'month': '2100-12'}).is_valid())


class BudgetViewTests(BudgetDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def expense_values(self, **extra):
        return {'amount': '30.25', 'category': 'transport', 'spent_on': '2026-10-01', 'note': 'Bus to PPA', **extra}

    def test_guests_cannot_read_or_mutate_budget_routes(self):
        self.client.logout()
        expense = self.record()
        for name, kwargs in (
            ('home', {}), ('budget_edit', {}), ('budget_delete', {}), ('expense_add', {}),
            ('expense_edit', {'pk': expense.pk}), ('expense_delete', {'pk': expense.pk}),
        ):
            url = reverse('budgeting:' + name, kwargs=kwargs)
            with self.subTest(name=name):
                self.assertEqual(self.client.get(url).status_code, 302)
                self.assertEqual(self.client.post(url, self.expense_values()).status_code, 302)
        self.assertEqual(Expense.objects.count(), 1)

    def test_unverified_member_can_use_private_budget_and_forged_owner_is_ignored(self):
        self.assertFalse(self.user.email_verified)
        response = self.client.post(reverse('budgeting:expense_add'), self.expense_values(user=self.other.pk, user_id=self.other.pk))
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.user_id, self.user.pk)
        self.assertEqual(expense.amount, Decimal('30.25'))
        response = self.client.post(reverse('budgeting:budget_edit'), {'month': '2026-10', 'limit': '1000.00', 'user': self.other.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MonthlyBudget.objects.get().user_id, self.user.pk)

    def test_other_members_including_staff_cannot_read_update_or_delete_expense_ids(self):
        expense = self.record()
        self.client.force_login(self.other)
        for route in ('expense_edit', 'expense_delete'):
            url = reverse('budgeting:' + route, args=[expense.pk])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {**self.expense_values(), 'confirm_delete': 'on'}).status_code, 404)
        expense.refresh_from_db()
        self.assertEqual(expense.note, 'My private expense')

    def test_summary_never_exposes_other_members_notes_or_limits(self):
        self.record(self.other, note='OTHER PRIVATE SPENDING NOTE')
        save_budget(self.other, month=self.month, limit=Decimal('89765.43'))
        response = self.client.get(reverse('budgeting:home'), {'month': '2026-10', 'user': self.other.pk})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'OTHER PRIVATE SPENDING NOTE')
        self.assertNotContains(response, '89,765.43')
        self.assertContains(response, 'No expenses recorded')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_form_gets_and_delete_confirmation_have_no_database_side_effects(self):
        expense = self.record()
        for url in (
            reverse('budgeting:budget_edit') + '?month=2026-10',
            reverse('budgeting:expense_add') + '?month=2026-10',
            reverse('budgeting:expense_edit', args=[expense.pk]),
            reverse('budgeting:expense_delete', args=[expense.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertFalse(MonthlyBudget.objects.exists())
        self.assertEqual(self.client.get(reverse('budgeting:budget_delete')).status_code, 405)

    def test_delete_requires_post_confirmation_and_preserves_another_members_history(self):
        expense = self.record()
        other_expense = self.record(self.other)
        url = reverse('budgeting:expense_delete', args=[expense.pk])
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())
        self.assertEqual(self.client.post(url, {'confirm_delete': 'on'}).status_code, 302)
        self.assertFalse(Expense.objects.filter(pk=expense.pk).exists())
        self.assertTrue(Expense.objects.filter(pk=other_expense.pk).exists())

    def test_removing_limit_is_separately_confirmed_and_preserves_expenses(self):
        expense = self.record()
        save_budget(self.user, month=self.month, limit=Decimal('500'))
        url = reverse('budgeting:budget_delete')
        self.assertEqual(self.client.post(url, {'month': '2026-10'}).status_code, 400)
        self.assertTrue(MonthlyBudget.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.post(url, {'month': '2026-10', 'confirm_delete': 'on'}).status_code, 302)
        self.assertFalse(MonthlyBudget.objects.filter(user=self.user).exists())
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())

    def test_csrf_is_required_for_every_budget_mutation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        expense = self.record()
        for route, kwargs in (
            ('budget_edit', {}), ('budget_delete', {}), ('expense_add', {}),
            ('expense_edit', {'pk': expense.pk}), ('expense_delete', {'pk': expense.pk}),
        ):
            with self.subTest(route=route):
                response = client.post(reverse('budgeting:' + route, kwargs=kwargs), {
                    **self.expense_values(), 'month': '2026-10', 'limit': '50', 'confirm_delete': 'on',
                })
                self.assertEqual(response.status_code, 403)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertFalse(MonthlyBudget.objects.exists())
        client.get(reverse('budgeting:expense_add'))
        token = client.cookies['csrftoken'].value
        response = client.post(reverse('budgeting:expense_add'), self.expense_values(csrfmiddlewaretoken=token))
        self.assertEqual(response.status_code, 302)

    def test_invalid_months_return_validation_error_and_boundary_navigation_stays_supported(self):
        for value in ('bad', '9999-12', '2026-99'):
            self.assertEqual(self.client.get(reverse('budgeting:home'), {'month': value}).status_code, 400)
        first = self.client.get(reverse('budgeting:home'), {'month': '1900-01'})
        last = self.client.get(reverse('budgeting:home'), {'month': '2100-12'})
        self.assertEqual(first.status_code, 200)
        self.assertIsNone(first.context['previous_month'])
        self.assertEqual(last.status_code, 200)
        self.assertIsNone(last.context['next_month'])

    def test_expense_edit_updates_only_this_members_record_and_month_totals(self):
        expense = self.record(amount='50.00')
        response = self.client.post(reverse('budgeting:expense_edit', args=[expense.pk]), self.expense_values(
            spent_on='2026-09-30', note='Updated private expense', user=self.other.pk,
        ))
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.user_id, self.user.pk)
        self.assertEqual(expense.amount, Decimal('30.25'))
        self.assertEqual(monthly_summary(self.user, self.month)['total_spent'], Decimal('0.00'))
        self.assertEqual(monthly_summary(self.user, date(2026, 9, 1))['total_spent'], Decimal('30.25'))

    def test_private_notes_are_escaped_and_page_size_is_bounded(self):
        Expense.objects.bulk_create([
            Expense(user=self.user, amount=1, category='food', spent_on=self.month, note='<script>private()</script>')
            for _ in range(25)
        ])
        response = self.client.get(reverse('budgeting:home'), {'month': '2026-10'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['page_obj']), 20)
        self.assertContains(response, '&lt;script&gt;private()&lt;/script&gt;')
        self.assertNotContains(response, '<script>private()</script>')
        self.assertContains(response, 'month=2026-10&amp;page=2')
        self.assertNotContains(response, self.user.state_code)
        self.assertNotContains(response, self.user.email)

    def test_authenticated_expense_spam_is_bounded(self):
        url = reverse('budgeting:expense_add')
        for _ in range(30):
            self.assertEqual(self.client.post(url, self.expense_values()).status_code, 302)
        response = self.client.post(url, self.expense_values())
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
        self.assertEqual(Expense.objects.filter(user=self.user).count(), 30)
