"""
Status changes for records, declared in code per record type. Not a configurable engine.

    TRANSITIONS = [
        Transition('close', source=(RFI.STATUS_RESPONDED,), target=RFI.STATUS_CLOSED, label='Accept & close',
                   allowed=is_staff, on_apply=stamp_closed),
    ]
    register_record_type(RFI, prefix='RFI', transitions=TRANSITIONS, ...)

    apply_transition(rfi, 'close', request.user)

apply_transition() checks the record is in a source status and the user is
allowed, sets the status (and ball in court, if the transition says so), runs
on_apply for any other fields, then saves once: that save bumps the version,
writes one AuditEntry named after the transition with the field-level diff,
and updates RecordIndex. `allowed` only decides who may act on a record they
can already reach; which records a person can open is still the view's job
(get_project() and the record's visibility).
"""
from dataclasses import dataclass
from typing import Callable

from .exceptions import InvalidTransition, TransitionForbidden
from .registry import get_record_type

ANY = '*'
KEEP = object()  # sets_ball_in_court default: leave it where it is


@dataclass(frozen=True)
class Transition:
    name: str
    source: tuple  # statuses it can be made from; (ANY,) for any
    target: str
    label: str = ''
    # (record, user) -> bool. None means anyone who can reach the record.
    allowed: Callable | None = None
    # KEEP, None (nobody), or (record, user) -> the user who holds the ball afterwards
    sets_ball_in_court: object = KEEP
    # (record, user, **params) -> None: set the transition's other fields (responded_at, closed_at...)
    on_apply: Callable | None = None

    def applies_from(self, status):
        return ANY in self.source or status in self.source

    def permits(self, record, user):
        return self.allowed is None or bool(self.allowed(record, user))


def available_transitions(record, user):
    """The transitions this user can make on the record now, for buttons in templates."""
    return [t for t in get_record_type(record).transitions.values()
            if t.applies_from(record.status) and t.permits(record, user)]


def apply_transition(record, name, user, note='', **params):
    rt = get_record_type(record)
    t = rt.transitions.get(name)
    if t is None:
        raise InvalidTransition(f'{rt.label} has no transition {name!r}.')
    if not t.applies_from(record.status):
        raise InvalidTransition(f'Cannot {t.label or t.name} {record.reference}: it is {record.get_status_display()}.')
    if not t.permits(record, user):
        raise TransitionForbidden(f'You cannot {t.label or t.name} {record.reference}.')
    record.status = t.target
    if t.sets_ball_in_court is not KEEP:
        record.ball_in_court_user = t.sets_ball_in_court(record, user) if callable(t.sets_ball_in_court) else None
    if t.on_apply:
        t.on_apply(record, user, **params)
    record.save(actor=user, audit_action=t.name, audit_note=note)
    return record
