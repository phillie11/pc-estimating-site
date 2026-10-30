"""
The built-in "Needs me" providers. Each one reads the same rows, with the same access
rules, as the page its items link to:

  provider            who           rows                                   access as
  ------------------  ------------  -------------------------------------  ------------------------------
  records             anyone        RecordIndex, open, ball in court       core.access.needs_me (RFIs...)
  client_variation    client        Variation SUBMITTED                    variation_detail (client role)
  rams_revise         subcontractor Rams REJECTED, their company           rams_detail (delivery.access)
  rams_review         staff         Rams SUBMITTED on jobs they run        rams_detail (staff)
  sub_variation       staff         SubVariation SUBMITTED on their jobs   subvariation_detail (staff)
  addendum            tendering sub Addendum without their receipt         tender (bidder)
  tender              tendering sub Package ENQUIRY for their company      tender (bidder)
  snag                anyone        Issue OPEN assigned to them            issue_detail
  snag_inspect        staff         Issue READY they raised or manage      issue_detail
  task                anyone        Task not done, assigned to them        task_list / task_edit

"Jobs they run" is the same group RAMS and sub variation alerts go to
(delivery.views.site_team): the project manager and staff on the project team, or the
bid lead when a job has neither.
"""
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from .registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item


def _staff(request):
    from projects.access import is_internal
    return is_internal(request)


def _portal(request):
    from projects.access import portal_projects
    return portal_projects(request)


def _my_memberships(request, roles):
    """This external user's ProjectMember rows with these roles, on projects they can open."""
    from projects.access import visible_projects
    from projects.models import ProjectMember
    return ProjectMember.objects.filter(user=request.user, role__in=roles,
                                        project__in=visible_projects(request).values('pk'))


def jobs_i_run(request):
    """Live jobs whose RAMS and sub variations wait on this member of staff (delivery.views.site_team)."""
    from projects.access import visible_projects
    from projects.models import Project, ProjectMember
    internal_roles = [r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]
    team = ProjectMember.objects.filter(project=OuterRef('pk'), role__in=internal_roles)
    on_team = ProjectMember.objects.filter(project=OuterRef('pk'), role__in=internal_roles, user=request.user)
    return visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,)).filter(
        Q(manager=request.user) | Q(Exists(on_team)) | (Q(manager__isnull=True, owner=request.user) & ~Q(Exists(team))))


def _priority(value):
    return PRIORITY_HIGH if value in ('HIGH', 'URGENT') else PRIORITY_NORMAL


class RecordsProvider:
    """Open records of any type on core.RecordBase whose ball is in this person's court."""
    kind, label = 'record', 'RFIs and records'

    def items(self, request):
        from core.access import needs_me

        from . import registry
        qs = needs_me(request).exclude(record_type__in=registry.covered_record_types())
        out = []
        for idx in qs.select_related('project')[:200]:
            responded = idx.status == 'RESPONDED'
            out.append(Item(
                kind=idx.record_type.split('.')[-1], title=idx.title, url=idx.url, ref=idx.ref, project=idx.project,
                due=idx.due_date, status_label=idx.status_label, tone='info' if responded else 'accent',
                action='Accept the answer or reopen it' if responded else 'Answer it',
                kind_label='RFIs' if idx.record_type == 'rfis.rfi' else idx.record_type.split('.')[-1].upper(),
                key=f'idx:{idx.pk}',
            ))
        return out


class ClientVariationProvider:
    """Variations submitted to the client and waiting for them to approve or reject."""
    kind, label = 'client_variation', 'Variations to approve'
    covers = ('variations.variation',)

    def items(self, request):
        from projects.models import ProjectMember
        from variations.models import Variation
        if _staff(request):
            return []
        projects = _my_memberships(request, [ProjectMember.ROLE_CLIENT]).values('project_id')
        qs = Variation.objects.filter(project_id__in=projects, status=Variation.STATUS_SUBMITTED) \
            .filter(project__in=_portal(request).values('pk'))
        return [Item(
            kind=self.kind, title=v.title, url=v.get_absolute_url(), ref=v.reference, project=v.project,
            due=None, priority=PRIORITY_HIGH if v.proceed_at_risk else PRIORITY_NORMAL,
            action='Approve or reject', status_label=v.get_status_display(), tone=v.status_tone, amount=v.value,
            detail=(f'{v.time_impact_days} days time impact' if v.time_impact_days else ''),
            key=f'vo:{v.pk}',
        ) for v in qs.select_related('project', 'project__organization').order_by('submitted_at', 'number')]


