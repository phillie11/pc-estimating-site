"""
Who may use the Safety tab on a job.

  staff on the job   the project manager, the project team (internal ProjectMember roles),
                     owners and admins: report incidents, add actions and photos, keep the CDM
                     register, see who is on site, sign people in and out, print the poster
  project manager    (Project.manager or a PM on the team), owners and admins: start an
                     investigation, close or reopen an incident, replace the QR code
  owners and admins  the company-wide incident register
  everyone else      nothing on the Safety tab. Externals (client, consultant, subcontractor)
                     only meet safety through the public QR sign-in page, which shows nothing
                     but the job's name, and an appointed subcontractor's "Report a near miss"
                     page in their portal (safety/portal.py: the report form and their own
                     company's reports, never the investigation).

The rule for "staff on the job" is the site diary's (diary.access), so the two tabs always
agree. Every view still fetches the project with projects.access.get_project() first, so
sections, archived jobs (read only) and external roles are enforced as everywhere else.
"""
from django.core.exceptions import PermissionDenied

from diary.access import is_pm_or_admin, user_on_job
from projects.access import get_project, is_internal

__all__ = ['can_use_safety', 'safety_project', 'is_pm_or_admin', 'user_on_job', 'job_staff', 'can_see_company_register']


def can_use_safety(request, project):
    if not is_internal(request) or project.section != 'delivery':  # won, live or complete jobs
        return False
    return user_on_job(request.user, project, request.membership)


def safety_project(request, pk):
    """get_project() plus the safety rule: staff on a won, live or completed job only."""
    project = get_project(request, pk)
    if not can_use_safety(request, project):
        raise PermissionDenied
    return project


def job_staff(project):
    """Everyone who passes user_on_job() for this job, as a queryset: owners, admins, the manager, the team."""
    from django.db.models import Q

    from projects.access import staff_users
    from projects.models import ProjectMember
    from tenancy.models import Membership
    admins = Membership.objects.filter(organization_id=project.organization_id, role__in=Membership.ADMIN_ROLES,
                                       is_active=True).values('user_id')
    team = ProjectMember.objects.filter(project=project, role__in=[r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]) \
        .values('user_id')
    return staff_users(project.organization).filter(Q(pk__in=admins) | Q(pk__in=team) | Q(pk=project.manager_id))


def can_see_company_register(request):
    return request.membership is not None and request.membership.is_internal and request.membership.is_admin
