"""
Siteline settings.

One settings file, driven by environment variables (loaded from .env).
Everything production-sensitive defaults to the safe value.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


DEBUG = env_bool('DEBUG', False)
SECRET_KEY = env('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'dev-only-insecure-key-change-me'
    else:
        raise RuntimeError('SECRET_KEY must be set when DEBUG is off.')

ALLOWED_HOSTS = [h.strip() for h in env('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]

# Product branding (white-label friendly)
PRODUCT_NAME = env('PRODUCT_NAME', 'Siteline')
SITE_URL = env('SITE_URL', 'http://localhost:8000').rstrip('/')
SUPPORT_EMAIL = env('SUPPORT_EMAIL', 'support@example.com')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'axes',
    'accounts',
    'tenancy',
    'billing',
    'projects',
    'core',
    'rfis',
    'variations',
    'directory',
    'tenders',
    'delivery',
    'jobs',
    'security',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Content-Security-Policy and Permissions-Policy (security/middleware.py)
    'security.middleware.SecurityHeadersMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',
    # Rate limits on sign-up, password reset, invites, codes, uploads, exports and search (429 page)
    'security.middleware.RateLimitMiddleware',
    # Owners and admins without two-factor sign-in are sent to set it up
    'security.middleware.TwoFactorEnforcementMiddleware',
    # Resolves /<org_slug>/... to an Organization, checks membership and
    # subscription, and scopes every tenant query to that organisation.
    'tenancy.middleware.TenantMiddleware',
    # After TenantMiddleware, so a non-member still gets a 404 before any "confirm it's you"
    'security.middleware.StepUpMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'tenancy.context_processors.tenant',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database: SQLite by default; MySQL on PythonAnywhere via DB_ENGINE=mysql.
if env('DB_ENGINE', 'sqlite') == 'mysql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': env('DB_NAME'),
            'USER': env('DB_USER'),
            'PASSWORD': env('DB_PASSWORD'),
            'HOST': env('DB_HOST'),
            'PORT': env('DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            'CONN_MAX_AGE': 280,  # PythonAnywhere closes idle MySQL connections at 300s
        }
    }
elif env('DB_ENGINE') == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': env('DB_NAME'),
            'USER': env('DB_USER'),
            'PASSWORD': env('DB_PASSWORD'),
            'HOST': env('DB_HOST'),
            'PORT': env('DB_PORT', '5432'),
            'OPTIONS': {'sslmode': env('DB_SSLMODE', 'prefer')},  # AWS RDS: DB_SSLMODE=require
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'

# Brute-force protection
# The visitor's address, shared by django-axes and the rate limits. On PythonAnywhere set
# CLIENT_IP_HEADER=X-Real-IP; on AWS config/alb_wsgi.py already fixes REMOTE_ADDR. Unset, only
# REMOTE_ADDR is trusted. See security/ip.py.
CLIENT_IP_HEADER = env('CLIENT_IP_HEADER', '')
CLIENT_IP_PROXY_COUNT = int(env('CLIENT_IP_PROXY_COUNT', '1'))
AXES_CLIENT_IP_CALLABLE = 'security.ip.client_ip'
AXES_FAILURE_LIMIT = 6
AXES_COOLOFF_TIME = 1  # hours
AXES_LOCKOUT_PARAMETERS = [['username', 'ip_address']]
AXES_USERNAME_FORM_FIELD = 'username'
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = 'security/locked_out.html'  # wrong passwords and wrong two-factor codes both count

# Two-factor sign-in (security/). Required for workspace owners and admins and platform staff,
# optional for everyone else. TWO_FACTOR_ENFORCE=0 only for a short emergency (check --deploy warns).
TWO_FACTOR_ENFORCE = env_bool('TWO_FACTOR_ENFORCE', True)
TWO_FACTOR_REMEMBER_DAYS = 30
TWO_FACTOR_PENDING_SECONDS = 300   # time allowed between the password and the code
# Billing, team and role changes, exports and archiving/deleting need a password or code this recently
STEP_UP_SECONDS = 600

# Cache: the app's own database, shared by every web process and server with no Redis (table made
# by security/migrations/0002, the same as `manage.py createcachetable`). Rate limits don't use it:
# their counters are rows in their own table (security.RateLimitCounter), which is never culled.
if env('CACHE_BACKEND', 'database') == 'locmem':
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
                          'LOCATION': 'siteline_cache', 'OPTIONS': {'MAX_ENTRIES': 20000}}}
RATELIMIT_ENABLED = env_bool('RATELIMIT_ENABLED', True)

LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Europe/London'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
                    if not DEBUG else 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# Uploaded files are NEVER served from a public URL. Every download goes
# through a view that checks the user belongs to the file's organisation and
# project. Do not add a /media/ static mapping on PythonAnywhere.
MEDIA_ROOT = Path(env('MEDIA_ROOT', BASE_DIR / 'private_media'))

# Cloud file storage. Leave STORAGE_BUCKET empty to keep files on this server's disk.
# Any S3-compatible service works: AWS S3 (London is eu-west-2), DigitalOcean Spaces
# (set STORAGE_ENDPOINT_URL=https://lon1.digitaloceanspaces.com) or Cloudflare R2.
# The bucket must be private. Siteline checks permissions, then hands the browser a
# signed link that expires after STORAGE_LINK_SECONDS.
STORAGE_BUCKET = env('STORAGE_BUCKET', '')
STORAGE_LINK_SECONDS = int(env('STORAGE_LINK_SECONDS', '300'))
STORAGE_REGION = env('STORAGE_REGION', 'eu-west-2')
# GuardDuty Malware Protection tags every upload; files are only handed out once tagged clean.
# Set STORAGE_MALWARE_SCAN=0 only if scanning is not enabled on the bucket.
STORAGE_MALWARE_SCAN = env_bool('STORAGE_MALWARE_SCAN', True)
# On AWS (ECS), STORAGE_USE_IAM_ROLE=1 uses the task role via boto3's default credential chain instead of keys
STORAGE_USE_IAM_ROLE = env_bool('STORAGE_USE_IAM_ROLE', False)
if STORAGE_BUCKET:
    _missing = [] if STORAGE_USE_IAM_ROLE else [
        k for k in ('STORAGE_ACCESS_KEY', 'STORAGE_SECRET_KEY') if not (env(k) or '').strip()]
    if _missing:
        # Fail at start-up with a plain message, not a signing error deep inside boto on the first upload
        raise RuntimeError(f'STORAGE_BUCKET is set but {" and ".join(_missing)} {"are" if len(_missing) > 1 else "is"} empty in .env. '
                           'Add the siteline-app access key, or put # before STORAGE_BUCKET to keep files on disk.')
    STORAGES['default'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': STORAGE_BUCKET,
            # Without an endpoint, use AWS's regional one (London by default), never the global one
            'endpoint_url': env('STORAGE_ENDPOINT_URL') or f'https://s3.{STORAGE_REGION}.amazonaws.com',
            'region_name': STORAGE_REGION,
            'access_key': None if STORAGE_USE_IAM_ROLE else env('STORAGE_ACCESS_KEY'),
            'secret_key': None if STORAGE_USE_IAM_ROLE else env('STORAGE_SECRET_KEY'),
            'default_acl': None,            # never public; the bucket policy decides
            'querystring_auth': True,       # every link is signed...
            'querystring_expire': STORAGE_LINK_SECONDS,  # ...and short-lived
            # Every stored name contains a random folder, so the pre-upload "is this name taken?"
            # lookup is unnecessary, and it would be refused once the bucket only serves scanned files
            'file_overwrite': True,
            'signature_version': 's3v4',
        },
    }
MEDIA_URL = '/files-not-public/'
MAX_UPLOAD_MB = int(env('MAX_UPLOAD_MB', '50'))
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email
EMAIL_BACKEND = env('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = env('EMAIL_HOST', '')
EMAIL_PORT = int(env('EMAIL_PORT', '587'))
EMAIL_HOST_USER = env('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', f'{PRODUCT_NAME} <no-reply@example.com>')
# Seconds per SMTP operation. Without it a mail server that stops answering hangs the worker for
# good. Keep it well under JOBS_LOCK_TIMEOUT (system check jobs.W001).
EMAIL_TIMEOUT = int(env('EMAIL_TIMEOUT', '30'))
# 1: send each email during the request, as before (the worker only retries failures).
# 0: queue it in the outbox for `manage.py run_worker`. Only set 0 where a worker is running.
EMAIL_SYNC = env_bool('EMAIL_SYNC', True)

# Background jobs (jobs/__init__.py). JOBS_RUN_INLINE=1 runs each job in the web process
# once its transaction commits, for local development without a worker.
JOBS_RUN_INLINE = env_bool('JOBS_RUN_INLINE', False)
JOBS_MAX_ATTEMPTS = int(env('JOBS_MAX_ATTEMPTS', '5'))
JOBS_BACKOFF_BASE = int(env('JOBS_BACKOFF_BASE', '30'))     # seconds before the first retry, doubling after
JOBS_BACKOFF_MAX = int(env('JOBS_BACKOFF_MAX', '3600'))
JOBS_LOCK_TIMEOUT = int(env('JOBS_LOCK_TIMEOUT', '900'))    # a running job older than this is assumed lost
JOBS_POLL_SECONDS = float(env('JOBS_POLL_SECONDS', '5'))
JOBS_KEEP_DAYS = int(env('JOBS_KEEP_DAYS', '30'))           # finished jobs and sent emails are deleted after this
JOBS_KEEP_FAILED_DAYS = int(env('JOBS_KEEP_FAILED_DAYS', '90'))  # dead jobs and failed emails
JOBS_EMAIL_BODY_DAYS = int(env('JOBS_EMAIL_BODY_DAYS', '7'))  # sent emails' bodies (live invite links) are blanked

# Stripe
STRIPE_SECRET_KEY = env('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = env('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = env('STRIPE_WEBHOOK_SECRET', '')
TRIAL_DAYS = int(env('TRIAL_DAYS', '14'))
# Days a workspace keeps working after a failed payment before it locks.
PAYMENT_GRACE_DAYS = int(env('PAYMENT_GRACE_DAYS', '7'))

# Plans. Price IDs come from your Stripe dashboard (Products > Prices).
# External collaborators (clients, subcontractors, consultants) are free
# and unlimited on every plan; only internal staff seats count.
PLANS = {
    'starter': {
        'name': 'Starter',
        'price_label': '£49',
        'interval': 'month',
        'stripe_price_id': env('STRIPE_PRICE_STARTER', ''),
        'max_staff': 3,
        'max_active_projects': 10,
        'blurb': 'For a small team running a handful of jobs.',
    },
    'team': {
        'name': 'Team',
        'price_label': '£129',
        'interval': 'month',
        'stripe_price_id': env('STRIPE_PRICE_TEAM', ''),
        'max_staff': 10,
        'max_active_projects': 50,
        'blurb': 'Bids, live jobs and a full supply chain.',
    },
    'business': {
        'name': 'Business',
        'price_label': '£299',
        'interval': 'month',
        'stripe_price_id': env('STRIPE_PRICE_BUSINESS', ''),
        'max_staff': 30,
        'max_active_projects': None,  # unlimited
        'blurb': 'Multiple teams and regions.',
    },
}
TRIAL_PLAN = 'team'

# Security headers and cookies. `manage.py check --deploy` is clean apart from two deliberate choices:
#  * security.W005 (SECURE_HSTS_INCLUDE_SUBDOMAINS off): other services may run on subdomains of
#    the domain over plain HTTP. SECURE_HSTS_INCLUDE_SUBDOMAINS=1 once every subdomain is HTTPS.
#  * security.W021 (SECURE_HSTS_PRELOAD off): preloading is hard to undo, so it waits for the
#    final domain. SECURE_HSTS_PRELOAD=1 when ready (it also needs include-subdomains).
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # A year, unless SECURE_HSTS_SECONDS says otherwise (0 switches it off, e.g. while trying a new domain)
    SECURE_HSTS_SECONDS = int(env('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
    SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', False)
    # Browsers only accept a __Host- cookie if it is Secure, on / and has no Domain, so a sibling
    # subdomain can't plant or overwrite the session. (The new name signs everyone out once.)
    SESSION_COOKIE_NAME = '__Host-sessionid'
SECURE_CONTENT_TYPE_NOSNIFF = True
# Invite and password-reset links carry their token in the path: never send it to another site
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
CSRF_COOKIE_SAMESITE = 'Lax'
# CSRF_COOKIE_HTTPONLY stays off: static/js/app.js reads the token for the pipeline board's fetch()
# Content-Security-Policy (security/middleware.py). CSP_REPORT_ONLY=1 sends it as report-only.
CSP_REPORT_ONLY = env_bool('CSP_REPORT_ONLY', False)

# The test suite turns two-factor enforcement off except in the tests that switch it back on
# (security/tests), so the other apps' tests can keep signing in with force_login.
TEST_RUNNER = 'security.testing.TestRunner'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': env('LOG_LEVEL', 'INFO')},
    'loggers': {'django.db.backends': {'level': 'WARNING'}, 'django.request': {'level': 'ERROR'}},
}
