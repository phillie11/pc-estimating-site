"""
Status changes for the monthly CVR and daywork sheets, registered in CvrConfig.ready(). Views call
core.workflow.apply_transition(record, name, user, **params); never set status by hand.

  Monthly CVR:   DRAFT --submit--> SUBMITTED --sign_off--> SIGNED_OFF (locked for good)
                 SUBMITTED --send_back--> DRAFT
    submit       the job's PM or QS, owners and admins; only once the month has ended. Freezes the
                 figures as they stand.
    send_back    whoever may submit or sign off; unfreezes them (they are worked out live again).
    sign_off     owners and admins (and the job's PM or QS where the company allows it); only when
                 every earlier month's CVR on the job is signed off.

  Daywork sheet: DRAFT --sign--> SIGNED --send_to_change--> IN_CHANGE
                 DRAFT --send_to_change--> IN_CHANGE (unsigned: the change event says so)
                 DRAFT / SIGNED --cancel--> VOID
    All staff on the job (the records are internal). Signing needs at least one line, the
    signature picture and the signer's name; lines and photos can only change before it is signed.
"""
from django.utils import timezone

from core.access import record_staff
from core.exceptions import InvalidTransition
from core.workflow import Transition

from . import figures, rules
from .access import can_sign_off, prepares_cvr
from .models import CVRPeriod as P
from .models import DayworkSheet as DW


def _may_prepare(period, user):
    return prepares_cvr(user, period.project)


def _may_sign_off(period, user):
    return can_sign_off(user, period)


def _may_send_back(period, user):
    return _may_prepare(period, user) or _may_sign_off(period, user)


def _submit(period, user, today=None, **params):
    today = today or timezone.localdate()
    if not rules.has_ended(period.month, today):
        nxt = rules.add_months(period.month, 1)
        raise InvalidTransition(f'{period.label} has not ended yet. Its CVR can be submitted from '
                                f'{nxt:%a} {nxt.day} {nxt:%b %Y}.')
    figures.freeze(period, figures.live_for(period))  # status is already SUBMITTED here
    period.submitted_at, period.submitted_by = timezone.now(), user


def _send_back(period, user, **params):
    figures.unfreeze(period)
    period.submitted_at = period.submitted_by = None


def _sign_off(period, user, **params):
    earlier = P.objects.filter(project_id=period.project_id, month__lt=period.month).exclude(status=P.SIGNED_OFF) \
        .order_by('month').first()
    if earlier is not None:
        raise InvalidTransition(f'Sign off {earlier.reference} ({earlier.label}) first: months are signed off in order.')
    period.signed_off_at, period.signed_off_by = timezone.now(), user


CVR_TRANSITIONS = [
    Transition('submit', source=(P.DRAFT,), target=P.SUBMITTED, label='submit for sign-off', allowed=_may_prepare,
               on_apply=_submit),
    Transition('send_back', source=(P.SUBMITTED,), target=P.DRAFT, label='send back for changes',
               allowed=_may_send_back, on_apply=_send_back),
    Transition('sign_off', source=(P.SUBMITTED,), target=P.SIGNED_OFF, label='sign off', allowed=_may_sign_off,
               on_apply=_sign_off),
]
CVR_AUDIT = ('month', 'commentary')


def _sign(sheet, user, signature=None, signer='', role='', **params):
    if not sheet.lines.exists():
        raise InvalidTransition('Add at least one line of labour, plant or materials before it is signed.')
    if not signature or not signer:
        raise InvalidTransition('The signature and the name of the person signing are both needed.')
    sheet.signature.save(signature.name, signature, save=False)
    sheet.signed_by_name, sheet.signed_by_role = signer[:150], role
    sheet.signed_at, sheet.signature_recorded_by = timezone.now(), user


def _sent(sheet, user, change_event=None, **params):
    if change_event is None:
        raise InvalidTransition('Choose the change event it goes on.')
    sheet.change_event = change_event


DAYWORK_TRANSITIONS = [
    Transition('sign', source=(DW.DRAFT,), target=DW.SIGNED, label='record the signature', allowed=record_staff,
               on_apply=_sign),
    Transition('send_to_change', source=(DW.DRAFT, DW.SIGNED), target=DW.IN_CHANGE, label='add to the change register',
               allowed=record_staff, on_apply=_sent),
    Transition('cancel', source=(DW.DRAFT, DW.SIGNED), target=DW.VOID, label='cancel', allowed=record_staff),
]
DAYWORK_AUDIT = ('work_date', 'location', 'instructed_by', 'description', 'signed_by_name', 'signed_by_role')
