"""Route-scoped multipart limits, installed before CSRF accesses request.POST."""
import re

from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponse

from config.security import check_rate_limit

from .processing import MAX_INPUT_BYTES


MAX_REQUEST_BYTES = 9 * 1024 * 1024
UPLOAD_PATH = re.compile(r'^/photos/conversations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/add/$')
LIMIT_MESSAGE = 'Upload one photo no larger than 8 MiB.'


class BoundedPhotoUploadHandler(FileUploadHandler):
    """Count actual decoded file chunks before memory or temporary-file handlers."""
    chunk_size = 64 * 1024

    def __init__(self, request):
        super().__init__(request)
        self.files_seen = 0
        self.bytes_seen = 0

    def _reject(self):
        self.request._photo_upload_rejected = True
        # Django closes the in-progress temporary file and consumes the rest
        # without retaining it. Middleware then prevents the view from running.
        raise StopUpload(connection_reset=False)

    def new_file(self, *args, **kwargs):
        self.files_seen += 1
        if self.files_seen > 1:
            self._reject()
        super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.bytes_seen += len(raw_data)
        if self.bytes_seen > MAX_INPUT_BYTES:
            self._reject()
        return raw_data

    def file_complete(self, file_size):
        return None


def _too_large():
    response = HttpResponse(LIMIT_MESSAGE, status=413, content_type='text/plain')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


class PhotoUploadLimitMiddleware:
    """Bound only photo submission routes; other forms retain existing settings.

    Parse before calling the next middleware. Otherwise a rejected second file
    can leave a completed first file available to a view before a response-only
    guard notices the rejection. The eager parse is still subject to Django's
    existing text-field/part limits, and ordinary CSRF validation follows it.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST' or not UPLOAD_PATH.fullmatch(request.path_info):
            return self.get_response(request)

        limit = check_rate_limit(
            request, 'private-photo-upload-network', 60, period=300,
            identity='ip:' + request.META.get('REMOTE_ADDR', 'unknown'),
        )
        if limit is not None:
            limit['Cache-Control'] = 'private, no-store'
            limit['X-Content-Type-Options'] = 'nosniff'
            return limit

        declared = request.META.get('CONTENT_LENGTH', '')
        try:
            if int(declared) > MAX_REQUEST_BYTES:
                return _too_large()
        except (TypeError, ValueError, OverflowError):
            pass

        request.upload_handlers.insert(0, BoundedPhotoUploadHandler(request))
        if request.content_type == 'multipart/form-data':
            # Accessing both properties completes the bounded parse before any
            # application side effect. It also makes the CSRF token available.
            request.POST
            files = request.FILES
            if getattr(request, '_photo_upload_rejected', False):
                for _, values in files.lists():
                    for upload in values:
                        upload.close()
                files.clear()
                return _too_large()
        return self.get_response(request)
