"""Explicit, authorised private-photo uploads and binary responses."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from config.security import rate_limit
from messenger import services as messenger
from . import services
from .forms import PhotoForm


def _jpeg_response(result, *, reported=False):
    response = FileResponse(
        result['file'], content_type='image/jpeg',
        filename='reported-photo.jpg' if reported else 'conversation-photo.jpg',
        as_attachment=False,
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cross-Origin-Resource-Policy'] = 'same-origin'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@transaction.non_atomic_requests
@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('chat-photo-upload', 15)
def upload(request, conversation_id):
    # Resolve membership before showing a form or validating an upload.
    conversation = messenger.get_thread(request.user, conversation_id, limit=1)['conversation']
    if not services.photo_send_enabled(request.user, conversation_id):
        raise PermissionDenied('Photo sharing is unavailable. Both members must enable photos in an accepted conversation.')
    form = PhotoForm(request.POST if request.method == 'POST' else None, request.FILES if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.send_photo(
                request.user, conversation_id, upload=form.cleaned_data['image'],
                caption=form.cleaned_data['caption'], client_token=form.cleaned_data['client_token'],
                rights_confirmed=form.cleaned_data['rights_confirmed'],
            )
        except ValidationError as error:
            if hasattr(error, 'message_dict'):
                for name, errors in error.message_dict.items():
                    for message in errors:
                        form.add_error(name if name in form.fields else None, message)
            else:
                form.add_error(None, error)
        else:
            messages.success(request, 'Your photo was sent inside this private conversation.')
            return redirect('messenger:thread', conversation_id=conversation_id)
    return render(request, 'photos/upload.html', {'form': form, 'thread': conversation}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
@rate_limit('chat-photo-view', 180, methods=('GET',))
def content(request, photo_id):
    return _jpeg_response(services.open_photo(request.user, photo_id))


@login_required
@never_cache
@require_GET
@rate_limit('chat-photo-report-view', 60, methods=('GET',))
def reported_content(request, report_id):
    return _jpeg_response(services.open_report_evidence_file(request.user, report_id), reported=True)
