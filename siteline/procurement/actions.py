"""
"Needs me" items from procurement, registered from ProcurementConfig.ready():

  provider     who             rows                                                 access as
  -----------  --------------  ---------------------------------------------------  ------------------------
  submittal    staff           Submittals in review / to submit, ball in their court  submittal_detail (staff)
               subcontractor   Submittals to make or resubmit on their appointed       submittal_detail (sub)
                               packages (ball with their company)
               consultant      Submittals forwarded to them, waiting on their comment  submittal_detail (consultant)
  order_by     staff (the PM)  Lines not ordered whose order-by date is within 7 days  procurement_schedule
                               or past, on jobs they manage (the bid lead when a job
                               has no project manager)

Every item has the due date of whoever holds it. The submittal provider covers the type's
record-index rows so nobody sees a submittal twice.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from directory.models import Package
from projects.access import is_internal, portal_projects, visible_projects
from projects.models import Project, ProjectMember

from . import dates
from .models import ScheduleLine, Submittal

ACTIONS = {
    Submittal.REQUIRED: 'Upload it for review',
    Submittal.REJECTED: 'Revise it and resubmit',
    Submittal.SUBMITTED: 'Review it: A, B or C',
    Submittal.WITH_CONSULTANT: 'Comment on it',
}


def _item(s, external):
    return Item(kind='submittal', title=s.title, url=s.get_absolute_url(), ref=s.ref, project=s.project, due=s.due_date,
                priority=PRIORITY_HIGH if s.status == Submittal.REJECTED else PRIORITY_NORMAL,
                action=ACTIONS.get(s.status, ''), tone=s.status_tone,
                status_label='With you' if external and s.status == Submittal.WITH_CONSULTANT else s.get_status_display(),
                detail='' if external else str(s.subcontractor or ''), key=f'sub:{s.pk}')


class SubmittalProvider:
    kind, label = 'submittal', 'Submittals'
    covers = ('procurement.submittal',)

    def items(self, request):
        base = Submittal.objects.filter(project__in=portal_projects(request).values('pk'),
                                        status__in=Submittal.OPEN_STATUSES) \
            .select_related('project', 'project__organization', 'package__subcontractor')
        if is_internal(request):
            qs = base.filter(ball_in_court_user=request.user,
                             project__in=visible_projects(request).values('pk'))
            return [_item(s, False) for s in qs]
        memberships = ProjectMember.objects.filter(user=request.user,
                                                   project__in=visible_projects(request).values('pk'))
        q = Q(pk__in=[])
        for project_id, role, sub_id in memberships.values_list('project_id', 'role', 'subcontractor_id'):
            if role == ProjectMember.ROLE_SUBCONTRACTOR and sub_id:
                q |= Q(project_id=project_id, status__in=Submittal.TO_SUBMIT, ball_in_court_company_id=sub_id,
                       package__subcontractor_id=sub_id, package__status=Package.APPOINTED)
            elif role == ProjectMember.ROLE_CONSULTANT:
                q |= Q(project_id=project_id, status=Submittal.WITH_CONSULTANT, ball_in_court_user=request.user,
                       revisions__forwards__consultant=request.user, revisions__forwards__replied_at__isnull=True)
        return [_item(s, True) for s in base.filter(q).distinct()]


def managed_jobs(request):
    """
    Live jobs whose orders wait on this member of staff: the project manager, or, where a job has
    none, the company's owners and admins (the same fallback as the submittal reviewer, never the bid lead).
    """
    mine = Q(manager=request.user)
    if request.membership is not None and request.membership.is_admin:
        mine |= Q(manager__isnull=True)
    return visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES).filter(mine)


class OrderByProvider:
    kind, label = 'order_by', 'Orders to place'

    def items(self, request):
        if not is_internal(request):
            return []
        today = timezone.localdate()
        soon = today + timedelta(days=dates.NEEDS_ME_DAYS)
        qs = ScheduleLine.objects.filter(project__in=managed_jobs(request).values('pk'), order_by__lte=soon) \
            .exclude(status__in=ScheduleLine.DONE_STATUSES) \
            .select_related('project', 'project__organization', 'package__subcontractor')
        out = []
        for line in qs:
            late = line.order_by < today
            out.append(Item(
                kind=self.kind, title=f'Order {line.title}', url=line.get_absolute_url(), project=line.project,
                due=line.order_by, priority=PRIORITY_HIGH if late else PRIORITY_NORMAL,
                action='Place the order, or update the dates' if late else 'Place the order before the order-by date',
                status_label=line.rag_label, tone=line.rag_tone, detail=line.who or line.get_status_display(),
                key=f'line:{line.pk}'))
        return out
