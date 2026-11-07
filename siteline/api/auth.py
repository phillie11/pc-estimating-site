"""
Making and checking company API tokens.

A token looks like `slk_<prefix>_<secret>`:

    prefix   10 random letters and digits, stored as they are so the row can be found (not a secret)
    secret   40 random letters and digits (about 238 bits), never stored: only a salted PBKDF2 hash

As with two-factor recovery codes (security.models.RecoveryCode), the hash is made and checked with
Django's PBKDF2 hasher, whose verify() compares in constant time. The secret is random, not chosen by
a person, so it needs far fewer rounds than a password to be out of reach offline; TOKEN_HASH_ITERATIONS
keeps each call's check to a few milliseconds. An unknown prefix still runs one hash, so a wrong token
takes as long to refuse as a right one takes to accept.

authenticate(request) -> (ApiToken, None) or (None, (status, message))

A token works only while:
  * it is not revoked and has not run out,
  * the person who made it is still an active owner or admin of the company (a token never reads more
    than its maker could, and stops when they leave or lose the role),
  * the company's subscription is in good standing (402 otherwise, as the workspace's lock page).

Limits: LIMIT calls per WINDOW seconds per token, and BAD_LIMIT refused tokens per address per WINDOW
(security.ratelimit.hit, the same database counters as every other limit).
"""
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.db import transaction
from django.utils import timezone

from security import ratelimit
from security.ip import client_ip
from tenancy.models import Membership

TOKEN_START = 'slk_'
ALPHABET = string.ascii_letters + string.digits
PREFIX_LENGTH = 10
SECRET_LENGTH = 40
TOKEN_HASH_ITERATIONS = 10_000

LIMIT, WINDOW = 600, 600        # 600 calls per 10 minutes per token: one a second on average
BAD_LIMIT = 30                  # refused tokens per address per WINDOW
KEEP_DAYS = 90                  # API call log
TOUCH_SECONDS = 60              # last-used time and pruning at most once a minute per token

_DUMMY_HASH = None


def _hasher():
    return PBKDF2PasswordHasher()


def _random(n):
    return ''.join(secrets.choice(ALPHABET) for _ in range(n))


def hash_secret(secret):
    hasher = _hasher()
    return hasher.encode(secret, hasher.salt(), TOKEN_HASH_ITERATIONS)


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_secret(_random(SECRET_LENGTH))
    return _DUMMY_HASH


def create_token(org, user, name, scopes, expires_at=None):
    """Make a token for `org`. Returns (ApiToken, the full token text): the only time the text exists."""
    from .models import ALWAYS, SCOPE_LABELS, ApiToken
    clean = sorted({s for s in scopes if s in SCOPE_LABELS} | set(ALWAYS))
    secret = _random(SECRET_LENGTH)
    for _ in range(5):
        prefix = _random(PREFIX_LENGTH)
        # all_objects: prefixes are unique across every company, so the check is too
        if not ApiToken.all_objects.filter(prefix=prefix).exists():
            break
    token = ApiToken.objects.create(organization=org, name=name, prefix=prefix, secret_hash=hash_secret(secret),
                                    scopes=clean, created_by=user, expires_at=expires_at)
    return token, f'{TOKEN_START}{prefix}_{secret}'


def parse(text):
    """(prefix, secret) from a token's text, or None if it isn't shaped like one."""
    text = (text or '').strip()
    if not text.startswith(TOKEN_START):
        return None
    body = text[len(TOKEN_START):]
    prefix, sep, secret = body.partition('_')
    if not sep or len(prefix) != PREFIX_LENGTH or len(secret) != SECRET_LENGTH:
        return None
    if not all(c in ALPHABET for c in prefix + secret):
        return None
    return prefix, secret


def token_from_header(request):
    """The token text from `Authorization: Bearer <token>`. Tokens in the query string are not accepted."""
    header = request.META.get('HTTP_AUTHORIZATION', '')
    scheme, _, value = header.partition(' ')
    if scheme.lower() != 'bearer':
        return ''
    return value.strip()


def find(text):
    """The ApiToken this text is, or None. Constant-time on the secret, and one hash whatever happens."""
    from .models import ApiToken
    parts = parse(text)
    hasher = _hasher()
    if parts is None:
        hasher.verify(_random(SECRET_LENGTH), _dummy_hash())
        return None
    prefix, secret = parts
    # all_objects: no organisation is current yet; the row is pinned by its unique prefix and then by the hash
    token = ApiToken.all_objects.select_related('organization', 'created_by').filter(prefix=prefix).first()
    if token is None:
        hasher.verify(secret, _dummy_hash())
        return None
    if not hasher.verify(secret, token.secret_hash):
        return None
    return token


def _limits_on():
    return getattr(settings, 'RATELIMIT_ENABLED', True)  # off only on a local demo server, as every other limit


def maker_still_admin(token):
    if token.created_by_id is None or not token.created_by.is_active:
        return False
    return Membership.objects.filter(organization_id=token.organization_id, user_id=token.created_by_id,
                                     role__in=Membership.ADMIN_ROLES, is_active=True).exists()


def authenticate(request):
    ip = ratelimit.ip_bucket(client_ip(request) or 'unknown')
    text = token_from_header(request)
    if not text:
        return None, (401, 'Send your API token in the Authorization header: "Authorization: Bearer slk_...".')
    token = find(text)
    if token is None or not token.is_live or not maker_still_admin(token):
        if _limits_on() and not ratelimit.hit('api-bad-token', ip, BAD_LIMIT, WINDOW):
            return None, (429, 'Too many tries with a token that does not work. Wait ten minutes.')
        return None, (401, 'This API token does not work. It may have been revoked or run out, or the person who '
                           'made it is no longer an owner or admin. Ask an owner or admin for a new one.')
    if not token.organization.has_access:
        return token, (402, 'This workspace is locked until its subscription is paid. An owner or admin can sort '
                           'it out on the Billing page.')
    if _limits_on() and not ratelimit.hit('api-token', f't{token.pk}', LIMIT, WINDOW):
        return token, (429, f'Too many calls: this token may make {LIMIT} calls every {WINDOW // 60} minutes.')
    return token, None


def record(token, request, status):
    """Log the call, and every minute or so note when the token was last used and drop old log rows."""
    from .models import ApiAccess, ApiToken
    now = timezone.now()
    ApiAccess.all_objects.create(organization_id=token.organization_id, token=token, at=now, method=request.method[:8],
                                 path=request.get_full_path()[:300], status=status, ip=client_ip(request))
    if token.last_used_at is None or now - token.last_used_at >= timedelta(seconds=TOUCH_SECONDS):
        with transaction.atomic():
            # Pinned to this token's row and company (the caller holds both)
            ApiToken.all_objects.filter(pk=token.pk).update(last_used_at=now)
            ApiAccess.all_objects.filter(organization_id=token.organization_id,
                                         at__lt=now - timedelta(days=KEEP_DAYS)).delete()
        token.last_used_at = now
