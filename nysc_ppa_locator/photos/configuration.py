"""Build private photo settings without contacting a storage provider."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from botocore.config import Config
from django.core.exceptions import ImproperlyConfigured


def private_photo_configuration(*, debug, base_dir, environ=None):
    env = os.environ if environ is None else environ
    value = env.get('PPA_PHOTOS_ENABLED', 'True' if debug else 'False').strip().lower()
    if value not in {'true', 'false'}:
        raise ImproperlyConfigured('PPA_PHOTOS_ENABLED must be True or False.')
    enabled = value == 'true'
    backend = env.get('PPA_PHOTO_STORAGE_BACKEND', 'filesystem' if debug else 's3')
    if backend not in {'filesystem', 's3'}:
        raise ImproperlyConfigured('Choose a supported private photo storage backend.')
    if enabled and not debug and backend != 's3':
        raise ImproperlyConfigured('Production photos require private durable object storage.')
    if backend == 'filesystem':
        root = Path(env.get('PPA_PHOTO_STORAGE_ROOT') or Path(base_dir).parent / '.private-photos')
        if not root.is_absolute():
            raise ImproperlyConfigured('The private photo directory must be an absolute path.')
        root = root.resolve()
        for public in (Path(base_dir) / 'static', Path(base_dir) / 'staticfiles', Path(base_dir).parent / 'static', Path(base_dir).parent / 'staticfiles'):
            if root == public.resolve() or root.is_relative_to(public.resolve()):
                raise ImproperlyConfigured('Keep private photos outside static-file directories.')
        storage = {'BACKEND': 'photos.storage.PrivateFileSystemStorage', 'OPTIONS': {
            'location': str(root), 'file_permissions_mode': 0o600, 'directory_permissions_mode': 0o700,
        }}
    else:
        bucket = env.get('PPA_PHOTO_BUCKET', '').strip()
        endpoint = env.get('PPA_PHOTO_S3_ENDPOINT_URL', '').strip() or None
        if endpoint:
            try:
                parsed = urlsplit(endpoint)
                if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                        or parsed.password is not None or parsed.query or parsed.fragment
                        or any(char.isspace() or ord(char) < 32 for char in endpoint)):
                    raise ValueError
                parsed.port
            except ValueError:
                raise ImproperlyConfigured('Use an HTTPS endpoint without embedded credentials or query parameters.') from None
        key = env.get('PPA_PHOTO_S3_ACCESS_KEY_ID') or None
        secret = env.get('PPA_PHOTO_S3_SECRET_ACCESS_KEY') or None
        if bool(key) != bool(secret):
            raise ImproperlyConfigured('Configure both private photo storage credential fields, or use an operator-managed IAM role.')
        if enabled and not bucket:
            raise ImproperlyConfigured('Set PPA_PHOTO_BUCKET before enabling object-backed photos.')
        storage = {'BACKEND': 'photos.storage.PrivateS3PhotoStorage', 'OPTIONS': {
            'bucket_name': bucket, 'region_name': env.get('PPA_PHOTO_S3_REGION', 'us-east-1'),
            'endpoint_url': endpoint, 'access_key': key, 'secret_key': secret,
            'default_acl': None, 'querystring_auth': True, 'custom_domain': None,
            'file_overwrite': False, 'use_ssl': True, 'verify': True,
            'max_memory_size': 2 * 1024 * 1024,
            'client_config': Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 2, 'mode': 'standard'}),
            'object_parameters': {'CacheControl': 'private, no-store', 'ContentType': 'image/jpeg'},
        }}
    return enabled, storage
