import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


class SMSProviderError(Exception):
    pass


def _msg91_request(url, method='POST', payload=None):
    if not settings.MSG91_AUTH_KEY or not settings.MSG91_OTP_TEMPLATE_ID:
        raise SMSProviderError('Phone OTP login is not configured.')

    headers = {
        'Accept': 'application/json',
        'authkey': settings.MSG91_AUTH_KEY,
    }
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SMSProviderError('The SMS provider is temporarily unavailable. Please try again.') from error

    if not isinstance(result, dict) or str(result.get('type', '')).casefold() != 'success':
        raise SMSProviderError('The SMS provider could not complete the OTP request.')
    return result


def send_phone_otp(phone_e164):
    url = f'{settings.MSG91_OTP_SEND_URL}?{urlencode({"template_id": settings.MSG91_OTP_TEMPLATE_ID, "mobile": phone_e164.lstrip("+")})}'
    return _msg91_request(url, payload={})


def verify_phone_otp(phone_e164, otp):
    url = f'{settings.MSG91_OTP_VERIFY_URL}?{urlencode({"mobile": phone_e164.lstrip("+"), "otp": otp})}'
    return _msg91_request(url, method='GET')
