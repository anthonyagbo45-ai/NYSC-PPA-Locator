"""Conservative address-component checks, not geographic boundary verification.

Google text search can return places outside its textual state/LGA context. These
checks compare explicit structured administrative labels and flag a mismatch.
They deliberately do not infer an LGA from the formatted address, coordinates,
nearby locality or a country-sized bounding box. Correct coordinates inside an
actual LGA require a reliable boundary dataset and a separate point-in-polygon
check. An address match must never be presented as official NYSC verification.
"""
import re
import unicodedata


_STATE_ALIASES = {
    'fct': 'federal capital territory',
    'abuja': 'federal capital territory',
    'abuja fct': 'federal capital territory',
    'fct abuja': 'federal capital territory',
    'federal capital territory abuja': 'federal capital territory',
    'federal capital territory fct': 'federal capital territory',
}


def _normalise(value, kind):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    value = re.sub(r'[^\w\s]', ' ', value)
    if kind == 'state':
        value = re.sub(r'\bstate\b', ' ', value)
    else:
        value = re.sub(r'\blocal\s+government(?:\s+area)?\b|\bl\s+g\s+a\b|\blga\b|\barea\s+council\b', ' ', value)
    value = ' '.join(value.split())
    return _STATE_ALIASES.get(value, value) if kind == 'state' else value


def _labels(components, component_type, kind):
    labels = set()
    for component in components:
        if not isinstance(component, dict):
            continue
        types = component.get('types')
        if not isinstance(types, list) or component_type not in types:
            continue
        for key in ('longText', 'shortText'):
            label = _normalise(component.get(key), kind)
            if label:
                labels.add(label)
    return labels


def assess_place_location(place, state_name, lga_name):
    """Return (address_checked | mismatch | unconfirmed, user-facing message).

    Locality labels can describe a town inside a different-named LGA, so they are
    never evidence of an LGA mismatch or sufficient to establish an LGA match.
    Only administrative_area_level_2 is treated as an explicit LGA candidate;
    absence of that component leaves the location unconfirmed.
    """
    components = place.get('address_components', place.get('addressComponents', []))
    if not isinstance(components, list):
        return 'unconfirmed', 'The address could not be checked against this state and LGA. Please confirm it from your posting information.'
    expected_state = _normalise(state_name, 'state')
    expected_lga = _normalise(lga_name, 'lga')
    countries = _labels(components, 'country', 'state')
    if countries and not countries.intersection({'nigeria', 'ng'}):
        return 'mismatch', 'This result lists a country outside Nigeria. Check the destination before saving.'
    states = _labels(components, 'administrative_area_level_1', 'state')
    lgas = _labels(components, 'administrative_area_level_2', 'lga')
    if expected_state and states and expected_state not in states:
        return 'mismatch', 'This result lists a different state. Check the selected state and the exact branch before saving.'
    if expected_lga and lgas and expected_lga not in lgas:
        return 'mismatch', 'This result lists a different LGA. Check the selected LGA and the exact branch before saving.'
    if expected_state and expected_lga and expected_state in states and expected_lga in lgas:
        return 'address_checked', 'The result’s state and LGA address labels match your selection. Confirm that this is your exact PPA branch.'
    return 'unconfirmed', 'The result does not establish the selected state and LGA. Confirm the location from your posting information before saving.'
