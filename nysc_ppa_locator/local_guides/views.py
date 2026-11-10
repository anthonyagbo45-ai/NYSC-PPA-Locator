"""Free local browsing with explicit public projections and author-scoped writes."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from ppas.models import PPA
from .forms import DirectoryFilterForm, ListingForm, ListingReportForm, ReconfirmationForm, _positive_identifier
from .models import FareDetails, HousingDetails, Listing, ListingBookmark, ListingReport, ServiceDetails
from . import services


DETAIL_FIELDS = {
    Listing.Kind.HOUSING: ('reported_rent', 'price_period', 'room_type', 'amenities', 'availability', 'contributor_role', 'authorization_attested', 'authorization_basis'),
    Listing.Kind.FARE: ('origin_area', 'destination_area', 'mode', 'amount_low', 'amount_high'),
    Listing.Kind.SERVICE: ('category', 'opening_hours', 'public_business_address'),
}
COMMON_FIELDS = ('title', 'area', 'summary', 'state', 'lga', 'ppa', 'source_type', 'source_url', 'observed_on')


def _errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        form.add_error(None, error)


def _project_page(queryset, request, per_page=20):
    page = Paginator(queryset, per_page).get_page(request.GET.get('page'))
    page.object_list = [services.safe_public_listing(item, user=request.user) for item in page.object_list]
    # Count/page bounds have already been cached by get_page(). Do not retain a
    # second raw-model path through page.paginator.object_list in the context.
    page.paginator.object_list = page.object_list
    return page


def _page_query(request):
    query = request.GET.copy()
    query.pop('page', None)
    return query.urlencode()


def _listing_query():
    return Listing.objects.select_related('state', 'lga', 'ppa', 'housing', 'fare', 'service')


def _initial(request, listing=None, kind=None):
    if listing is not None:
        values = {name: getattr(listing, name) for name in COMMON_FIELDS if name not in ('state', 'lga', 'ppa')}
        values.update(state=listing.state_id, lga=listing.lga_id, ppa=listing.ppa_id)
        detail = getattr(listing, listing.kind, None)
        if detail:
            values.update({name: getattr(detail, name, '') for name in DETAIL_FIELDS[listing.kind]})
        return values
    values = {'source_type': Listing.Source.MEMBER, 'observed_on': timezone.localdate()}
    ppa_id = _positive_identifier(request.GET.get('ppa'))
    ppa = PPA.objects.filter(pk=ppa_id).first() if ppa_id else None
    if ppa:
        values.update(state=ppa.state_id, lga=ppa.lga_id, ppa=ppa.pk)
    elif request.user.is_authenticated:
        # Posting preferences are voluntary defaults, never a published location.
        values.update(state=request.user.state_posted_id, lga=request.user.lga_posted_id)
    if kind == Listing.Kind.HOUSING:
        values.update(price_period='yearly', room_type='other', availability='available', contributor_role='member')
    return values


@require_GET
def home(request):
    data = request.GET.copy()
    selected_ppa = None
    ppa_id = _positive_identifier(data.get('ppa'))
    if ppa_id:
        selected_ppa = PPA.objects.filter(pk=ppa_id).only('id', 'name', 'state_id', 'lga_id').first()
        if selected_ppa:
            if not data.get('state'):
                data['state'] = selected_ppa.state_id
            if not data.get('lga'):
                data['lga'] = selected_ppa.lga_id
    form = DirectoryFilterForm(data)
    queryset = services.public_listings()
    if form.is_valid():
        filters = form.cleaned_data
        for field in ('kind', 'state', 'lga', 'ppa'):
            if filters.get(field):
                queryset = queryset.filter(**{field: filters[field]})
        if filters.get('q'):
            queryset = queryset.filter(Q(title__icontains=filters['q']) | Q(area__icontains=filters['q']) | Q(summary__icontains=filters['q']))
        if filters.get('max_rent') is not None:
            queryset = queryset.filter(kind=Listing.Kind.HOUSING, housing__reported_rent__lte=filters['max_rent'])
        if filters.get('price_period'):
            queryset = queryset.filter(kind=Listing.Kind.HOUSING, housing__price_period=filters['price_period'])
        if filters.get('room_type'):
            queryset = queryset.filter(kind=Listing.Kind.HOUSING, housing__room_type=filters['room_type'])
        if filters.get('amenity'):
            # Supported amenities are a fixed validated enum. Matching the JSON
            # quoted token is portable across SQLite and PostgreSQL, unlike
            # JSONField.contains which SQLite does not implement.
            queryset = queryset.filter(kind=Listing.Kind.HOUSING, housing__amenities__icontains='"' + filters['amenity'] + '"')
        if filters.get('mode'):
            queryset = queryset.filter(kind=Listing.Kind.FARE, fare__mode=filters['mode'])
        if filters.get('category'):
            queryset = queryset.filter(kind=Listing.Kind.SERVICE, service__category=filters['category'])
    else:
        queryset = queryset.none()
    page = _project_page(queryset.order_by('-observed_on', '-pk'), request)
    return render(request, 'local_guides/home.html', {
        'form': form, 'listings': page, 'page_obj': page, 'page_query': _page_query(request),
        'selected_ppa': {'id': selected_ppa.pk, 'name': selected_ppa.name} if selected_ppa else None,
    }, status=200 if form.is_valid() else 400)


@require_GET
def detail(request, pk):
    listing = get_object_or_404(_listing_query(), pk=pk)
    own = request.user.is_authenticated and listing.author_id == request.user.pk
    if listing.publication_status != Listing.Publication.APPROVED and not own:
        raise Http404
    is_saved = request.user.is_authenticated and ListingBookmark.objects.filter(user=request.user, listing_id=pk).exists()
    return render(request, 'local_guides/detail.html', {
        'listing': services.safe_public_listing(listing, user=request.user), 'own': own, 'is_saved': is_saved,
        'private_preview': listing.publication_status != Listing.Publication.APPROVED,
    })


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('local-guide-write', 15)
def create(request, kind, pk=None):
    if kind not in Listing.Kind.values:
        raise Http404
    listing = get_object_or_404(_listing_query(), pk=pk, author=request.user) if pk is not None else None
    if listing is not None and listing.kind != kind:
        raise Http404
    if listing is not None and listing.publication_status == Listing.Publication.ARCHIVED:
        messages.info(request, 'This contribution is archived. Create a new report if circumstances have changed.')
        return redirect('local_guides:mine')
    form = ListingForm(request.POST if request.method == 'POST' else None, kind=kind, initial=_initial(request, listing, kind))
    if request.method == 'POST' and form.is_valid():
        cleaned = {name: form.cleaned_data[name] for name in COMMON_FIELDS}
        cleaned['kind'] = kind
        details = {name: form.cleaned_data.get(name) for name in DETAIL_FIELDS[kind]}
        try:
            services.save_listing(user=request.user, cleaned_data=cleaned, details=details, listing=listing, submit=form.cleaned_data['action'] == 'submit')
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Submitted for moderation.' if form.cleaned_data['action'] == 'submit' else 'Private draft saved.')
            return redirect('local_guides:mine')
    ppa_id = form.initial.get('ppa')
    linked_ppa = PPA.objects.filter(pk=ppa_id).values('id', 'name').first() if ppa_id else None
    return render(request, 'local_guides/form.html', {
        'form': form, 'kind': kind, 'kind_label': dict(Listing.Kind.choices)[kind], 'editing': listing is not None,
        'linked_ppa': linked_ppa,
    }, status=400 if request.method == 'POST' else 200)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
def edit(request, pk):
    listing = get_object_or_404(Listing.objects.only('id', 'kind'), pk=pk, author=request.user)
    return create(request, listing.kind, pk=pk)


@login_required
@never_cache
@require_GET
def mine(request):
    page = _project_page(_listing_query().filter(author=request.user).order_by('-updated_at', '-pk'), request)
    return render(request, 'local_guides/mine.html', {'listings': page, 'page_obj': page, 'page_query': _page_query(request), 'now': timezone.now()})


@login_required
@never_cache
@require_GET
def history(request, pk):
    listing = get_object_or_404(_listing_query(), pk=pk, author=request.user)
    revisions = services.owner_revisions(listing=listing, user=request.user)
    page = Paginator(revisions, 20).get_page(request.GET.get('page'))
    for revision in page.object_list:
        revision.update(
            publication_label=dict(Listing.Publication.choices).get(revision.get('publication_status'), 'Status not recorded'),
            source_label=dict(Listing.Source.choices).get(revision.get('source_type'), 'Source not recorded'),
            verification_label=dict(Listing.Verification.choices).get(revision.get('verification_status'), 'Verification not recorded'),
        )
        values = revision.get('details', {})
        choices = {
            'price_period': HousingDetails.PricePeriod.choices, 'room_type': HousingDetails.RoomType.choices,
            'availability': HousingDetails.Availability.choices, 'contributor_role': HousingDetails.ContributorRole.choices,
            'mode': FareDetails.Mode.choices, 'category': ServiceDetails.Category.choices,
        }
        for name, labels in choices.items():
            if name in values:
                values[name + '_label'] = dict(labels).get(values[name], 'Not recorded')
        if isinstance(values.get('amenities'), list):
            allowed = dict(HousingDetails.AMENITY_CHOICES)
            values['amenity_labels'] = [allowed[code] for code in values['amenities'] if isinstance(code, str) and code in allowed]
    return render(request, 'local_guides/history.html', {'listing': services.safe_public_listing(listing, user=request.user), 'revisions': page, 'page_obj': page, 'page_query': _page_query(request)})


@login_required
@verified_email_required
@never_cache
@require_POST
@rate_limit('local-guide-archive', 20)
def archive(request, pk):
    listing = get_object_or_404(Listing, pk=pk, author=request.user)
    services.archive_listing(listing=listing, user=request.user)
    messages.success(request, 'Contribution withdrawn from public browsing. Your private history is retained.')
    return redirect('local_guides:mine')


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('local-guide-reconfirm', 15)
def reconfirm(request, pk):
    listing = get_object_or_404(_listing_query(), pk=pk, author=request.user)
    form = ReconfirmationForm(request.POST if request.method == 'POST' else None, housing=listing.kind == Listing.Kind.HOUSING, initial={'observed_on': timezone.localdate(), 'available': 'yes'})
    if request.method == 'POST' and form.is_valid():
        try:
            services.reconfirm_listing(listing=listing, user=request.user, observed_on=form.cleaned_data['observed_on'], available=(form.cleaned_data.get('available') == 'yes') if listing.kind == Listing.Kind.HOUSING else None)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your new check is awaiting moderation. Earlier independent verification has not been carried over.')
            return redirect('local_guides:mine')
    return render(request, 'local_guides/reconfirm.html', {'listing': services.safe_public_listing(listing, user=request.user), 'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_POST
@rate_limit('local-guide-bookmark', 30)
def bookmark(request, pk):
    action = request.POST.get('action')
    if action == 'remove':
        # A withdrawn or newly private listing can still be removed without
        # revealing its latest private title or contribution contents.
        ListingBookmark.objects.filter(user=request.user, listing_id=pk).delete()
        messages.success(request, 'Removed from your saved local guidance.')
        return redirect('local_guides:saved')
    if action != 'save':
        return HttpResponseBadRequest('Choose save or remove.')
    listing = services.get_public_listing(pk)
    ListingBookmark.objects.get_or_create(user=request.user, listing=listing)
    messages.success(request, 'Saved to your private local watchlist.')
    return redirect('local_guides:detail', pk=pk)


@login_required
@never_cache
@require_GET
def saved(request):
    bookmarks = ListingBookmark.objects.filter(user=request.user).select_related('listing__state', 'listing__lga', 'listing__ppa', 'listing__housing', 'listing__fare', 'listing__service').order_by('-created_at', '-pk')
    page = Paginator(bookmarks, 20).get_page(request.GET.get('page'))
    page.object_list = [
        {'id': item.listing_id, 'listing': services.safe_public_listing(item.listing) if item.listing.publication_status == Listing.Publication.APPROVED else None}
        for item in page.object_list
    ]
    page.paginator.object_list = page.object_list
    return render(request, 'local_guides/saved.html', {'bookmarks': page, 'page_obj': page, 'page_query': _page_query(request)})


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('local-guide-report', 10)
def report(request, pk):
    listing = services.get_public_listing(pk)
    form = ListingReportForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_report(user=request.user, listing=listing, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private report is in the moderation queue. It does not automatically change a published listing.')
            return redirect('local_guides:reports')
    return render(request, 'local_guides/report.html', {'listing': services.safe_public_listing(listing), 'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def reports(request):
    reports = ListingReport.objects.filter(reporter=request.user).order_by('-created_at', '-pk').values('id', 'category', 'details', 'proposed_correction', 'status', 'created_at')
    page = Paginator(reports, 20).get_page(request.GET.get('page'))
    rows = list(page.object_list)
    for row in rows:
        row.update(category_label=dict(ListingReport.Category.choices)[row['category']], status_label=dict(ListingReport.Status.choices)[row['status']])
    page.object_list = rows
    page.paginator.object_list = page.object_list
    return render(request, 'local_guides/reports.html', {'reports': page, 'page_obj': page, 'page_query': _page_query(request)})
