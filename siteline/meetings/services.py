"""
Making meetings, carrying actions forward and issuing the minutes.

    people(project)                        who can be put on the attendee list, grouped
    create(project, user, **fields)        a meeting with its agenda, attendees and actions brought forward
    issue(meeting, user)                   the `issue` transition, then a bell notification and an email
                                           with a link (never an attachment) to each person who should have them
"""
from django.db import transaction
from django.urls import reverse

from core.workflow import apply_transition
from projects.models import ProjectMember
from tenancy import notify
from tenancy.models import Notification

from .models import OTHER, AgendaItem, AgendaTemplate, Attendee, Meeting, MeetingAction

OTHER_AGENDA = ['Attendance and apologies', 'Matters discussed', 'Any other business']
INTERNAL_ROLES = [r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]
EXTERNAL_ROLES = [r for r, _ in ProjectMember.EXTERNAL_ROLE_CHOICES]  # never tendering subcontractors


def people(project, also=()):
    """
    [(group label, [(user, company or role)])]: the job's team and the people on its portal. Tendering
    subcontractors are left out: they can't see the project. `also` adds users already chosen.
    """
    members = ProjectMember.objects.filter(project=project, role__in=INTERNAL_ROLES + EXTERNAL_ROLES,
                                           user__is_active=True).select_related('user', 'subcontractor')
    team, outside, seen = [], [], set()
    if project.manager_id and project.manager.is_active:
        team.append((project.manager, 'Project manager'))
        seen.add(project.manager_id)
    for m in members:
        if m.user_id in seen:
            continue
        seen.add(m.user_id)
        if m.role in INTERNAL_ROLES:
            team.append((m.user, m.get_role_display()))
        else:
            label = m.subcontractor.company_name if m.subcontractor_id else (m.company_name or m.get_role_display())
            outside.append((m.user, f'{label} ({m.get_role_display().lower()})'))
    for u in also:
        if u is not None and u.pk not in seen:
            seen.add(u.pk)
            team.append((u, 'Our team'))
    return [(label, rows) for label, rows in (('Our team', team), ('Client, consultants and subcontractors', outside))
            if rows]


def default_attendees(project, kind, user):
    """Ticked to start with: the person making it, the project manager, and the client for a client meeting."""
    from .models import CLIENT
    chosen = {user.pk}
    if project.manager_id:
        chosen.add(project.manager_id)
    if kind == CLIENT:
        chosen |= set(ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CLIENT)
                      .values_list('user_id', flat=True))
    return chosen


def parse_others(text):
    """'Name, Company' per line -> [(name, company)]."""
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, company = line.partition(',')
        out.append((name.strip()[:150], company.strip()[:150]))
    return out


def carry_forward(meeting):
    """Bring every action still open from earlier meetings of the same kind on this job into this one."""
    open_actions = MeetingAction.objects.filter(project=meeting.project, status=MeetingAction.OPEN,
                                                meeting__kind=meeting.kind, meeting__deleted_at__isnull=True) \
        .exclude(meeting=meeting).exclude(meeting__held_on__gt=meeting.held_on)
    meeting.brought_forward.add(*open_actions)
    return open_actions.count()


@transaction.atomic
def create(project, user, *, kind, held_on, starts_at='', location='', chair=None, attendees=(), others='',
           title=''):
    m = Meeting(project=project, kind=kind, held_on=held_on, starts_at=starts_at, location=location,
                chair=chair or user, title=title)
    m.save(actor=user)
    agendas = AgendaTemplate.for_org(project.organization)
    items = agendas[kind].item_list if kind in agendas and kind != OTHER else OTHER_AGENDA
    AgendaItem.objects.bulk_create([AgendaItem(meeting=m, position=i, heading=h[:200]) for i, h in enumerate(items)])
    for u in attendees:
        Attendee.objects.create(meeting=m, user=u)
    for name, company in parse_others(others):
        Attendee.objects.create(meeting=m, name=name, company=company)
    carry_forward(m)
    return m


def recipients(meeting, attendees=None, actions=None, staff_ids=None):
    """
    (user, url, their open actions) for everyone the issued minutes go to: attendees with a login, and
    owners of its open actions (raised or brought forward) who weren't on the list. Pass rows a page has
    already loaded (attendees with user, actions with owner) to save the queries.
    """
    org, pid = meeting.organization.slug, meeting.project_id
    detail = reverse('meeting_detail', args=[org, pid, meeting.pk])
    mine = reverse('meeting_list', args=[org, pid])
    if actions is None:
        actions = list(meeting.actions.select_related('owner')) + list(
            meeting.brought_forward.filter(status=MeetingAction.OPEN).select_related('owner', 'meeting'))
    if attendees is None:
        attendees = list(meeting.attendees.select_related('user'))
    by_owner = {}
    for a in actions:
        if a.owner_id and a.is_open:
            by_owner.setdefault(a.owner_id, []).append(a)
    out, seen = [], set()
    for att in attendees:
        if att.user_id and att.user.is_active and att.user_id not in seen:
            seen.add(att.user_id)
            out.append((att.user, detail, by_owner.get(att.user_id, [])))
    for owner_id, owned in by_owner.items():
        if owner_id in seen or not owned[0].owner.is_active:
            continue
        seen.add(owner_id)
        if staff_ids is None:
            staff_ids = _staff_ids(meeting)
        # Someone not at the meeting sees their own actions; staff can open the minutes themselves
        out.append((owned[0].owner, detail if owner_id in staff_ids else mine, owned))
    return out


def _staff_ids(meeting):
    from tenancy.models import Membership
    return set(Membership.objects.filter(organization_id=meeting.organization_id, role__in=Membership.INTERNAL_ROLES,
                                         is_active=True).values_list('user_id', flat=True))


def hand_out_actions(meeting, user):
    """The minutes are out: each open action raised at this meeting is now in its owner's court (Needs me)."""
    for a in meeting.actions.filter(status=MeetingAction.OPEN, owner__isnull=False):
        if a.ball_in_court_user_id != a.owner_id:
            a.ball_in_court_user_id = a.owner_id
            a.save(actor=user, audit_action='reassigned', audit_note='Minutes issued')


def issue(meeting, user, note=''):
    """Issue (or re-issue after a correction) and tell everyone. Returns how many people were sent it."""
    with transaction.atomic():
        apply_transition(meeting, 'issue', user, note=note)
        hand_out_actions(meeting, user)
    corrected = meeting.issue_count > 1
    sent = 0
    subject = f"{'Corrected minutes' if corrected else 'Minutes'}: {meeting.title}"
    for person, url, owned in recipients(meeting):
        if person.pk == user.pk:
            continue
        Notification.objects.create(organization=meeting.organization, user=person, actor=user,
                                    project=meeting.project, title=subject[:255], url=url[:300])
        notify.send(person.email, subject, 'meeting_minutes', {
            'user': person, 'actor': user, 'meeting': meeting, 'project': meeting.project, 'actions': owned,
            'corrected': corrected, 'link': notify.absolute(url)}, org=meeting.organization)
        sent += 1
    return sent


TRANSITION_FOR = {MeetingAction.DONE: 'complete', MeetingAction.DROPPED: 'drop', MeetingAction.OPEN: 'reopen'}


def close_action(action, user, status, update=''):
    """Move an action to `status` through its workflow (core.workflow), or just record a new update."""
    if status != action.status:
        return apply_transition(action, TRANSITION_FOR[status], user, update=update)
    if update and update != action.update:
        action.update = update[:2000]
        action.save(actor=user, audit_action='updated')
    return action
