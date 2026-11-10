"""Member contributions never overwrite a destination without moderator review."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from accounts.decorators import verified_email_required
from config.security import rate_limit
from .models import Bookmark, Confirmation, PPA, PlaceChangeLog, PlaceReport, Review


class ConfirmationForm(forms.Form):
    reported_here = forms.BooleanField(label='I have reported to this organisation as my PPA.')
    note = forms.CharField(max_length=500, required=False)


class PlaceReportForm(forms.ModelForm):
    review_id = forms.IntegerField(min_value=1, max_value=2147483647, required=False, widget=forms.HiddenInput)

    class Meta:
        model = PlaceReport
        fields = ['category', 'details', 'proposed_correction']
        widgets = {'details': forms.Textarea(attrs={'rows': 3}), 'proposed_correction': forms.Textarea(attrs={'rows': 3})}


@login_required
@require_POST
@verified_email_required
@rate_limit('confirm-place', 10)
def confirm_ppa(request, pk):
    ppa = get_object_or_404(PPA, pk=pk)
    form = ConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Confirm that you have reported here, and keep any note under 500 characters.')
        return redirect('ppa_detail', pk=pk)
    with transaction.atomic():
        ppa = PPA.objects.select_for_update().get(pk=pk)
        now = timezone.now()
        Confirmation.objects.update_or_create(ppa=ppa, user=request.user, defaults={'confirmed_at': now, 'note': form.cleaned_data['note']})
        old_status = ppa.status
        if ppa.status == PPA.Status.DISCOVERED:
            ppa.status = PPA.Status.MEMBER_CONFIRMED
        ppa.last_confirmed_at = now
        ppa.save(update_fields=['status', 'last_confirmed_at', 'updated_at'])
        if old_status != ppa.status:
            PlaceChangeLog.objects.create(ppa=ppa, actor=request.user, changes={'status': {'from': old_status, 'to': ppa.status}}, reason='A member with a verified email reported attending this PPA; this does not establish official NYSC approval.')
    messages.success(request, 'Your confirmation has been recorded. Thanks for helping the next corps member.')
    return redirect('ppa_detail', pk=pk)


@login_required
@require_http_methods(['GET', 'POST'])
@rate_limit('report-place', 10)
def report_ppa(request, pk):
    ppa = get_object_or_404(PPA, pk=pk)
    initial = {}
    selected_review = None
    if request.method == 'GET':
        category = request.GET.get('category', '')
        if category and category not in PlaceReport.Category.values:
            return HttpResponseBadRequest('Choose a valid report category.')
        if category:
            initial['category'] = category
        if request.GET.get('review'):
            try:
                review_id = PlaceReportForm.base_fields['review_id'].clean(request.GET['review'])
            except ValidationError:
                return HttpResponseBadRequest('Choose a valid review.')
            selected_review = get_object_or_404(Review, pk=review_id, ppa=ppa, is_public=True)
            initial['review_id'] = selected_review.pk
            initial.setdefault('category', PlaceReport.Category.REVIEW_ABUSE)
        form = PlaceReportForm(initial=initial)
        return render(request, 'ppas/report_form.html', {'ppa': ppa, 'form': form, 'selected_review': selected_review})
    form = PlaceReportForm(request.POST)
    if not form.is_valid():
        return render(request, 'ppas/report_form.html', {'ppa': ppa, 'form': form}, status=400)
    review = None
    if form.cleaned_data['review_id']:
        review = get_object_or_404(Review, pk=form.cleaned_data['review_id'], ppa=ppa, is_public=True)
    if form.cleaned_data['category'] == PlaceReport.Category.REVIEW_ABUSE and review is None:
        form.add_error('review_id', 'Select the review you want to report.')
        return render(request, 'ppas/report_form.html', {'ppa': ppa, 'form': form}, status=400)
    PlaceReport.objects.create(ppa=ppa, reporter=request.user, review=review, **{field: form.cleaned_data[field] for field in ('category', 'details', 'proposed_correction')})
    messages.success(request, 'Your report is in the moderation queue. Published directions have not been changed.')
    return redirect('ppa_detail', pk=pk)


@login_required
@require_POST
@rate_limit('bookmark-place', 30)
def bookmark_ppa(request, pk):
    ppa = get_object_or_404(PPA, pk=pk)
    action = request.POST.get('action', 'save')
    if action == 'save':
        Bookmark.objects.get_or_create(ppa=ppa, user=request.user)
        messages.success(request, 'PPA saved to your destinations.')
    elif action == 'remove':
        Bookmark.objects.filter(ppa=ppa, user=request.user).delete()
        messages.success(request, 'PPA removed from your saved destinations.')
    else:
        messages.error(request, 'Choose save or remove.')
    return redirect('ppa_detail', pk=pk)


@login_required
@require_GET
def saved_ppas(request):
    places = PPA.objects.filter(bookmarks__user=request.user).select_related('state', 'lga').order_by('name', 'pk')
    page = Paginator(places, 25).get_page(request.GET.get('page'))
    return render(request, 'ppas/saved.html', {'ppas': page, 'page_obj': page})


@login_required
@require_GET
def my_reports(request):
    # Deliberately project only member-visible fields. Moderator notes and evidence
    # may contain private investigation details and never enter this page context.
    reports = PlaceReport.objects.filter(reporter=request.user).order_by('-created_at', '-pk').values(
        'id', 'ppa_id', 'ppa__name', 'category', 'status', 'details', 'created_at',
    )
    page = Paginator(reports, 25).get_page(request.GET.get('page'))
    rows = list(page.object_list)
    category_labels = dict(PlaceReport.Category.choices)
    status_labels = dict(PlaceReport.Status.choices)
    for row in rows:
        row['category_label'] = category_labels[row['category']]
        row['status_label'] = status_labels[row['status']]
    page.object_list = rows
    return render(request, 'ppas/my_reports.html', {'reports': page, 'page_obj': page})
