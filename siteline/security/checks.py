import ipaddress

from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def two_factor_enforced(app_configs, **kwargs):
    if not getattr(settings, 'TWO_FACTOR_ENFORCE', True):
        return [checks.Warning(
            'TWO_FACTOR_ENFORCE is off, so owners and admins can work without two-factor sign-in.',
            hint='Remove TWO_FACTOR_ENFORCE=0 from the environment.', id='security.W001')]
    return []


def _is_ip(host):
    host = host.strip().strip('[]')
    try:
        ipaddress.ip_address(host.rsplit(':', 1)[0] if host.count(':') == 1 else host)
        return True
    except ValueError:
        return False


@checks.register(checks.Tags.security, deploy=True)
def client_ip_header_trusted(app_configs, **kwargs):
    """
    CLIENT_IP_HEADER is only safe when nobody can reach the app except through the proxy that sets
    it. A bare IP in ALLOWED_HOSTS suggests the server answers on its own address, where a visitor
    could send the header themselves and pick any address (best-effort hint, not proof).
    """
    if settings.DEBUG or not (getattr(settings, 'CLIENT_IP_HEADER', '') or '').strip():
        return []
    ips = [h for h in settings.ALLOWED_HOSTS if _is_ip(h)]
    if ips:
        return [checks.Warning(
            f'CLIENT_IP_HEADER is set, but ALLOWED_HOSTS includes {", ".join(ips)}. If the server can be '
            'reached on that address without going through the proxy, anyone can forge the header and '
            'dodge rate limits and the login lockout.',
            hint='Take the IP address out of ALLOWED_HOSTS, or unset CLIENT_IP_HEADER.', id='security.W003')]
    return []

