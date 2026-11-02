"""
"Needs me" items from close-out, registered from CloseoutConfig.ready():

  provider        who                       rows                                              links to
  --------------  ------------------------  ------------------------------------------------  -----------------
  defects_end     staff: the job's project   Completion sections whose defects period ends     closeout_overview
                  manager (owners and        within 6 weeks, or has ended, with no making-good
                  admins where a job has     date recorded. Due: the end of the defects period.
                  none)
  defect_assign   the same people            Defects reported by the client side that nobody   closeout_defects
                                             has been given to fix yet. No due date.

Defects being fixed and checked are already on Needs me as snags ("Snags to fix" for whoever has
it, "Snags to inspect" for the PM), and O&M information to send is a submittal, so neither is
repeated here. Externals always get [].
"""
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal

from . import rules
from .models import CompletionSection


def jobs_i_manage(request):
    """Won, live and complete jobs this person runs: their own, plus unmanaged ones for owners and admins."""
    from actions.providers import my_jobs  # one shared query behind the badge
    admin = request.membership is not None and request.membership.is_admin
    return [p for p in my_jobs(request) if p.manager_id == request.user.pk or (admin and p.manager_id is None)]


class DefectsEndProvider:
    kind, label = 'defects_end', 'End of defects period'

    def items(self, request):
        if request.membership is None or not is_internal(request):
            return []
        jobs = {p.pk: p for p in jobs_i_manage(request)}
        if not jobs:
            return []
        from contracts.models import MainContract
        months = dict(MainContract.objects.filter(project_id__in=jobs).values_list('project_id', 'defects_period_months'))
        today = timezone.localdate()
        out = []
        for s in CompletionSection.objects.filter(project_id__in=jobs, pc_date__isnull=False, made_good_on__isnull=True):
            project = jobs[s.project_id]
            end = rules.defects_end(s.pc_date, s.defects_period_months or months.get(s.project_id))
            if not rules.reminder_due(end, s.made_good_on, today):
                continue
            ended = end < today
            out.append(Item(
                kind=self.kind, title=f'Defects period ends: {s.name}', ref='Close-out', project=project, due=end,
                url=reverse('closeout_overview', args=[request.org.slug, project.pk]) + f'#section-{s.pk}',
                priority=PRIORITY_HIGH if ended else PRIORITY_NORMAL,
                action=('Inspect, get the defects made good and record the date' if ended
                        else 'Book the end-of-defects inspection with the client'),
                status_label='Defects period ended' if ended else 'Ends soon', tone='bad' if ended else 'warn',
                kind_label='Close-out', key=f'defects_end:{s.pk}'))
        return out


class DefectToAssignProvider:
    kind, label = 'defect_assign', 'Defects to give out'

    def items(self, request):
        if request.membership is None or not is_internal(request):
            return []
        jobs = {p.pk: p for p in jobs_i_manage(request)}
        if not jobs:
            return []
        from projects.models import Issue, ProjectMember

        from .access import CLIENT_ROLES
        client_side = ProjectMember.objects.filter(project_id__in=jobs, role__in=CLIENT_ROLES).values('user_id')
        qs = Issue.objects.filter(project_id__in=jobs, issue_type='DEFECT', assignee__isnull=True,
                                  created_by__in=client_side).exclude(status=Issue.STATUS_CLOSED) \
            .select_related('created_by')
        return [Item(
            kind=self.kind, title=i.title, ref=i.reference, project=jobs[i.project_id], due=i.due_date,
            url=reverse('issue_edit', args=[request.org.slug, i.project_id, i.pk]),
            action='Give it to whoever will fix it', status_label='Reported by the client', tone='warn',
            detail=i.location, kind_label='Defects', key=f'issue:{i.pk}') for i in qs]
