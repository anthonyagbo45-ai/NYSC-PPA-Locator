"""Guest route estimates. Starting points exist only during this request."""
import json
from types import SimpleNamespace
from django.core import signing
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_POST
from config.security import check_rate_limit
from .models import PPA
from .routing import compute_route, RouteUnavailable


def invalid(message, code='invalid_input', status=400):
    return JsonResponse({'status': 'invalid', 'code': code, 'message': message}, status=status)


@never_cache
@require_POST
@sensitive_variables()
def route_estimate(request):
    # Keep this paid endpoint's abuse budget per IP even after login. Never
    # accept an origin IP supplied through forwarded headers.
    client = SimpleNamespace(META=request.META, user=SimpleNamespace(is_authenticated=False))
    limited = check_rate_limit(client, 'route-estimates', 20, period=300)
    if limited is not None:
        response = JsonResponse({'status': 'unavailable', 'code': 'client_limit' if limited.status_code == 429 else 'budget_unavailable',
                                 'message': 'Route estimates are busy. Please try again shortly; the map is still available.'}, status=limited.status_code)
        if limited.status_code == 429:
            response['Retry-After'] = limited['Retry-After']
        return response
    if request.content_type != 'application/json':
        return invalid('Send a starting point and destination as JSON.', status=415)
    try:
        payload = json.loads(request.body)
    except RequestDataTooBig:
        return invalid('The route request is too large.', status=413)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return invalid('The route request could not be read.')
    if not isinstance(payload, dict):
        return invalid('Choose a starting point and destination.')
    if ('ppa_id' in payload) == ('preview_token' in payload):
        return invalid('Choose one destination.')
    if 'ppa_id' in payload:
        identifier = payload['ppa_id']
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 < identifier <= 9223372036854775807:
            return invalid('Choose a valid PPA destination.')
        place = PPA.objects.select_related('state', 'lga').filter(pk=identifier).first()
        if place is None:
            return invalid('This PPA could not be found.', 'not_found', status=404)
    else:
        token = payload['preview_token']
        if not isinstance(token, str) or len(token) > 20000:
            return invalid('Select this place again from search.', 'invalid_selection')
        try:
            selected = signing.loads(token, salt='place-preview', max_age=900)
            place = selected['place']
            if not isinstance(place, dict):
                raise ValueError
        except (signing.BadSignature, TypeError, KeyError, ValueError):
            return invalid('This selection has expired. Select the place again from search.', 'invalid_selection')
    origin = payload.get('origin')
    try:
        result = compute_route(place, origin, payload.get('mode', 'driving'))
    except ValueError as exc:
        return invalid(str(exc))
    except RouteUnavailable as exc:
        response = JsonResponse({'status': exc.status, 'code': exc.code, 'message': exc.message}, status=exc.http_status)
        if exc.http_status == 429:
            response['Retry-After'] = '60'
        return response
    return JsonResponse(result)
