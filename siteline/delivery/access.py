"""Who may use the live-job portal (RAMS and subcontractor variations) on a project."""
from django.core.exceptions import PermissionDenied

from projects.access import is_internal
from projects.models import ProjectMember


def subcontractor_id(request):
    """The subcontractor company this external user represents on the project, if appointed."""
    pm = getattr(request, 'project_member', None)
    if pm is not None and pm.role == ProjectMember.ROLE_SUBCONTRACTOR and pm.subcontractor_id:
        return pm.subcontractor_id
    return None


def can_use_site_portal(request):
    return is_internal(request) or subcontractor_id(request) is not None


def scope(request):
    """None for staff (everything); a subcontractor id for an appointed sub; refuses anyone else."""
    if is_internal(request):
        return None
    sub = subcontractor_id(request)
    if sub is None:
        raise PermissionDenied
    return sub


def site_counts(request, project):
    from .models import Rams, SubVariation
    rams, subvars = Rams.objects.filter(project=project), SubVariation.objects.filter(project=project)
    if is_internal(request):
        return {'rams': rams.filter(status=Rams.SUBMITTED).count(),
                'subvariations': subvars.filter(status=SubVariation.SUBMITTED).count()}
    sub = subcontractor_id(request)
    return {'rams': rams.filter(subcontractor_id=sub, status=Rams.REJECTED).count(),
            'subvariations': subvars.filter(subcontractor_id=sub, status=SubVariation.SUBMITTED).count()}
