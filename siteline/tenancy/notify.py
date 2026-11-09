"""Notifications: in-app (the bell) plus an email that links back to the portal.

Emails go through the outbox (jobs/outbox.py): rendered here, stored, and sent
by the worker with retries, or straight away while EMAIL_SYNC is on. Failures are
logged, never shown to the user as errors.

Email is only a notification: every conversation happens in the portal. So no email
has a Reply-To, the sender is DEFAULT_FROM_EMAIL's no-reply address (shown as
"<Company> via <product>" for a company's emails), and every notification starts with
a line saying replies aren't read, then the link to the item.
"""
import logging
import re
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

SENDER_NAME_MAX = 60
# Control characters (CR, LF, tab, ...), quotes, angle brackets and backslashes have no place in a header
_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\'<>\\`]')


def absolute(path):
    return f'{settings.SITE_URL}{path}'


def header_safe(text, limit=SENDER_NAME_MAX):
    """Text made safe for an email header: no line breaks, quotes or angle brackets, one line, capped."""
    text = ' '.join(_UNSAFE.sub(' ', str(text or '')).split())
    return text[:limit].strip()


def sender_for(org=None):
    """
    The From header. A company's emails show "<Company> via <PRODUCT_NAME>" with DEFAULT_FROM_EMAIL's
    address. Anything else (no company, or a name with nothing left once cleaned) gets DEFAULT_FROM_EMAIL as set.
    """
    if org is None:
        return settings.DEFAULT_FROM_EMAIL
    company = header_safe(getattr(org, 'name', ''))
    _, address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    if not company or not address:
        return settings.DEFAULT_FROM_EMAIL
    return formataddr((f'{company} via {header_safe(settings.PRODUCT_NAME)}', address))


def no_reply_line():
    return f"Reply in {settings.PRODUCT_NAME}, not to this email. Replies to this address aren't read."


def send(to, subject, template, context, org=None, invitation=False):
    """
    Render email/<template>.txt and queue it.

    There is no reply_to, by design: replies to these emails aren't read, because the portal is
    the record. Don't add one back.

    A notification's body starts with the no-reply line and then the link to the item
    (context['link'], else the company's home page). An invitation (invitation=True: the person
    may have no login yet) keeps its own wording instead; its template includes
    email/_invitation_footer.txt, which says replies aren't read and points to SUPPORT_EMAIL.
    """
    recipients = [to] if isinstance(to, str) else [getattr(u, 'email', u) for u in to]
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    ctx = {'PRODUCT_NAME': settings.PRODUCT_NAME, 'SUPPORT_EMAIL': settings.SUPPORT_EMAIL, 'org': org,
           'SITE_URL': settings.SITE_URL, **context}
    body = render_to_string(f'email/{template}.txt', ctx)
    if not invitation:
        link = ctx.get('link') or (absolute(f'/{org.slug}/') if org is not None else settings.SITE_URL)
        body = f'{no_reply_line()}\n{link}\n\n{body}'
    company = header_safe(org.name) if org is not None else ''
    prefix = f'[{company}] ' if company else ''
    from jobs.outbox import queue_email
    try:
        with transaction.atomic():  # a savepoint: a failed write mustn't break the caller's transaction
            queue_email(prefix + subject, body, recipients, org=org, from_email=sender_for(org))
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
