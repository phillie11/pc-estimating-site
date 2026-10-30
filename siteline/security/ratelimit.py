"""
A small fixed-window rate limiter. No Redis and no cache: counts are rows in the app's own
database (security.models.RateLimitCounter), so every web process and server shares them, on
SQLite, MySQL (PythonAnywhere) and PostgreSQL (AWS) alike.

    from security.ratelimit import hit
    if not hit('signup', ip, limit=10, window=3600):
        ...  # over the limit

Each hit is one atomic `UPDATE ... SET count = count + 1`. The first hit in a window inserts the
row inside a savepoint, and an insert that loses a race (IntegrityError) falls back to the update,
so no transaction is left aborted on PostgreSQL and no count is lost. A table of its own is never
culled the way a cache is, so a flood of other keys can't reset a limit. IPv6 addresses are
counted per /64, the block one customer line or host normally gets, so cycling addresses inside
it doesn't buy fresh limits. The durable lock on wrong two-factor codes is separate again
(TwoFactorDevice.failed_attempts).

The middleware (security.middleware.RateLimitMiddleware) applies the limits in RULES to the
routes that need them, so views don't have to.
"""
import hashlib
import ipaddress
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Callable, Optional

from django.db import IntegrityError, transaction
from django.db.models import F


def _key(scope, ident, window, now):
    bucket = int(now // window)
    digest = hashlib.sha256(str(ident).encode()).hexdigest()[:32]  # emails and IPs aren't stored as-is
    return f'rl:{scope}:{digest}:{bucket}', window - (now % window)


def hit(scope, ident, limit, window, now=None):
    """Count one request. True if it's within `limit` per `window` seconds."""
    from .models import RateLimitCounter

    now = time.time() if now is None else now
    key, ttl = _key(scope, ident, window, now)
    counters = RateLimitCounter.objects
    with transaction.atomic():
        if not counters.filter(key=key).update(count=F('count') + 1):
            try:
                with transaction.atomic():  # savepoint: a duplicate key mustn't abort the outer transaction
                    counters.create(key=key, count=1,
                                    expires_at=datetime.fromtimestamp(now + ttl, tz=dt_timezone.utc))
            except IntegrityError:  # another request started this window first
                counters.filter(key=key).update(count=F('count') + 1)
            else:
                # A new window has started: clear out finished ones
                counters.filter(expires_at__lt=datetime.fromtimestamp(now, tz=dt_timezone.utc)).delete()
        count = counters.filter(key=key).values_list('count', flat=True).first() or 1
    return count <= limit


def retry_after(window, now=None):
    now = time.time() if now is None else now
    return int(window - (now % window)) + 1


@dataclass(frozen=True)
class Rule:
    """`limit` requests per `window` seconds, counted per `by` (ip, user or a POSTed field)."""
    scope: str
    url_names: tuple
    limit: int
    window: int
    methods: tuple = ('POST',)
    by: str = 'ip'
    when: Optional[Callable] = None
    any_url: bool = False  # match every route under a workspace (uploads, exports, search)


def is_upload(request):
    return (request.content_type or '').startswith('multipart/form-data')


def is_export(request):
    return bool(request.GET.get('export'))


def is_search(request):
    return bool(request.GET.get('q', '').strip())


def is_invite(request):
    return request.POST.get('action') == 'invite'


# Generous for people, tight for scripts. A limit that bites shows the "slow down" page (429).
RULES = (
    Rule('login', ('login',), limit=30, window=600),  # per IP; django-axes also locks email + IP after 6 failures
    Rule('signup', ('signup',), limit=10, window=3600),
    Rule('password-reset', ('password_reset',), limit=10, window=3600),
    Rule('password-reset-email', ('password_reset',), limit=3, window=3600, by='post:email'),
    Rule('team-invite', ('team',), limit=30, window=3600, by='user'),
    Rule('project-invite', ('project_team',), limit=30, window=3600, by='user', when=is_invite),
    Rule('invite-accept', ('accept_team_invite', 'accept_project_invite'), limit=20, window=3600),
    Rule('totp', ('two_factor_verify',), limit=10, window=600),
    Rule('totp-session', ('two_factor_verify', 'two_factor_setup', 'step_up'), limit=10, window=600,
         by='session'),
    Rule('upload', (), limit=60, window=600, by='user', when=is_upload, any_url=True),
    Rule('export', (), limit=20, window=600, methods=('GET',), by='user', when=is_export, any_url=True),
    Rule('search', (), limit=120, window=60, methods=('GET',), by='user', when=is_search, any_url=True),
    Rule('stripe-webhook', ('stripe_webhook',), limit=600, window=60),
)


def ip_bucket(ip):
    """An IPv4 address as it is; an IPv6 address as its /64 network."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped is not None:  # ::ffff:1.2.3.4 is just an IPv4 address
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f'{addr}/64', strict=False))
    return str(addr)


def identity(request, by, client_ip):
    client_ip = ip_bucket(client_ip)
    if by == 'ip':
        return client_ip
    if by == 'user':
        user = getattr(request, 'user', None)
        return f'u{user.pk}' if user is not None and user.is_authenticated else f'ip{client_ip}'
    if by == 'session':
        from .twofactor import PENDING_KEY
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return f'u{user.pk}'
        pending = request.session.get(PENDING_KEY) if hasattr(request, 'session') else None
        return f'p{pending["uid"]}' if pending else f'ip{client_ip}'
    if by.startswith('post:'):
        value = request.POST.get(by[5:], '').strip().lower()
        return value or None
    raise ValueError(by)
