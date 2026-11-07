"""
Needs me: handover pack open items given to this person (a HookProvider, registered in apps.py, staff only).

It runs behind the badge on every page and adds no query of its own: its figures are subqueries on the
shared actions.providers.my_jobs query (job_annotations below, through its JOB_ANNOTATIONS hook), which
lists the won, live and complete jobs this person can open. That is where open items get their owners and
due dates (at the handover), so that is where Needs me shows them; on a bid they are on the pack's Open items
page. The rule is the pack's own (handoverpack/access.py): owners and admins, or the job's project manager or
a PM or QS on its team. One line per job; several items link to the job's open items. Items never name a
figure: the money stays on the pack's pages.
"""
from django.db.models import Count, IntegerField, Min, OuterRef, Subquery
from django.urls import reverse

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item


def job_annotations(request):
    from projects.access import is_internal

    from .models import OpenItem
    if request.membership is None or not is_internal(request):
        return {}
    mine = OpenItem.objects.filter(project=OuterRef('pk'), owner=request.user.pk, status=OpenItem.OPEN)
    first = mine.order_by('due_date', 'pk')
    return {
        'hp_open_n': Subquery(mine.order_by().values('project').annotate(n=Count('pk')).values('n'),
                              output_field=IntegerField()),
        'hp_open_bad': Subquery(mine.filter(severity='bad').order_by().values('project').annotate(n=Count('pk'))
                                .values('n'), output_field=IntegerField()),
        'hp_open_due': Subquery(mine.order_by().values('project').annotate(d=Min('due_date')).values('d')),
        'hp_open_pk': Subquery(first.values('pk')[:1]),
        'hp_open_title': Subquery(first.values('title')[:1]),
    }


def items(request):
    from actions.providers import my_jobs
    from projects.access import is_internal
    if request.membership is None or not is_internal(request):
        return []
    admin, me, slug, out = request.membership.is_admin, request.user.pk, request.org.slug, []
    for p in my_jobs(request):
        n = getattr(p, 'hp_open_n', None)
        if not n or not (admin or p.manager_id == me or p.on_team_pm or p.on_team_qs):
            continue
        url = reverse('hpack_items', args=[slug, p.pk]) + (f'#item-{p.hp_open_pk}' if n == 1 else '')
        out.append(Item(kind='handover_item', title=p.hp_open_title if n == 1 else f'{n} handover pack open items',
                        url=url, project=p, due=p.hp_open_due,
                        priority=PRIORITY_HIGH if p.hp_open_bad else PRIORITY_NORMAL,
                        status_label='Handover open item', tone='bad' if p.hp_open_bad else 'warn',
                        key=f'hpack:{p.pk}'))
    return out
