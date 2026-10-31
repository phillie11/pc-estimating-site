"""
Who sees applications for payment.

  owners and admins          every application on every live job they can open; record notices
                             on the client's behalf
  the job's project manager  every application on their job (Project.manager, or a project
                             team member with the project manager role)
  other staff                nothing (a 403): the Commercial tab doesn't show Applications
  the job's client           submitted applications on their own job only; give payment and
                             pay less notices
  consultants, subcontractors, tendering subcontractors: nothing

Views always fetch the project with projects.access.get_project() first, which keeps out other
companies, other sections and projects an external isn't on.
"""
from django.core.exceptions import PermissionDenied

from projects.access import external_role, is_internal
from projects.models import ProjectMember


def is_project_pm(user, project):
    if not getattr(user, 'is_authenticated', False):
        return False
    return project.manager_id == user.pk or ProjectMember.objects.filter(
        project=project, user=user, role=ProjectMember.ROLE_PM).exists()


def manages_payments(request, project):
    """Owners, admins and the job's project manager (staff only)."""
    if not is_internal(request):
        return False
    return request.membership.is_admin or is_project_pm(request.user, project)


def user_manages_payments(user, project):
    """The same rule from a user, for transition predicates (no request)."""
    from core.access import is_admin, is_staff
    return is_admin(user, project.organization_id) or (is_staff(user, project.organization_id)
                                                      and is_project_pm(user, project))


def is_client(request):
    return external_role(request) == ProjectMember.ROLE_CLIENT


def can_see_applications(request, project):
    return manages_payments(request, project) or is_client(request)


def visible_applications(request, project):
    """Applications this person may open on a project they reached through get_project(), or 403."""
    from .models import Application
    qs = Application.objects.filter(project=project)
    if manages_payments(request, project):
        return qs
    if is_client(request):
        return qs.filter(visibility=Application.VIS_SHARED, status__in=Application.SHARED_STATUSES)
    raise PermissionDenied


def client_tab(request, project):
    """The client's 'Payments' register under Decisions -> Waiting on you (projects.views.project_context)."""
    from django.urls import reverse

    from .models import Application
    if not is_client(request):
        return None
    waiting = Application.objects.filter(project=project, status=Application.STATUS_SUBMITTED).count()
    return ('payments', 'Payments', reverse('application_list', args=[request.org.slug, project.pk]), waiting)
