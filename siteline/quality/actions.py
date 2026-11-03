"""
"Needs me" items from Quality. The records provider (actions.providers.RecordsProvider) already reads
the record index once for everyone's open, ball-in-court records behind the badge on every page;
QualityConfig.ready() registers format() for the three quality types so they are worded here and cost
no query of their own. Externals never see them: the types are registered with external_roles=().

  record            who has the ball                        item                                  due
  ----------------  --------------------------------------  ------------------------------------  ---------------
  inspection        whoever is doing it                     Finish the inspection                 inspect-by date
  permit, asked     the approver (the PM, else an admin)    Approve or refuse the permit          day work starts
  permit, approved  whoever asked for it                    on the day it runs out, and every     day it runs out
                                                            day after until it is closed
  penetration       the PM (sealed) / whoever recorded it   Inspect and sign off / get it         none
                    (failed)                                resealed

Ball holders are always staff on the job (the inspector is picked from them; approvers are the PM or an
admin; the rest are whoever recorded it), so the items link to pages they can open. The permit page
itself shows the exact state by the clock (active, expiring within the company's warning time, expired).
"""
from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item

from . import rules
from .models import Penetration

TYPES = ('quality.inspection', 'quality.permit', 'quality.penetration')


def _item(idx, **kw):
    kw.setdefault('status_label', idx.status_label)
    kind = {'Inspection': 'inspection', 'Fire-stopping': 'firestopping'}.get(kw.get('kind_label'), 'permit')
    return Item(kind=kind, title=idx.title, url=idx.url, ref=idx.ref, project=idx.project, due=idx.due_date,
                key=f'idx:{idx.pk}', **kw)


def format(idx, today):  # noqa: A001 - the formatter registered for the three record types
    if idx.record_type == 'quality.inspection':
        late = idx.due_date is not None and idx.due_date < today
        return _item(idx, action='Finish the inspection', tone='bad' if late else 'warn', kind_label='Inspection',
                     priority=PRIORITY_HIGH if late else PRIORITY_NORMAL)
    if idx.record_type == 'quality.penetration':
        sealed = idx.status == Penetration.SEALED
        return _item(idx, action='Inspect the seal and sign it off' if sealed
                     else 'Get it resealed, then mark it resealed', tone='accent' if sealed else 'bad',
                     kind_label='Fire-stopping', priority=PRIORITY_NORMAL if sealed else PRIORITY_HIGH)
    if idx.status == rules.REQUESTED:
        return _item(idx, action='Approve or refuse the permit', tone='accent', kind_label='Permit to work',
                     priority=PRIORITY_HIGH)
    # Approved: from the day it runs out until someone closes it
    if idx.due_date is None or idx.due_date > today:
        return None
    if idx.due_date < today:
        return _item(idx, action='Close the permit, or ask for a new one if work carries on', tone='bad',
                     status_label='Expired', kind_label='Permit to work', priority=PRIORITY_HIGH)
    return _item(idx, action='Runs out today. Close it when the work is done, or ask for a new one', tone='warn',
                 status_label='Runs out today', kind_label='Permit to work', priority=PRIORITY_HIGH)
