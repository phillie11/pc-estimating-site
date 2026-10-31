"""
Who sees applications for payment.

  owners and admins          every application on every live job they can open; record notices
                             on the payer's behalf, reverse a payment recorded by mistake
  the job's project manager  every application on their job (Project.manager, or a project
                             team member with the project manager role)
  other staff                nothing (a 403): the Commercial tab doesn't show Applications
  the payer's side           the job's client, and the consultant named on the job's contract as
                             its administrator (Contract Administrator under JCT, Project Manager
                             under NEC), who certifies under the contract: submitted applications
                             on that job only; give payment and pay less notices
  other consultants, subcontractors, tendering subcontractors: nothing

Views always fetch the project with projects.access.get_project() first, which keeps out other
companies, other sections and projects an external isn't on.
"""
from django.core.exceptions import PermissionDenied

from projects.access import is_internal
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


def administrator_member(project):
    """The consultant ProjectMember the job's contract names as its administrator, or None."""
    from contracts.models import MainContract
    contract = MainContract.objects.filter(project=project).select_related('administrator').first()
    admin = contract.administrator if contract else None
    if admin is None or admin.project_id != project.pk or admin.role != ProjectMember.ROLE_CONSULTANT:
        return None
    return admin


def payer_members(project):
    """The project's clients and, if the contract names one, its consultant administrator."""
    members = list(ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CLIENT).select_related('user'))
    admin = administrator_member(project)
    if admin is not None:
        members.append(admin)
    return members


def is_payer(user, project):
    """
    The user gives notices on this job's applications: a client on it, or the consultant its
    contract names as administrator. Active external members of the organisation only.
    """
    from core.access import is_staff
    from tenancy.models import Membership
    if not getattr(user, 'is_authenticated', False) or is_staff(user, project.organization_id):
        return False
    if not Membership.objects.filter(organization_id=project.organization_id, user=user, is_active=True).exists():
        return False
    if ProjectMember.objects.filter(project=project, user=user, role=ProjectMember.ROLE_CLIENT).exists():
        return True
    admin = administrator_member(project)
    return admin is not None and admin.user_id == user.pk


def is_client(request, project=None):
    """The request is from the payer's side on the project get_project() fetched."""
    if is_internal(request):
        return False
    project = project or getattr(getattr(request, 'project_member', None), 'project', None)
    return project is not None and is_payer(request.user, project)


def can_see_applications(request, project):
    return manages_payments(request, project) or is_client(request, project)


def visible_applications(request, project):
    """Applications this person may open on a project they reached through get_project(), or 403."""
    from .models import Application
    qs = Application.objects.filter(project=project)
    if manages_payments(request, project):
        return qs
    if is_client(request, project):
        return qs.filter(visibility=Application.VIS_SHARED, status__in=Application.SHARED_STATUSES)
    raise PermissionDenied


def client_tab(request, project):
    """The payer's 'Payments' register under Decisions -> Waiting on you (projects.views.project_context)."""
    from django.urls import reverse

    from .models import Application
    if not is_client(request, project):
        return None
    waiting = Application.objects.filter(project=project, status=Application.STATUS_SUBMITTED).count()
    return ('payments', 'Payments', reverse('application_list', args=[request.org.slug, project.pk]), waiting)
