"""
"Needs me" items for safety (SafetyProvider, registered in SafetyConfig.ready()). Staff only.

    items(request) -> [actions.Item]

  incident actions      open actions owned by this person, due on their "do it by" date
  RIDDOR reports        reportable incidents not yet reported, for the job's project manager and
                        whoever holds the incident, due on the RIDDOR deadline (safety/rules.py)
  construction phase    a live or won job this person manages, starting within 14 days (or
  plan                  started), with no plan uploaded and no "in place" date, due on the start
  F10 notification      a notifiable job this person manages with no "sent to HSE" date, due on
                        the start on site

Open incidents themselves (to investigate and close) come through the record index, because
reporting one puts the ball in the project manager's court.

This runs behind the badge on every page, so it is one query whatever the size of the company:
the jobs come from actions.providers.my_jobs() (fetched once per request and shared with the
other providers), and the four lists are one UNION ALL. Only jobs from my_jobs() are looked at,
which is the same rule as the pages the items link to (visible_projects, delivery section), so
nothing needs filtering afterwards.
"""
from django.db.models import CharField, DateField, DateTimeField, F, Q, Value
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal
from projects.models import Project

from . import rules
from .models import CDMProfile, Incident, IncidentAction, PhasePlan

COLUMNS = ('row', 'pk', 'parent', 'project_id', 'ref', 'title', 'text', 'd1', 'd2', 'at')


def _v(value, field):
    return Value(value, output_field=field)


def _branch(qs, row, parent, project, ref, title, text, d1, d2, at):
    """Every column an annotation, in the same order on every branch, so the UNION lines up. No ORDER BY in a branch."""
    qs = qs.order_by().annotate(c_row=_v(row, CharField()), c_pk=F('pk'), c_parent=parent, c_project_id=project,
                                c_ref=ref, c_title=title, c_text=text, c_d1=d1, c_d2=d2, c_at=at)
    return qs.values_list(*(f'c_{c}' for c in COLUMNS))


def _rows(request, jobs):
    """(row kind, pk, incident, project, ref, title, text, date, date, datetime) for every list, in one query."""
    user = request.user
    ids = [p.pk for p in jobs]
    managed = [p.pk for p in jobs if p.manager_id == user.pk and p.stage in Project.DELIVERY_STAGES]
    blank, no_date, no_time = _v('', CharField()), _v(None, DateField()), _v(None, DateTimeField())
    parts = [
        _branch(IncidentAction.objects.filter(owner=user, done_at__isnull=True, incident__deleted_at__isnull=True,
                                              incident__project_id__in=ids),
                'action', F('incident_id'), F('incident__project_id'), F('incident__ref'), F('incident__title'),
                F('description'), F('due_date'), no_date, no_time),
        _branch(Incident.objects.filter(riddor_category__in=rules.REPORTABLE, riddor_reported_on__isnull=True,
                                        project_id__in=ids)
                .filter(Q(ball_in_court_user=user) | Q(project__manager=user)),
                'riddor', F('pk'), F('project_id'), F('ref'), F('title'), F('riddor_category'), no_date, no_date,
                F('occurred_at')),
    ]
    if managed:
        parts += [
            _branch(CDMProfile.objects.filter(project_id__in=managed), 'cdm', F('pk'), F('project_id'), blank,
                    F('notifiable'), blank, F('f10_submitted_on'), F('cpp_in_place_on'), no_time),
            _branch(PhasePlan.objects.filter(project_id__in=managed), 'plan', F('pk'), F('project_id'), blank, blank,
                    blank, no_date, no_date, no_time),
        ]
    first, *rest = parts
    return [dict(zip(COLUMNS, r)) for r in first.union(*rest, all=True)]


def _incident_url(org, project_id, pk, anchor):
    return reverse('incident_detail', args=[org.slug, project_id, pk]) + anchor


def items(request, today=None):
    if request.membership is None or not is_internal(request):
        return []
    from actions.providers import my_jobs  # one shared query behind the badge
    jobs = my_jobs(request)
    if not jobs:
        return []
    today = today or timezone.localdate()
    by_id = {p.pk: p for p in jobs}
    out, profiles, with_plan = [], {}, set()
    labels = dict(rules.RIDDOR_CHOICES)
    for r in _rows(request, jobs):
        project = by_id.get(r['project_id'])
        if project is None:
            continue
        if r['row'] == 'action':
            late = r['d1'] is not None and r['d1'] < today
            out.append(Item(
                kind='safety', ref=r['ref'], title=r['text'], url=_incident_url(request.org, project.pk, r['parent'],
                                                                                 '#actions'),
                project=project, due=r['d1'], status_label='Overdue' if late else 'To do',
                tone='bad' if late else 'warn', action='Do this safety action, then mark it done',
                kind_label='Safety action', detail=f'After {r["ref"]} {r["title"]}', key=f'safety:action:{r["pk"]}',
                priority=PRIORITY_HIGH if late else PRIORITY_NORMAL))
        elif r['row'] == 'riddor':
            occurred = timezone.localdate(r['at'])
            due = rules.riddor_deadline(r['text'], occurred) or occurred
            out.append(Item(
                kind='safety', ref=r['ref'], title=f'RIDDOR report: {r["title"]}',
                url=_incident_url(request.org, project.pk, r['pk'], '#riddor'), project=project, due=due,
                status_label='RIDDOR overdue' if due < today else 'RIDDOR to report', tone='bad',
                priority=PRIORITY_HIGH, action='Make sure it is reported to HSE, then record the date',
                kind_label='RIDDOR', detail=labels.get(r['text'], ''), key=f'safety:riddor:{r["pk"]}'))
        elif r['row'] == 'cdm':
            profiles[project.pk] = r
        else:
            with_plan.add(project.pk)
    for p in jobs:
        if p.manager_id != request.user.pk or p.stage not in Project.DELIVERY_STAGES:
            continue
        cdm = profiles.get(p.pk)
        url = reverse('cdm_register', args=[request.org.slug, p.pk])
        due = rules.cpp_check_due(p.start_date, today)
        if due and p.pk not in with_plan and not (cdm and cdm['d2']):
            out.append(Item(
                kind='safety', ref='CDM', title='Construction phase plan not uploaded', url=url + '#plan', project=p,
                due=due, status_label='Missing', tone='warn', kind_label='CDM', key=f'safety:cpp:{p.pk}',
                action='Upload the construction phase plan before work starts',
                priority=PRIORITY_HIGH if due <= today else PRIORITY_NORMAL))
        if cdm and cdm['title'] == 'YES' and not cdm['d1']:
            out.append(Item(
                kind='safety', ref='CDM', title='F10 notification to HSE not recorded', url=url + '#notification',
                project=p, due=p.start_date or today, status_label='Not recorded', tone='warn',
                action='Check the client has sent the F10 to HSE, then record the date and reference',
                kind_label='CDM', key=f'safety:f10:{p.pk}'))
    return out


class SafetyProvider:
    """
    Not a HookProvider: that would look up the person's portal projects again to drop items on jobs
    they can't open, one more query on every page. Every row here already comes from my_jobs().
    """
    kind, label, covers = 'safety', 'Safety', ()

    def items(self, request):
        return items(request)

