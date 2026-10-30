"""
Hooks for the "Needs me" list and the daily digest, which other parts of the product own.
Register these there; nothing in tenders calls them.

    from tenders.actions import coverage_alerts            # in a request (Needs me)
    from tenders.actions import coverage_alerts_for_org    # in send_reminders (digest)
    from tenders.actions import estimating_recipients      # who gets them: every estimator, company-wide

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
    For jobs and commands (no request): run inside org_context(org). Company-wide: every
    estimator gets every alert (product owner, 30 Oct 2026). With user, [] unless they work
    in estimating there, so the digest can call it per person without other checks.
    """
    if user is not None and user.pk not in {u.pk for u in estimating_recipients(org)}:
        return []
    return coverage.alerts(Project.objects.filter(organization=org), today=today or timezone.localdate())


def estimating_recipients(org):
    """Who the digest sends coverage alerts to: active staff with estimating access (owners and admins included)."""
    from projects.access import staff_users
    return list(staff_users(org, 'estimating'))
