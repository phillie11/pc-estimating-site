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


def visible_projects(request):
    qs = Project.objects.all()
    if not is_internal(request):
        qs = qs.filter(members__user=request.user)
    return qs.select_related('owner')


def portal_projects(request):
    """Projects whose shared content (RFIs, feed) this user may see: bidders' tenders excluded."""
    qs = visible_projects(request)
    if not is_internal(request):
        qs = qs.exclude(members__user=request.user, members__role=ProjectMember.ROLE_BIDDER)
    return qs


def get_project(request, pk, allow_bidder=False):
    """Fetch a project the user may see; sets request.project_member."""
    project = get_object_or_404(visible_projects(request), pk=pk)
    request.project_member = ProjectMember.objects.filter(project=project, user=request.user) \
        .select_related('subcontractor').first()
    if not is_internal(request) and request.project_member is None:
        raise PermissionDenied
    if is_bidder(request) and not allow_bidder:
        raise PermissionDenied
    return project


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


def staff_users(org):
    User = get_user_model()
    return User.objects.filter(
        memberships__organization=org, memberships__role__in=Membership.INTERNAL_ROLES, memberships__is_active=True
    ).distinct().order_by('full_name')
