import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.conf import settings
from .models import PPA, Review
from locations.models import State, LGA

User = get_user_model()

def landing(request):
    """Render the public home/landing page with states loaded for the dropdown."""
    states = State.objects.all().order_by('name')
    context = {
        'states': states,
    }
    return render(request, 'ppas/search.html', context)

def portal_entry(request):
    """Handle corps member sign-in and registration from the landing form."""
    if request.method == 'GET':
        state_code = request.GET.get('state_code')
        password = request.GET.get('password')
        full_name = request.GET.get('full_name')
        email = request.GET.get('email')
        state_id = request.GET.get('state')
        lga_id = request.GET.get('lga')

        if not state_code or not password:
            messages.error(request, "State code and password are required.")
            return redirect('landing')

        # Try to authenticate as an existing user
        user = authenticate(request, username=state_code, password=password)
        
        if user is not None:
            login(request, user)
        else:
            try:
                user = User.objects.get(state_code=state_code)
                messages.error(request, "Invalid password for this state code.")
                return redirect('landing')
            except User.DoesNotExist:
                state_obj = State.objects.filter(id=state_id).first() if state_id else None
                lga_obj = LGA.objects.filter(id=lga_id).first() if lga_id else None

                user = User.objects.create_user(
                    state_code=state_code,
                    email=email or '',
                    password=password,
                    first_name=full_name or ''
                )
                
                if hasattr(user, 'state_posted'):
                    user.state_posted = state_obj
                if hasattr(user, 'lga_posted'):
                    user.lga_posted = lga_obj
                user.save()

                login(request, user)

        return redirect('dashboard')
        
    return redirect('landing')

@login_required
def dashboard(request):
    """Render the main user dashboard (Step 1)."""
    context = {
        'user': request.user,
    }
    return render(request, 'ppas/dashboard.html', context)

@login_required
def search_ppa(request):
    states = State.objects.all().order_by('name')
    context = {
        'states': states,
        'default_state': getattr(request.user, 'state_posted', None),
        'default_lga': getattr(request.user, 'lga_posted', None),
    }
    return render(request, 'ppas/search.html', context)

@login_required
def search_results(request):
    query = request.GET.get('q', '')
    state_id = request.GET.get('state')
    lga_id = request.GET.get('lga')
    category = request.GET.get('category')

    results = PPA.objects.select_related('state', 'lga').all()

    if query:
        results = results.filter(name__icontains=query)
    if state_id:
        results = results.filter(state_id=state_id)
    if lga_id:
        results = results.filter(lga_id=lga_id)
    if category:
        results = results.filter(category=category)

    return render(request, 'ppas/results.html', {
        'ppas': results,
        'query': query,
        'selected_state': State.objects.filter(id=state_id).first() if state_id else None,
        'selected_lga': LGA.objects.filter(id=lga_id).first() if lga_id else None,
    })

def ppa_detail(request, pk):
    ppa = get_object_or_404(PPA.objects.select_related('state', 'lga'), pk=pk)

    if request.method == 'POST':
        if not request.user.is_authenticated:
            messages.error(request, 'You must be logged in to submit a review.')
            return redirect('login')

        rating = request.POST.get('rating')
        comment = request.POST.get('comment', '').strip()

        if not rating or not comment:
            messages.error(request, 'Please provide both a star rating and a comment.')
            return redirect('ppa_detail', pk=pk)

        try:
            rating_val = int(rating)
            if not (1 <= rating_val <= 5):
                raise ValueError("Out of range")
        except ValueError:
            messages.error(request, 'Invalid rating submitted.')
            return redirect('ppa_detail', pk=pk)

        review, created = Review.objects.update_or_create(
            ppa=ppa,
            user=request.user,
            defaults={
                'rating': rating_val,
                'comment': comment,
            }
        )

        if created:
            messages.success(request, 'Your review has been posted ✓')
        else:
            messages.success(request, 'Your review has been updated ✓')

        return redirect('ppa_detail', pk=pk)

    reviews = ppa.reviews.select_related('user').order_by('-created_at')

    return render(request, 'ppas/detail.html', {
        'ppa': ppa,
        'reviews': reviews,
    })

# API Endpoint for dynamic LGA loading (Vanilla JS Fetch)
def load_lgas(request):
    state_id = request.GET.get('state')
    if not state_id:
        return JsonResponse([], safe=False)
    lgas = LGA.objects.filter(state_id=state_id).order_by('name')
    return JsonResponse(list(lgas.values('id', 'name')), safe=False)

