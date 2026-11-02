"""
Who may use Quality (inspections, permits to work, fire-stopping) on a job.

  staff on the job      the project manager, the project team (internal ProjectMember roles), owners
                        and admins: everything below unless it says otherwise
  approve a permit      the project manager (Project.manager or a PM on the team), owners and admins
  checklist library     owners, admins and staff who work in Projects: edit; other staff: read
  other staff           nothing on the job (estimators can't open live jobs anyway: projects.access)
  externals             nothing in this release: clients, consultants and subcontractors get a 403
                        (snags raised from a failed check reach a subcontractor through Snags)

Every view still fetches the project with projects.access.get_project() first, so sections, archived
jobs (read only) and external roles are enforced as everywhere else. The job rule is the site diary's
(diary.access), so the people who keep the diary are the people who run quality.
"""
from django.core.exceptions import PermissionDenied

from diary.access import is_pm_or_admin, user_on_job
from projects.access import get_project, is_internal

__all__ = ['can_use_quality', 'quality_project', 'is_pm_or_admin', 'user_on_job', 'can_edit_library',
           'can_approve_permit']


def can_use_quality(request, project):
    if not is_internal(request) or project.section != 'delivery':  # won, live or complete
        return False
    return user_on_job(request.user, project, request.membership)


def quality_project(request, pk):
    """get_project() plus the quality rule: staff on a won, live or completed job only."""
    project = get_project(request, pk)
    if not can_use_quality(request, project):
        raise PermissionDenied
    return project


def can_approve_permit(user, project):
    return is_pm_or_admin(user, project)


def can_edit_library(request):
    m = request.membership
    return m is not None and m.is_internal and (m.is_admin or m.can_deliver)


def require_library_editor(request):
    if not can_edit_library(request):
        raise PermissionDenied
