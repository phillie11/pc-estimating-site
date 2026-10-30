"""
Hooks for the "Needs me" list and the daily digest, which other parts of the product own.
Register these there; nothing in tenders calls them.

    from tenders.actions import coverage_alerts            # in a request (Needs me)
    from tenders.actions import coverage_alerts_for_org    # in send_reminders (digest)

Each returns a list of dicts:

    {'kind': 'tenders.thin_coverage', 'project': <Project>, 'title': str, 'detail': str,
     'url': str (path, add SITE_URL for email), 'due': date, 'tone': 'bad' | 'warn',
     'owner': <User or None>, 'trades': [trade names]}

one per open tender whose subcontract quotes are due within 3 days (today included) and
that has a trade with fewer quotes in than the company's target. Internal staff only:
both return [] for anyone else, so a tendering subcontractor can never see coverage.
"""
from django.utils import timezone

from projects.models import Project

from . import coverage


def coverage_alerts(request, mine_only=False):
    """For the person making the request: open tenders they can see in estimating (their own with mine_only)."""
    m = getattr(request, 'membership', None)
    if m is None or not m.can_estimate:
        return []
    from projects.access import visible_projects
    projects = visible_projects(request)
    if mine_only:
        projects = projects.filter(owner=request.user)
    return coverage.alerts(projects, today=timezone.localdate())


def coverage_alerts_for_org(org, user=None, today=None):
    """
    For jobs and commands (no request): run inside org_context(org). With user, only bids
    they lead. The digest's other tender lines are company-wide, so pass user=None to match.
    """
    projects = Project.objects.filter(organization=org)
    if user is not None:
        projects = projects.filter(owner=user)
    return coverage.alerts(projects, today=today or timezone.localdate())
