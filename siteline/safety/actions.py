"""
"Needs me" items for safety (registered as a HookProvider in SafetyConfig.ready()). Staff only.

    items(request) -> [actions.Item]

  incident actions      open actions owned by this person, due on their "do it by" date
  RIDDOR reports        reportable incidents not yet reported, for the job's project manager and
                        whoever holds the incident, due on the RIDDOR deadline (safety/rules.py)
  construction phase    a live or won job this person manages, starting within 14 days (or
  plan                  started), with no plan uploaded and no "in place" date, due on the start
  F10 notification      a notifiable job this person manages with no "sent to HSE" date, due on
                        the start on site

Open incidents themselves (to investigate and close) come through the record index, because
reporting one puts the ball in the project manager's court. A fixed number of queries
whatever the size of the company: it runs behind the badge on every page.
"""
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal, visible_projects
from projects.models import Project

from . import rules
from .models import CDMProfile, Incident, IncidentAction, PhasePlan


def _action_items(request, today):
    out = []
    rows = IncidentAction.objects.filter(
        owner=request.user, done_at__isnull=True, incident__deleted_at__isnull=True,
        incident__project__in=visible_projects(request).values('pk')).select_related('incident__project')
    for a in rows:
        i = a.incident
        out.append(Item(
            kind='safety', ref=i.ref, title=a.description, url=i.get_absolute_url() + '#actions', project=i.project,
            due=a.due_date, status_label='Overdue' if a.due_date and a.due_date < today else 'To do',
            tone='bad' if a.due_date and a.due_date < today else 'warn', action='Do this safety action, then mark it done',
            kind_label='Safety action', detail=f'After {i.ref} {i.title}', key=f'safety:action:{a.pk}',
            priority=PRIORITY_HIGH if a.due_date and a.due_date < today else PRIORITY_NORMAL))
    return out


def _riddor_items(request, today):
    out = []
    rows = Incident.objects.filter(riddor_category__in=rules.REPORTABLE, riddor_reported_on__isnull=True,
                                   project__in=visible_projects(request).values('pk')) \
        .filter(Q(ball_in_court_user=request.user) | Q(project__manager=request.user)).select_related('project')
    for i in rows:
        due = i.riddor_deadline or i.occurred_on
        out.append(Item(
            kind='safety', ref=i.ref, title=f'RIDDOR report: {i.title}', url=i.get_absolute_url() + '#riddor',
            project=i.project, due=due, status_label='RIDDOR overdue' if due < today else 'RIDDOR to report',
            tone='bad', priority=PRIORITY_HIGH, action='Make sure it is reported to HSE, then record the date',
            kind_label='RIDDOR', detail=i.get_riddor_category_display(), key=f'safety:riddor:{i.pk}'))
    return out


def _cdm_items(request, today):
    from actions.providers import my_jobs  # one shared query behind the badge
    jobs = [p for p in my_jobs(request) if p.stage in Project.DELIVERY_STAGES and p.manager_id == request.user.pk
            and not p.is_archived]
    if not jobs:
        return []
    ids = [p.pk for p in jobs]
    profiles = {c.project_id: c for c in CDMProfile.objects.filter(project_id__in=ids)}
    with_plan = set(PhasePlan.objects.filter(project_id__in=ids).values_list('project_id', flat=True).distinct())
    out = []
    for p in jobs:
        cdm = profiles.get(p.pk)
        url = reverse('cdm_register', args=[request.org.slug, p.pk])
        due = rules.cpp_check_due(p.start_date, today)
        if due and p.pk not in with_plan and not (cdm and cdm.cpp_in_place_on):
            out.append(Item(
                kind='safety', ref='CDM', title='Construction phase plan not uploaded', url=url + '#plan', project=p,
                due=due, status_label='Missing', tone='warn', action='Upload the construction phase plan before work starts',
                kind_label='CDM', key=f'safety:cpp:{p.pk}',
                priority=PRIORITY_HIGH if due <= today else PRIORITY_NORMAL))
        if cdm and cdm.notifiable == 'YES' and not cdm.f10_submitted_on:
            out.append(Item(
                kind='safety', ref='CDM', title='F10 notification to HSE not recorded', url=url + '#notification',
                project=p, due=p.start_date or today, status_label='Not recorded', tone='warn',
                action='Check the client has sent the F10 to HSE, then record the date and reference',
                kind_label='CDM', key=f'safety:f10:{p.pk}'))
    return out


def items(request, today=None):
    if request.membership is None or not is_internal(request):
        return []
    today = today or timezone.localdate()
    return _action_items(request, today) + _riddor_items(request, today) + _cdm_items(request, today)
