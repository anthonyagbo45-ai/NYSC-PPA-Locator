"""Private community inbox. All delivery and state changes require POST."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from config.security import rate_limit
from . import community_services as services
from .forms import InboxFilterForm
from .views import _page_query, _preference_values


@login_required
@never_cache
@require_GET
def inbox(request):
    form = InboxFilterForm(request.GET)
    form.fields['status'].choices = [('all', 'All current community alerts'), ('unread', 'Unread'), ('read', 'Read')]
    rows = services.community_items(request.user)
    if form.is_valid():
        selected = form.cleaned_data.get('status') or 'all'
        if selected == 'unread':
            rows = [row for row in rows if not row['is_read']]
        elif selected == 'read':
            rows = [row for row in rows if row['is_read']]
    else:
        rows = []
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    return render(request, 'notifications/community_inbox.html', {
        'form': form, 'notifications': page, 'page_obj': page, 'page_query': _page_query(request),
        'unread_count': services.community_unread_count(request.user), 'preferences': _preference_values(request.user),
    }, status=200 if form.is_valid() else 400)


@login_required
@never_cache
@require_POST
@rate_limit('check-my-community-alerts', 10)
def check_due(request):
    services.dispatch_user_community(request.user, limit=100)
    messages.info(request, 'Your chosen community alerts have been checked.')
    return redirect('community_notifications:inbox')


@login_required
@never_cache
@require_POST
@rate_limit('read-community-alert', 60)
def read(request, pk):
    services.mark_community_read(request.user, pk)
    return redirect('community_notifications:inbox')


@login_required
@never_cache
@require_POST
@rate_limit('dismiss-community-alert', 60)
def dismiss(request, pk):
    services.dismiss_community_notification(request.user, pk)
    return redirect('community_notifications:inbox')