# Google Places API (New) Integration with optional LGA filtering
def search_google_places(query, state_name="Nigeria", lga_name=""):
    """
    Searches Google Places API (New) for institutions, offices, and businesses in Nigeria.
    """
    api_key = settings.GOOGLE_MAPS_API_KEY
    if not api_key:
        print("DEBUG: Google Maps API key is missing from settings.")
        return []

    if lga_name:
        search_query = f"{query}, {lga_name}, {state_name}, Nigeria"
    else:
        search_query = f"{query}, {state_name}, Nigeria"
        
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': api_key,
        'X-Goog-FieldMask': 'places.id,places.displayName,places.formattedAddress,places.location,places.googleMapsUri'
    }
    payload = {'textQuery': search_query}

    try:
        response = requests.post(url, headers=headers, json=payload)
        data = response.json()
        
        places_list = data.get('places', [])
        results = []
        for place in places_list:
            display_name = place.get('displayName', {}).get('text', '')
            address = place.get('formattedAddress', '')
            location = place.get('location', {})
            place_id = place.get('id', '')
            google_maps_uri = place.get('googleMapsUri', '')
            
            if not google_maps_uri and place_id:
                google_maps_uri = f"https://www.google.com/maps/search/?api=1&query=Google&query_place_id={place_id}"

            results.append({
                'name': display_name,
                'address': address,
                'latitude': location.get('latitude'),
                'longitude': location.get('longitude'),
                'place_id': place_id,
                'google_maps_url': google_maps_uri,
            })
        return results
    except Exception as e:
        print(f"Google Places API Exception: {e}")
    
    return []

@login_required
def save_live_place_view(request):
    """
    Saves selected Google Places result with State, LGA, and GPS coordinates,
    then transitions directly to Step 3 (PPA Detail View).
    """
    if request.method == 'POST':
        name = request.POST.get('name')
        address = request.POST.get('address')
        state_id = request.POST.get('state')
        lga_id = request.POST.get('lga')
        place_id = request.POST.get('place_id')
        latitude = request.POST.get('latitude')
        longitude = request.POST.get('longitude')
        google_maps_url = request.POST.get('google_maps_url')

        if not name or not address:
            messages.error(request, "Invalid place data submitted.")
            return redirect('live_search')

        if not google_maps_url:
            if place_id:
                google_maps_url = f"https://www.google.com/maps/search/?api=1&query=Google&query_place_id={place_id}"
            else:
                google_maps_url = f"https://www.google.com/maps/search/?q={requests.utils.quote(name)}"

        # Resolve State with fallback to user's registered state
        state_obj = None
        if state_id:
            if str(state_id).isdigit():
                state_obj = State.objects.filter(id=int(state_id)).first()
            else:
                state_obj = State.objects.filter(name__iexact=state_id).first()
        if not state_obj and hasattr(request.user, 'state_posted'):
            state_obj = request.user.state_posted
        if not state_obj:
            state_obj = State.objects.first()

        # Resolve LGA with fallback to user's registered LGA or state default to satisfy NOT NULL constraint
        lga_obj = LGA.objects.filter(id=lga_id).first() if lga_id else None
        if not lga_obj and hasattr(request.user, 'lga_posted'):
            lga_obj = request.user.lga_posted
        if not lga_obj and state_obj:
            lga_obj = LGA.objects.filter(state=state_obj).first()

        ppa, created = PPA.objects.get_or_create(
            name=name,
            state=state_obj,
            lga=lga_obj,
            defaults={
                'address': address,
                'category': 'Public Sector / Organization',
                'description': f'Discovered via Google Places Live Search (Place ID: {place_id})',
                'verified': True,
                'latitude': float(latitude) if latitude else None,
                'longitude': float(longitude) if longitude else None,
                'google_maps_url': google_maps_url,
                'phone': 'N/A',  
            }
        )

        if created:
            messages.success(request, f"Successfully saved '{name}' to your PPA database! ✓")
        else:
            messages.info(request, f"'{name}' already exists in your database.")

        # Transition directly to Step 3 (PPA Detail View)
        return redirect('ppa_detail', pk=ppa.pk)
        
    return redirect('live_search')

def dynamic_place_search_view(request):
    query = request.GET.get('q', '')
    state_param = request.GET.get('state', '')
    lga_id = request.GET.get('lga', '')
    places = []

    states = State.objects.all().order_by('name')

    # Handle state_param whether it's sent as a numeric ID or text name (e.g. 'Benue')
    state_obj = None
    if state_param:
        if str(state_param).isdigit():
            state_obj = State.objects.filter(id=int(state_param)).first()
        else:
            state_obj = State.objects.filter(name__iexact=state_param).first()

    if not state_obj:
        state_obj = states.first()

    state_name = state_obj.name if state_obj else "Nigeria"
    state_id = str(state_obj.id) if state_obj else ""

    lga_obj = LGA.objects.filter(id=lga_id).first() if lga_id else None
    lga_name = lga_obj.name if lga_obj else ""

    if query:
        places = search_google_places(query, state_name=state_name, lga_name=lga_name)

    # Retrieve saved PPAs from shared database matching the logged-in user's registered State and LGA strictly
    saved_ppas = []
    if request.user.is_authenticated:
        user_state = getattr(request.user, 'state_posted', None)
        user_lga = getattr(request.user, 'lga_posted', None)
        if user_state and user_lga:
            saved_ppas = PPA.objects.filter(state=user_state, lga=user_lga).select_related('state', 'lga')

    context = {
        'query': query,
        'states': states,
        'places': places,
        'saved_ppas': saved_ppas,
        'selected_state': state_id,
        'selected_lga': lga_id,
    }
    return render(request, 'ppas/live_search.html', context)