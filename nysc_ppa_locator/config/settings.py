import os
from pathlib import Path
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

# Load environment variables from .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Security Settings
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', os.getenv('SECRET_KEY', ''))
DEBUG = os.getenv('DEBUG', 'False') == 'True'
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')

# Hostnames only; production requires an explicit list.
ALLOWED_HOSTS = [host.strip() for host in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if host.strip()]

# Add your custom apps
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'django_otp.plugins.otp_static',
    
    # Local Apps
    'accounts',
    'locations',
    'ppas',
    'service_year.apps.ServiceYearConfig',
    'budgeting.apps.BudgetingConfig',
    'local_guides.apps.LocalGuidesConfig',
    'service_resources.apps.ServiceResourcesConfig',
    'notifications.apps.NotificationsConfig',
]

# Set the Custom User Model
AUTH_USER_MODEL = 'accounts.CorpMember'
LOGIN_URL = 'login'

# These references identify the official site, not a verified local schedule.
# Add another official host only after independently verifying its ownership.
NYSC_OFFICIAL_SOURCE_HOSTS = ('nysc.gov.ng', 'www.nysc.gov.ng')
NYSC_RESOURCE_LINKS = (
    {'title': 'NYSC official website', 'url': 'https://www.nysc.gov.ng/', 'checked_on': None},
)

# Ensure CSRF Middleware is active
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',  # CSRF Protection
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_otp.middleware.OTPMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'config.security.PrivateResponseMiddleware',
]

ROOT_URLCONF = 'config.urls'

# Configure Templates & Custom Context Processor
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')], # Point to root templates folder
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Inject Google Maps Key globally
                'config.context_processors.google_maps_api', 
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database Configuration
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
        'OPTIONS': {'user_attributes': ('state_code', 'full_name', 'email')},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Lagos'
USE_I18N = True
USE_TZ = True

# Static Files Configuration
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').is_dir() else []
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Production runs with DEBUG=False. A local .env may enable development only.
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'development-only-do-not-use-in-production'
    else:
        raise ImproperlyConfigured('Set DJANGO_SECRET_KEY for production.')
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith(('development-', 'django-insecure-')) or SECRET_KEY == 'your-fallback-in-development'):
    raise ImproperlyConfigured('Production requires a strong, unique DJANGO_SECRET_KEY (at least 50 characters).')
if not DEBUG and (not os.getenv('ALLOWED_HOSTS') or not ALLOWED_HOSTS or '*' in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Set explicit ALLOWED_HOSTS for production.')

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if origin.strip()]
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
# Enable only when a trusted edge strips incoming X-Forwarded-Proto and sets its own.
if os.getenv('TRUST_PROXY_HTTPS') == 'True':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 50
FILE_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024

# PostgreSQL handles concurrent writes; SQLite is retained for local development.
if os.getenv('POSTGRES_DB'):
    if not os.getenv('POSTGRES_USER'):
        raise ImproperlyConfigured('Configure POSTGRES_USER for the PostgreSQL connection.')
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ['POSTGRES_DB'],
        'USER': os.environ['POSTGRES_USER'],
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'sslmode': os.getenv('POSTGRES_SSLMODE', 'verify-full'), 'connect_timeout': 5},
    }
elif not DEBUG:
    raise ImproperlyConfigured('Configure POSTGRES_DB and POSTGRES_USER for production.')

# A shared Redis cache makes throttles apply across all application workers.
if os.getenv('REDIS_URL'):
    CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ['REDIS_URL'],
        'TIMEOUT': 300,
        'OPTIONS': {'socket_connect_timeout': 3, 'socket_timeout': 3},
    }}
elif not DEBUG:
    raise ImproperlyConfigured('Configure REDIS_URL for shared production throttling.')
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

GOOGLE_MAPS_BROWSER_API_KEY = os.getenv('GOOGLE_MAPS_BROWSER_API_KEY', '')

# Recovery and verification mail: credentials are deployment variables, never source.
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.locmem.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False') == 'True'
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True' and not EMAIL_USE_SSL
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'no-reply@example.invalid')
PASSWORD_RESET_TIMEOUT = 60 * 60
EMAIL_VERIFICATION_TIMEOUT = 60 * 60 * 24
OTP_TOTP_ISSUER = 'NYSC PPA Locator'
OTP_TOTP_THROTTLE_FACTOR = 1
OTP_STATIC_THROTTLE_FACTOR = 1

# Request budgets are shared across workers using Redis in production.
PLACES_DAILY_REQUEST_LIMIT = int(os.getenv('PLACES_DAILY_REQUEST_LIMIT', '1000'))
PLACES_MINUTE_REQUEST_LIMIT = int(os.getenv('PLACES_MINUTE_REQUEST_LIMIT', '60'))
SUPPORT_EMAIL = os.getenv('SUPPORT_EMAIL', '')

# Browser Embed Directions and transient server-side travel estimates.
GOOGLE_ROUTES_API_KEY = os.getenv('GOOGLE_ROUTES_API_KEY', '')
ROUTES_DAILY_REQUEST_LIMIT = int(os.getenv('ROUTES_DAILY_REQUEST_LIMIT', '500'))
ROUTES_MINUTE_REQUEST_LIMIT = int(os.getenv('ROUTES_MINUTE_REQUEST_LIMIT', '30'))


# Collected assets remain available under production Gunicorn without a CDN.
# Development/test settings keep unhashed URLs; production uses hashed gzip assets.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG else 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {'private_data': {'()': 'accounts.logging.SensitiveDataFilter'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'filters': ['private_data']}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django.server': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'gunicorn.access': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'gunicorn.error': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

from .static_headers import add_security_headers as _static_security_headers
WHITENOISE_ADD_HEADERS_FUNCTION = _static_security_headers
