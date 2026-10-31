"""
Needs me: jobs whose forecast margin is under the company's threshold (Settings > Cost control).

    items(request) -> [actions.Item]

Listed for owners and admins (every job they can open) and for the job's project manager (the
job's manager, or a PM on its team). Registered from CostsConfig.ready() as a HookProvider, so
externals get nothing and items on jobs outside portal_projects are dropped. There is no due
date: it stays on the list until the forecast margin is back above the threshold.
"""
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from actions.registry import PRIORITY_HIGH, Item
from projects.access import is_internal
from projects.models import ProjectMember

from .access import cost_projects
from .models import LedgerEntry
from .report import cost_report


def _alert_projects(request):
    qs = cost_projects(request)
    if not request.membership.is_admin:
        pm = ProjectMember.objects.filter(project=OuterRef('pk'), user=request.user, role=ProjectMember.ROLE_PM)
        qs = qs.filter(Q(manager=request.user) | Q(Exists(pm)))
    with_costs = LedgerEntry.objects.filter(project=OuterRef('pk'))
    return qs.filter(Exists(with_costs))


def items(request):
    if request.membership is None or not is_internal(request):
        return []
    out = []
    for project in _alert_projects(request):
        report = cost_report(project)
        if not report['below']:
            continue
        pct = report['margin_pct']
        if pct is None:
            detail = 'No contract sum yet, so the margin can\'t be worked out'
            status = 'No contract sum'
        else:
            detail = f'Forecast margin {pct}% against a {report["threshold"].normalize():f}% threshold'
            status = f'Margin {pct}%'
        out.append(Item(kind='cost_margin', title=f'Forecast margin below {report["threshold"].normalize():f}%',
                        url=reverse('cost_report', args=[request.org.slug, project.pk]), project=project, ref=project.reference,
                        priority=PRIORITY_HIGH, status_label=status, tone='bad', amount=report['margin'],
                        detail=detail, key=f'cost_margin:{project.pk}', action='Review the cost report'))
    return out
