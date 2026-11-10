from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from config.security import rate_limit
from ppas.models import PPA
from .forms import AssignmentConfirmationForm, EndAssignmentForm, ProgressForm, ServiceProfileForm
from .models import MemberPPA, ServiceProfile
from .services import assign_ppa as save_assignment, dashboard_context, end_assignment as archive_assignment, save_service_profile, update_progress


@login_required
@never_cache
@require_GET
def dashboard(request):
    from django.utils import timezone
    from notifications.services import dashboard_summary
    from service_resources.models import CDSGroup, CDSPreference
    from service_resources.services import safe_group

    context = dashboard_context(request.user)
    context['reminder_summary'] = dashboard_summary(request.user)
    preference = CDSPreference.objects.select_related('group__state', 'group__lga').filter(user=request.user).first()
    context['selected_cds_group'] = None
    if preference and preference.group_id:
        group = preference.group
        context['selected_cds_group'] = (
            {'available': True, 'group': safe_group(group)}
            if group.publication_status == CDSGroup.Publication.APPROVED and group.expires_at > timezone.now()
            else {'available': False}
        )
    return render(request, 'service_year/dashboard.html', context)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('assigned-ppa-save', 15)
def assign_ppa(request, ppa_id):
    ppa = get_object_or_404(PPA.objects.select_related('state', 'lga'), pk=ppa_id)
    current = MemberPPA.objects.filter(user=request.user, ended_at__isnull=True).select_related('ppa').first()
    form = AssignmentConfirmationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        _, created = save_assignment(request.user, ppa)
        messages.success(request, 'Your PPA was saved to your private tracker.' if created else 'This is already your current PPA.')
        return redirect('service_year_dashboard')
    return render(request, 'service_year/assign.html', {'ppa': ppa, 'current_assignment': current, 'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def assignment_history(request):
    assignments = MemberPPA.objects.filter(user=request.user).select_related('ppa__state', 'ppa__lga')
    page = Paginator(assignments, 20).get_page(request.GET.get('page'))
    return render(request, 'service_year/history.html', {'page_obj': page})


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('assigned-ppa-progress', 30)
def assignment_update(request, pk):
    assignment = get_object_or_404(MemberPPA.objects.select_related('ppa__state', 'ppa__lga'), pk=pk, user=request.user)
    form = ProgressForm(request.POST if request.method == 'POST' else None, instance=assignment)
    if request.method == 'POST' and form.is_valid():
        try:
            update_progress(request.user, assignment.pk, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'Your private progress was updated.')
            return redirect('assignment_update', pk=assignment.pk)
    changes = assignment.status_history.order_by('-created_at', '-pk')[:20]
    return render(request, 'service_year/progress.html', {'assignment': assignment, 'form': form, 'end_form': EndAssignmentForm(), 'changes': changes}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_POST
@rate_limit('assigned-ppa-end', 15)
def end_assignment(request, pk):
    # Check ownership even when the submitted confirmation is invalid.
    assignment = get_object_or_404(MemberPPA, pk=pk, user=request.user)
    form = EndAssignmentForm(request.POST)
    if form.is_valid():
        archive_assignment(request.user, assignment.pk)
        messages.success(request, 'This PPA is now in your private history.')
        return redirect('service_year_dashboard')
    return render(request, 'service_year/end.html', {'assignment': assignment, 'form': form}, status=400)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-profile', 15)
def service_profile(request):
    profile = ServiceProfile.objects.filter(user=request.user).first()
    form = ServiceProfileForm(request.POST if request.method == 'POST' else None, instance=profile)
    if request.method == 'POST' and form.is_valid():
        save_service_profile(request.user, **form.cleaned_data)
        messages.success(request, 'Your private service preferences were saved.')
        return redirect('service_year_dashboard')
    return render(request, 'service_year/profile.html', {'form': form}, status=400 if request.method == 'POST' else 200)
