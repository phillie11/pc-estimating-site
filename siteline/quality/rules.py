"""
The quality rules as pure functions and plain data, so they can be signed off from a fixed test
table (quality/tests/test_rules.py) without a database.

Everything about permits here is advisory: a permit to work supports, and never replaces, the
contractor's own safe system of work. Check with your safety adviser.

  Permits to work
    PERMIT_TYPES             hot works, working at height, isolation, confined space: the label,
                             the longest a permit may run and the precautions the approver confirms
    validity_problem()       why a start and expiry can't be used (None if they can)
    permit_state()           requested / approved (not started) / active / expiring / expired /
                             closed / refused / withdrawn, from the stored status and the clock
    expiring()               whether an approved permit belongs on Needs me yet

  Inspections
    inspection_outcome()     passed / failed / not finished, from the item results

  Fire-stopping
    sign_off_problem()       why a penetration can't be signed off yet (None if it can)
"""
import datetime

ADVISORY = 'Advisory: check with your safety adviser.'

# --- permits to work --------------------------------------------------------------------

HOT_WORKS, HEIGHT, ISOLATION, CONFINED = 'HOT_WORKS', 'HEIGHT', 'ISOLATION', 'CONFINED'

# Longest a permit can run, by type (hours). Hot works and confined space: one shift at most.
MAX_HOURS = {HOT_WORKS: 12, HEIGHT: 7 * 24, ISOLATION: 7 * 24, CONFINED: 12}

PERMIT_TYPES = {
    HOT_WORKS: {
        'label': 'Hot works',
        'help': 'Welding, cutting, grinding, brazing, soldering or a naked flame.',
        'precautions': [
            'Combustible materials removed or covered with fire blankets for 10 m around the work',
            'Floors, walls, voids and openings checked for hidden combustibles and sealed off',
            'A suitable fire extinguisher (and someone trained to use it) at the work area',
            'Fire detection in the area protected or isolated, with the building manager agreeing',
            'A fire watch during the work and for the agreed time after it stops',
            'The person doing the work knows how to raise the alarm',
        ],
    },
    HEIGHT: {
        'label': 'Working at height',
        'help': 'Above ground where a fall could injure someone and a stepladder alone is not enough.',
        'precautions': [
            'Access equipment (tower, MEWP, podium) inspected and tagged as safe to use',
            'Edge protection, guard rails or harness and anchor points in place',
            'Area below fenced off or an exclusion zone set out',
            'Operatives trained for the equipment (PASMA, IPAF or similar)',
            'A rescue plan for someone stuck or suspended at height',
            'Weather and wind checked for outdoor work',
        ],
    },
    ISOLATION: {
        'label': 'Isolation',
        'help': 'Electrical, mechanical, gas or water isolation before work on a live system.',
        'precautions': [
            'The circuit or system identified on the drawings or schedule',
            'Isolated and locked off, with the key held by the person doing the work',
            'Proved dead or de-pressurised with a tested instrument',
            'Warning notices fixed at the point of isolation',
            'Anyone affected (the client, other trades) told before isolating',
        ],
    },
    CONFINED: {
        'label': 'Confined space',
        'help': 'A tank, riser, ceiling void, chamber or duct where air or access is restricted.',
        'precautions': [
            'The atmosphere tested before entry and monitored throughout',
            'Everyone entering is trained for confined space work',
            'An attendant stays outside the space the whole time',
            'Rescue equipment and a rescue plan are ready',
            'Ventilation in place where it is needed',
        ],
    },
}
PERMIT_TYPE_CHOICES = [(code, t['label']) for code, t in PERMIT_TYPES.items()]


def precautions_for(permit_type):
    return list(PERMIT_TYPES[permit_type]['precautions'])


def validity_problem(permit_type, starts_at, expires_at):
    """
    Why this start and expiry can't be used, in words that say how to fix it; None if they can.
    Expiry must be after the start, and no longer after it than the type allows (MAX_HOURS).
    """
    if starts_at is None or expires_at is None:
        return 'Enter when the work starts and when the permit runs out.'
    if expires_at <= starts_at:
        return 'The permit must run out after it starts. Pick a later expiry time.'
    hours = MAX_HOURS[permit_type]
    if expires_at - starts_at > datetime.timedelta(hours=hours):
        limit = f'{hours} hours' if hours < 48 else f'{hours // 24} days'
        return (f'A {PERMIT_TYPES[permit_type]["label"].lower()} permit can run for {limit} at most. '
                'Pick an earlier expiry and issue a new permit when it runs out.')
    return None


# Stored statuses (Permit.status) and the states people see
REQUESTED, APPROVED, CLOSED, REFUSED, WITHDRAWN = 'REQUESTED', 'APPROVED', 'CLOSED', 'REFUSED', 'WITHDRAWN'

STATE_LABELS = {
    'requested': ('Waiting for approval', 'accent'),
    'approved': ('Approved, not started', 'info'),
    'active': ('Active', 'good'),
    'expiring': ('Expiring soon', 'warn'),
    'expired': ('Expired', 'bad'),
    'closed': ('Closed', 'muted'),
    'refused': ('Refused', 'muted'),
    'withdrawn': ('Withdrawn', 'muted'),
}


def permit_state(status, starts_at, expires_at, now, warning_hours):
    """
    What a permit is now. Only an approved permit changes with the clock:
      before its start                      approved (not started)
      from its start                        active
      within warning_hours of its expiry    expiring
      at or after its expiry                expired (until someone closes it)
    """
    if status == REQUESTED:
        return 'requested'
    if status == APPROVED:
        if now >= expires_at:
            return 'expired'
        if now >= expires_at - datetime.timedelta(hours=warning_hours):
            return 'expiring'
        if now < starts_at:
            return 'approved'
        return 'active'
    return {CLOSED: 'closed', REFUSED: 'refused', WITHDRAWN: 'withdrawn'}[status]


def expiring(status, starts_at, expires_at, now, warning_hours):
    """An approved permit goes on Needs me from warning_hours before its expiry, and stays while expired."""
    return permit_state(status, starts_at, expires_at, now, warning_hours) in ('expiring', 'expired')


# --- inspections -----------------------------------------------------------------------

PASS, FAIL, NA = 'PASS', 'FAIL', 'NA'


def inspection_outcome(results):
    """
    'FAILED' if any item failed, 'PASSED' if every item is pass or N/A, None while any is unanswered.
    An inspection with no items can't be finished (None). N/A counts as answered, not as a pass.
    """
    results = list(results)
    if not results or any(r not in (PASS, FAIL, NA) for r in results):
        return None
    if FAIL in results:
        return 'FAILED'
    return 'PASSED'


# --- fire-stopping ---------------------------------------------------------------------

def sign_off_problem(after_photos, system):
    """A penetration is signed off only with the product or system recorded and a photo of it sealed."""
    if not (system or '').strip():
        return 'Record the fire-stopping system or product used before signing it off.'
    if after_photos < 1:
        return 'Add a photo of the finished seal ("after") before signing it off.'
    return None
