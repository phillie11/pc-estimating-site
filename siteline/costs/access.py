"""
Who sees cost control.

  who                                 cost report, budget, orders, actuals, ledger
  ----------------------------------  ---------------------------------------------
  owner / admin                       every won, live or complete job
  staff who run the job               that job: its project manager, or a PM or QS on its team
  other staff (site, estimators...)   no (403); estimators can't open live jobs at all (projects.access)
  client, consultant, subcontractor   never (403)
  another company / non-member        404 (tenant middleware and get_project)

Archived jobs are read only for everyone (get_project refuses anything but GET/HEAD).
The cost code library and margin threshold are owners' and admins' (Settings > Cost control).
"""
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils import timezone

from projects.access import get_project, is_internal, visible_projects
from projects.models import Project, ProjectMember

COST_ROLES = (ProjectMember.ROLE_PM, ProjectMember.ROLE_QS)
COST_STAGES = Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,)


def runs_job(user, project):
    return project.manager_id == user.pk or ProjectMember.objects.filter(
        project=project, user=user, role__in=COST_ROLES).exists()


def can_view_costs(request, project):
    if not is_internal(request) or project.stage not in COST_STAGES:
        return False
    return request.membership.is_admin or runs_job(request.user, project)


def costs_project(request, pk):
    """get_project() plus the cost rules above. Every costs view that takes a project comes through here."""
    project = get_project(request, pk)
    if not is_internal(request):
        raise PermissionDenied
    if project.stage not in COST_STAGES:
        raise Http404('Cost control is for won and live jobs.')
    if not can_view_costs(request, project):
        raise PermissionDenied('Only the job\'s project manager or QS, owners and admins see its costs.')
    return project


def cost_projects(request):
    """Jobs whose costs this person sees (not archived), for company-wide lists and Needs me."""
    from django.db.models import Exists, OuterRef, Q
    if not is_internal(request):
        return Project.objects.none()
    qs = visible_projects(request).filter(stage__in=COST_STAGES)
    if request.membership.is_admin:
        return qs
    on_team = ProjectMember.objects.filter(project=OuterRef('pk'), user=request.user, role__in=COST_ROLES)
    return qs.filter(Q(manager=request.user) | Q(Exists(on_team)))


def settings_for(org):
    """The company's cost settings, creating them (and the default cost code library) the first time."""
    from django.db import IntegrityError, transaction

    from .defaults import DEFAULT_CODES
    from .models import CostCode, CostSettings
    obj = CostSettings.objects.filter(organization=org).first()
    if obj is not None:
        return obj
    try:
        with transaction.atomic():
            obj = CostSettings.objects.create(organization=org, codes_seeded_at=timezone.now())
            existing = set(CostCode.objects.filter(organization=org).values_list('code', flat=True))
            CostCode.objects.bulk_create([CostCode(organization=org, code=c, name=n, group=g)
                                          for c, n, g in DEFAULT_CODES if c not in existing])
    except IntegrityError:  # two first requests at once: the other one seeded it
        obj = CostSettings.objects.get(organization=org)
    return obj
