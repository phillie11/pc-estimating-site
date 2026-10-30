"""
Security middleware, in the order settings.MIDDLEWARE lists them:

  * SecurityHeadersMiddleware: Content-Security-Policy and Permissions-Policy on every response.
  * RateLimitMiddleware: the limits in ratelimit.RULES, answered with a friendly 429 page.
  * TwoFactorEnforcementMiddleware: owners, admins and platform staff without two-factor
    sign-in set up are sent to set it up, wherever they go.
  * StepUpMiddleware: billing, team and role changes, exports and archiving/deleting need a
    password or code in the last 10 minutes (STEP_UP in this file).

Routes are matched by URL name, so the views themselves (in other apps) don't change, and the
whole list is in one place to review.
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from . import ratelimit, twofactor
from .ip import client_ip

log = logging.getLogger('security')


def _url_name(request):
    match = getattr(request, 'resolver_match', None)
    return getattr(match, 'url_name', None)


def _is_static(request):
    return request.path.startswith(settings.STATIC_URL)


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def _storage_sources():
    """Where signed links to uploaded files point (images are shown from there), if cloud storage is on."""
    if not getattr(settings, 'STORAGE_BUCKET', ''):
        return []
    from urllib.parse import urlsplit
    endpoint = settings.STORAGES['default'].get('OPTIONS', {}).get('endpoint_url') or ''
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.hostname:
        return []
    host = parts.hostname + (f':{parts.port}' if parts.port else '')
    # Path-style (endpoint/bucket/key) and virtual-hosted (bucket.endpoint/key) links
    return [f'{parts.scheme}://{host}', f'{parts.scheme}://*.{host}']


def build_csp():
    """
    Built from what the templates load: our own CSS, JS and fonts (fonts are self-hosted, see
    static/fonts), images from our own views (logos, snag photos) or the file store's signed
    links, and nothing inline except styles. Inline styles stay allowed because the brand colour
    is set in a <style> block and many templates use style="..." attributes; scripts, which are
    what an injection would need, are 'self' only. Forms may only post to us, or be redirected on
    to Stripe Checkout and the Stripe billing portal.
    """
    img = ["'self'"] + _storage_sources()
    policy = {
        'default-src': ["'self'"],
        'script-src': ["'self'"],
        'style-src': ["'self'", "'unsafe-inline'"],
        'img-src': img,
        'font-src': ["'self'"],
        'connect-src': ["'self'"],
        'frame-src': ["'none'"],
        'object-src': ["'none'"],
        'base-uri': ["'self'"],
        'form-action': ["'self'", 'https://checkout.stripe.com', 'https://billing.stripe.com'],
        'frame-ancestors': ["'none'"],
    }
    for directive, extra in getattr(settings, 'CSP_EXTRA', {}).items():
        policy.setdefault(directive, []).extend(extra)
    return '; '.join(f'{k} {" ".join(v)}' for k, v in policy.items())


PERMISSIONS_POLICY = ', '.join(f'{f}=()' for f in (
    'accelerometer', 'autoplay', 'browsing-topics', 'camera', 'display-capture', 'geolocation',
    'gyroscope', 'magnetometer', 'microphone', 'midi', 'payment', 'usb', 'xr-spatial-tracking',
))


class SecurityHeadersMiddleware:
    """
    Django 5.2 has no Content-Security-Policy support (it arrives in 6.0). django-csp would add a
    dependency and a settings vocabulary for what is one fixed header here (no nonces needed: the
    templates have no inline scripts), so it's built once at start-up instead.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = build_csp()
        self.csp_header = ('Content-Security-Policy-Report-Only' if getattr(settings, 'CSP_REPORT_ONLY', False)
                           else 'Content-Security-Policy')

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault(self.csp_header, self.csp)
        response.headers.setdefault('Permissions-Policy', PERMISSIONS_POLICY)
        return response


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

def too_many_requests(request, retry_after=60):
    response = render(request, 'security/429.html', {'retry_minutes': max(1, round(retry_after / 60))}, status=429)
    response['Retry-After'] = str(retry_after)
    return response


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not getattr(settings, 'RATELIMIT_ENABLED', True) or _is_static(request):
            return None
        name = _url_name(request)
        in_workspace = 'org_slug' in view_kwargs
        ip = None
        for rule in ratelimit.RULES:
            if request.method not in rule.methods:
                continue
            if rule.any_url:
                if not in_workspace:
                    continue
            elif name not in rule.url_names:
                continue
            if rule.when is not None and not rule.when(request):
                continue
            ip = ip or client_ip(request) or 'unknown'
            ident = ratelimit.identity(request, rule.by, ip)
            if ident is None:
                continue
            if not ratelimit.hit(rule.scope, ident, rule.limit, rule.window):
                log.warning('Rate limit %s hit on %s', rule.scope, request.path)
                return too_many_requests(request, ratelimit.retry_after(rule.window))
        return None


# ---------------------------------------------------------------------------
# Two-factor required for owners and admins
# ---------------------------------------------------------------------------

# Reachable without two-factor set up: signing out, setting it up, and the error pages it might hit
ENFORCEMENT_EXEMPT = {'logout', 'two_factor_setup', 'two_factor_qr', 'two_factor_codes', 'health',
                      'stripe_webhook'}


class TwoFactorEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not getattr(settings, 'TWO_FACTOR_ENFORCE', True) or _is_static(request):
            return None
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or _url_name(request) in ENFORCEMENT_EXEMPT:
            return None
        if not twofactor.requires_two_factor(user) or twofactor.has_two_factor(user):
            return None
        target = reverse('two_factor_setup')
        if request.method == 'GET':
            target += '?' + urlencode({'next': request.get_full_path()})
        return redirect(target)


# ---------------------------------------------------------------------------
# Step-up re-authentication
# ---------------------------------------------------------------------------

def _post(request):
    return request.method == 'POST'


def _any(request):
    return True


# url name -> when a recent password or code is needed
STEP_UP = {
    # Billing
    'billing_checkout': _post,
    'billing_portal': _any,
    # Team members and roles
    'team': _post,            # inviting staff
    'member_update': _post,   # role, area, removal
    'invite_revoke': _post,
    'two_factor_reset_member': _post,
    'project_team': lambda r: r.method == 'POST' and ratelimit.is_invite(r),  # inviting external collaborators
    # Company settings (name, logo, brand colour, contract defaults)
    'org_settings': _post,
    # Danger zone: archive, restore and delete
    'project_archive': _post,
    'project_delete': _post,
    'sub_archive': _post,
    # Your own two-factor settings
    'two_factor_disable': _post,
    'two_factor_regenerate': _post,
}


def needs_step_up(request, view_kwargs):
    name = _url_name(request)
    rule = STEP_UP.get(name)
    if rule is not None and rule(request):
        return True
    # Data exports: any workspace list downloaded with ?export=csv
    return 'org_slug' in view_kwargs and request.method == 'GET' and bool(request.GET.get('export'))


def _safe_return(request):
    """Where to go back to after confirming: this page for a GET, the page the form was on for a POST."""
    if request.method == 'GET':
        return request.get_full_path()
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(referer, {request.get_host()}, request.is_secure()):
        return referer
    return reverse('home')


class StepUpMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or _is_static(request):
            return None
        if not needs_step_up(request, view_kwargs) or twofactor.recently_authenticated(request):
            return None
        return redirect(reverse('step_up') + '?' + urlencode({'next': _safe_return(request)}))
