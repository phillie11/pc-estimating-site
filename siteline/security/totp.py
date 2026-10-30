"""
Time-based one-time passwords (RFC 6238 on top of RFC 4226 HOTP), standard library only.

Why not django-otp: its TOTP plugin would do the same maths, but its static-token plugin (the
usual source of recovery codes) stores the codes in plain text, which we may not do, and its
device/verification model would have to be bent around the remember-device cookie and step-up
re-authentication anyway. What is left is about 30 lines of HMAC, tested against the RFC 6238
Appendix B vectors in security/tests/test_totp.py, with no extra dependency to keep patched.

Settings every authenticator app understands: SHA-1, 6 digits, 30-second steps. A code is
accepted one step either side of now (clock drift), and the step it matched is stored so the
same code can't be used twice (see TwoFactorDevice.verify).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

PERIOD = 30
DIGITS = 6
DRIFT_STEPS = 1


def new_secret():
    """A fresh 160-bit secret (the RFC 4226 recommended length), base32 without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _key(secret):
    s = secret.strip().replace(' ', '').upper()
    return base64.b32decode(s + '=' * (-len(s) % 8))


def hotp(key, counter, digits=DIGITS, digest=hashlib.sha1):
    """RFC 4226 section 5.3: HMAC the counter, dynamically truncate, keep `digits` digits."""
    mac = hmac.new(key, struct.pack('>Q', counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def time_step(now=None):
    return int((time.time() if now is None else now) // PERIOD)


def code_at(secret, now=None):
    return hotp(_key(secret), time_step(now))


def normalise(code):
    return ''.join(ch for ch in str(code or '') if ch.isdigit())


def matching_step(secret, code, now=None, drift=DRIFT_STEPS):
    """The time step `code` is valid for (now, or `drift` steps either side), or None."""
    code = normalise(code)
    if len(code) != DIGITS:
        return None
    key = _key(secret)
    current = time_step(now)
    found = None
    # Check every candidate step, so the time taken doesn't say which one matched
    for step in range(current - drift, current + drift + 1):
        if hmac.compare_digest(hotp(key, step), code):
            found = step
    return found


def provisioning_uri(secret, account, issuer):
    """The otpauth:// link a QR code carries (Google Authenticator key URI format)."""
    label = quote(f'{issuer}:{account}', safe='@:')
    query = urlencode({'secret': secret, 'issuer': issuer, 'algorithm': 'SHA1',
                       'digits': DIGITS, 'period': PERIOD}, quote_via=quote)
    return f'otpauth://totp/{label}?{query}'


def grouped(secret):
    """ABCD EFGH ... for typing in by hand."""
    return ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))
