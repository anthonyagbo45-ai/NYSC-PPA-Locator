"""Apply private-data redaction before Gunicorn access/error records are emitted."""
import logging


def post_worker_init(worker):
    from accounts.logging import SensitiveDataFilter
    for name in ('gunicorn.access', 'gunicorn.error', 'django.server'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SensitiveDataFilter) for item in logger.filters):
            logger.addFilter(SensitiveDataFilter())
        for handler in logger.handlers:
            if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
                handler.addFilter(SensitiveDataFilter())
