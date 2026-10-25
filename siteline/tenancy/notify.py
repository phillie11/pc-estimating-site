"""Email notifications. Failures are logged, never shown to the user as errors."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def absolute(path):
    return f'{settings.SITE_URL}{path}'


def send(to, subject, template, context, org=None, reply_to=None):
    recipients = [to] if isinstance(to, str) else [getattr(u, 'email', u) for u in to]
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    ctx = {'PRODUCT_NAME': settings.PRODUCT_NAME, 'org': org, 'SITE_URL': settings.SITE_URL, **context}
    body = render_to_string(f'email/{template}.txt', ctx)
    prefix = f'[{org.name}] ' if org else ''
    try:
        send_mail(prefix + subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception:  # noqa: BLE001 - email must never break a request
        logger.exception('Email to %s failed (%s)', recipients, subject)
