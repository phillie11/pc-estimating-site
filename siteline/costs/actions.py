"""
Needs me: jobs whose forecast margin is under the company's threshold (Settings > Cost control).

    items(request) -> [actions.Item]

Listed for owners and admins (every job they can open) and for the job's project manager (the
job's manager, or a PM on its team). Registered from CostsConfig.ready() as a HookProvider, so
externals get nothing and items on jobs outside portal_projects are dropped. There is no due
date: it stays on the list until the forecast margin is back above the threshold.

It runs on every page (the count by the bell), so it works out every job's margin together in a
fixed number of queries (costs.report.forecast_costs / final_accounts), with the same rules as the
cost report.
"""
from django.urls import reverse

from actions.registry import PRIORITY_HIGH, Item
from projects.access import is_internal

from .access import settings_for
from .money import margin
from .report import final_accounts, forecast_costs, is_below


def _alert_projects(request):
    """Owners and admins: every job; otherwise the job's manager or a PM on its team. Jobs with costs only."""
    from actions.providers import my_jobs  # one shared query behind the badge
    admin, me = request.membership.is_admin, request.user.pk
    return [p for p in my_jobs(request) if p.has_ledger and (admin or p.manager_id == me or p.on_team_pm)]


def items(request):
    if request.membership is None or not is_internal(request):
        return []
    projects = _alert_projects(request)
    if not projects:
        return []
    threshold = settings_for(request.org).margin_threshold
    costs = forecast_costs([p.pk for p in projects])
    accounts = final_accounts(projects)
    label = f'{threshold.normalize():f}'
    out = []
    for project in projects:
        cost, account = costs.get(project.pk), accounts[project.pk]
        if cost is None or not is_below(account, cost, threshold):
            continue
        amount, pct = margin(account, cost)
        if pct is None:
            detail, status = 'No contract sum yet, so the margin can\'t be worked out', 'No contract sum'
        else:
            detail, status = f'Forecast margin {pct}% against a {label}% threshold', f'Margin {pct}%'
        out.append(Item(kind='cost_margin', title=f'Forecast margin below {label}%',
                        url=reverse('cost_report', args=[request.org.slug, project.pk]), project=project,
                        ref=project.reference, priority=PRIORITY_HIGH, status_label=status, tone='bad',
                        amount=amount, detail=detail, key=f'cost_margin:{project.pk}',
                        action='Review the cost report'))
    return out
