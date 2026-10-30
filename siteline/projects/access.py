"""
Who can see what inside a project.

Internal staff (Owner/Admin/Staff) see every project in their organisation.
External collaborators see only projects they were invited to, and inside
those only what is shared with them:

  area        | client / consultant           | subcontractor
  ------------+-------------------------------+-------------------------------
  overview    | no commercial figures         | no commercial figures
  documents   | shared docs; can upload       | shared docs; can upload
  RFIs        | shared RFIs; raise; respond   | shared RFIs; raise; respond
  variations  | non-draft; client approves    | hidden
  tasks/issues| assigned to them              | assigned to them
  packages    | hidden                        | hidden

A tendering subcontractor (ProjectMember.ROLE_BIDDER) is a subcontractor
pricing a package. They see only the tender page: their package and quote,
documents issued to tenderers, addenda and published query answers. Every
other project view refuses them. Appointing them makes them an ordinary
subcontractor; an unsuccessful or declined bidder loses access.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404

from tenancy.models import Membership

from .models import Project, ProjectMember


def is_internal(request):
    return request.membership is not None and request.membership.is_internal


def visible_projects(request, include_archived=False):
    """
    External people see the projects they were invited to. Staff see the projects in the
    sections they work in: estimators see bids, the projects team sees won and live jobs.
    Archived projects are left out unless asked for (only owners and admins may open them).
    """
    qs = Project.objects.all()
    if not include_archived:
        qs = qs.filter(archived_at__isnull=True)
    if not is_internal(request):
        qs = qs.filter(members__user=request.user)
    elif not request.membership.can_deliver:
        qs = qs.filter(stage__in=Project.ESTIMATING_STAGES)
    elif not request.membership.can_estimate:
        qs = qs.exclude(stage__in=Project.ESTIMATING_STAGES)
    return qs.select_related('owner', 'manager')


def openable(request):
    """
    Q for rows (activity, notifications) tied to no project or to one this person can open:
    their sections only, archived ones only for owners and admins.
    """
    projects = visible_projects(request, include_archived=can_see_archived(request))
    return Q(project__isnull=True) | Q(project__in=projects.values('pk'))


def portal_projects(request):
    """Projects whose shared content (RFIs, feed) this user may see: bidders' tenders excluded."""
    qs = visible_projects(request)
    if not is_internal(request):
        qs = qs.exclude(members__user=request.user, members__role=ProjectMember.ROLE_BIDDER)
    return qs


SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def get_project(request, pk, allow_bidder=False, allow_archived_write=False):
    """
    Fetch a project the user may see; sets request.project_member. Owners and admins can open
    archived ones, read only: archiving frees a plan slot, so work must not carry on there.
    Every project view comes through here, so any request that could write (anything but GET
    and HEAD) to an archived project is refused unless the view says otherwise, which only
    archive/restore and deleting an empty project do.
    """
    project = get_object_or_404(visible_projects(request, include_archived=can_see_archived(request)), pk=pk)
    if project.is_archived and request.method not in SAFE_METHODS and not allow_archived_write:
        raise PermissionDenied('This project is archived, so it is read only. Restore it to make changes.')
    request.project_member = ProjectMember.objects.filter(project=project, user=request.user) \
        .select_related('subcontractor').first()
    if not is_internal(request) and request.project_member is None:
        raise PermissionDenied
    if is_bidder(request) and not allow_bidder:
        raise PermissionDenied
    return project


def can_see_archived(request):
    return is_internal(request) and request.membership.is_admin


def is_bidder(request):
    pm = getattr(request, 'project_member', None)
    return not is_internal(request) and pm is not None and pm.is_bidder


def external_role(request):
    pm = getattr(request, 'project_member', None)
    return pm.role if pm and pm.is_external else None


def can_see_variations(request):
    return is_internal(request) or external_role(request) in (ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT)


def can_decide_variations(request):
    return external_role(request) == ProjectMember.ROLE_CLIENT


def require_internal(request):
    if not is_internal(request):
        raise PermissionDenied


def assignable_users(project):
    """People who can be given an RFI, task or issue: org staff plus project members."""
    User = get_user_model()
    return User.objects.filter(
        Q(memberships__organization_id=project.organization_id,
          memberships__role__in=Membership.INTERNAL_ROLES, memberships__is_active=True)
        | Q(project_memberships__project=project, memberships__organization_id=project.organization_id,
            memberships__is_active=True)
    ).distinct().order_by('full_name')


def staff_users(org, area=None):
    """Staff of the organisation; area='estimating' or 'delivery' narrows to people who work there."""
    User = get_user_model()
    # One filter() call so every condition applies to the same membership row (this org's)
    q = Q(memberships__organization=org, memberships__role__in=Membership.INTERNAL_ROLES, memberships__is_active=True)
    wanted = {'estimating': Membership.AREA_ESTIMATING, 'delivery': Membership.AREA_DELIVERY}.get(area)
    if wanted:
        q &= Q(memberships__role__in=Membership.ADMIN_ROLES) | Q(memberships__area__in=[Membership.AREA_BOTH, wanted])
    return User.objects.filter(q).distinct().order_by('full_name')
