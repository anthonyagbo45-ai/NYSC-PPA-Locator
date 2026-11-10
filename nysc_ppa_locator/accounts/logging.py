"""Redact private account/location inputs before application/access formatting."""
import logging
import re


_TOKEN_PATHS = (
    (re.compile(r'(/account/verify-email/)[^/\s?\"\']+'), r'\1[redacted]'),
    (re.compile(r'(/account/password-reset/)[^/\s?\"\']+/[^/\s?\"\']+/'), r'\1[redacted]/[redacted]/'),
)
_QUERY = re.compile(r'((?:https?://|/)[^\s?\"\']*)\?[^\s\"\']*')
_PRIVATE_FIELDS = re.compile(
    r'(?i)([\"\']?(?:password\d*|confirm_password|current_password|new_password\d*|'
    r'token|otp_token|secret|api_key|key|state_code|username|email|latitude|longitude|'
    r'lat|lng|origin|destination|query|q)[\"\']?\s*[:=]\s*)'
    r'(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;&}\]]+)'
)
_EMAIL = re.compile(r'(?i)\b[A-Z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
_STATE_CODE = re.compile(r'(?i)\b[A-Z]{2}/\d{2}[A-Z]/\d{3,8}\b')
_COORDINATE_PAIR = re.compile(r'(?<!\w)[+-]?\d{1,3}\.\d{3,}\s*,\s*[+-]?\d{1,3}\.\d{3,}(?!\w)')


def redact_sensitive_text(value):
    value = str(value)
    for pattern, replacement in _TOKEN_PATHS:
        value = pattern.sub(replacement, value)
    value = _QUERY.sub(r'\1?[redacted]', value)
    value = _COORDINATE_PAIR.sub('[redacted]', value)
    value = _PRIVATE_FIELDS.sub(r'\1[redacted]', value)
    value = _EMAIL.sub('[redacted]', value)
    return _STATE_CODE.sub('[redacted]', value)


class SensitiveDataFilter(logging.Filter):
    """Compatible with Django handlers and Gunicorn access/error handlers.

    Filtering the rendered message also catches Gunicorn's positional request
    arguments. This is defence in depth: call sites must still avoid logging
    request bodies, passwords, provider payloads or private evidence.
    """

    def filter(self, record):
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_sensitive_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_sensitive_text(record.stack_info)
        return True
