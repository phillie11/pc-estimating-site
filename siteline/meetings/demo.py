"""
Demo data for the programme (look-ahead, MS Project import) and meetings, called by `manage.py
seed_demo` inside the demo organisation's org_context, after the Leeds job, its programme and
packages exist.

  Leeds bar job (project 1): programme lines given to the appointed subcontractors (so the joinery
      subcontractor sees their own lines in the portal look-ahead) and trades; four meetings:
        MTG-001 pre-start with the joinery subcontractor (issued, actions done)
        MTG-002 site progress meeting (issued): an action for the subcontractor, one overdue for the PM
        MTG-003 client meeting (issued): the client has an action due in 3 days (their Needs me)
        MTG-004 site progress meeting yesterday, minutes started but not issued (the PM's Needs me),
                with MTG-002's open actions brought forward
  Salford Quays (project 4): the programme imported from a Microsoft Project XML file (in the import
      history), and one issued site progress meeting.

Issuing here goes through the workflow transition, not services.issue(), so seeding sends no emails.
No real people: attendees without a login are named by role.
"""
from datetime import timedelta
from io import BytesIO
from xml.sax.saxutils import escape

from django.utils import timezone

from core.workflow import apply_transition
from directory.models import Package
from projects import programme_import
from projects.models import Activity, Project, ProjectMember

from . import services
from .models import CLIENT, PRESTART, PROGRESS, MeetingAction


def _link_programme(project):
    """Give each Leeds line the appointed subcontractor (and trade) its "Who" names."""
    appointed = list(Package.objects.filter(project=project, status=Package.APPOINTED)
                     .select_related('subcontractor', 'trade'))
    for a in Activity.objects.filter(project=project).exclude(responsible=''):
        who = a.responsible.lower()
        for p in appointed:
            name = p.subcontractor.company_name.lower()
            if name.startswith(who) or who.startswith(name.split(' ')[0]):
                a.subcontractor, a.trade = p.subcontractor, p.trade
                a.save()
                break


def _mspdi(today):
    """A small programme as Microsoft Project would save it as XML, dated around today."""
    def d(days, hour=8):
        return f'{today + timedelta(days=days):%Y-%m-%d}T{hour:02d}:00:00'
    rows = [  # uid, wbs, name, level, summary, milestone, start, finish, % done, after uid, resource
        (1, '1', 'Enabling works', 1, True, False, -14, 3, 70, None, ''),
        (2, '1.1', 'Site set-up and hoarding', 2, False, False, -14, -10, 100, None, ''),
        (3, '1.2', 'Soft strip', 2, False, False, -9, 3, 60, 2, ''),
        (4, '2', 'Fit-out, levels 3 to 5', 1, True, False, 4, 70, 0, None, ''),
        (5, '2.1', 'Raised access floor', 2, False, False, 4, 18, 0, 3, 'Mersey Flooring Co'),
        (6, '2.2', 'Partitions and ceilings', 2, False, False, 11, 39, 0, 5, 'Pennine Drylining'),
        (7, '2.3', 'M&E first fix', 2, False, False, 8, 32, 0, 3, 'Brightside Electrical'),
        (8, '2.4', 'Building control first fix inspection', 2, False, True, 33, 33, 0, 7, ''),
        (9, '2.5', 'Decorations', 2, False, False, 40, 60, 0, 6, ''),
        (10, '3', 'Practical completion', 1, False, True, 84, 84, 0, 9, ''),
    ]
    resources = sorted({r[10] for r in rows if r[10]})
    res_uid = {name: i + 1 for i, name in enumerate(resources)}
    tasks = ''.join(
        f'<Task><UID>{uid}</UID><ID>{uid}</ID><Name>{escape(name)}</Name><WBS>{wbs}</WBS><OutlineLevel>{level}</OutlineLevel>'
        f'<Summary>{int(summary)}</Summary><Milestone>{int(ms)}</Milestone><Start>{d(s)}</Start>'
        f'<Finish>{d(f, 8 if ms else 17)}</Finish><PercentComplete>{pc}</PercentComplete>'
        + (f'<PredecessorLink><PredecessorUID>{after}</PredecessorUID><Type>1</Type></PredecessorLink>' if after else '')
        + '</Task>'
        for uid, wbs, name, level, summary, ms, s, f, pc, after, _ in rows)
    res = ''.join(f'<Resource><UID>{i}</UID><Name>{escape(n)}</Name></Resource>' for n, i in res_uid.items())
    assigns = ''.join(f'<Assignment><TaskUID>{r[0]}</TaskUID><ResourceUID>{res_uid[r[10]]}</ResourceUID></Assignment>'
                      for r in rows if r[10])
    return ('<?xml version="1.0" encoding="UTF-8"?><Project xmlns="http://schemas.microsoft.com/project">'
            f'<Name>Salford Quays CAT A rev C.xml</Name><Tasks>{tasks}</Tasks><Resources>{res}</Resources>'
            f'<Assignments>{assigns}</Assignments></Project>').encode()


