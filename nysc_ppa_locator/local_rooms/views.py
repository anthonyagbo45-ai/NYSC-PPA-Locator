"""Free public discovery and explicitly approved, private room access."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from notifications.models import NotificationPreference
from ppas.models import PPA
from service_resources.models import CDSGroup
from . import services
from .forms import ArchivePostForm, JoinRoomForm, LeaveRoomForm, RoomFilterForm, RoomPostForm, RoomPreferenceForm, RoomReportForm, positive_id
from .models import Room, RoomMembership, RoomPost


def _errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


def _page_query(request):
    values = request.GET.copy()
    values.pop('page', None)
    return values.urlencode()


def _page(queryset, request, project):
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    page.object_list = [project(item) for item in page.object_list]
    # Keep no second raw model path through the paginator in template context.
    page.paginator.object_list = page.object_list
    return page


def _room_context(room, user):
    if room.status == Room.Status.ARCHIVED:
        # Only owner-scoped leave/preferences actions reach archived rooms.
        return {'id': room.pk, 'title': room.title, 'status': room.status, 'status_label': room.get_status_display()}
    return services.safe_public_room(room, user=user)


def _community_notifications_enabled(user):
    return bool(user.is_authenticated and NotificationPreference.objects.filter(user=user, community_replies=True, community_enabled_at__isnull=False).exists())


def _post_for_owner(request, pk):
    post = get_object_or_404(RoomPost.objects.select_related('room', 'room__state', 'room__lga', 'room__ppa', 'room__group', 'author'), pk=pk, author=request.user)
    if not services.can_read_room(post.room, request.user):
        raise Http404
    return post


@require_GET
def home(request):
    values = request.GET.copy()
    # A context link selects locality without treating it as a membership claim.
    for field, model in [('ppa', PPA), ('group', CDSGroup)]:
        identifier = positive_id(values.get(field))
        queryset = model.objects.all()
        if field == 'group':
            queryset = queryset.filter(publication_status=CDSGroup.Publication.APPROVED, expires_at__gt=timezone.now())
        context = queryset.filter(pk=identifier).values('state_id', 'lga_id').first() if identifier else None
        if context:
            values.setdefault('state', context['state_id'])
            values.setdefault('lga', context['lga_id'])
    form = RoomFilterForm(values)
    rooms = services.public_rooms(user=request.user)
    valid = form.is_valid()
    if valid:
        data = form.cleaned_data
        for field in ('state', 'lga', 'visibility'):
            if data.get(field):
                rooms = rooms.filter(**{field: data[field]})
        for field in ('ppa', 'group'):
            if data.get(field):
                rooms = rooms.filter(**{field + '_id': data[field]})
        if data.get('q'):
            rooms = rooms.filter(Q(title__icontains=data['q']) | Q(description__icontains=data['q']))
    else:
        rooms = rooms.none()
    page = _page(rooms.order_by('title', 'pk'), request, lambda room: _room_context(room, request.user))
    return render(request, 'local_rooms/home.html', {'form': form, 'rooms': page, 'page_obj': page, 'page_query': _page_query(request)}, status=200 if valid else 400)


@never_cache
@require_GET
def room_detail(request, pk):
    room = services.get_room(pk)
    row = _room_context(room, request.user)
    posts = _page(services.room_posts(room=room, user=request.user), request, lambda post: services.safe_public_post(post, user=request.user)) if row['can_read'] else None
    return render(request, 'local_rooms/detail.html', {'room': row, 'posts': posts, 'page_obj': posts, 'page_query': _page_query(request), 'community_notifications_enabled': _community_notifications_enabled(request.user)})


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-join', 15)
def request_join(request, pk):
    room = services.get_room(pk)
    row = _room_context(room, request.user)
    if request.method == 'GET' and not row['can_request_join']:
        messages.info(request, 'A new request is unavailable. Check your membership and the current room state below.')
        return redirect('local_rooms:room_detail', pk=pk)
    form = JoinRoomForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            membership = services.request_membership(room=room, user=request.user)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your membership is already approved.' if membership.status == RoomMembership.Status.APPROVED else 'Your request is awaiting a moderator. Room membership has not been granted.')
            return redirect('local_rooms:room_detail', pk=pk)
    return render(request, 'local_rooms/action.html', {'room': row, 'form': form, 'action_title': 'Request room membership', 'button_label': 'Request moderator approval', 'explanation': 'Every member needs explicit approval. Your PPA, state and private CDS preference do not verify room membership.'}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-leave', 20)
def leave(request, pk):
    membership = get_object_or_404(RoomMembership.objects.select_related('room', 'room__state', 'room__lga', 'room__ppa', 'room__group'), room_id=pk, user=request.user)
    room = membership.room
    form = LeaveRoomForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.leave_room(room=room, user=request.user)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'You have left or cancelled your request. Member-only access and room notifications are now off.')
            return redirect('local_rooms:my_rooms')
    return render(request, 'local_rooms/action.html', {'room': _room_context(room, request.user), 'form': form, 'action_title': 'Leave room or cancel request', 'button_label': 'Confirm leaving', 'explanation': 'Leaving immediately removes access to member-only discussions and disables room notifications. A public discussion remains readable to visitors.'}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-preferences', 20)
def preferences(request, pk):
    membership = get_object_or_404(RoomMembership.objects.select_related('room', 'room__state', 'room__lga', 'room__ppa', 'room__group'), room_id=pk, user=request.user)
    form = RoomPreferenceForm(request.POST if request.method == 'POST' else None, initial={'notifications_enabled': membership.notifications_enabled})
    if request.method == 'POST' and form.is_valid():
        try:
            services.set_membership_notifications(room=membership.room, user=request.user, enabled=form.cleaned_data['notifications_enabled'])
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your room notification choice was saved.')
            return redirect('local_rooms:my_rooms')
    return render(request, 'local_rooms/action.html', {
        'room': _room_context(membership.room, request.user), 'form': form,
        'action_title': 'Room notification choice', 'button_label': 'Save notification choice',
        'explanation': 'Room notifications are voluntary and off by default. Enable community notifications in My reminders as well to receive in-app updates.',
        'notification_settings': True,
        'community_notifications_enabled': _community_notifications_enabled(request.user),
    }, status=400 if request.method == 'POST' else 200)


def _post_form(request, room, post=None):
    if not services.can_read_room(room, request.user):
        raise Http404
    if not services.can_post_room(room, request.user):
        raise PermissionDenied('Posting requires an active room and your explicitly approved membership.')
    initial = {'body': post.body, 'source_url': post.source_url, 'expected_version': post.version} if post else {}
    form = RoomPostForm(request.POST if request.method == 'POST' else None, initial=initial, editing=post is not None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_post(room=room, user=request.user, cleaned_data={name: form.cleaned_data[name] for name in ('body', 'source_url')}, post=post, expected_version=form.cleaned_data.get('expected_version'))
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your discussion post was saved.')
            return redirect('local_rooms:room_detail', pk=room.pk)
    return render(request, 'local_rooms/post_form.html', {'room': _room_context(room, request.user), 'form': form, 'editing': post is not None}, status=400 if request.method == 'POST' else 200)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-post-add', 20)
def post_add(request, pk):
    return _post_form(request, services.get_room(pk))


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-post-edit', 20)
def post_edit(request, pk):
    post = _post_for_owner(request, pk)
    return _post_form(request, post.room, post)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-post-archive', 20)
def post_archive(request, pk):
    post = _post_for_owner(request, pk)
    form = ArchivePostForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.archive_post(post=post, user=request.user)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your discussion post was withdrawn.')
            return redirect('local_rooms:my_rooms')
    return render(request, 'local_rooms/action.html', {'room': _room_context(post.room, request.user), 'form': form, 'action_title': 'Withdraw discussion post', 'button_label': 'Withdraw my post', 'explanation': 'This removes your post from the discussion. Private edit history remains available only while you have access to the room.'}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def post_history(request, pk):
    post = _post_for_owner(request, pk)
    revisions = services.owner_post_revisions(post=post, user=request.user)
    page = _page(revisions, request, lambda row: dict(row, status_label=dict(RoomPost.Status.choices).get(row.get('snapshot', {}).get('status'), 'Status not recorded')))
    return render(request, 'local_rooms/history.html', {'room': _room_context(post.room, request.user), 'revisions': page, 'page_obj': page, 'page_query': _page_query(request)})


def _report(request, room, post=None):
    form = RoomReportForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.submit_report(user=request.user, category=form.cleaned_data['category'], details=form.cleaned_data['details'], room=None if post else room, post=post)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private report was sent for moderator review.')
            return redirect('local_rooms:my_reports')
    return render(request, 'local_rooms/action.html', {'room': _room_context(room, request.user), 'form': form, 'action_title': 'Report discussion post' if post else 'Report room', 'button_label': 'Submit private report', 'explanation': 'Your report is private to you and the moderation team. A report does not establish that a claim is correct.'}, status=400 if request.method == 'POST' else 200)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-report', 15)
def report_room(request, pk):
    return _report(request, services.get_room(pk))


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('room-post-report', 15)
def report_post(request, pk):
    post = get_object_or_404(RoomPost.objects.select_related('room', 'room__state', 'room__lga', 'room__ppa', 'room__group', 'author'), pk=pk)
    if not services.can_read_room(post.room, request.user):
        raise Http404
    if not services.room_posts(room=post.room, user=request.user).filter(pk=pk).exists():
        raise Http404
    return _report(request, post.room, post)


@login_required
@never_cache
@require_GET
def my_rooms(request):
    page = _page(services.my_memberships(request.user), request, lambda membership: services.safe_owner_membership(membership, user=request.user))
    return render(request, 'local_rooms/mine.html', {'memberships': page, 'page_obj': page, 'page_query': _page_query(request), 'community_notifications_enabled': _community_notifications_enabled(request.user)})


@login_required
@never_cache
@require_GET
def my_reports(request):
    page = _page(services.my_reports(request.user), request, lambda report: services.safe_owner_report(report, user=request.user))
    return render(request, 'local_rooms/reports.html', {'reports': page, 'page_obj': page, 'page_query': _page_query(request)})
