"""Shared-cache abuse limits. Only trust REMOTE_ADDR, never client-supplied headers."""
from functools import wraps
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.crypto import salted_hmac


def check_rate_limit(request, scope, limit, period=300, *, identity=None):
    if identity is None:
        identity = request.META.get('REMOTE_ADDR', 'unknown')
        if request.user.is_authenticated:
            identity = f'user:{request.user.pk}'
    key = 'limit:' + salted_hmac(scope, identity).hexdigest()
    try:
        if cache.add(key, 1, timeout=period):
            count = 1
        else:
            try:
                count = cache.incr(key)
            except ValueError:  # Expired between add and incr.
                cache.add(key, 1, timeout=period)
                count = 1
    except Exception:
        return HttpResponse('Service temporarily unavailable.', status=503)
    if count > limit:
        response = HttpResponse('Too many requests. Please try again later.', status=429)
        response['Retry-After'] = str(period)
        return response
    return None


def check_account_rate_limit(request, scope, limit, period=300, *, fields=('username', 'state_code'), network_multiplier=10):
    """Bound both distributed account attacks and shared-network abuse.

    Hash every submitted identifier, including unknown accounts, without a
    lookup. The same response/counters apply whether an account exists or not.
    A larger network ceiling lets different members share camp/cafe networks.
    Empty submissions keep the original smaller network limit.
    """
    identifier = next((request.POST.get(field, '').strip().lower() for field in fields if request.POST.get(field, '').strip()), '')
    identity = 'ip:' + request.META.get('REMOTE_ADDR', 'unknown')
    response = check_rate_limit(request, scope + ':network', limit * network_multiplier if identifier else limit, period, identity=identity)
    if response is not None or not identifier:
        return response
    return check_rate_limit(request, scope + ':account', limit, period, identity=identifier[:320])


def account_rate_limit(scope, limit, period=300, *, fields=('username', 'state_code'), network_multiplier=10):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == 'POST':
                response = check_account_rate_limit(request, scope, limit, period, fields=fields, network_multiplier=network_multiplier)
                if response is not None:
                    return response
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


def rate_limit(scope, limit, period=300, methods=('POST',)):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods:
                response = check_rate_limit(request, scope, limit, period)
                if response is not None:
                    return response
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.resolver_match.view_name == 'admin:login' and request.method == 'POST':
            return check_account_rate_limit(request, 'login', 20, fields=('username',))
        return None

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated or request.path in ('/', '/login/', '/register/', '/portal-entry/'):
            response['Cache-Control'] = 'private, no-store'
        if not request.path.startswith('/admin/'):
            response['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: https://maps.gstatic.com; connect-src 'self'; "
                "frame-src https://www.google.com https://maps.google.com; "
                "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            )
        return response
