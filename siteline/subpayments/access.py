"""
Who sees subcontractor payments.

  who                                   what
  ------------------------------------  -----------------------------------------------------------
  owner / admin                         every subcontract on every won, live or complete job; CIS and
                                        VAT details of subcontractors; the company's CIS summary and
                                        default terms; reverse a payment
  the job's PM or QS                    that job's subcontracts (the same people as costs.access):
                                        terms, applications, our notices, payments
  appointed subcontractor               only their own applications and payment advices, on packages
                                        they are appointed on; apply for payment
  other staff, client, consultant,      nothing (403)
  tendering subcontractor
  another company / non-member          404 (tenant middleware and get_project)

Views take the project from projects.access.get_project() first (archived jobs read only).
"""
from django.core.exceptions import PermissionDenied

from costs.access import COST_STAGES, can_view_costs, runs_job
from delivery.access import subcontractor_id
from directory.models import Package
from projects.access import is_internal

STAFF, SUB = 'staff', 'sub'


def role(request, project):
    """STAFF (owners, admins, the job's PM/QS), SUB (appointed on a package on this job), or None."""
    from tenancy.context import request_memo
    if project.stage not in COST_STAGES:
        return None
    # Asked several times a page (the view, the tab, the access checks): once per request
    return request_memo(('subpay_role', request.user.pk, project.pk), lambda: _role(request, project))


def _role(request, project):
    if is_internal(request):
        return STAFF if can_view_costs(request, project) else None
    sub = subcontractor_id(request)
    if sub is not None and Package.objects.filter(project=project, subcontractor_id=sub,
                                                  status=Package.APPOINTED).exists():
        return SUB
    return None


def require(request, project):
    who = role(request, project)
    if who is None:
        raise PermissionDenied
    return who


def require_staff(request, project):
    if role(request, project) != STAFF:
        raise PermissionDenied


def user_is_staff_for(user, project):
    """For Needs me: owners/admins, or the job's PM/QS (no request)."""
    from core.access import is_admin, is_staff
    return is_admin(user, project.organization_id) or (is_staff(user, project.organization_id)
                                                      and runs_job(user, project))


def packages_for(request, project):
    """Appointed packages this person may see on the job."""
    qs = Package.objects.filter(project=project, status=Package.APPOINTED).select_related('subcontractor', 'trade')
    who = role(request, project)
    if who == STAFF:
        return qs
    if who == SUB:
        return qs.filter(subcontractor_id=subcontractor_id(request))
    raise PermissionDenied


def applications_for(request, project):
    from .models import SubApplication
    qs = SubApplication.objects.filter(project=project).select_related(
        'subcontract__package__subcontractor', 'subcontract__package__trade')
    who = require(request, project)
    if who == SUB:
        qs = qs.filter(subcontract__package__subcontractor_id=subcontractor_id(request),
                       subcontract__package__status=Package.APPOINTED)
    return qs


def sub_tab(request, project):
    """The appointed subcontractor's 'Payments' register under Decisions (projects.views.project_context)."""
    from django.urls import reverse
    if is_internal(request) or role(request, project) != SUB:
        return None
    return ('subpayments', 'Payments', reverse('subpay_list', args=[request.org.slug, project.pk]), None)
