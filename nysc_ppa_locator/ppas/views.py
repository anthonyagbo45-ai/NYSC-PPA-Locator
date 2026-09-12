from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from .models import PPA, Review
from locations.models import State, LGA

def landing(request):
    """Render the public home/landing page with states loaded for the dropdown."""
    states = State.objects.all().order_by('name')
    context = {
        'states': states,
    }
    return render(request, 'ppas/search.html', context)

@login_required
def search_ppa(request):
    states = State.objects.all().order_by('name')
    context = {
        'states': states,
        'default_state': request.user.state_posted,
        'default_lga': request.user.lga_posted,
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

        # Upsert: updates existing review by user or creates a new one
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