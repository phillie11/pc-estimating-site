"""
The visitor's IP address, for rate limits and for django-axes (AXES_CLIENT_IP_CALLABLE).

Headers can be forged by anyone, so by default only REMOTE_ADDR is trusted. Behind a proxy that
REMOTE_ADDR is the proxy, so every visitor would share one limit; say which header the proxy sets:

  * PythonAnywhere: CLIENT_IP_HEADER=X-Real-IP
  * AWS: nothing to set; config/alb_wsgi.py already puts the load balancer's last
    X-Forwarded-For entry into REMOTE_ADDR.
  * Any other proxy chain: CLIENT_IP_HEADER=X-Forwarded-For and CLIENT_IP_PROXY_COUNT=<n>, the
    number of proxies that add to the header; the address that many from the end is used, so
    anything the visitor put at the front is ignored.
"""
import ipaddress

from django.conf import settings


def _valid(value):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request):
    """
    The address, or None if there isn't a valid one (force_login()'s bare request, a broken proxy).
    django-axes stores it in a GenericIPAddressField, so it must never be a placeholder string.
    """
    header = (getattr(settings, 'CLIENT_IP_HEADER', '') or '').strip()
    if header:
        raw = request.META.get('HTTP_' + header.upper().replace('-', '_'), '')
        if raw:
            parts = [p for p in (x.strip() for x in raw.split(',')) if p]
            count = max(1, int(getattr(settings, 'CLIENT_IP_PROXY_COUNT', 1) or 1))
            if parts:
                ip = _valid(parts[-count] if len(parts) >= count else parts[0])
                if ip:
                    return ip
    return _valid(request.META.get('REMOTE_ADDR', '') or '')
