from difflib import SequenceMatcher
import re
from urllib.parse import urlencode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.conf import settings
from django.views.decorators.cache import never_cache
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from config.security import rate_limit, check_rate_limit
from accounts.views import register, user_login
from .forms import SearchForm, ReviewForm, LivePlaceForm, CommunityPlaceForm
from .models import PPA
from .profile_services import save_member_review, get_public_profile_claims
from .navigation import directions_url, destination_data
from .services import search_google_places, PlacesUnavailable
from .location import assess_place_location
from locations.models import State, LGA


@require_GET
def landing(request):
    return dynamic_place_search_view(request)


@sensitive_post_parameters('password', 'confirm_password')
@require_POST
def portal_entry(request):
    if request.POST.get('mode') == 'login':
        return user_login(request)
    if request.POST.get('mode') != 'register':
        return render(request, 'ppas/form_error.html', {'error': 'Choose sign in or registration to continue.'}, status=400)
    return register(request)


@login_required
@require_GET
def dashboard(request):
    from service_year.views import dashboard as service_dashboard
    return service_dashboard(request)


@require_GET
def search_ppa(request):
    return dynamic_place_search_view(request)


@require_GET
def search_results(request):
    return dynamic_place_search_view(request)


def _normalise(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def _possible_matches(queryset, query):
    """Only examine a bounded, selected-LGA set; do not fuzzy-scan the country."""
    query = _normalise(query)
    matches = []
    for record in queryset.values('pk', 'name', 'aliases')[:300]:
        variants = [record['name'], *(record['aliases'] or '').split(',')]
        best = 0
        for variant in variants:
            norm = _normalise(variant)
            acronym = ''.join(word[0] for word in norm.split() if word not in ('of', 'the', 'and'))
            score = 1 if query == acronym else SequenceMatcher(None, query, norm).ratio()
            best = max(best, score)
        if best >= 0.72:
            matches.append((best, record['pk']))
    return [pk for _, pk in sorted(matches, reverse=True)[:25]]


@require_GET
def dynamic_place_search_view(request):
    params = request.GET.copy()
    if request.user.is_authenticated and 'state' not in params:
        params['state'] = str(request.user.state_posted_id or '')
        if 'lga' not in params:
            params['lga'] = str(request.user.lga_posted_id or '')
    form = SearchForm(params)
    context = {'search_form': form, 'states': State.objects.order_by('name'), 'lgas': [], 'places': [],
               'query': params.get('q', ''), 'selected_state': params.get('state', ''),
               'selected_lga': params.get('lga', ''), 'provider_error': '', 'possible_matches': False}
    if not form.is_valid():
        return render(request, 'ppas/directory.html', context, status=400)
    data = form.cleaned_data
    state = State.objects.filter(pk=data['state']).first() if data['state'] else None
    lga = LGA.objects.filter(pk=data['lga'], state=state).first() if data['lga'] and state else None
    if data['state'] and not state:
        form.add_error('state', 'Choose a valid state.')
    if data['lga'] and not lga:
        form.add_error('lga', 'Choose an LGA belonging to this state.')
    if form.errors:
        return render(request, 'ppas/directory.html', context, status=400)
    if state:
        context['lgas'] = LGA.objects.filter(state=state).order_by('name')
    results = PPA.objects.select_related('state', 'lga').annotate(confirmation_total=Count('confirmations')).order_by('name', 'pk')
    if state:
        results = results.filter(state=state)
    if lga:
        results = results.filter(lga=lga)
    if data['category']:
        results = results.filter(category=data['category'])
    scoped = results
    if data['q']:
        results = results.filter(Q(name__icontains=data['q']) | Q(aliases__icontains=data['q']))
        if lga and not results.exists():
            ids = _possible_matches(scoped.order_by('pk'), data['q'])
            if ids:
                results = scoped.filter(pk__in=ids)
                context['possible_matches'] = True
    elif not state:
        results = results.none()
    page = Paginator(results, 25).get_page(params.get('page'))
    page_params = params.copy()
    page_params.pop('page', None)
    page_params.pop('google', None)  # Turning a directory page never repeats a paid API call.
    context.update({'saved_ppas': page, 'ppas': page, 'page_obj': page,
                    'page_query': page_params.urlencode(), 'search_query': page_params.urlencode()})
    discover = params.get('google') == '1' or (params.get('find') == '1' and data['q'] and not page.paginator.count and 'page' not in params)
    if discover:
        if not data['q'] or not state or not lga:
            context['provider_error'] = 'Enter a PPA name and select a state and LGA before searching Google.'
        else:
            limited = check_rate_limit(request, 'places-search', 15, period=60)
            if limited is not None:
                context['provider_error'] = 'Search is busy. Please try again shortly; saved PPAs are still available.'
            else:
                try:
                    places = search_google_places(data['q'], state.name, lga.name)
                    for place in places:
                        status, message = assess_place_location(place, state.name, lga.name)
                        place.update(location_status=status, location_message=message)
                        place['preview_token'] = signing.dumps({'place': place.copy(), 'state': state.pk, 'lga': lga.pk, 'query': data['q']}, salt='place-preview', compress=True)
                        if request.user.is_authenticated:
                            place['token'] = signing.dumps({'user_id': request.user.pk, 'place': place}, salt='google-place', compress=True)
                    context['places'] = places
                    context['google_searched'] = True
                except PlacesUnavailable as exc:
                    context['provider_error'] = exc.message
    response = render(request, 'ppas/directory.html', context)
    if discover:
        response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@rate_limit('reviews', 20)
def ppa_detail(request, pk):
    ppa = get_object_or_404(PPA.objects.select_related('state', 'lga'), pk=pk)
    member_review = ppa.reviews.filter(user=request.user).first() if request.user.is_authenticated else None
    form = ReviewForm(request.POST if request.method == 'POST' else None, instance=member_review)
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.email_verified:
            messages.info(request, 'Verify your email before contributing reviews.')
            return redirect('profile')
        if form.is_valid():
            try:
                save_member_review(ppa=ppa, user=request.user, cleaned_data=form.cleaned_data)
            except ValidationError as error:
                if hasattr(error, 'message_dict'):
                    for field, errors in error.message_dict.items():
                        form.add_error(field if field in form.fields else None, errors)
                else:
                    form.add_error(None, error)
            else:
                messages.success(request, 'Your review has been saved.')
                return redirect('ppa_detail', pk=pk)
    reviews = Paginator(ppa.reviews.filter(is_public=True).select_related('user').order_by('-created_at', '-pk'), 20).get_page(request.GET.get('page'))
    context = {'ppa': ppa, 'reviews': reviews, 'page_obj': reviews, 'review_form': form,
               'directions_url': directions_url(ppa), 'walking_url': directions_url(ppa, 'walking'),
               'confirmation_total': ppa.confirmations.count(), 'bookmark_saved': False, 'member_review': member_review}
    if request.user.is_authenticated:
        context['bookmark_saved'] = ppa.bookmarks.filter(user=request.user).exists()
    context['profile_claims'] = get_public_profile_claims(ppa)
    from .submission_services import get_public_submission_provenance
    context['independent_provenance'] = get_public_submission_provenance(ppa)
    context['is_current_ppa'] = False
    if request.user.is_authenticated:
        from service_year.models import MemberPPA
        context['is_current_ppa'] = MemberPPA.objects.filter(user=request.user, ppa=ppa, ended_at__isnull=True).exists()
    context['navigation_config'] = _navigation_config(ppa, ppa_id=ppa.pk)
    return render(request, 'ppas/detail.html', context, status=400 if request.method == 'POST' and form.errors else 200)


def _navigation_config(place, **reference):
    destination = destination_data(place)
    if isinstance(place, dict):
        area = place.get('area', '')
    else:
        area = f'{place.lga.name}, {place.state.name}'
    return {
        'name': destination['name'], 'address': destination['address'], 'area': area,
        'destination': destination['map_destination'], 'external_url': directions_url(place),
        'browser_key': settings.GOOGLE_MAPS_BROWSER_API_KEY, 'summary_url': '/api/route/', **reference,
    }


@require_POST
@never_cache
def discovered_place(request):
    """Transient details preserve search → select → directions for guests too."""
    token = request.POST.get('preview_token', '')
    try:
        payload = signing.loads(token, salt='place-preview', max_age=900)
        place = payload['place']
        state = State.objects.get(pk=payload['state'])
        lga = LGA.objects.get(pk=payload['lga'], state=state)
        validation = LivePlaceForm({**place, 'state': str(state.pk), 'lga': str(lga.pk)})
        if not validation.is_valid() or not place.get('place_id'):
            raise ValueError('Invalid place')
    except (signing.BadSignature, KeyError, TypeError, ValueError, State.DoesNotExist, LGA.DoesNotExist):
        return render(request, 'ppas/form_error.html', {'error': 'This search result has expired. Search again to choose your PPA.'}, status=400)
    status, message = assess_place_location(place, state.name, lga.name)
    place.update(area=f'{lga.name}, {state.name}', lga=lga.name, state=state.name, location_status=status, location_message=message)
    context = {
        'place': place, 'preview_token': token,
        'navigation_config': _navigation_config(place, preview_token=token),
        'selected_state': str(state.pk), 'selected_lga': str(lga.pk),
        'query': payload.get('query', ''), 'back_query': urlencode({'state': state.pk, 'lga': lga.pk, 'q': payload.get('query', '')}),
    }
    if request.user.is_authenticated:
        context['place_token'] = signing.dumps({'user_id': request.user.pk, 'place': place}, salt='google-place', compress=True)
    return render(request, 'ppas/discovered_detail.html', context)


@require_GET
def load_lgas(request):
    value = request.GET.get('state', '')
    if not value:
        return JsonResponse([], safe=False)
    if not value.isascii() or not value.isdigit() or len(value) > 10 or not 0 < int(value) <= 2147483647:
        return JsonResponse({'error': 'Invalid state.'}, status=400)
    return JsonResponse(list(LGA.objects.filter(state_id=int(value)).order_by('name').values('id', 'name')), safe=False)


@login_required
@require_POST
@rate_limit('save-place', 10)
def save_live_place_view(request):
    if not request.user.email_verified:
        messages.info(request, 'Verify your email before adding a PPA.')
        return redirect('profile')
    try:
        payload = signing.loads(request.POST.get('place_token', ''), salt='google-place', max_age=1800)
        if payload['user_id'] != request.user.pk:
            raise signing.BadSignature('Wrong user')
        place = payload['place']
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return render(request, 'ppas/form_error.html', {'error': 'This selection has expired. Search again to select a place.'}, status=400)
    google_form = LivePlaceForm({**place, 'state': request.POST.get('state', ''), 'lga': request.POST.get('lga', '')})
    form = CommunityPlaceForm(request.POST)
    if not google_form.is_valid() or not form.is_valid() or not place.get('place_id'):
        return render(request, 'ppas/form_error.html', {'error': 'Check the PPA details and confirm its state and LGA.', 'form': form}, status=400)
    data = form.cleaned_data.copy()
    data.pop('community_details_confirmed')
    status, message = assess_place_location(place, data['state'].name, data['lga'].name)
    if status == 'mismatch':
        return render(request, 'ppas/form_error.html', {'error': message + ' Please choose the correct area and search again.'}, status=400)
    # Only the Place ID comes from Google. Long-lived names, addresses and notes
    # are independently supplied by the member, not populated from API content.
    defaults = {**data, 'verified': False, 'source': 'community', 'location_status': status, 'submitted_by': request.user}
    ppa, created = PPA.objects.get_or_create(place_id=place['place_id'], defaults=defaults)
    if not created and (ppa.state_id != data['state'].pk or ppa.lga_id != data['lga'].pk):
        messages.warning(request, 'This place is already listed in a different area. Report a correction if its location is wrong.')
    messages.success(request, 'PPA saved for the community.' if created else 'This PPA is already in the shared directory.')
    return redirect('ppa_detail', pk=ppa.pk)
