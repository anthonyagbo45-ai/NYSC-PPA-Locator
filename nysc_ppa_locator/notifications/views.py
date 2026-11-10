"""Owner-private calendars and opt-in in-app reminders; GET requests never write."""
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from config.security import rate_limit
from service_resources.models import ServiceNotice
from service_resources.services import notice_reminder_eligible
from .forms import ConfirmEventDeleteForm, ConfirmUnsubscribeForm, EventFilterForm, EventForm, InboxFilterForm, NoticeSubscriptionForm, PreferenceForm
from .models import Notification, NotificationPreference, NoticeSubscription, PersonalEvent
from . import services


EVENT_FIELDS = ('title', 'note', 'kind', 'starts_at', 'ends_at', 'timezone_name', 'remind_before', 'notifications_enabled', 'status')


def _form_errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


def _page_query(request):
    query = request.GET.copy()
    query.pop('page', None)
    return query.urlencode()


def _event_row(event):
    try:
        zone = ZoneInfo(event.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo('UTC')
    return {
        'id': event.pk, 'title': event.title, 'note': event.note,
        'kind_label': event.get_kind_display(), 'status_label': event.get_status_display(),
        'status': event.status, 'starts_at': timezone.localtime(event.starts_at, zone),
        'ends_at': timezone.localtime(event.ends_at, zone) if event.ends_at else None,
        'timezone_name': str(zone), 'remind_before_label': event.get_remind_before_display(),
        'notifications_enabled': event.notifications_enabled,
    }


def _preference_values(user):
    preference = NotificationPreference.objects.filter(user=user).first()
    return {
        'personal_reminders': bool(preference and preference.personal_reminders),
        'notice_reminders': bool(preference and preference.notice_reminders),
        'community_replies': bool(preference and preference.community_replies),
    }


@login_required
@never_cache
@require_GET
def home(request):
    data = request.GET.copy()
    data.setdefault('period', 'upcoming')
    form = EventFilterForm(data)
    events = PersonalEvent.objects.filter(user=request.user)
    if form.is_valid():
        period = form.cleaned_data.get('period') or 'upcoming'
        if period == 'upcoming':
            events = events.filter(starts_at__gte=timezone.now())
        elif period == 'past':
            events = events.filter(starts_at__lt=timezone.now())
        if form.cleaned_data.get('status'):
            events = events.filter(status=form.cleaned_data['status'])
        events = events.order_by('-starts_at', '-pk') if period == 'past' else events.order_by('starts_at', 'pk')
    else:
        events = events.none()
    page = Paginator(events, 20).get_page(request.GET.get('page'))
    page.object_list = [_event_row(event) for event in page.object_list]
    return render(request, 'notifications/home.html', {
        'form': form, 'events': page, 'page_obj': page, 'page_query': _page_query(request),
        'preferences': _preference_values(request.user),
        'subscriptions': services.subscription_rows(request.user),
    }, status=200 if form.is_valid() else 400)


def _event_form_view(request, event=None):
    initial = {'kind': PersonalEvent.Kind.PERSONAL, 'status': PersonalEvent.Status.ACTIVE, 'timezone_name': 'Africa/Lagos', 'event_date': timezone.localdate(), 'remind_before': 60}
    if event is not None:
        row = _event_row(event)
        initial = {name: getattr(event, name) for name in ('title', 'note', 'kind', 'status', 'timezone_name', 'remind_before', 'notifications_enabled')}
        initial.update(event_date=row['starts_at'].date(), event_time=row['starts_at'].time())
        if row['ends_at']:
            initial.update(end_date=row['ends_at'].date(), end_time=row['ends_at'].time())
    form = EventForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_event(request.user, cleaned_data={name: form.cleaned_data[name] for name in EVENT_FIELDS}, event_id=event.pk if event else None)
        except ValidationError as error:
            _form_errors(form, error)
        else:
            messages.success(request, 'Your private event was saved. It is a personal plan, not an official NYSC decision.')
            return redirect('notifications:home')
    return render(request, 'notifications/event_form.html', {
        'form': form, 'editing': event is not None, 'preferences': _preference_values(request.user),
    }, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('personal-event-add', 30)
def event_add(request):
    return _event_form_view(request)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('personal-event-edit', 30)
def event_edit(request, pk):
    event = get_object_or_404(PersonalEvent, pk=pk, user=request.user)
    return _event_form_view(request, event)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('personal-event-delete', 30)
def event_delete(request, pk):
    event = get_object_or_404(PersonalEvent, pk=pk, user=request.user)
    form = ConfirmEventDeleteForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        services.delete_event(request.user, event_id=event.pk)
        messages.success(request, 'Your private event and its reminders were removed.')
        return redirect('notifications:home')
    return render(request, 'notifications/event_delete.html', {'event': _event_row(event), 'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('reminder-preferences', 20)
def preferences(request):
    form = PreferenceForm(request.POST if request.method == 'POST' else None, initial=_preference_values(request.user))
    if request.method == 'POST' and form.is_valid():
        services.save_preferences(request.user, **form.cleaned_data)
        messages.success(request, 'Your reminder choices were saved. No email or text messages are sent.')
        return redirect('notifications:preferences')
    return render(request, 'notifications/preferences.html', {'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def inbox(request):
    form = InboxFilterForm(request.GET)
    rows = services.visible_notifications(request.user)
    if form.is_valid():
        selected = form.cleaned_data.get('status') or 'all'
        if selected == 'unread':
            rows = [row for row in rows if not row['is_read']]
        elif selected == 'read':
            rows = [row for row in rows if row['is_read']]
    else:
        rows = []
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    return render(request, 'notifications/inbox.html', {
        'form': form, 'notifications': page, 'page_obj': page, 'page_query': _page_query(request),
        'unread_count': services.unread_count(request.user), 'preferences': _preference_values(request.user),
    }, status=200 if form.is_valid() else 400)


@login_required
@never_cache
@require_POST
@rate_limit('check-my-reminders', 10)
def check_due(request):
    services.dispatch_user_reminders(request.user, limit=100)
    messages.info(request, 'Your enabled reminders have been checked. Only current reminders you chose can appear here.')
    return redirect('notifications:inbox')


@login_required
@never_cache
@require_POST
@rate_limit('read-reminder', 60)
def read(request, pk):
    get_object_or_404(Notification, pk=pk, user=request.user)
    try:
        services.mark_read(request.user, notification_id=pk)
    except ValidationError:
        messages.info(request, 'This reminder is no longer current.')
    return redirect('notifications:inbox')


@login_required
@never_cache
@require_POST
@rate_limit('dismiss-reminder', 60)
def dismiss(request, pk):
    get_object_or_404(Notification, pk=pk, user=request.user)
    try:
        services.dismiss_notification(request.user, notification_id=pk)
    except ValidationError:
        messages.info(request, 'This reminder is no longer current.')
    return redirect('notifications:inbox')


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('follow-source-notice', 20)
def subscribe_notice(request, notice_id):
    notice = get_object_or_404(ServiceNotice.objects.select_related('group', 'state', 'lga'), pk=notice_id)
    if not notice_reminder_eligible(notice):
        raise Http404('This notice is not eligible for a confirmed-source reminder.')
    form = NoticeSubscriptionForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.subscribe_notice(request.user, notice=notice, remind_before=form.cleaned_data['remind_before'])
        except ValidationError as error:
            _form_errors(form, error)
        else:
            messages.success(request, 'You are following this confirmed notice. Your notice-reminder preference also controls delivery.')
            return redirect('notifications:home')
    return render(request, 'notifications/subscribe_notice.html', {
        'form': form, 'notice': {'id': notice.pk, 'title': notice.title, 'event_at': notice.event_at, 'timezone_name': notice.timezone_name},
        'preferences': _preference_values(request.user),
    }, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_POST
@rate_limit('unfollow-source-notice', 20)
def unsubscribe_notice(request, subscription_id):
    get_object_or_404(NoticeSubscription, pk=subscription_id, user=request.user)
    form = ConfirmUnsubscribeForm(request.POST)
    if not form.is_valid():
        return render(request, 'notifications/unsubscribe_notice.html', {'form': form, 'subscription_id': subscription_id}, status=400)
    services.unsubscribe_notice(request.user, subscription_id=subscription_id)
    messages.success(request, 'Reminders for that followed notice were stopped.')
    return redirect('notifications:home')