class RamsReviseProvider:
    """RAMS the contractor sent back to this subcontractor to revise and resubmit."""
    kind, label = 'rams_revise', 'RAMS to revise'

    def items(self, request):
        from delivery.models import Rams
        from projects.models import ProjectMember
        if _staff(request):
            return []
        pairs = list(_my_memberships(request, [ProjectMember.ROLE_SUBCONTRACTOR])
                     .filter(subcontractor__isnull=False).values_list('project_id', 'subcontractor_id'))
        if not pairs:
            return []
        q = Q()
        for project_id, sub_id in pairs:
            q |= Q(project_id=project_id, subcontractor_id=sub_id)
        qs = Rams.objects.filter(q, status=Rams.REJECTED, project__in=_portal(request).values('pk'))
        return [Item(
            kind=self.kind, title=r.title, url=r.get_absolute_url(), ref=r.reference, project=r.project,
            priority=PRIORITY_HIGH, action='Revise and resubmit', status_label=r.get_status_display(), tone=r.status_tone,
            detail=(r.review_comments[:120] if r.review_comments else ''), key=f'rams:{r.pk}',
        ) for r in qs.select_related('project', 'project__organization').order_by('reviewed_at')]


class RamsReviewProvider:
    """RAMS a subcontractor submitted on a job this member of staff runs."""
    kind, label = 'rams_review', 'RAMS to review'

    def items(self, request):
        from delivery.models import Rams
        if not _staff(request):
            return []
        qs = Rams.objects.filter(project__in=jobs_i_run(request).values('pk'), status=Rams.SUBMITTED)
        return [Item(
            kind=self.kind, title=r.title, url=r.get_absolute_url(), ref=r.reference, project=r.project,
            action='Review it', status_label=r.get_status_display(), tone=r.status_tone, detail=str(r.subcontractor),
            key=f'rams:{r.pk}',
        ) for r in qs.select_related('project', 'project__organization', 'subcontractor').order_by('submitted_at')]


class SubVariationProvider:
    """Subcontractor variations waiting for this member of staff's decision."""
    kind, label = 'sub_variation', 'Sub variations to decide'
    covers = ('delivery.subvariation',)

    def items(self, request):
        from delivery.models import SubVariation
        if not _staff(request):
            return []
        qs = SubVariation.objects.filter(project__in=jobs_i_run(request).values('pk'), status=SubVariation.SUBMITTED)
        return [Item(
            kind=self.kind, title=v.title, url=v.get_absolute_url(), ref=v.reference, project=v.project,
            action='Approve or reject', status_label=v.get_status_display(), tone=v.status_tone, amount=v.value,
            detail=str(v.subcontractor), key=f'sv:{v.pk}',
        ) for v in qs.select_related('project', 'project__organization', 'subcontractor').order_by('created_at')]


def _bidding(request):
    """(project_id, subcontractor_id) for each tender this external user's company is pricing."""
    from projects.models import ProjectMember
    if _staff(request):
        return []
    return list(_my_memberships(request, [ProjectMember.ROLE_BIDDER, ProjectMember.ROLE_SUBCONTRACTOR])
                .filter(subcontractor__isnull=False).values_list('project_id', 'subcontractor_id'))


class TenderProvider:
    """Invitations to tender this subcontractor hasn't priced or declined yet."""
    kind, label = 'tender', 'Tenders to price'

    def items(self, request):
        from directory.models import Package
        from tenders.services import tender_url
        pairs = _bidding(request)
        if not pairs:
            return []
        q = Q()
        for project_id, sub_id in pairs:
            q |= Q(project_id=project_id, subcontractor_id=sub_id)
        qs = Package.objects.filter(q, status=Package.ENQUIRY).select_related('project', 'project__organization', 'trade')
        return [Item(
            kind=self.kind, title=f'Price {p.trade or "the package"}', url=tender_url(p.project), ref='',
            project=p.project, due=p.quote_due, action='Submit your quote or decline', status_label=p.get_status_display(),
            tone=p.status_tone, key=f'pkg:{p.pk}',
        ) for p in qs.order_by('quote_due')]


