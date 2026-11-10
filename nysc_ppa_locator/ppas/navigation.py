"""Safe, transient map destinations and navigation links.

Google's server key is never used in browser map links. The Embed API receives
only its separately restricted public browser key. Stored arbitrary Maps URLs
are deliberately ignored.
"""
from math import isfinite
import re
from urllib.parse import urlencode


_MODES = {'driving', 'walking'}


def _value(place, key, default=None):
    return place.get(key, default) if isinstance(place, dict) else getattr(place, key, default)


def _label(value):
    if isinstance(value, str):
        return value.strip()
    return str(getattr(value, 'name', '') or '').strip()


def _coordinates(latitude, longitude):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
        if isfinite(latitude) and isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def destination_data(place):
    """Return JSON-safe map data, preferring Place ID, then coordinates/address.

    The result is meant for one page interaction, not a cache or a new database
    copy of any Google-sourced fields. ``map_destination`` is the value expected
    by Maps Embed Directions; display labels remain independently escaped.
    """
    name = _label(_value(place, 'name')) or 'PPA destination'
    address = _label(_value(place, 'address'))
    area = [_label(_value(place, key)) for key in ('lga', 'state')]
    address_parts = []
    for item in (name, address, *area, 'Nigeria'):
        if item and item.casefold() not in {part.casefold() for part in address_parts}:
            address_parts.append(item)
    full_address = ', '.join(address_parts)
    point = _coordinates(_value(place, 'latitude'), _value(place, 'longitude'))
    place_id = _value(place, 'place_id') or ''
    if not isinstance(place_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', place_id):
        place_id = ''
    coordinate_text = f'{point[0]:.7f},{point[1]:.7f}' if point else ''
    return {
        'name': name, 'address': address, 'full_address': full_address,
        'place_id': place_id,
        'latitude': point[0] if point else None,
        'longitude': point[1] if point else None,
        'map_destination': ('place_id:' + place_id) if place_id else coordinate_text or full_address,
        'destination': coordinate_text or full_address,
    }


def directions_url(place, travel_mode='driving'):
    data = destination_data(place)
    mode = travel_mode if isinstance(travel_mode, str) and travel_mode in _MODES else 'driving'
    params = {'api': 1, 'destination': data['destination'], 'travelmode': mode}
    if data['place_id']:
        params['destination_place_id'] = data['place_id']
    return 'https://www.google.com/maps/dir/?' + urlencode(params)


def embed_directions_url(place, browser_key, origin, mode='driving'):
    """Build an official Embed Directions URL after the member chooses an origin.

    ``origin`` is a starting-address string or a dict of latitude/longitude.
    This helper does not perform geolocation, store the origin or call Google.
    Invalid/missing configuration or origin returns an empty string.
    """
    if not isinstance(browser_key, str) or not browser_key or not isinstance(mode, str) or mode not in _MODES:
        return ''
    if isinstance(origin, dict):
        point = _coordinates(origin.get('latitude'), origin.get('longitude'))
        origin = f'{point[0]:.7f},{point[1]:.7f}' if point else ''
    elif isinstance(origin, str):
        origin = origin.strip()
        if len(origin) > 250 or any(ord(char) < 32 or ord(char) == 127 for char in origin):
            origin = ''
    else:
        origin = ''
    if not origin:
        return ''
    return 'https://www.google.com/maps/embed/v1/directions?' + urlencode({
        'key': browser_key, 'origin': origin,
        'destination': destination_data(place)['map_destination'], 'mode': mode,
    })
