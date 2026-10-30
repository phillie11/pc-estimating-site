"""Notifications: in-app (the bell) plus an email that links back to the portal.

Emails go through the outbox (jobs/outbox.py): rendered here, stored, and sent
by the worker with retries, or straight away while EMAIL_SYNC is on. Failures are
logged, never shown to the user as errors.
"""
import logging

from django.conf import settings
from django.db import transaction
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
    from jobs.outbox import queue_email
    try:
        with transaction.atomic():  # a savepoint: a failed write mustn't break the caller's transaction
            queue_email(prefix + subject, body, recipients, org=org,
                        reply_to=[reply_to] if isinstance(reply_to, str) else reply_to)
    except Exception:  # noqa: BLE001 - email must never break a request
        logger.exception('Queueing an email failed (%s, %s recipient(s))', subject, len(recipients))


def alert(users, title, url, project=None, actor=None, org=None, email=True):
    """
    Tell people something needs them. Each gets an in-app notification and,
    unless email=False, a short email with a link to the item in the portal.
    Nothing is attached and nothing else is sent: the portal is the record.
    """
    from .context import get_current_org
    from .models import Notification
    org = org or (project.organization if project else get_current_org())
    seen = set()
    for user in users:
        if user is None or user.pk in seen or (actor is not None and user.pk == actor.pk) or not user.is_active:
            continue
        seen.add(user.pk)
        Notification.objects.create(organization=org, user=user, actor=actor, project=project, title=title[:255],
                                    url=url[:300])
        if email:
            send(user.email, title, 'notification', {'user': user, 'actor': actor, 'project': project, 'title': title,
                                                      'link': absolute(url)}, org=org)