class AddendumProvider:
    """Addenda issued on a tender this subcontractor is pricing and hasn't acknowledged."""
    kind, label = 'addendum', 'Addenda to acknowledge'

    def items(self, request):
        from directory.models import Package
        from tenders.models import Addendum, AddendumReceipt
        from tenders.services import tender_url
        pairs = _bidding(request)
        out = []
        for project_id, sub_id in pairs:
            if not Package.objects.filter(project_id=project_id, subcontractor_id=sub_id,
                                          status__in=Package.OPEN_STATUSES).exists():
                continue  # nothing left to price here, so an addendum no longer needs them
            acked = AddendumReceipt.objects.filter(addendum=OuterRef('pk'), subcontractor_id=sub_id)
            for a in Addendum.objects.filter(project_id=project_id).exclude(Exists(acked)) \
                    .select_related('project', 'project__organization').order_by('number'):
                out.append(Item(
                    kind=self.kind, title=a.title, url=tender_url(a.project) + '#addenda', ref=a.reference,
                    project=a.project, priority=PRIORITY_HIGH, action='Read it and confirm your price allows for it',
                    status_label='Not acknowledged', tone='warn', key=f'add:{a.pk}:{sub_id}',
                ))
        return out


class SnagProvider:
    """Snags and issues assigned to this person that are still open (not yet ready for inspection)."""
    kind, label = 'snag', 'Snags to fix'

    def items(self, request):
        from projects.models import Issue
        qs = Issue.objects.filter(assignee=request.user, status=Issue.STATUS_OPEN, project__in=_portal(request).values('pk'))
        return [Item(
            kind=self.kind, title=i.title, url=reverse('issue_detail', args=[i.project.organization.slug, i.project_id, i.pk]),
            ref=i.reference, project=i.project, due=i.due_date, priority=_priority(i.priority),
            action='Fix it, then mark it ready for inspection', status_label=i.get_status_display(), tone=i.status_tone,
            detail=i.location, key=f'issue:{i.pk}',
        ) for i in qs.select_related('project', 'project__organization')]


class SnagInspectProvider:
    """Snags marked ready for inspection that this member of staff raised, or on a job they manage."""
    kind, label = 'snag_inspect', 'Snags to inspect'

    def items(self, request):
        from projects.models import Issue
        if not _staff(request):
            return []
        qs = Issue.objects.filter(Q(created_by=request.user) | Q(project__manager=request.user),
                                  status=Issue.STATUS_READY, project__in=_portal(request).values('pk'))
        return [Item(
            kind=self.kind, title=i.title, url=reverse('issue_detail', args=[i.project.organization.slug, i.project_id, i.pk]),
            ref=i.reference, project=i.project, due=i.due_date, action='Inspect and close it, or reopen it',
            status_label='Ready to inspect', tone='info', detail=(i.assignee.full_name if i.assignee else ''),
            key=f'issue:{i.pk}',
        ) for i in qs.select_related('project', 'project__organization', 'assignee').distinct()]


class TaskProvider:
    kind, label = 'task', 'Tasks'

    def items(self, request):
        from projects.models import Task
        staff = _staff(request)
        qs = Task.objects.filter(assignee=request.user, project__in=_portal(request).values('pk')) \
            .exclude(status=Task.STATUS_DONE).select_related('project', 'project__organization')
        out = []
        for t in qs:
            slug = t.project.organization.slug
            url = reverse('task_edit', args=[slug, t.project_id, t.pk]) if staff \
                else reverse('task_list', args=[slug, t.project_id]) + f'#task-{t.pk}'
            out.append(Item(
                kind=self.kind, title=t.title, url=url, ref='', project=t.project, due=t.due_date,
                priority=_priority(t.priority), action='Do it and mark it done', status_label=t.get_status_display(),
                tone='info' if t.status == Task.STATUS_DOING else 'muted', key=f'task:{t.pk}',
            ))
        return out


BUILT_IN = [RecordsProvider, ClientVariationProvider, RamsReviseProvider, RamsReviewProvider, SubVariationProvider,
            TenderProvider, AddendumProvider, SnagProvider, SnagInspectProvider, TaskProvider]
