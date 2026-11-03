"""
Who sees the CVR, cashflow and dayworks (all under the Commercial tab, staff only).

  who                                    CVR and cashflow              dayworks
  -------------------------------------  ----------------------------  ---------------------------------
  owner / admin                          every won, live or complete    every job: record, sign, send to
                                         job; sign off any CVR; the     the change register, set rates
                                         company CVR; settings
  the job's PM or QS (costs.access)      that job; prepare and submit   that job, and set its rates
                                         its CVR; sign off only if the
                                         company allows it
  other staff on the job (site team)     no (403)                       record, get signed and send on
  client, consultant, subcontractor      never (403)                    never (403): the client signs on
                                                                        the site team's phone
  another company / non-member           404 (tenant middleware and get_project)

Archived jobs are read only for everyone (get_project refuses anything but GET/HEAD).
"""
from django.core.exceptions import PermissionDenied
from django.http import Http404

from costs.access import COST_STAGES, can_view_costs, cost_projects, costs_project, runs_job
from projects.access import get_project, is_internal


def cvr_project(request, pk):
    """get_project() plus the cost rules (costs.access.costs_project): the CVR and cashflow pages."""
    return costs_project(request, pk)


def daywork_project(request, pk):
    project = get_project(request, pk)
    if not is_internal(request):
        raise PermissionDenied
    if project.stage not in COST_STAGES:
        raise Http404('Dayworks are for won and live jobs.')
    return project


def can_set_rates(request, project):
    return can_view_costs(request, project)


def can_sign_off(user, period, settings_obj=None):
    """Owners and admins; the job's PM or QS too where the company allows it (Settings > Commercial reporting)."""
    from core.access import is_admin, is_staff
    org_id = period.organization_id
    if is_admin(user, org_id):
        return True
    if not is_staff(user, org_id):
        return False
    from .services import settings_for
    settings_obj = settings_obj or settings_for(period.project.organization)
    return settings_obj.pm_can_sign_off and runs_job(user, period.project)


def prepares_cvr(user, project):
    """Owners, admins and the job's PM or QS: start, adjust and submit the CVR."""
    from core.access import is_admin, is_staff
    org_id = project.organization_id
    return is_admin(user, org_id) or (is_staff(user, org_id) and runs_job(user, project))


__all__ = ['cvr_project', 'daywork_project', 'can_set_rates', 'can_sign_off', 'prepares_cvr', 'cost_projects',
           'can_view_costs']
