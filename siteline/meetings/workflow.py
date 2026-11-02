"""
Status changes for meetings and their actions, registered in MeetingsConfig.ready().

Meetings (MTG-):
  PLANNED --issue (staff)---------> ISSUED     minutes emailed to attendees as a link; ball cleared
  ISSUED  --correct (staff)-------> PLANNED    to put something right; ball with whoever corrects it
  PLANNED --cancel (staff)--------> CANCELLED  ball cleared
  CANCELLED --reinstate (staff)---> PLANNED    ball back with the chair

Actions (ACT-):
  OPEN --complete (staff or the owner)--> DONE        with an optional update; ball cleared
  OPEN --drop (staff)-------------------> DROPPED     no longer needed; ball cleared
  DONE / DROPPED --reopen (staff)-------> OPEN        ball back with the owner once the minutes are issued

The emails are sent by the view after `issue` (meetings/services.py), not here, so a transition
made in a test, a script or seed data never emails anyone by surprise.
"""
from django.utils import timezone

from core.access import record_staff
from core.workflow import Transition

from .models import Meeting as M
from .models import MeetingAction as A


def _issued(meeting, user, **params):
    meeting.issued_at, meeting.issued_by = timezone.now(), user
    meeting.issue_count = (meeting.issue_count or 0) + 1


def _chair(meeting, user):
    return meeting.chair


def _corrector(meeting, user):
    return user


TRANSITIONS = [
    Transition('issue', source=(M.STATUS_PLANNED,), target=M.STATUS_ISSUED, label='issue the minutes of',
               allowed=record_staff, sets_ball_in_court=None, on_apply=_issued),
    Transition('correct', source=(M.STATUS_ISSUED,), target=M.STATUS_PLANNED, label='correct the minutes of',
               allowed=record_staff, sets_ball_in_court=_corrector),
    Transition('cancel', source=(M.STATUS_PLANNED,), target=M.STATUS_CANCELLED, label='cancel', allowed=record_staff,
               sets_ball_in_court=None),
    Transition('reinstate', source=(M.STATUS_CANCELLED,), target=M.STATUS_PLANNED, label='reinstate',
               allowed=record_staff, sets_ball_in_court=_chair),
]

AUDIT_FIELDS = ('kind', 'held_on', 'starts_at', 'location', 'chair', 'notes', 'issued_at', 'issue_count')


def staff_or_owner(action, user):
    """Staff, or the person it was given to once the minutes are out."""
    return record_staff(action, user) or (
        action.owner_id == getattr(user, 'pk', None) and action.meeting.status == M.STATUS_ISSUED)


def _closed(action, user, update=''):
    action.closed_at, action.closed_by = timezone.now(), user
    if update:
        action.update = update[:2000]


def _reopened(action, user, update=''):
    action.closed_at = action.closed_by = None
    if update:
        action.update = update[:2000]


def _owner_if_issued(action, user):
    return action.owner if action.meeting.status == M.STATUS_ISSUED else None


ACTION_TRANSITIONS = [
    Transition('complete', source=(A.OPEN,), target=A.DONE, label='mark as done', allowed=staff_or_owner,
               sets_ball_in_court=None, on_apply=_closed),
    Transition('drop', source=(A.OPEN,), target=A.DROPPED, label='mark as no longer needed', allowed=record_staff,
               sets_ball_in_court=None, on_apply=_closed),
    Transition('reopen', source=(A.DONE, A.DROPPED), target=A.OPEN, label='reopen', allowed=record_staff,
               sets_ball_in_court=_owner_if_issued, on_apply=_reopened),
]

ACTION_AUDIT_FIELDS = ('meeting', 'item', 'owner', 'owner_name', 'update', 'closed_at')
