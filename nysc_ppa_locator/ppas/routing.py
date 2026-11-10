"""Transient Google Routes summaries with a separate shared request budget.

No origin, destination content or route response is persisted or cached. Only
integer budget counters live in Redis. The fixed API URL cannot be selected by
clients. Exceptions and logs intentionally omit provider bodies and addresses.
"""
from datetime import datetime, timezone
import logging
import math
import re

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables
from .navigation import _coordinates, destination_data

logger = logging.getLogger(__name__)


class RouteUnavailable(Exception):
    def __init__(self, code, message, *, status='unavailable', http_status=503):
        self.code = code
        self.message = message
        self.status = status
        self.http_status = http_status
        super().__init__(message)


def validate_origin(origin):
    if not isinstance(origin, dict):
        raise ValueError('Choose a starting point.')
    if 'address' in origin:
        if set(origin) != {'address'} or not isinstance(origin['address'], str):
            raise ValueError('Enter one starting address.')
        address = origin['address'].strip()
        if not address or len(address) > 250 or any(ord(char) < 32 or ord(char) == 127 for char in address):
            raise ValueError('Enter a starting address of at most 250 characters.')
        return {'address': address}
    if set(origin) != {'latitude', 'longitude'}:
        raise ValueError('Choose a valid starting point.')
    point = _coordinates(origin.get('latitude'), origin.get('longitude'))
    if not point:
        raise ValueError('Choose a valid starting point.')
    return {'location': {'latLng': {'latitude': point[0], 'longitude': point[1]}}}


def destination_waypoint(place):
    data = destination_data(place)
    if data['place_id']:
        return {'placeId': data['place_id']}
    if data['latitude'] is not None and data['longitude'] is not None:
        return {'location': {'latLng': {'latitude': data['latitude'], 'longitude': data['longitude']}}}
    raw_name = place.get('name') if isinstance(place, dict) else getattr(place, 'name', '')
    raw_address = place.get('address') if isinstance(place, dict) else getattr(place, 'address', '')
    if not raw_name and not raw_address:
        raise ValueError('The destination does not have a usable location.')
    if len(data['full_address']) > 3000:
        raise ValueError('The destination address is too long.')
    return {'address': data['full_address']}


def _reserve_counter(key, limit, timeout, code):
    try:
        count = 1 if cache.add(key, 1, timeout=timeout) else cache.incr(key)
        if count > limit:
            cache.decr(key)
            if cache.add(key + ':exhausted', True, timeout=timeout):
                logger.warning('Route request budget exhausted (%s)', code)
            raise RouteUnavailable(code, 'Route estimates are busy. The map and live navigation option are still available.', http_status=429)
        return count
    except RouteUnavailable:
        raise
    except Exception:
        logger.warning('Route budget cache unavailable')
        raise RouteUnavailable('budget_unavailable', 'Route estimates are temporarily unavailable. You can still use the map.') from None


def _reserve_request():
    try:
        minute_limit = int(getattr(settings, 'ROUTES_MINUTE_REQUEST_LIMIT', 30))
        daily_limit = int(getattr(settings, 'ROUTES_DAILY_REQUEST_LIMIT', 500))
        if minute_limit < 1 or daily_limit < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise RouteUnavailable('not_configured', 'Route estimates are temporarily unavailable. You can still use the map.') from None
    now = datetime.now(timezone.utc)
    prefix = 'routes-budget:' + salted_hmac('routes-budget', 'project').hexdigest()[:16]
    minute_key = prefix + ':minute:' + now.strftime('%Y%m%d%H%M')
    daily_key = prefix + ':day:' + now.strftime('%Y%m%d')
    _reserve_counter(minute_key, minute_limit, 120, 'minute_budget_exhausted')
    try:
        daily_count = _reserve_counter(daily_key, daily_limit, 86520, 'daily_budget_exhausted')
    except RouteUnavailable:
        try:
            cache.decr(minute_key)
        except Exception:
            pass
        raise
    if daily_count == max(1, math.ceil(daily_limit * .8)):
        logger.warning('Route daily request budget has reached 80 percent')


@sensitive_variables()
def compute_route(place, origin, mode='driving'):
    if mode not in ('driving', 'walking'):
        raise ValueError('Choose driving or walking.')
    waypoint = validate_origin(origin)
    destination = destination_waypoint(place)
    key = getattr(settings, 'GOOGLE_ROUTES_API_KEY', '') or getattr(settings, 'GOOGLE_MAPS_API_KEY', '')
    if not key:
        raise RouteUnavailable('not_configured', 'Route estimates are temporarily unavailable. You can still use the map or live navigation.')
    _reserve_request()
    try:
        response = requests.post(
            'https://routes.googleapis.com/directions/v2:computeRoutes',
            headers={'X-Goog-Api-Key': key, 'X-Goog-FieldMask': 'routes.distanceMeters,routes.duration'},
            json={'origin': waypoint, 'destination': destination,
                  'travelMode': 'DRIVE' if mode == 'driving' else 'WALK',
                  'computeAlternativeRoutes': False, 'units': 'METRIC'},
            timeout=(3.05, 8),
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or 'error' in payload or not isinstance(payload.get('routes', []), list):
            raise ValueError
        routes = payload.get('routes', [])
        if not routes:
            raise RouteUnavailable('no_route', 'No route was found from that starting point. Try another address or travel mode.', status='no_route', http_status=200)
        first = routes[0]
        if not isinstance(first, dict):
            raise ValueError
        distance = first.get('distanceMeters')
        duration = first.get('duration')
        if isinstance(distance, bool) or not isinstance(distance, int) or not 0 <= distance <= 40100000:
            raise ValueError
        if not isinstance(duration, str) or not re.fullmatch(r'\d+(?:\.\d+)?s', duration):
            raise ValueError
        seconds = float(duration[:-1])
        if not math.isfinite(seconds) or not 0 <= seconds <= 31536000:
            raise ValueError
        return {'status': 'ok', 'distance_meters': distance, 'duration_seconds': math.ceil(seconds)}
    except RouteUnavailable:
        raise
    except requests.Timeout:
        logger.warning('Route provider request timed out')
        raise RouteUnavailable('timeout', 'The route estimate took too long. Please retry; the map is still available.') from None
    except requests.HTTPError as exc:
        code = 'provider_quota' if getattr(exc.response, 'status_code', None) == 429 else 'provider_unavailable'
        logger.warning('Route provider request failed')
        raise RouteUnavailable(code, 'Route estimates are temporarily unavailable. You can still use the map.') from None
    except requests.RequestException:
        logger.warning('Route provider connection failed')
        raise RouteUnavailable('provider_unavailable', 'Route estimates are temporarily unavailable. You can still use the map.') from None
    except (ValueError, TypeError, AttributeError, OverflowError):
        logger.warning('Route provider returned an invalid response')
        raise RouteUnavailable('invalid_response', 'The route estimate could not be read. Please retry or use the map.') from None
