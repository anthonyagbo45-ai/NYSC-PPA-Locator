"""Member contributions and private review history, scoped to their author."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from .forms import PPAClaimForm
from .models import PPA, PPAClaim, Review, ReviewRevision
from .profile_services import MEMBER_CLAIM_FIELDS, archive_member_claim, save_member_claim


def _add_service_errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        form.add_error(None, error)


@login_required
@verified_email_required
@require_http_methods(['GET', 'POST'])
@rate_limit('ppa-context-claim', 10)
def submit_claim(request, pk, claim_id=None):
    ppa = get_object_or_404(PPA.objects.select_related('state', 'lga'), pk=pk)
    claim = None
    if claim_id is not None:
        # Filtering the ownership and PPA together avoids leaking another draft.
        claim = get_object_or_404(PPAClaim, pk=claim_id, ppa=ppa, author=request.user)
        if claim.publication_status == PPAClaim.Publication.ARCHIVED:
            messages.info(request, 'This contribution is archived. Submit a new report if the information has changed.')
            return redirect('ppa_profiles:my_claims')
    initial = {field: getattr(claim, field) for field in MEMBER_CLAIM_FIELDS} if claim else {'source_type': PPAClaim.Source.MEMBER}
    form = PPAClaimForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            save_member_claim(ppa=ppa, user=request.user, cleaned_data=form.cleaned_data, claim=claim,
                              submit=form.cleaned_data['action'] == 'submit')
        except ValidationError as error:
            _add_service_errors(form, error)
        else:
            messages.success(request, 'Contribution submitted for moderation.' if form.cleaned_data['action'] == 'submit' else 'Private draft saved.')
            return redirect('ppa_profiles:my_claims')
    # Never pass the complete claim (including its private moderator note) to a
    # member page. Only the explicitly editable fields are present in the form.
    return render(request, 'ppas/claim_form.html', {
        'ppa': ppa, 'form': form, 'editing': claim is not None,
    }, status=400 if request.method == 'POST' else 200)


@login_required
@require_GET
def my_claims(request):
    claims = PPAClaim.objects.filter(author=request.user).order_by('-updated_at', '-pk').values(
        'id', 'ppa_id', 'ppa__name', 'topic', 'content', 'publication_status', 'verification_status',
        'source_type', 'observed_on', 'expires_at', 'updated_at',
    )
    page = Paginator(claims, 20).get_page(request.GET.get('page'))
    rows = list(page.object_list)
    for row in rows:
        row.update(topic_label=dict(PPAClaim.Topic.choices)[row['topic']],
                   publication_label=dict(PPAClaim.Publication.choices)[row['publication_status']],
                   source_label=dict(PPAClaim.Source.choices)[row['source_type']],
                   verification_label=dict(PPAClaim.Verification.choices)[row['verification_status']])
    page.object_list = rows
    return render(request, 'ppas/claim_list.html', {'claims': page, 'page_obj': page})


@login_required
@verified_email_required
@require_POST
@rate_limit('ppa-context-archive', 20)
def archive_claim(request, claim_id):
    claim = get_object_or_404(PPAClaim, pk=claim_id, author=request.user)
    archive_member_claim(claim=claim, user=request.user)
    messages.success(request, 'Contribution archived and removed from public profiles.')
    return redirect('ppa_profiles:my_claims')


@login_required
@require_GET
def review_history(request, review_id):
    # Moderators use the scoped admin. This member endpoint never permits a
    # staff flag or an arbitrary account ID to bypass authorship.
    review = get_object_or_404(Review.objects.select_related('ppa'), pk=review_id, user=request.user)
    revisions = ReviewRevision.objects.filter(review=review).values(
        'rating', 'comment', 'topic_tags', 'context_date', 'is_public', 'reason', 'created_at',
    ).order_by('-created_at', '-pk')
    page = Paginator(revisions, 20).get_page(request.GET.get('page'))
    return render(request, 'ppas/review_history.html', {'review': review, 'revisions': page, 'page_obj': page})
