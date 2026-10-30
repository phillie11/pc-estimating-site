"""
RFI status changes. Registered with the RFI in RfisConfig.ready(); views call
core.workflow.apply_transition(rfi, name, user).

  OPEN ──respond──────────► RESPONDED ──close──► CLOSED
   │  └─respond_and_close──────────────────────► CLOSED
   └──────────────void─────► VOID
  RESPONDED / CLOSED / VOID ──reopen──► OPEN

Responding is open to staff and to whoever has the ball in court; closing it with the
response, closing, reopening and voiding are staff only. Responding passes the ball back to
whoever raised the RFI, to accept and close or reopen (BALL_AFTER_RESPONSE below). Voiding a
closed RFI means reopening it first. Both are recommended defaults pending the product owner.
"""
from django.utils import timezone

from core.access import record_staff, record_staff_or_ball_in_court
from core.workflow import Transition

from .models import RFI


def ball_after_response(rfi, user):
    """Who has the ball once an RFI is answered: whoever raised it. To keep it with the responder, use KEEP."""
    return rfi.created_by


BALL_AFTER_RESPONSE = ball_after_response  # or core.workflow.KEEP


def _record_response(rfi, user, response=''):
    rfi.response = response
    rfi.responded_by, rfi.responded_at = user, timezone.now()


def _respond_and_close(rfi, user, response=''):
    _record_response(rfi, user, response)
    rfi.closed_at = timezone.now()


def _stamp_closed(rfi, user):
    rfi.closed_at = timezone.now()


def _clear_closed(rfi, user):
    rfi.closed_at = None


TRANSITIONS = [
    Transition('respond', source=(RFI.STATUS_OPEN,), target=RFI.STATUS_RESPONDED, label='respond to',
               allowed=record_staff_or_ball_in_court, sets_ball_in_court=BALL_AFTER_RESPONSE,
               on_apply=_record_response),
    Transition('respond_and_close', source=(RFI.STATUS_OPEN,), target=RFI.STATUS_CLOSED, label='respond to and close',
               allowed=record_staff, sets_ball_in_court=BALL_AFTER_RESPONSE, on_apply=_respond_and_close),
    Transition('close', source=(RFI.STATUS_OPEN, RFI.STATUS_RESPONDED), target=RFI.STATUS_CLOSED, label='close',
               allowed=record_staff, on_apply=_stamp_closed),
    Transition('reopen', source=(RFI.STATUS_RESPONDED, RFI.STATUS_CLOSED, RFI.STATUS_VOID), target=RFI.STATUS_OPEN,
               label='reopen', allowed=record_staff, on_apply=_clear_closed),
    Transition('void', source=(RFI.STATUS_DRAFT, RFI.STATUS_OPEN, RFI.STATUS_RESPONDED), target=RFI.STATUS_VOID,
               label='void', allowed=record_staff, on_apply=_stamp_closed),
]

# Audited on top of the RecordBase fields (title, status, visibility, due date, ball in court...)
AUDIT_FIELDS = ('question', 'category', 'priority', 'document_reference', 'attachment', 'response',
                'cost_impact', 'programme_impact_days')
