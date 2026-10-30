"""
Who needs two-factor sign-in, the half-signed-in state between password and code, the
"remember this device" cookie and the step-up (recent sign-in) clock.

Sign-in with two-factor on is two steps. The password step (accounts.views.LoginView) does NOT
call login(): it parks the user's id in the session (start_pending) and sends them to the code
page. Only a correct code (or recovery code) calls login(), so until then the person is
anonymous to every view, decorator and middleware in the app.
"""
import time

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import TwoFactorDevice

PENDING_KEY = 'sl_2fa_pending'
AUTH_AT_KEY = 'sl_auth_at'
REMEMBER_SALT = 'security.remember-device'


def _setting(name, default):
    return getattr(settings, name, default)


# ---------------------------------------------------------------------------
# Who needs it
# ---------------------------------------------------------------------------

def device_for(user):
    """The user's confirmed authenticator, or None."""
    if not getattr(user, 'pk', None):
        return None
    return TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=False).first()


def has_two_factor(user):
    return TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def requires_two_factor(user):
    """Owners and admins of any workspace, and platform staff, must use two-factor sign-in."""
    from tenancy.models import Membership  # Membership is not tenant-scoped: it's how a workspace is found

    if user.is_superuser or user.is_staff:
        return True
    return Membership.objects.filter(user=user, is_active=True, role__in=Membership.ADMIN_ROLES,
                                     organization__isnull=False).exists()


# ---------------------------------------------------------------------------
# Between password and code
# ---------------------------------------------------------------------------

def start_pending(request, user, next_url):
    request.session[PENDING_KEY] = {
        'uid': user.pk, 'at': int(time.time()), 'next': next_url,
        'backend': getattr(user, 'backend', 'django.contrib.auth.backends.ModelBackend'),
        # If the password changes (a reset) between the two steps, the half sign-in is void
        'pw': salted_hmac('security.pending', user.password).hexdigest(),
    }


def pending_user(request):
    """The user who has passed the password step in this session, if it's still valid."""
    from accounts.models import User

    data = request.session.get(PENDING_KEY)
    if not data:
        return None
    if time.time() - data.get('at', 0) > _setting('TWO_FACTOR_PENDING_SECONDS', 300):
        clear_pending(request)
        return None
    user = User.objects.filter(pk=data.get('uid'), is_active=True).first()
    if user is None or not constant_time_compare(
            data.get('pw', ''), salted_hmac('security.pending', user.password).hexdigest()):
        clear_pending(request)
        return None
    user.backend = data.get('backend')
    return user


def pending_next(request):
    return (request.session.get(PENDING_KEY) or {}).get('next') or ''


def clear_pending(request):
    request.session.pop(PENDING_KEY, None)


# ---------------------------------------------------------------------------
# Remember this device for 30 days
# ---------------------------------------------------------------------------

def _fingerprint(user, device):
    """Changes when the password changes or two-factor is reset, which voids every remembered device."""
    return salted_hmac(REMEMBER_SALT, f'{user.pk}|{user.password}|{device.secret}|{device.confirmed_at}').hexdigest()


def remember_cookie_name():
    return _setting('TWO_FACTOR_REMEMBER_COOKIE', 'sl_trusted_device')


def remember_days():
    return _setting('TWO_FACTOR_REMEMBER_DAYS', 30)


def set_remember_cookie(response, user, device):
    value = signing.TimestampSigner(salt=REMEMBER_SALT).sign(f'{user.pk}:{_fingerprint(user, device)}')
    response.set_cookie(remember_cookie_name(), value, max_age=remember_days() * 86400,
                        secure=True, httponly=True, samesite='Lax')


def is_remembered(request, user, device):
    raw = request.COOKIES.get(remember_cookie_name())
    if not raw or device is None:
        return False
    try:
        value = signing.TimestampSigner(salt=REMEMBER_SALT).unsign(raw, max_age=remember_days() * 86400)
    except signing.BadSignature:  # forged, tampered with or expired (SignatureExpired is a subclass)
        return False
    uid, _, fingerprint = value.partition(':')
    return uid == str(user.pk) and constant_time_compare(fingerprint, _fingerprint(user, device))


def forget_device(response):
    response.delete_cookie(remember_cookie_name(), samesite='Lax')


# ---------------------------------------------------------------------------
# Step-up: password or code in the last 10 minutes
# ---------------------------------------------------------------------------

def mark_authenticated(request):
    request.session[AUTH_AT_KEY] = int(time.time())


def recently_authenticated(request):
    at = request.session.get(AUTH_AT_KEY)
    return bool(at) and time.time() - at <= _setting('STEP_UP_SECONDS', 600)
