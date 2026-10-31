"""
Diary entry status changes, registered in DiaryConfig.ready().

  RECORDED --unlock (project manager or admin, with a reason)--> UNLOCKED
  UNLOCKED --relock (anyone who can edit the diary)-----------> RECORDED

Locking itself isn't a status: an entry locks by date (diary/rules.py). `unlock` opens a locked
entry until the end of the next working day and writes the reason into the audit trail.
"""
from django.utils import timezone

from core.workflow import Transition

from . import rules
from .models import DiaryEntry as E


def may_unlock(entry, user):
    from .access import is_pm_or_admin
    return is_pm_or_admin(user, entry.project)


def may_relock(entry, user):
    from .access import user_on_job
    return user_on_job(user, entry.project)


def _unlocked(entry, user, reason=''):
    entry.unlocked_until = rules.unlock_until(timezone.localdate(), *entry.calendar)
    entry.unlock_reason = reason


def _relocked(entry, user, **params):
    entry.unlocked_until = None


TRANSITIONS = [
    Transition('unlock', source=(E.STATUS_OPEN, E.STATUS_UNLOCKED), target=E.STATUS_UNLOCKED,
               label='unlock for correction', allowed=may_unlock, on_apply=_unlocked),
    Transition('relock', source=(E.STATUS_UNLOCKED,), target=E.STATUS_OPEN, label='lock again', allowed=may_relock,
               on_apply=_relocked),
]

AUDIT_FIELDS = ('entry_date', 'weather_am', 'weather_pm', 'weather_note', 'site_start', 'site_finish',
                'own_staff_count', 'own_staff', 'hs_incident', 'hs_notes', 'notes', 'unlocked_until', 'unlock_reason')
