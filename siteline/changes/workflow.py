"""
Status changes for early warnings and change events, registered in ChangesConfig.ready().
Views call core.workflow.apply_transition(record, name, user). Staff only, as the records are.

  Early warning:  OPEN --close--> CLOSED          OPEN --escalate--> ESCALATED (it became a change event)
                  CLOSED / ESCALATED --reopen--> OPEN

  Change event:   IDENTIFIED --notify--> NOTIFIED --quote--> QUOTED --agree--> AGREED --implement--> IMPLEMENTED
                  (quote straight from IDENTIFIED, and agree straight from NOTIFIED, are allowed: JCT
                  instructions are often agreed without a formal quotation)
                  IDENTIFIED / NOTIFIED / QUOTED --reject--> REJECTED
"""
from django.utils import timezone

from core.access import record_staff
from core.workflow import Transition

from .models import ChangeEvent as CE
from .models import EarlyWarning as EW


def _stamp_closed(ew, user):
    ew.closed_at = timezone.now()


def _clear_closed(ew, user):
    ew.closed_at = None


def _notified(ce, user):
    ce.notified_on = ce.notified_on or timezone.localdate()


def _quoted(ce, user):
    ce.quotation_submitted_on = ce.quotation_submitted_on or timezone.localdate()


def _agreed(ce, user):
    if ce.quotation_submitted_on and not ce.reply_received_on:
        ce.reply_received_on = timezone.localdate()


EARLY_WARNING_TRANSITIONS = [
    Transition('close', source=(EW.STATUS_OPEN,), target=EW.STATUS_CLOSED, label='close', allowed=record_staff,
               on_apply=_stamp_closed),
    Transition('escalate', source=(EW.STATUS_OPEN,), target=EW.STATUS_ESCALATED, label='escalate',
               allowed=record_staff, on_apply=_stamp_closed),
    Transition('reopen', source=(EW.STATUS_CLOSED, EW.STATUS_ESCALATED), target=EW.STATUS_OPEN, label='reopen',
               allowed=record_staff, on_apply=_clear_closed),
]

CHANGE_EVENT_TRANSITIONS = [
    Transition('notify', source=(CE.STATUS_IDENTIFIED,), target=CE.STATUS_NOTIFIED, label='mark as notified',
               allowed=record_staff, on_apply=_notified),
    Transition('quote', source=(CE.STATUS_IDENTIFIED, CE.STATUS_NOTIFIED), target=CE.STATUS_QUOTED,
               label='mark as quoted', allowed=record_staff, on_apply=_quoted),
    Transition('agree', source=(CE.STATUS_NOTIFIED, CE.STATUS_QUOTED), target=CE.STATUS_AGREED,
               label='mark as agreed', allowed=record_staff, on_apply=_agreed),
    Transition('reject', source=(CE.STATUS_IDENTIFIED, CE.STATUS_NOTIFIED, CE.STATUS_QUOTED), target=CE.STATUS_REJECTED,
               label='mark as rejected', allowed=record_staff),
    Transition('implement', source=(CE.STATUS_AGREED,), target=CE.STATUS_IMPLEMENTED, label='mark as implemented',
               allowed=record_staff),
]

EARLY_WARNING_AUDIT = ('description', 'raised_by_side', 'risk_cost', 'risk_time', 'risk_quality', 'mitigation',
                       'meeting_date')
CHANGE_EVENT_AUDIT = ('origin', 'description', 'time_impact_days', 'aware_on', 'notified_on',
                      'quotation_instructed_on', 'quotation_submitted_on', 'reply_received_on', 'failure_notified_on',
                      'delay_notice_given_on')
