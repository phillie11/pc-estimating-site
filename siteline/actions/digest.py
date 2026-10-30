"""
The round-up email's rules: when it goes, what it lists, and the signed unsubscribe link.

The daily one goes only when something is overdue or due within URGENT_DAYS. Items with no
date never send it on their own; when it goes anyway they are listed, capped at EXTRA_CAP.
The weekly one is a full summary: it goes whenever anything is waiting and lists everything.
Each person picks daily (default), weekly on Mondays, or off (actions.DigestPreference).
"""
from datetime import timedelta

from django.core import signing
from django.urls import reverse
from django.utils import timezone

URGENT_DAYS = 3
EXTRA_CAP = 5
WEEKLY_DAY = 0  # Monday
UNSUBSCRIBE_SALT = 'actions.digest.unsubscribe'


def is_due_day(frequency, today=None):
    from .models import DigestPreference
    today = today or timezone.localdate()
    if frequency == DigestPreference.OFF:
        return False
    if frequency == DigestPreference.WEEKLY:
        return today.weekday() == WEEKLY_DAY
    return True


def sections_for(items, today=None, cap=EXTRA_CAP):
    """
    (urgent, sections, more): urgent is True when anything is overdue or due within
    URGENT_DAYS; sections are [(label, [item...])]; more counts items left out by the cap
    (None lists them all).
    """
    today = today or timezone.localdate()
    soon = today + timedelta(days=URGENT_DAYS)
    overdue = [i for i in items if i.due is not None and i.due < today]
    due_soon = [i for i in items if i.due is not None and today <= i.due <= soon]
    listed = {id(i) for i in overdue + due_soon}
    rest = [i for i in items if id(i) not in listed]
    sections = []
    if overdue:
        sections.append(('Overdue', overdue))
    if due_soon:
        sections.append((f'Due in the next {URGENT_DAYS} days', due_soon))
    if rest:
        sections.append(('Also waiting on you', rest if cap is None else rest[:cap]))
    return bool(overdue or due_soon), sections, 0 if cap is None else max(0, len(rest) - cap)


def unsubscribe_token(user):
    # No timestamp: the same link for a person every time, and old emails' links keep working
    return signing.Signer(salt=UNSUBSCRIBE_SALT).sign_object({'u': user.pk}, compress=True)


def user_from_token(token):
    """The user a signed unsubscribe token was made for, or None if it was tampered with."""
    from django.contrib.auth import get_user_model
    try:
        data = signing.Signer(salt=UNSUBSCRIBE_SALT).unsign_object(token)
    except signing.BadSignature:
        return None
    return get_user_model().objects.filter(pk=data.get('u'), is_active=True).first()


def unsubscribe_path(user):
    return reverse('digest_unsubscribe', args=[unsubscribe_token(user)])
