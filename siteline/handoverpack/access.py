"""
Who sees a project's pricing workbook and handover pack. It carries cost, margin and buy-out targets.

  who                                          bid (lead, tendering, submitted,       won, live or complete job
                                               lost, declined)
  -------------------------------------------  -------------------------------------  ---------------------------------
  owner / admin                                yes                                    yes
  staff who work in estimating                 yes: upload while in the pipeline      only if they also run the job
  the job's project manager, or a PM or QS     (they can't open bids unless they      yes: upload until the pack is
  on its team (costs.access.can_view_costs)    also work in estimating)               issued, edit, issue
  other staff (site team, ...)                 404 (can't open bids)                  403
  client, consultant, subcontractor, bidder    403                                    403
  another company / non-member                 404 (tenant middleware and get_project)

On a won job this is exactly the commercial check the CVR, cashflow and cost pages use
(costs.access.can_view_costs). Archived projects are read only (get_project refuses writes).
"""
from django.core.exceptions import PermissionDenied

from costs.access import COST_STAGES, can_view_costs
from projects.access import get_project, is_internal
from projects.models import Project


def can_see(request, project):
    if not is_internal(request):
        return False
    if project.stage in Project.ESTIMATING_STAGES:
        return request.membership.can_estimate
    if project.stage in COST_STAGES:
        return can_view_costs(request, project)
    return False


def pack_project(request, pk):
    """get_project() plus the table above. Every view in this app that takes a project comes through here."""
    if request.membership is None or not request.membership.is_internal:
        raise PermissionDenied
    project = get_project(request, pk)
    if not can_see(request, project):
        raise PermissionDenied('Only staff who see this job\'s commercial figures see its pricing workbook.')
    return project


def can_upload(project, pack=None):
    """While the bid is in the pipeline, or on a won job until the pack has been issued."""
    if project.is_archived or (pack is not None and pack.is_issued):
        return False
    return project.stage in Project.PIPELINE_STAGES or project.stage in COST_STAGES


def can_edit(project):
    """Clarifications, site rules and open items: any time the project isn't archived, lost or declined."""
    return not project.is_archived and project.stage not in (Project.STAGE_LOST, Project.STAGE_DECLINED)


def possible_owners(project):
    """Staff an open item can be given to: people who can open the pack, so the link in their Needs me works."""
    from django.contrib.auth import get_user_model
    from django.db.models import Exists, OuterRef, Q

    from projects.access import staff_users
    from projects.models import ProjectMember
    from tenancy.models import Membership
    if project.stage in Project.ESTIMATING_STAGES:
        return staff_users(project.organization, 'estimating')
    User = get_user_model()
    member = Membership.objects.filter(organization=project.organization, user=OuterRef('pk'), is_active=True)
    team = ProjectMember.objects.filter(project=project, user=OuterRef('pk'),
                                        role__in=(ProjectMember.ROLE_PM, ProjectMember.ROLE_QS))
    return User.objects.filter(Exists(member.filter(role__in=Membership.INTERNAL_ROLES))).filter(
        Q(Exists(member.filter(role__in=Membership.ADMIN_ROLES))) | Q(pk=project.manager_id) | Q(Exists(team))
    ).order_by('full_name')
