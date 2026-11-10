import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


POSTAL_LOOKUP_URL = 'https://api.postalpincode.in/pincode/{pincode}'


class PostalLookupError(Exception):
    pass


def _normalize(value):
    return re.sub(r'\s+', ' ', value or '').strip().casefold()


def _matches_area(city, area):
    normalized_city = _normalize(city)
    normalized_area = _normalize(area)
    return (
        normalized_city == normalized_area
        or normalized_area.startswith(normalized_city + ' ')
        or normalized_city.startswith(normalized_area + ' ')
    )


def lookup_pincode(pincode):
    if not re.fullmatch(r'[1-9][0-9]{5}', pincode or ''):
        raise PostalLookupError('Enter a valid six-digit Indian PIN code.')

    request = Request(
        POSTAL_LOOKUP_URL.format(pincode=pincode),
        headers={'Accept': 'application/json', 'User-Agent': 'AvatarStore/1.0'},
    )
    try:
        with urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PostalLookupError(
            'The PIN-code directory is temporarily unavailable. Please try again shortly.'
        ) from error

    if (
        not isinstance(data, list)
        or not data
        or not isinstance(data[0], dict)
        or data[0].get('Status') != 'Success'
        or not isinstance(data[0].get('PostOffice'), list)
    ):
        raise PostalLookupError('That PIN code was not found in the Indian postal directory.')

    offices = [office for office in data[0]['PostOffice'] if isinstance(office, dict)]
    if not offices:
        raise PostalLookupError('That PIN code has no delivery post offices listed.')

    return offices


def validate_delivery_location(pincode, city, state):
    offices = lookup_pincode(pincode)
    normalized_city = _normalize(city)
    normalized_state = _normalize(state)
    matching_offices = [
        office for office in offices
        if _normalize(office.get('State')) == normalized_state
        and any(
            _matches_area(normalized_city, office.get(field))
            for field in ('Name', 'District', 'Division', 'Block', 'Region')
            if office.get(field)
        )
    ]
    if not matching_offices:
        states = sorted({office.get('State', '') for office in offices if office.get('State')})
        raise PostalLookupError(
            'The city or state does not match this PIN code. Postal directory state: '
            + ', '.join(states)
        )
    if not any(_normalize(office.get('DeliveryStatus')) == 'delivery' for office in matching_offices):
        raise PostalLookupError(
            'This PIN code and locality are listed as non-delivery by the postal directory.'
        )

    office = matching_offices[0]
    return {
        'pincode': pincode,
        'state': office.get('State', ''),
        'district': office.get('District', ''),
        'locality': office.get('Name', ''),
        'delivery': True,
    }
