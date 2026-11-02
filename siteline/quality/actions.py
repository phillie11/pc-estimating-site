"""
"Needs me" items from Quality, registered from QualityConfig.ready(). Staff only: externals always get [].

  provider       who                                         rows                                 due
  -------------  ------------------------------------------  -----------------------------------  ---------------
  inspection     whoever is doing it (ball in court)         inspections still in progress        inspect-by date
  permit         the approver (ball in court)                permits waiting for approval         day work starts
                 whoever asked for it, and who approved it   approved permits from the warning    day it runs out
                                                             time before expiry, and expired ones
                                                             not yet closed (rules.expiring)
  firestopping   the PM (ball in court)                      penetrations sealed, to inspect      none
                 whoever recorded it (ball in court)         penetrations that failed inspection  none

Each provider covers its record type, so the record-index provider doesn't list them twice, and only
lists jobs the person can still open in Quality (quality.access: staff on the job).
"""
import datetime

from django.db.models import Q
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal, visible_projects
from projects.models import Project, ProjectMember

from . import rules
from .models import Inspection, Penetration, Permit, QualitySettings

INTERNAL_ROLES = [r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]
MAX_WARNING_HOURS = 48  # the most QualitySettings.expiry_warning_hours allows


def _my_quality_jobs(request):
    """Ids of won, live and completed jobs this member of staff can open in Quality."""
    from tenancy.context import request_memo

    def fetch():
        jobs = visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,))
        if not request.membership.is_admin:
            team = ProjectMember.objects.filter(user=request.user, role__in=INTERNAL_ROLES).values('project_id')
            jobs = jobs.filter(Q(manager=request.user) | Q(pk__in=team))
        return list(jobs.values_list('pk', flat=True))
    return request_memo(('quality_jobs', request.user.pk), fetch)


def _staff(request):
    return request.membership is not None and is_internal(request)


class InspectionProvider:
    kind, label = 'inspection', 'Inspections to finish'
    covers = ('quality.inspection',)

    def items(self, request):
        if not _staff(request):
            return []
        jobs = _my_quality_jobs(request)
        if not jobs:
            return []
        qs = Inspection.objects.filter(status=Inspection.IN_PROGRESS, ball_in_court_user=request.user,
                                       project_id__in=jobs) \
            .select_related('project', 'project__organization')
        today = timezone.localdate()
        return [Item(kind=self.kind, title=f'Inspect {i.title}', url=i.get_absolute_url(), ref=i.ref,
                     project=i.project, due=i.due_date, action='Finish the inspection',
                     priority=PRIORITY_HIGH if i.due_date and i.due_date < today else PRIORITY_NORMAL,
                     status_label=i.get_status_display(), tone=i.status_tone, detail=i.area,
                     kind_label='Inspection', key=f'ins:{i.pk}') for i in qs]


class PermitProvider:
    kind, label = 'permit', 'Permits to work'
    covers = ('quality.permit',)

    def items(self, request, now=None):
        if not _staff(request):
            return []
        jobs = _my_quality_jobs(request)
        if not jobs:
            return []
        now = now or timezone.now()
        # One query: waiting for my approval, or approved and within the longest possible warning of
        # expiry; the company's own warning time is applied below (and only looked up if needed)
        horizon = now + datetime.timedelta(hours=MAX_WARNING_HOURS)
        mine = Q(ball_in_court_user=request.user)
        rows = list(Permit.objects.filter(project_id__in=jobs).filter(
            Q(status=rules.REQUESTED) & mine
            | Q(status=rules.APPROVED, expires_at__lte=horizon) & (mine | Q(approved_by=request.user)))
            .select_related('project', 'project__organization'))
        warning = None
        out = []
        for p in rows:
            if p.status == rules.REQUESTED:
                out.append(Item(kind=self.kind, title=f'{p.type_label}: {p.title}', url=p.get_absolute_url(),
                                ref=p.ref, project=p.project, due=p.due_date, action='Approve or refuse the permit',
                                priority=PRIORITY_HIGH, status_label='Waiting for approval', tone='accent',
                                detail=p.location, kind_label='Permit to work', key=f'ptw:{p.pk}'))
                continue
            if warning is None:
                warning = QualitySettings.for_org(request.org).expiry_warning_hours
            p._warning_hours = warning
            state = p.state_at(now)
            if state not in ('expiring', 'expired'):
                continue
            local = timezone.localtime(p.expires_at)
            out.append(Item(
                kind=self.kind, title=f'{p.type_label}: {p.title}', url=p.get_absolute_url(), ref=p.ref,
                project=p.project, due=timezone.localdate(p.expires_at),
                action='Close the permit, or ask for a new one if work carries on' if state == 'expired'
                else f'Runs out at {local:%H:%M}. Close it when the work is done',
                priority=PRIORITY_HIGH, status_label=rules.STATE_LABELS[state][0], tone=rules.STATE_LABELS[state][1],
                detail=p.location, kind_label='Permit to work', key=f'ptw:{p.pk}'))
        return out


class FirestopProvider:
    kind, label = 'firestopping', 'Fire-stopping'
    covers = ('quality.penetration',)

    def items(self, request):
        if not _staff(request):
            return []
        jobs = _my_quality_jobs(request)
        if not jobs:
            return []
        qs = Penetration.objects.filter(status__in=Penetration.OPEN_STATUSES, ball_in_court_user=request.user,
                                        project_id__in=jobs) \
            .select_related('project', 'project__organization')
        return [Item(kind=self.kind, title=p.title, url=p.get_absolute_url(), ref=p.ref, project=p.project,
                     due=p.due_date,
                     action='Inspect the seal and sign it off' if p.status == Penetration.SEALED
                     else 'Get it resealed, then mark it resealed',
                     priority=PRIORITY_HIGH if p.status == Penetration.FAILED else PRIORITY_NORMAL,
                     status_label=p.get_status_display(), tone=p.status_tone, detail=p.location,
                     kind_label='Fire-stopping', key=f'fs:{p.pk}') for p in qs]
