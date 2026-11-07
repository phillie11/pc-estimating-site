"""
Needs me: handover pack open items given to this person (a HookProvider, registered in apps.py, staff only).

One query behind the badge on every page. The same rule as the pack's pages (handoverpack/access.py), in
SQL: owners and admins on any project they can open; on a bid, staff who work in estimating; on a won,
live or complete job, its project manager or a PM or QS on its team. Items never name a figure: the title
says what to sort out, and the money stays on the pack's pages.
"""
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item


def items(request):
    from costs.access import COST_STAGES
    from projects.access import is_internal, visible_projects
    from projects.models import Project, ProjectMember

    from .models import OpenItem
    m = request.membership
    if m is None or not is_internal(request):
        return []
    qs = OpenItem.objects.filter(owner=request.user, status=OpenItem.OPEN,
                                 project__in=visible_projects(request).values('pk'))
    if not m.is_admin:
        runs = ProjectMember.objects.filter(project=OuterRef('project'), user=request.user,
                                            role__in=(ProjectMember.ROLE_PM, ProjectMember.ROLE_QS))
        allowed = Q(project__stage__in=COST_STAGES) & (Q(project__manager=request.user) | Q(Exists(runs)))
        if m.can_estimate:
            allowed |= Q(project__stage__in=Project.ESTIMATING_STAGES)
        qs = qs.filter(allowed)
    out = []
    for i in qs.select_related('project').order_by('due_date', 'pk')[:200]:
        url = reverse('hpack_items', args=[request.org.slug, i.project_id]) + f'#item-{i.pk}'
        out.append(Item(kind='handover_item', title=i.title, url=url, ref='', project=i.project, due=i.due_date,
                        priority=PRIORITY_HIGH if i.severity == 'bad' else PRIORITY_NORMAL,
                        status_label='Handover open item', tone='bad' if i.severity == 'bad' else 'warn',
                        detail=i.trade, key=f'hpack:{i.pk}'))
    return out
