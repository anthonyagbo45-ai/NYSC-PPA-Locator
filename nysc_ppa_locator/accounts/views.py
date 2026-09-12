from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import RegistrationForm
from locations.models import State

def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
        
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.save()
            login(request, user)
            messages.success(request, 'Account created successfully ✓')
            return redirect('dashboard')
        else:
            messages.error(request, '⚠ Please correct the highlighted fields')
    else:
        form = RegistrationForm()
        
    return render(request, 'accounts/register.html', {'form': form})

def user_login(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
        
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            email = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(email=email, password=password)
            if user is not None:
                login(request, user)
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid email or password.')
        else:
            messages.error(request, 'Invalid email or password.')
    else:
        form = AuthenticationForm()
        
    return render(request, 'accounts/login.html', {'form': form})

def user_logout(request):
    logout(request)
    return redirect('landing')

@login_required
def dashboard(request):
    return render(request, 'accounts/dashboard.html', {'user': request.user})

@login_required
def profile(request):
    if request.method == 'POST':
        user = request.user
        user.full_name = request.POST.get('full_name', user.full_name)
        user.state_code = request.POST.get('state_code', user.state_code)
        
        state_id = request.POST.get('state_posted')
        lga_id = request.POST.get('lga_posted')
        
        if state_id:
            user.state_posted_id = state_id
        if lga_id:
            user.lga_posted_id = lga_id
            
        user.save()
        messages.success(request, 'Profile updated successfully ✓')
        return redirect('profile')
        
    states = State.objects.all().order_by('name')
    return render(request, 'accounts/profile.html', {'user': request.user, 'states': states})