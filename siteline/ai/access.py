"""
Who sees the AI pages.

  page                                   who
  -------------------------------------  ------------------------------------------------------------------
  Settings > AI drafting, playbooks,     owners and admins (403 for everyone else)
  usage
  Bid brief (tender reader) on a bid     staff who work in estimating, as the bid's other estimating pages
                                         (404 for staff who don't: they can't open bids); starting a reading
                                         also needs AI drafting on, for their role, with budget left
                                         (ai/service.status)
  Draft clarifications on a pack         as the handover pack (handoverpack/access.py), plus the same AI check
  client, consultant, subcontractor,     403; another company or a non-member: 404 (tenant middleware and
  bidder                                 get_project). Nothing here has a portal page or an API resource.

Archived projects are read only: get_project refuses any write first.
"""
from django.core.exceptions import PermissionDenied
from django.http import Http404

from projects.access import get_project
from projects.models import Project


def brief_project(request, pk):
    if request.membership is None or not request.membership.is_internal:
        raise PermissionDenied
    project = get_project(request, pk)  # 404 for a bid this person can't open, 403 for an archived write
    if not request.membership.can_estimate or project.stage not in Project.ESTIMATING_STAGES:
        raise Http404('The bid brief is for bids.')
    return project


def can_edit_brief(project):
    return not project.is_archived and project.stage in Project.PIPELINE_STAGES
