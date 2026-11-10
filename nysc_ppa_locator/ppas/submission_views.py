"""Author-only proposal screens; published PPAs use the existing detail flow."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from .models import PPAClaim, PPASubmission, SubmissionRevision
from .profile_views import _add_service_errors
from .submission_forms import PPASubmissionForm
from .submission_services import MEMBER_FIELDS, duplicate_candidates, save_submission, withdraw_submission


@require_GET
def overview(request):
    return render(request, 'ppas/submission_overview.html')


@login_required
@verified_email_required
@require_http_methods(['GET', 'POST'])
@rate_limit('independent-ppa-proposal', 12)
def submit(request, pk=None):
    submission = None
    initial = {'source_type': 'member'}
    if pk is not None:
        submission = get_object_or_404(PPASubmission, pk=pk, author=request.user)
        if submission.publication_status == PPASubmission.Publication.WITHDRAWN:
            messages.info(request, 'Withdrawn proposals are kept as history. Submit a new proposal if needed.')
            return redirect('ppa_submissions:mine')
        initial.update({field: getattr(submission, field) for field in MEMBER_FIELDS})
    else:
        # Profile geography is a voluntary default and must still pass the same
        # state/LGA validation as an explicit choice on every POST.
        initial.update(state=request.user.state_posted_id, lga=request.user.lga_posted_id)
    form = PPASubmissionForm(request.POST if request.method == 'POST' else None, initial=initial)
    candidates = []
    if request.method == 'POST' and form.is_valid():
        try:
            result = save_submission(user=request.user, cleaned_data=form.cleaned_data,
                                     submission=submission, submit=form.cleaned_data['action'] == 'submit')
        except ValidationError as error:
            _add_service_errors(form, error)
        else:
            messages.success(request, 'PPA proposal submitted for review. It is not published yet.' if result.shared_for_review else 'Private PPA draft saved.')
            return redirect('ppa_submissions:mine')
    selected_state = form.cleaned_data.get('state') if form.is_bound else initial.get('state')
    selected_lga = form.cleaned_data.get('lga') if form.is_bound else initial.get('lga')
    proposed_name = form.cleaned_data.get('name', '') if form.is_bound else initial.get('name', '')
    if selected_state and selected_lga and proposed_name:
        candidates = list(duplicate_candidates(name=proposed_name, state_id=getattr(selected_state, 'pk', selected_state),
                                              lga_id=getattr(selected_lga, 'pk', selected_lga)).values('id', 'name', 'address')[:10])
    return render(request, 'ppas/submission_form.html', {'form': form, 'editing': submission is not None,
                                                       'duplicate_candidates': candidates},
                  status=400 if request.method == 'POST' else 200)


@login_required
@require_GET
def mine(request):
    proposals = PPASubmission.objects.filter(author=request.user).select_related('state', 'lga').values(
        'id', 'name', 'state__name', 'lga__name', 'publication_status', 'observed_on', 'expires_at',
        'source_type', 'verification_status', 'published_ppa_id', 'updated_at',
    ).order_by('-updated_at', '-pk')
    page = Paginator(proposals, 20).get_page(request.GET.get('page'))
    rows = list(page.object_list)
    for row in rows:
        row['publication_label'] = dict(PPASubmission.Publication.choices)[row['publication_status']]
        row['source_label'] = dict(PPAClaim.Source.choices)[row['source_type']]
        row['verification_label'] = dict(PPAClaim.Verification.choices)[row['verification_status']]
        row['is_stale'] = row['expires_at'] <= timezone.now()
    page.object_list = rows
    return render(request, 'ppas/submission_list.html', {'proposals': page, 'page_obj': page})


@login_required
@require_GET
def history(request, pk):
    submission = get_object_or_404(PPASubmission.objects.only('id', 'name'), pk=pk, author=request.user)
    # Never project operator reasons, private evidence or actor identifiers into
    # member history. Even a proposal author cannot read private investigations.
    fields = ('name', 'address', 'landmark', 'entrance_instructions', 'source_type', 'source_url',
              'observed_on', 'publication_status', 'verification_status', 'published_ppa_id')
    revisions = SubmissionRevision.objects.filter(submission=submission).order_by('-created_at', '-pk').values(
        'created_at', *('snapshot__'+field for field in fields),
    )
    page = Paginator(revisions, 20).get_page(request.GET.get('page'))
    rows = []
    for item in page.object_list:
        row = {field: item['snapshot__'+field] for field in fields}
        row['created_at'] = item['created_at']
        row['publication_label'] = dict(PPASubmission.Publication.choices).get(row['publication_status'], 'Historical version')
        rows.append(row)
    page.object_list = rows
    return render(request, 'ppas/submission_history.html', {'proposal_name': submission.name,
                                                          'revisions': page, 'page_obj': page})


@login_required
@require_POST
@rate_limit('withdraw-independent-ppa', 20)
def withdraw(request, pk):
    submission = get_object_or_404(PPASubmission, pk=pk, author=request.user)
    withdraw_submission(submission=submission, user=request.user)
    messages.success(request, 'Proposal withdrawn. Any existing directory record remains unchanged; report corrections on its profile.')
    return redirect('ppa_submissions:mine')
