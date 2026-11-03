"""
"Needs me" items from close-out, registered from CloseoutConfig.ready():

  provider        who                       rows                                              links to
  --------------  ------------------------  ------------------------------------------------  -----------------
  defects_end     staff: the job's project   Completion sections whose defects period ends     closeout_overview
                  manager (owners and        within 6 weeks, or has ended, with no making-good
                  admins where a job has     date recorded. Due: the end of the defects period.
                  none)
  defect_assign   the same people            Defects reported by the client side that nobody   issue_edit
                                             has been given to fix yet. Due: its fix-by date.

Both read one UNION query, shared for the request, because the badge runs on every page.
Defects being fixed and checked are already on Needs me as snags ("Snags to fix" for whoever has
it, "Snags to inspect" for the PM), and O&M information to send is a submittal, so neither is
repeated here. Externals always get [].
"""
from django.db.models import CharField, DateField, ExpressionWrapper, F, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, NullIf
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal

from . import rules
from .models import CompletionSection

SECTION, DEFECT = 's', 'i'


def jobs_i_manage(request):
    """Won, live and complete jobs this person runs: their own, plus unmanaged ones for owners and admins."""
    from actions.providers import my_jobs  # one shared query behind the badge
    admin = request.membership is not None and request.membership.is_admin
    return [p for p in my_jobs(request) if p.manager_id == request.user.pk or (admin and p.manager_id is None)]


def _rows(request):
    """(jobs by pk, rows): sections still in their defects period and client defects nobody has, in one query."""
    from tenancy.context import request_memo

    def fetch():
        jobs = {p.pk: p for p in jobs_i_manage(request)}
        if not jobs:
            return jobs, []
        from contracts.models import MainContract
        from projects.models import Issue, ProjectMember

        from .access import CLIENT_ROLES
        months = Subquery(MainContract.objects.filter(project_id=OuterRef('project_id'))
                          .values('defects_period_months')[:1], output_field=IntegerField())
        sections = CompletionSection.objects.filter(project_id__in=list(jobs), pc_date__isnull=False,
                                                    made_good_on__isnull=True).annotate(
            kind=Value(SECTION, output_field=CharField()), rid=F('pk'), pid=F('project_id'),
            day=ExpressionWrapper(F('pc_date'), output_field=DateField()),
            n=Coalesce(NullIf('defects_period_months', Value(0)), months,  # 0 = use the contract's, as on the page
                        output_field=IntegerField()),
            text=Coalesce('name', Value(''), output_field=CharField()), where=Value('', output_field=CharField()),
        ).values_list('kind', 'rid', 'pid', 'day', 'n', 'text', 'where').order_by()
        client_side = ProjectMember.objects.filter(project_id__in=list(jobs), role__in=CLIENT_ROLES).values('user_id')
        defects = Issue.objects.filter(project_id__in=list(jobs), issue_type='DEFECT', assignee__isnull=True,
                                       created_by__in=client_side).exclude(status=Issue.STATUS_CLOSED).annotate(
            kind=Value(DEFECT, output_field=CharField()), rid=F('pk'), pid=F('project_id'),
            day=ExpressionWrapper(F('due_date'), output_field=DateField()),
            n=Coalesce('number', Value(0), output_field=IntegerField()),
            text=Coalesce('title', Value(''), output_field=CharField()),
            where=Coalesce('location', Value(''), output_field=CharField()),
        ).values_list('kind', 'rid', 'pid', 'day', 'n', 'text', 'where').order_by()
        return jobs, list(sections.union(defects, all=True))
    return request_memo(('closeout_needs_me', request.user.pk), fetch)


class DefectsEndProvider:
    kind, label = 'defects_end', 'End of defects period'

    def items(self, request):
        if request.membership is None or not is_internal(request):
            return []
        jobs, rows = _rows(request)
        today = timezone.localdate()
        out = []
        for kind, pk, project_id, pc_date, months, name, _ in rows:
            if kind != SECTION:
                continue
            end = rules.defects_end(pc_date, months)
            if not rules.reminder_due(end, None, today):
                continue
            ended = end < today
            out.append(Item(
                kind=self.kind, title=f'Defects period ends: {name}', ref='Close-out', project=jobs[project_id], due=end,
                url=reverse('closeout_overview', args=[request.org.slug, project_id]) + f'#section-{pk}',
                priority=PRIORITY_HIGH if ended else PRIORITY_NORMAL,
                action=('Inspect, get the defects made good and record the date' if ended
                        else 'Book the end-of-defects inspection with the client'),
                status_label='Defects period ended' if ended else 'Ends soon', tone='bad' if ended else 'warn',
                kind_label='Close-out', key=f'defects_end:{pk}'))
        return out


class DefectToAssignProvider:
    kind, label = 'defect_assign', 'Defects to give out'

    def items(self, request):
        if request.membership is None or not is_internal(request):
            return []
        jobs, rows = _rows(request)
        return [Item(
            kind=self.kind, title=title, ref=f'IS-{number:03d}', project=jobs[project_id], due=due,
            url=reverse('issue_edit', args=[request.org.slug, project_id, pk]),
            action='Give it to whoever will fix it', status_label='Reported by the client', tone='warn',
            detail=where, kind_label='Defects', key=f'issue:{pk}')
            for kind, pk, project_id, due, number, title, where in rows if kind == DEFECT]
