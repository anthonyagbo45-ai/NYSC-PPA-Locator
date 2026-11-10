"""Participant-only HTML messaging, with explicit POST actions and no polling."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from . import services
from .forms import MessageForm, PreferenceForm, ReadForm, ReportForm, RespondForm


def _errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        form.add_error(None, error)


def _positive_int(value):
    value = str(value)
    if not value.isascii() or not value.isdigit() or len(value) > 19:
        raise Http404('Page unavailable.')
    result = int(value)
    if not 1 <= result <= 9223372036854775807:
        raise Http404('Page unavailable.')
    return result


def _thread_response(request, conversation_id, *, form=None, status=200):
    try:
        before = _positive_int(request.GET['before']) if 'before' in request.GET else None
    except Http404:
        return HttpResponseBadRequest('Choose a valid message page.')
    result = services.get_thread(request.user, conversation_id, before=before)
    conversation = dict(result['conversation'])
    conversation['is_incoming'] = not conversation['is_initiator']
    conversation['is_blocked'] = conversation['blocked']
    from photos.services import photo_send_enabled
    conversation['can_send_photo'] = photo_send_enabled(request.user, conversation_id)
    rows = [dict(row, is_own=row['is_mine']) for row in result['messages']]
    older_url = None
    if result['next_before']:
        older_url = reverse('messenger:thread', args=[conversation_id]) + '?before=' + str(result['next_before'])
    return render(request, 'messenger/thread.html', {
        'thread': conversation, 'message_rows': rows,
        'form': form if form is not None else MessageForm(),
        'older_url': older_url, 'latest_message_id': result['last_message_id'],
        'show_photo_previews': services.get_preferences(request.user).get('allow_photos', False),
    }, status=status)


@login_required
@never_cache
@require_GET
def inbox(request):
    folder = request.GET.get('folder', 'all')
    if folder not in ('all', 'pending', 'accepted', 'closed'):
        return HttpResponseBadRequest('Choose a valid message folder.')
    page = services.get_inbox(request.user, folder=folder, page=request.GET.get('page', 1))
    return render(request, 'messenger/inbox.html', {
        'rows': page.object_list, 'page_obj': page, 'folder': folder,
        'page_query': 'folder=' + folder,
        'preference': services.get_preferences(request.user),
    })


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('messenger-preferences', 20)
def preferences(request):
    initial = services.get_preferences(request.user)
    form = PreferenceForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_preferences(request.user, form.cleaned_data['allow_requests'], allow_photos=form.cleaned_data['allow_photos'])
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private message preference was saved.')
            return redirect('messenger:preferences')
    return render(request, 'messenger/preferences.html', {'form': form}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@verified_email_required
@rate_limit('messenger-request', 10)
def request_chat(request, context_type, context_id):
    form = MessageForm(request.POST if request.method == 'POST' else None)
    try:
        context = services.get_request_context(request.user, context_type, context_id)
    except ValidationError as error:
        return render(request, 'messenger/request.html', {
            'form': form, 'request_unavailable': '; '.join(error.messages),
            'context': {'title': 'Community discussion', 'public_name': 'Corps member', 'context_url': reverse('community:home')},
        }, status=400)
    if request.method == 'POST' and form.is_valid():
        try:
            conversation_id = services.request_chat(request.user, context_type, context_id, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your request was sent. Wait for acceptance before sending another message.')
            return redirect('messenger:thread', conversation_id=conversation_id)
    return render(request, 'messenger/request.html', {
        'form': form, 'context': {
            'title': context['context_title'], 'public_name': context['recipient_name'],
            'context_url': context.get('context_url', reverse('community:home')),
        },
    }, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def thread(request, conversation_id):
    return _thread_response(request, conversation_id)


@login_required
@never_cache
@require_POST
@verified_email_required
@rate_limit('messenger-send', 30)
def send(request, conversation_id):
    # Resolve access before validating data; malformed input cannot reveal a thread.
    services.get_thread(request.user, conversation_id, limit=1)
    form = MessageForm(request.POST)
    if form.is_valid():
        try:
            services.send_message(request.user, conversation_id, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            return redirect('messenger:thread', conversation_id=conversation_id)
    return _thread_response(request, conversation_id, form=form, status=400)


@login_required
@never_cache
@require_POST
@rate_limit('messenger-respond', 20)
def respond(request, conversation_id):
    services.get_thread(request.user, conversation_id, limit=1)
    form = RespondForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose accept or decline.')
    try:
        services.respond_request(request.user, conversation_id, accept=form.cleaned_data['action'] == 'accept')
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    return redirect('messenger:thread', conversation_id=conversation_id)


@login_required
@never_cache
@require_POST
@rate_limit('messenger-read', 60)
def read(request, conversation_id):
    services.get_thread(request.user, conversation_id, limit=1)
    form = ReadForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose a message in this conversation.')
    try:
        services.mark_read(request.user, conversation_id, form.cleaned_data['message_id'])
    except ValidationError as error:
        return HttpResponseBadRequest('; '.join(error.messages))
    return redirect('messenger:thread', conversation_id=conversation_id)


@login_required
@never_cache
@require_POST
@rate_limit('messenger-close', 20)
def close(request, conversation_id):
    services.close_conversation(request.user, conversation_id)
    messages.success(request, 'This conversation is closed. Its history remains available to you.')
    return redirect('messenger:thread', conversation_id=conversation_id)


@login_required
@never_cache
@require_POST
@rate_limit('messenger-block', 15)
def block(request, conversation_id):
    services.block_peer(request.user, conversation_id)
    messages.success(request, 'You blocked this member. New interactions are stopped; your private history remains available for reporting.')
    return redirect('messenger:thread', conversation_id=conversation_id)


@transaction.non_atomic_requests
@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('messenger-report', 10)
def report(request, conversation_id, message_id):
    selected = services.get_selected_message(request.user, conversation_id, message_id)
    form = ReportForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.report_message(request.user, conversation_id, message_id, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private message report is awaiting safety review.')
            return redirect('messenger:thread', conversation_id=conversation_id)
    return render(request, 'messenger/report.html', {
        'form': form, 'selectedmessage': selected, 'thread': {'id': conversation_id},
    }, status=400 if request.method == 'POST' else 200)
