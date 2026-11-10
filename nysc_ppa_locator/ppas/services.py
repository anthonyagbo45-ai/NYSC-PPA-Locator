"""Google discovery with bounded requests and project-wide shared-cache budgets.

Only request counters are cached here. Google-sourced result content is returned
for the current interaction; it is neither cached nor saved by this service.
The application can retain place IDs and independently supplied community facts.
Production must use a shared cache whose add/incr operations are atomic (Redis).
"""
import logging
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac

logger = logging.getLogger(__name__)


class PlacesUnavailable(Exception):
    """A public-safe failure, kept distinct from a successful empty search."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _safe_attributions(value):
    """Keep credit text as text; expose only credential-free HTTPS links.

    Templates must keep normal HTML autoescaping when displaying provider names.
    This parser does not mark any Google-supplied content as safe HTML.
    """
    if not isinstance(value, list):
        raise ValueError
    credits = []
    for item in value[:20]:
        if not isinstance(item, dict):
            raise ValueError
        provider = item.get('provider') or item.get('providerDisplayName') or ''
        if not isinstance(provider, str):
            raise ValueError
        provider = provider[:300]
        uri = item.get('providerUri') or ''
        safe_uri = ''
        if isinstance(uri, str) and len(uri) <= 2048 and not any(ord(char) < 32 or ord(char) == 127 for char in uri):
            try:
                parsed = urlsplit(uri)
                if parsed.scheme.lower() == 'https' and parsed.hostname and parsed.username is None and parsed.password is None:
                    parsed.port  # Reject invalid ports without sending a request.
                    safe_uri = uri
            except ValueError:
                pass
        if provider:
            credits.append({'provider': provider, 'provider_uri': safe_uri})
    return credits


def _reserve_counter(key, limit, timeout, code):
    """Reserve one attempt without allowing rejected calls to consume capacity.

    Fixed window keys live beyond their window, so a normal boundary transition
    cannot expire a counter between add and incr. If cache state is uncertain,
    reject the search rather than resetting a counter and spending unchecked.
    """
    try:
        count = 1 if cache.add(key, 1, timeout=timeout) else cache.incr(key)
        if count > limit:
            cache.decr(key)
            if cache.add(key + ':exhausted', True, timeout=timeout):
                logger.warning('Google Places request budget exhausted (%s)', code)
            raise PlacesUnavailable(code, 'Google discovery has reached its request limit. Please try again later; saved PPAs are still available.')
        return count
    except PlacesUnavailable:
        raise
    except Exception:
        logger.warning('Google Places budget cache unavailable')
        raise PlacesUnavailable('budget_unavailable', 'Google discovery is temporarily unavailable. You can still browse saved PPAs.') from None


def _reserve_request():
    try:
        daily_limit = int(getattr(settings, 'PLACES_DAILY_REQUEST_LIMIT', 1000))
        minute_limit = int(getattr(settings, 'PLACES_MINUTE_REQUEST_LIMIT', 60))
        if daily_limit < 1 or minute_limit < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise PlacesUnavailable('not_configured', 'Google discovery is unavailable. You can still browse saved PPAs.') from None

    now = datetime.now(timezone.utc)
    prefix = 'places-budget:' + salted_hmac('places-budget', 'project').hexdigest()[:16]
    minute_key = prefix + ':minute:' + now.strftime('%Y%m%d%H%M')
    daily_key = prefix + ':day:' + now.strftime('%Y%m%d')
    _reserve_counter(minute_key, minute_limit, 120, 'minute_budget_exhausted')
    try:
        daily_count = _reserve_counter(daily_key, daily_limit, 86520, 'daily_budget_exhausted')
    except PlacesUnavailable:
        try:
            cache.decr(minute_key)
        except Exception:
            # Keep the conservative reservation if the cache fails during release.
            pass
        raise
    if daily_count == max(1, math.ceil(daily_limit * 0.8)):
        logger.warning('Google Places daily request budget has reached 80 percent')


def search_google_places(query, state_name='Nigeria', lga_name=''):
    """Return transient discovery results or raise a public-safe service failure.

    Every outbound attempt consumes a budget reservation, including failed API
    requests because providers can bill attempts. No-match searches return [].
    This text search supplies geographic context, not a guarantee of LGA bounds.
    """
    if not settings.GOOGLE_MAPS_API_KEY:
        raise PlacesUnavailable('not_configured', 'Google discovery is not configured yet. You can still browse saved PPAs.')
    _reserve_request()
    parts = (query, lga_name, state_name, '' if state_name == 'Nigeria' else 'Nigeria')
    text = ', '.join(part for part in parts if part)
    try:
        response = requests.post(
            'https://places.googleapis.com/v1/places:searchText',
            headers={
                'X-Goog-Api-Key': settings.GOOGLE_MAPS_API_KEY,
                'X-Goog-FieldMask': 'places.id,places.displayName,places.formattedAddress,places.location,places.addressComponents,places.attributions',
            },
            json={'textQuery': text, 'pageSize': 20}, timeout=(3.05, 8), allow_redirects=False,
        )
        if isinstance(response.status_code, int) and 300 <= response.status_code < 400:
            raise requests.HTTPError(response=response)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or 'error' in data or not isinstance(data.get('places', []), list):
            raise ValueError
        results = []
        for place in data.get('places', [])[:20]:
            if not isinstance(place, dict):
                raise ValueError
            place_id = place.get('id', '')
            if not isinstance(place_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', place_id):
                continue
            location = place.get('location') or {}
            display_name = place.get('displayName') or {}
            if not isinstance(location, dict) or not isinstance(display_name, dict):
                raise ValueError
            components = place.get('addressComponents', [])
            if not isinstance(components, list):
                raise ValueError
            results.append({
                'name': display_name.get('text', ''),
                'address': place.get('formattedAddress', ''),
                'latitude': location.get('latitude'), 'longitude': location.get('longitude'),
                'place_id': place_id, 'address_components': components,
                'attributions': _safe_attributions(place.get('attributions', [])),
            })
        return results
    except requests.Timeout:
        logger.warning('Google Places request timed out')
        raise PlacesUnavailable('timeout', 'Google discovery took too long. Please retry or browse saved PPAs.') from None
    except requests.HTTPError as exc:
        status = getattr(exc.response, 'status_code', None)
        logger.warning('Google Places provider request failed')
        code = 'provider_quota' if status == 429 else 'provider_unavailable'
        raise PlacesUnavailable(code, 'Google discovery is temporarily unavailable. Please retry later; saved PPAs are still available.') from None
    except requests.RequestException:
        logger.warning('Google Places connection failed')
        raise PlacesUnavailable('provider_unavailable', 'Google discovery is temporarily unavailable. Please retry later; saved PPAs are still available.') from None
    except (ValueError, TypeError, AttributeError):
        logger.warning('Google Places returned an invalid response')
        raise PlacesUnavailable('invalid_response', 'Google discovery returned an unexpected response. Please retry later.') from None