def _meeting(project, pm, kind, days_ago, attendees, today, minutes=(), others='', location='Site office'):
    m = services.create(project, pm, kind=kind, held_on=today - timedelta(days=days_ago), starts_at='09:30',
                        location=location, attendees=attendees, others=others)
    items = list(m.items.all())
    for i, text in minutes:
        items[i].minutes = text
        items[i].save(update_fields=['minutes'])
    return m


def _action(meeting, text, owner=None, due=None, done=False, update='', item=None, owner_name=''):
    a = MeetingAction(meeting=meeting, title=text, owner=owner, owner_name=owner_name, due_date=due, update=update,
                      item=item)
    a.save(actor=meeting.created_by)
    if done:
        apply_transition(a, 'complete', meeting.created_by)
    return a


def _issue(meeting, pm):
    apply_transition(meeting, 'issue', pm)
    services.hand_out_actions(meeting, pm)  # each open action on its owner's Needs me, as issuing does


def seed(*, leeds, pm, owner, client, sub_user, today=None):
    today = today or timezone.localdate()
    _link_programme(leeds)

    # Salford: programme from an MS Project XML export, through the same preview-and-apply as the page
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    f = BytesIO(_mspdi(today))
    f.name = 'Salford Quays CAT A rev C.xml'
    imp = programme_import.stage(salford, f, pm)
    programme_import.apply(imp, pm)

    # --- Leeds meetings ------------------------------------------------------------------
    pre = _meeting(leeds, pm, PRESTART, 24, [pm, sub_user], today, minutes=[
        (1, 'Bar back, front and shelving to drawings JN-101 to JN-108. Kitchen wall cladding included.'),
        (2, 'Manufacture over four weeks; install after second fix M&E in the bar.'),
        (3, 'RAMS for installation and working at height to be submitted before starting.')])
    _action(pre, 'Submit RAMS for bar joinery installation', owner=sub_user, due=today - timedelta(days=20), done=True,
            update='Submitted and approved.')
    _action(pre, 'Confirm a storage area for joinery deliveries', owner=pm, due=today - timedelta(days=21), done=True,
            update='Rear store, ground floor.')
    _issue(pre, pm)

    prog1 = _meeting(leeds, pm, PROGRESS, 14, [pm, owner, sub_user], today,
                     others='Building control officer, Leeds City Council', minutes=[
                         (2, 'Toolbox talk on working beside live neighbouring units. No incidents.'),
                         (3, 'Strip-out complete. First fix M&E 60%, drylining started on the kitchen.'),
                         (4, 'Bar back veneer still waiting on the client (see RFI-001).')])
    items = list(prog1.items.all())
    _action(prog1, 'Issue joinery shop drawings for approval', owner=sub_user, due=today + timedelta(days=3),
            item=items[4])
    _action(prog1, 'Book the building control first fix inspection', owner=pm, due=today - timedelta(days=1),
            item=items[3])
    _action(prog1, 'Order the acoustic ceiling tiles for the private dining room', owner=pm,
            due=today - timedelta(days=7), done=True, update='Ordered, delivery in week 6.')
    _issue(prog1, pm)

    client_mtg = _meeting(leeds, pm, CLIENT, 7, [pm, owner, client], today, location="Client's office", minutes=[
        (3, 'On programme overall. Kitchen extract running a few days behind; no effect on completion yet.'),
        (4, 'The bar back veneer needs deciding this week to hold the joinery programme.')])
    _action(client_mtg, 'Confirm the bar back veneer: smoked eucalyptus or walnut', owner=client,
            due=today + timedelta(days=3), item=list(client_mtg.items.all())[4])
    _issue(client_mtg, pm)

    # Yesterday's progress meeting: minutes started, not issued yet (on the PM's Needs me), actions brought forward
    _meeting(leeds, pm, PROGRESS, 1, [pm, sub_user], today, minutes=[
        (3, 'Drylining 55%. Bar joinery manufacture 40%, on track for install in three weeks.')])

    # --- Salford ------------------------------------------------------------------------
    team = [pm] + [m.user for m in ProjectMember.objects.filter(project=salford, role__in=services.INTERNAL_ROLES)
                   .select_related('user') if m.user_id != pm.pk]
    sal = _meeting(salford, pm, PROGRESS, 7, team, today, location='Level 3 site office', minutes=[
        (3, 'Soft strip on levels 3 and 4 complete; level 5 under way.')])
    _action(sal, 'Agree the raised floor setting-out with the M&E designer', owner=pm, due=today + timedelta(days=5),
            owner_name='')
    _issue(sal, pm)
