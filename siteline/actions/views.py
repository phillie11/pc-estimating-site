from datetime import timedelta
from decimal import Decimal

from django.shortcuts import render
from django.utils import timezone

from projects.access import get_project, is_internal, visible_projects
from tenancy.permissions import internal_required

from . import registry


def _filters(request, items):
    kind = request.GET.get('kind', '')
    project = request.GET.get('project', '')
    kinds_here = {}
    for i in items:
        kinds_here.setdefault(i.kind, i.kind_label)
    projects_here = {}
    for i in items:
        if i.project is not None:
            projects_here.setdefault(i.project.pk, i.project)
    if kind:
        items = [i for i in items if i.kind == kind]
    if project.isdigit():
        items = [i for i in items if getattr(i.project, 'pk', None) == int(project)]
    return items, kind, project, kinds_here, projects_here


def needs_me(request, org_slug):
    """Everything waiting on this person, across every project and register, soonest first."""
    every = registry.for_user(request)
    items, kind, project, kinds_here, projects_here = _filters(request, every)
    return render(request, 'actions/needs_me.html', {
        'groups': registry.grouped(items), 'items': items, 'total_count': len(every),
        'kind': kind, 'project_filter': project, 'kinds_here': kinds_here, 'projects_here': projects_here.values(),
        'money': registry.total(items), 'show_money': not is_internal(request) or bool(kind),
    })


def project_decisions(request, org_slug, pk):
    """A project's "Decisions" tab: what is waiting on this person here, with the registers underneath."""
    from projects.views import project_context
    project = get_project(request, pk)
    items = registry.for_user(request, project=project)
    return render(request, 'actions/decisions.html', project_context(
        request, project, 'decisions', items=items, groups=registry.grouped(items), money=registry.total(items)))


@internal_required
def ui_kit(request, org_slug):
    """Staff-only page showing the shared template pieces, so new screens reuse them."""
    from projects.models import Issue
    from variations.models import Variation
    today = timezone.localdate()
    project = visible_projects(request).first()
    samples = [
        registry.Item(kind='client_variation', kind_label='Variations to approve', title='Extra power to bar back',
                      ref='VO-004', project=project, url='#', action='Approve or reject', status_label='Submitted to client',
                      tone='accent', amount=Decimal('1850.00'), detail='3 days time impact'),
        registry.Item(kind='record', kind_label='RFIs', title='Which veneer for the bar front?', ref='RFI-012',
                      project=project, url='#', due=today - timedelta(days=2), action='Answer it', status_label='Open'),
        registry.Item(kind='snag', kind_label='Snags to fix', title='Chipped skirting, unit 3', ref='IS-007',
                      project=project, url='#', due=today, action='Fix it, then mark it ready for inspection',
                      status_label='Open', priority=registry.PRIORITY_HIGH),
        registry.Item(kind='task', kind_label='Tasks', title='Send O&M manuals', project=project, url='#',
                      due=today + timedelta(days=9), action='Do it and mark it done', status_label='To do', tone='muted'),
    ]
    tones = ['good', 'warn', 'bad', 'info', 'accent', 'muted']
    statuses = [Variation(status=s) for s, _ in Variation.STATUS_CHOICES] + \
               [Issue(status=s) for s, _ in Issue.STATUS_CHOICES]
    return render(request, 'actions/ui_kit.html', {
        'samples': samples, 'groups': registry.grouped(samples), 'tones': tones, 'statuses': statuses,
        'money': registry.total(samples),
    })
