"""
Transmittal status changes. Registered in DocumentsConfig.ready(); views and
documents.services call core.workflow.apply_transition(transmittal, name, user).

  DRAFT ──issue──► ISSUED ──complete──► ACKNOWLEDGED
    └──cancel──► CANCELLED

Issuing and cancelling are staff only. `complete` happens by itself when the last
recipient acknowledges (documents.services.acknowledge), so it is allowed for staff and
for recipients. What issuing shares and who is told is in documents.services.issue.
"""
from django.utils import timezone

from core.access import record_staff
from core.workflow import Transition

from .models import Transmittal, TransmittalRecipient, recipient_q


def record_staff_or_recipient(transmittal, user):
    if record_staff(transmittal, user):
        return True
    return TransmittalRecipient.objects.filter(recipient_q(user), transmittal=transmittal).exists()


def _stamp_issued(transmittal, user):
    transmittal.issued_at, transmittal.issued_by = timezone.now(), user


TRANSITIONS = [
    Transition('issue', source=(Transmittal.STATUS_DRAFT,), target=Transmittal.STATUS_ISSUED, label='issue',
               allowed=record_staff, on_apply=_stamp_issued),
    Transition('complete', source=(Transmittal.STATUS_ISSUED,), target=Transmittal.STATUS_ACKNOWLEDGED,
               label='mark as received by everyone', allowed=record_staff_or_recipient, sets_ball_in_court=None),
    Transition('cancel', source=(Transmittal.STATUS_DRAFT,), target=Transmittal.STATUS_CANCELLED, label='cancel',
               allowed=record_staff),
]

AUDIT_FIELDS = ('purpose', 'cover_note', 'issued_at')
