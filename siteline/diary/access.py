"""
Who may use the site diary on a job.

  staff on the job      the project manager, the project team (internal ProjectMember roles),
                        owners and admins: read and write the diary, photos and delays
  unlock                the project manager (Project.manager or a PM on the team), owners, admins
  other staff           nothing (estimators can't open live jobs anyway: projects.access)
  client                photos marked "share with client", read only, on the project overview
  everyone else         nothing

Every view still fetches the project with projects.access.get_project() first, so sections,
archived jobs (read only) and external roles are enforced as everywhere else.
"""
from django.core.exceptions import PermissionDenied

from projects.access import get_project, is_internal
from projects.models import ProjectMember
from tenancy.models import Membership

INTERNAL_ROLES = [r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]


def _membership(user, org_id):
    if not getattr(user, 'is_authenticated', False):
        return None
    return Membership.objects.filter(organization_id=org_id, user=user, is_active=True).first()


def user_on_job(user, project, membership=None):
    """Staff on the job: the project manager, the project team, owners and admins."""
    m = membership or _membership(user, project.organization_id)
    if m is None or not m.is_internal:
        return False
    if m.is_admin or project.manager_id == user.pk:
        return True
    return ProjectMember.objects.filter(project=project, user=user, role__in=INTERNAL_ROLES).exists()


def is_pm_or_admin(user, project, membership=None):
    m = membership or _membership(user, project.organization_id)
    if m is None or not m.is_internal:
        return False
    if m.is_admin or project.manager_id == user.pk:
        return True
    return ProjectMember.objects.filter(project=project, user=user, role=ProjectMember.ROLE_PM).exists()


def can_use_diary(request, project):
    if not is_internal(request) or project.section != 'delivery':  # won, live or complete
        return False
    return user_on_job(request.user, project, request.membership)


def diary_project(request, pk):
    """get_project() plus the diary rule: staff on a won, live or completed job only."""
    project = get_project(request, pk)
    if not can_use_diary(request, project):
        raise PermissionDenied
    return project


def can_see_shared_photos(request):
    pm = getattr(request, 'project_member', None)
    return pm is not None and pm.role == ProjectMember.ROLE_CLIENT
