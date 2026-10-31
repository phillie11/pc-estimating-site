import csv
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db.models import Count, ProtectedError, Q, RestrictedError, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import admin_required, estimating_required, internal_required

from . import analytics, gantt
from .access import (can_see_archived, can_see_variations, get_project, is_bidder, is_internal, posted_id,
                     require_internal, visible_projects)
from .files import serve
from .forms import (ActivityForm, AddStaffForm, CommentForm, HandoverForm, IssueForm, ProjectForm, ProjectInviteForm,
                    TaskForm)
from .models import Activity, Comment, Issue, Project, ProjectInvite, ProjectMember, Task


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def use_section(request, section):
    """Remember which side of the business a staff member is working in, for the sidebar."""
    if is_internal(request) and section in request.membership.sections:
        request.session['section'] = section
    return section


def project_context(request, project, tab, **extra):
    """
    Context every project tab needs: the project, tabs and badge counts.

    Tabs follow the project's section. A bid shows estimating tabs (tender, sub
    quotes, documents). A won or live job shows the projects team's tabs
    (programme, procurement, RFIs, variations, RAMS, snags). Externals see what
    their role allows; a tendering subcontractor sees only their tender.
    """
    from directory.models import Package
    from delivery.access import can_use_site_portal, site_counts
    internal = is_internal(request)
    org, pk = request.org.slug, project.pk
    url = lambda name: reverse(name, args=[org, pk])  # noqa: E731
    section = use_section(request, project.section)
    base = {'project': project, 'tab': tab, 'project_member': getattr(request, 'project_member', None),
            'section': section, **extra}
    from actions.tabs import arrange  # row layout, "More" menu and the external portal's five tabs
    if is_bidder(request):
        return {**base, **arrange(request, project, [('tender', 'Your tender', url('tender'), None)], tab)}

    tabs = [('overview', 'Overview', url('project_detail'), None)]
    if internal and section == 'estimating':
        quotes = Package.objects.filter(project=project, status=Package.QUOTED).count()
        tabs += [('tender', 'Sub tenders', url('tender'), None),
                 ('packages', 'Sub quotes', url('package_list'), quotes),
                 ('documents', 'Documents', url('document_list'), None),
                 ('team', 'Team', url('project_team'), None)]
        return {**base, **arrange(request, project, tabs, tab)}

    from rfis.models import RFI
    rfi_qs = RFI.objects.filter(project=project, status=RFI.STATUS_OPEN)
    task_qs = Task.objects.filter(project=project).exclude(status=Task.STATUS_DONE)
    issue_qs = Issue.objects.filter(project=project).exclude(status=Issue.STATUS_CLOSED)
    if not internal:
        rfi_qs = rfi_qs.filter(visibility=RFI.VIS_SHARED)
        task_qs = task_qs.filter(assignee=request.user)
        issue_qs = issue_qs.filter(assignee=request.user, status=Issue.STATUS_OPEN)
    tabs.append(('programme', 'Programme', url('programme'), None))
    if internal:
        open_pkgs = Package.objects.filter(project=project, status__in=Package.OPEN_STATUSES)
        tabs.append(('packages', 'Packages', url('package_list'), open_pkgs.filter(status=Package.QUOTED).count()))
        if open_pkgs.exists():
            tabs.append(('tender', 'Sub tenders', url('tender'), None))
    tabs.append(('rfis', 'RFIs', url('rfi_list'), rfi_qs.count()))
    if can_see_variations(request):
        from variations.models import Variation
        vo_qs = Variation.objects.filter(project=project, status=Variation.STATUS_SUBMITTED)
        # Staff: one Commercial tab (contract, change register, client variations; contracts/commercial.py)
        tabs.append(('commercial', 'Commercial', url('contract_detail'), vo_qs.count()) if internal else
                    ('variations', 'Variations', url('variation_list'), vo_qs.count()))
    from payments.access import client_tab  # the client's applications for payment, under Decisions
    payments_tab = client_tab(request, project)
    if payments_tab:
        tabs.append(payments_tab)
    if can_use_site_portal(request):
        counts = site_counts(request, project)
        tabs += [('subvariations', 'Sub variations' if internal else 'Variations', url('subvariation_list'), counts['subvariations']),
                 ('rams', 'RAMS', url('rams_list'), counts['rams'])]
    from procurement.access import tab_for  # staff: Procurement; appointed subs and designers: Submittals
    procurement_tab = tab_for(request, project)
    if procurement_tab:
        tabs.append(procurement_tab)
    if internal:
        from diary.access import can_use_diary  # site diary: the PM, the project team, owners and admins
        if can_use_diary(request, project):
            tabs.append(('diary', 'Diary', url('diary_register'), None))
    tabs += [
        ('issues', 'Snags & issues', url('issue_list'), issue_qs.count()),
        ('tasks', 'Tasks', url('task_list'), task_qs.count()),
        ('documents', 'Documents', url('document_list'), None),
        ('team', 'Team', url('project_team'), None),
    ]
    return {**base, **arrange(request, project, tabs, tab)}


def comments_for(request, obj):
    ct = ContentType.objects.get_for_model(obj)
    qs = Comment.objects.filter(content_type=ct, object_id=obj.pk).select_related('author')
    if not is_internal(request):
        qs = qs.filter(internal_only=False)
    return qs


def handle_comment(request, obj, project, label):
    """Process a POSTed comment. Returns True if one was saved."""
    form = CommentForm(request.POST, external=not is_internal(request))
    if form.is_valid():
        c = form.save(commit=False)
        c.content_type = ContentType.objects.get_for_model(obj)
        c.object_id = obj.pk
        c.author = request.user
        c.save()
        target_shared = getattr(obj, 'is_shared', True) and getattr(obj, 'status', None) != 'DRAFT' \
            and type(obj).__name__ not in ('Variation', 'Task', 'Issue', 'SubVariation')
        log_activity(request, 'commented on', label, project=project, url=request.path,
                     internal_only=c.internal_only or not target_shared)
        return True
    return False


def _money(v):
    return '' if v is None else f'{v:.2f}'


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def project_list(request, org_slug, section=None):
    """
    Staff get one list per section: bids (estimating) at /bids/, jobs (projects) at /projects/.
    External people get every project they were invited to.
    """
    internal = is_internal(request)
    q = request.GET.get('q', '').strip()
    qs = visible_projects(request)
    if internal:
        section = section or 'delivery'
        if section not in request.membership.sections:
            if section == 'delivery' and request.membership.can_estimate:
                return redirect('bid_list', org_slug=org_slug)
            raise PermissionDenied
        use_section(request, section)
        if section == 'estimating':
            groups = {'open': Project.PIPELINE_STAGES, 'submitted': (Project.STAGE_SUBMITTED,),
                      'lost': (Project.STAGE_LOST, Project.STAGE_DECLINED), 'all': Project.ESTIMATING_STAGES}
        else:
            groups = {'live': Project.DELIVERY_STAGES, 'complete': (Project.STAGE_COMPLETE,),
                      'all': Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,)}
        if can_see_archived(request):
            groups['archived'] = groups['all']
        view = request.GET.get('view', 'open' if section == 'estimating' else 'live')
        view = view if view in groups else next(iter(groups))
    else:
        groups = {'active': Project.PIPELINE_STAGES + Project.DELIVERY_STAGES, 'live': Project.DELIVERY_STAGES,
                  'closed': Project.CLOSED_STAGES, 'all': tuple(dict(Project.STAGE_CHOICES))}
        view = request.GET.get('view', 'active')
    if view == 'archived' and 'archived' in groups:
        qs = visible_projects(request, include_archived=True).filter(archived_at__isnull=False)
    if view in groups:
        qs = qs.filter(stage__in=groups[view])
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(client_name__icontains=q) | Q(site_address__icontains=q))
    narrowed = []
    if internal:  # the filters each dashboard number links to (actions/filters.py)
        from actions.filters import apply as apply_dashboard_filters
        qs, narrowed = apply_dashboard_filters(request, qs, section)
    if request.GET.get('export') == 'csv' and is_internal(request):
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="projects.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Name', 'Client', 'Stage', 'Sector', 'Value', 'Probability', 'Tender due', 'Start', 'Completion', 'Lead'])
        for p in qs:
            w.writerow([p.reference, p.name, p.client_name, p.get_stage_display(), p.get_sector_display(), _money(p.value),
                        p.probability or '', p.tender_due.strftime('%Y-%m-%d %H:%M') if p.tender_due else '',
                        p.start_date or '', p.completion_date or '', p.owner or ''])
        return resp
    return render(request, 'projects/project_list.html', {'projects': qs, 'view': view, 'q': q, 'groups': groups,
                                                          'list_section': section, 'narrowed': narrowed})


@internal_required
def project_create(request, org_slug):
    org = request.org
    default = Project.STAGE_LEAD if request.membership.can_estimate else Project.STAGE_LIVE
    stage = request.GET.get('stage', default)
    initial = {'stage': stage, 'owner': request.user if stage in Project.ESTIMATING_STAGES else None,
               'manager': None if stage in Project.ESTIMATING_STAGES else request.user}
    form = ProjectForm(request.POST or None, org=org, membership=request.membership, initial=initial)
    if request.method == 'POST' and form.is_valid():
        target = form.cleaned_data['stage']
        # A bid created into Tendering, Submitted, Lost or Declined is saved as a lead first, then goes
        # through its bid/no-bid step; the plan check is on the stage actually saved
        saved = Project.STAGE_LEAD if target in Project.ESTIMATING_STAGES else target
        if saved not in Project.CLOSED_STAGES and not Project.plan_has_room(org):
            messages.error(request, f'Your {org.plan_config["name"]} plan allows {org.plan_config["max_active_projects"]} '
                                    'active projects. Close some or upgrade.')
        else:
            p = form.save(commit=False)
            p.created_by, p.stage = request.user, saved
            p.save()
            log_activity(request, 'created project', p.name, project=p, url=p.get_absolute_url())
            messages.success(request, f'{p.reference} created.')
            return redirect(_bid_step(p, p.stage, target) or p)
    return render(request, 'projects/project_form.html', {'form': form})


def project_detail(request, org_slug, pk):
    project = get_project(request, pk, allow_bidder=True)
    if is_bidder(request):
        return redirect('tender', org_slug=org_slug, pk=pk)
    from documents.access import latest_revisions, visible_documents
    from rfis.models import RFI
    from variations.models import Variation
    internal = is_internal(request)
    rfis = RFI.objects.filter(project=project)
    if not internal:
        rfis = rfis.filter(visibility=RFI.VIS_SHARED)
    stats = {
        'rfis_open': rfis.filter(status=RFI.STATUS_OPEN).count(),
        'rfis_overdue': rfis.filter(status=RFI.STATUS_OPEN, due_date__lt=timezone.localdate()).count(),
        'tasks_open': Task.objects.filter(project=project, **({} if internal else {'assignee': request.user}))
        .exclude(status=Task.STATUS_DONE).count(),
        'issues_open': Issue.objects.filter(project=project, **({} if internal else {'assignee': request.user,
                                                                                      'status': Issue.STATUS_OPEN}))
        .exclude(status=Issue.STATUS_CLOSED).count(),
        'docs': visible_documents(request, project).count(),
    }
    commercial = None
    if internal:
        vos = Variation.objects.filter(project=project)
        approved = vos.filter(status=Variation.STATUS_APPROVED).aggregate(
            s=Sum('approved_value'))['s'] or Decimal('0')
        pending = vos.filter(status=Variation.STATUS_SUBMITTED).aggregate(s=Sum('value'))['s'] or Decimal('0')
        commercial = {
            'contract': project.value or Decimal('0'), 'approved': approved, 'pending': pending,
            'forecast': (project.value or Decimal('0')) + approved,
            'at_risk': vos.filter(proceed_at_risk=True).exclude(
                status__in=[Variation.STATUS_APPROVED, Variation.STATUS_WITHDRAWN]).count(),
        }
    activity = project.activity.select_related('actor')
    if not internal:
        activity = activity.filter(internal_only=False)
    from delivery.access import subcontractor_id
    from delivery.views import subcontract_summary
    subcontracts = None
    if project.section == 'delivery' and (internal or subcontractor_id(request)):
        subcontracts = subcontract_summary(project, None if internal else subcontractor_id(request))
        if commercial is not None:
            commercial['committed'] = sum(r['total'] for r in subcontracts)
    latest = latest_revisions(request, project)
    tender = None
    if internal and project.section == 'estimating':
        from directory.models import Package
        from tenders.models import Addendum, TenderQuery
        pkgs = Package.objects.filter(project=project)
        tender = {'invited': pkgs.count(), 'quoted': pkgs.filter(submitted_at__isnull=False).count(),
                  'trades': pkgs.values('trade').distinct().count(),
                  'queries': TenderQuery.objects.filter(project=project, answered_at__isnull=True).count(),
                  'addenda': Addendum.objects.filter(project=project).count()}
    m = request.membership
    stage_choices = [c for c in Project.STAGE_CHOICES if internal and
                     (m.can_estimate if c[0] in Project.ESTIMATING_STAGES else m.can_deliver)]
    return render(request, 'projects/project_detail.html', project_context(
        request, project, 'overview', stats=stats, commercial=commercial, activity=activity[:12],
        members=project.members.select_related('user')[:12], stage_choices=stage_choices,
        subcontracts=subcontracts, latest_revisions=latest, tender=tender,
    ))


def _close_tenders_if_lost(request, project):
    if project.stage in (Project.STAGE_LOST, Project.STAGE_DECLINED):
        from tenders.services import close_all
        close_all(request, project)


@internal_required
def project_edit(request, org_slug, pk):
    project = get_project(request, pk)
    old_stage = project.stage
    form = ProjectForm(request.POST or None, instance=project, org=request.org, membership=request.membership)
    if request.method == 'POST' and form.is_valid():
        p = form.save(commit=False)
        if _crosses_to_delivery(old_stage, p.stage):
            # A win goes through the handover, which records who takes it on
            p.stage = old_stage
            p.save()
            messages.info(request, 'Saved. Complete the handover to move it to the projects team.')
            return redirect('project_handover', org_slug=org_slug, pk=pk)
        step = _bid_step(p, old_stage, p.stage)
        if step:
            # Bidding, declining or losing goes through its own questions first, like a win
            p.stage = old_stage
            p.save()
            messages.info(request, 'Saved. One more step: answer the questions on this page.')
            return redirect(step)
        p.save()
        if p.stage != old_stage:
            log_activity(request, f'moved to {p.get_stage_display()}', p.name, project=p, url=p.get_absolute_url())
            _close_tenders_if_lost(request, p)
        messages.success(request, 'Project saved.')
        return redirect(p)
    return render(request, 'projects/project_form.html', {'form': form, 'project': project,
                                                          'has_records': project.has_records()})


@require_POST
@admin_required
def project_delete(request, org_slug, pk):
    """Only for a project with nothing on it, such as a lead added by mistake. Anything else is archived."""
    project = get_project(request, pk, allow_archived_write=True)
    if project.has_records():
        messages.error(request, f'{project.reference} has records on it, so it can only be archived.')
        return redirect('project_edit', org_slug=org_slug, pk=pk)
    if request.POST.get('confirm') != project.reference:
        messages.error(request, f'Type {project.reference} to confirm deletion.')
        return redirect('project_edit', org_slug=org_slug, pk=pk)
    name = project.name
    try:
        project.delete()
    except (ProtectedError, RestrictedError):  # a record type not yet in RECORD_RELATIONS: archive instead
        messages.error(request, f'{project.reference} has records on it, so it can only be archived.')
        return redirect('project_edit', org_slug=org_slug, pk=pk)
    log_activity(request, 'deleted project', name)
    messages.success(request, f'{name} deleted.')
    return redirect('project_list', org_slug=org_slug)


@require_POST
@admin_required
def project_archive(request, org_slug, pk):
    """
    Take a project out of every list, the pipeline, workload and plan limits without
    losing its RFIs, variations, RAMS, documents, audit trail or download log.
    Collaborators lose access; owners and admins can still open it, read only, and restore it.
    """
    project = get_project(request, pk, allow_archived_write=True)
    if request.POST.get('action') == 'restore':
        if not project.is_archived:
            return redirect(project)
        if project.stage not in Project.CLOSED_STAGES and not Project.plan_has_room(request.org):
            messages.error(request, f'Your {request.org.plan_config["name"]} plan allows '
                                    f'{request.org.plan_config["max_active_projects"]} active projects. '
                                    'Close some or upgrade before restoring this one.')
            return redirect(project)
        project.restore()
        log_activity(request, 'restored project', project.name, project=project, url=project.get_absolute_url())
        messages.success(request, f'{project.reference} restored.')
        return redirect(project)
    if not project.is_archived:
        project.archive(request.user)
        log_activity(request, 'archived project', project.name, project=project, url=project.get_absolute_url())
        messages.success(request, f'{project.reference} archived. Nothing was deleted; find it under Archived.')
    return redirect(f'{reverse("bid_list" if project.section == "estimating" else "project_list", args=[org_slug])}'
                    '?view=archived')


# ---------------------------------------------------------------------------
# Bid pipeline board
# ---------------------------------------------------------------------------

@estimating_required
def pipeline(request, org_slug):
    qs = visible_projects(request)
    columns = []
    for code in Project.BOARD_STAGES:
        items = qs.filter(stage=code).order_by('tender_due', '-updated_at')
        total = items.aggregate(s=Sum('value'))['s'] or Decimal('0')
        columns.append({'code': code, 'label': dict(Project.STAGE_CHOICES)[code], 'items': items, 'total': total})
    recent_closed = qs.filter(stage__in=[Project.STAGE_LOST, Project.STAGE_DECLINED]).order_by('-decided_at')[:10]
    move_choices = [(c, dict(Project.STAGE_CHOICES)[c]) for c in Project.BOARD_STAGES]
    return render(request, 'projects/pipeline.html', {'columns': columns, 'recent_closed': recent_closed,
                                                      'stage_choices': Project.STAGE_CHOICES, 'move_choices': move_choices,
                                                      'section': use_section(request, 'estimating')})


@require_POST
@internal_required
def project_stage(request, org_slug, pk):
    """
    Change a project's stage: JSON from a pipeline drag, or a plain form from the project
    page's stage select or a pipeline card's "Move to" control (touch and keyboard users).
    """
    project = get_project(request, pk)
    is_json = request.content_type == 'application/json'
    try:
        stage = json.loads(request.body or '{}').get('stage') if is_json else request.POST.get('stage')
    except json.JSONDecodeError:
        stage = None
    back = redirect('pipeline', org_slug=org_slug) if request.POST.get('next') == 'pipeline' else redirect(project)

    def refuse(error, status):
        if is_json:
            return JsonResponse({'ok': False, 'error': error}, status=status)
        messages.error(request, error)
        return back

    if stage not in dict(Project.STAGE_CHOICES):
        return refuse('Unknown stage', 400)
    if project.stage in Project.CLOSED_STAGES and stage not in Project.CLOSED_STAGES and not Project.plan_has_room(request.org):
        return refuse('Active project limit reached for your plan', 402)
    if _crosses_to_delivery(project.stage, stage):
        # Winning is estimating's call, made through the handover (which checks who may do it)
        url = reverse('project_handover', args=[org_slug, pk])
        if is_json:
            return JsonResponse({'ok': True, 'redirect': url})
        return redirect(url)
    allowed = request.membership.can_estimate if stage in Project.ESTIMATING_STAGES else request.membership.can_deliver
    if not allowed:
        return refuse('That stage belongs to the other team.', 403)
    step = _bid_step(project, project.stage, stage)
    if step:
        return JsonResponse({'ok': True, 'redirect': step}) if is_json else redirect(step)
    if project.stage != stage:
        project.stage = stage
        project.save()
        log_activity(request, f'moved to {project.get_stage_display()}', project.name, project=project,
                     url=project.get_absolute_url())
        _close_tenders_if_lost(request, project)
        if not is_json and request.POST.get('next') == 'pipeline':
            messages.success(request, f'{project.name} moved to {project.get_stage_display()}.')
    if is_json:
        return JsonResponse({'ok': True, 'stage': stage})
    return back


def _crosses_to_delivery(old, new):
    return old in Project.ESTIMATING_STAGES and new not in Project.ESTIMATING_STAGES


def _bid_step(project, old, new):
    """The bid/no-bid gate, decline reason or loss review a move must go through first (tenders.gate), or None."""
    from tenders.gate import intercept
    return intercept(project, old, new)


@internal_required
def project_handover(request, org_slug, pk):
    """
    The join between the two halves of the business. Estimating marks a bid won and
    hands it to a project manager with the contract sum, dates and what they need
    to know; a projects user can also assign a manager to a job that has none.
    """
    project = get_project(request, pk)
    winning = project.section == 'estimating'
    if (winning and not request.membership.can_estimate) or (not winning and not request.membership.can_deliver):
        raise PermissionDenied
    if not winning and not project.needs_handover:
        return redirect(project)
    form = HandoverForm(request.POST or None, instance=project, org=request.org)
    if request.method == 'POST' and form.is_valid():
        if winning and project.stage in Project.CLOSED_STAGES and not Project.plan_has_room(request.org):
            # A lost or declined bid doesn't count against the plan; winning it would
            messages.error(request, f'Your {request.org.plan_config["name"]} plan allows '
                                    f'{request.org.plan_config["max_active_projects"]} active projects. '
                                    'Close some or upgrade before winning this one.')
            return redirect(project)
        p = form.save(commit=False)
        direct = winning and project.stage == Project.STAGE_LEAD
        if winning:
            p.stage = Project.STAGE_WON
        p.handed_over_at, p.handed_over_by = timezone.now(), request.user
        p.save()
        if direct:  # won straight from a lead: outside the bid/no-bid gate, but recorded for analytics
            from tenders.gate import record_direct_award
            record_direct_award(p, request.user)
        notify.alert([p.manager], f'Handed over to you: {p.name}', p.get_absolute_url(), project=p, actor=request.user,
                     org=request.org)
        log_activity(request, 'handed over to', f'{p.manager.full_name}: {p.name}', project=p, url=p.get_absolute_url())
        messages.success(request, f'{p.name} handed over to {p.manager.full_name}.')
        if request.membership.can_deliver:
            return redirect(p)
        return redirect('bid_list', org_slug=org_slug)
    from directory.models import Package
    quotes = list(Package.objects.filter(project=project, status__in=Package.OPEN_STATUSES + (Package.APPOINTED,))
                  .select_related('subcontractor', 'trade'))
    from levelling.services import carried_for
    carried = carried_for(project)  # quote levelling: {package id: levelled total} carried in the tender
    for q in quotes:
        q.carried, q.levelled = q.pk in carried, carried.get(q.pk)
    from costs.access import COST_STAGES, runs_job
    from levelling.models import LevellingSheet
    has_levelling = LevellingSheet.objects.filter(project=project).exists() and (
        request.membership.can_estimate or (project.stage in COST_STAGES and runs_job(request.user, project)))
    return render(request, 'projects/handover.html', {'project': project, 'form': form, 'winning': winning,
                                                      'quotes': quotes, 'has_levelling': has_levelling, 'section': use_section(request, 'estimating' if winning else 'delivery')})


@estimating_required
def tender_analytics(request, org_slug):
    # Outcomes span both sections: a won bid now belongs to the projects team but is still a win
    data = analytics.tender_analytics(Project.objects.filter(archived_at__isnull=True), request.GET.get('period', '12'),
                                      today=timezone.localdate())
    return render(request, 'projects/analytics.html', {**data, 'periods': analytics.PERIODS,
                                                       'section': use_section(request, 'estimating')})


# ---------------------------------------------------------------------------
# Programmes (Gantt)
# ---------------------------------------------------------------------------

@internal_required
def workload(request, org_slug):
    """Gantt of the company's workload: open tenders (estimating), or secured jobs on site (projects)."""
    m = request.membership
    default = 'tenders' if request.session.get('section') != 'delivery' and m.can_estimate else 'secured'
    view = request.GET.get('view', default)
    if view == 'secured' and not m.can_deliver:
        view = 'tenders'
    if view != 'secured' and not m.can_estimate:
        view = 'secured'
    use_section(request, 'delivery' if view == 'secured' else 'estimating')
    today = timezone.localdate()
    projects = visible_projects(request)
    leads = None
    if view == 'secured':
        show = request.GET.get('show', 'live')
        stages = Project.DELIVERY_STAGES + ((Project.STAGE_COMPLETE,) if show == 'all' else ())
        projects = projects.filter(stage__in=stages)
        undated = [p for p in projects if not (p.start_date and p.completion_date)]
        chart = gantt.build(projects, start=lambda p: p.start_date, finish=lambda p: p.completion_date, today=today)
        sort_key = lambda r: (r['item'].start_date, r['item'].completion_date)
    else:
        view, show = 'tenders', None
        projects = projects.filter(stage__in=Project.PIPELINE_STAGES)
        leads = sorted({p.owner for p in projects if p.owner}, key=lambda u: u.full_name)
        lead = request.GET.get('lead')
        if lead and lead.isdigit():
            projects = projects.filter(owner_id=int(lead))
        undated = [p for p in projects if not p.tender_due]
        chart = gantt.build(projects, start=lambda p: p.tender_start, finish=lambda p: p.tender_return_date,
                            marks=lambda p: p.tender_marks(), today=today)
        sort_key = lambda r: (r['item'].tender_due, r['item'].name)
    if chart:
        chart['rows'].sort(key=sort_key)
    return render(request, 'projects/workload.html', {
        'chart': chart, 'undated': undated, 'view': view, 'show': show, 'leads': leads,
        'section': 'delivery' if view == 'secured' else 'estimating',
        'lead': request.GET.get('lead', ''),
    })


def _visible_activities(request, project):
    qs = Activity.objects.filter(project=project).select_related('after')
    return qs if is_internal(request) else qs.filter(is_shared=True)


def programme(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    form = ActivityForm(project=project, initial={'start': timezone.localdate()}) if internal else None
    if request.method == 'POST':
        require_internal(request)
        form = ActivityForm(request.POST, project=project)
        if form.is_valid():
            a = form.save(commit=False)
            a.project, a.created_by = project, request.user
            a.save()
            log_activity(request, 'added to the programme', a.name, project=project,
                         url=reverse('programme', args=[org_slug, pk]), internal_only=not a.is_shared)
            messages.success(request, f'Added {a.name}.')
            return redirect('programme', org_slug=org_slug, pk=pk)
    activities = list(_visible_activities(request, project))
    chart = gantt.build(activities, today=timezone.localdate())
    summary = None
    if activities:
        summary = {
            'start': min(a.start for a in activities),
            'finish': max(a.finish for a in activities),
            'late': sum(1 for a in activities if a.is_late),
            'done': sum(1 for a in activities if a.is_complete),
            'count': len(activities),
        }
        if project.completion_date:
            summary['slip_days'] = (summary['finish'] - project.completion_date).days
    return render(request, 'projects/programme.html', project_context(
        request, project, 'programme', chart=chart, form=form, activities=activities, summary=summary))


def activity_edit(request, org_slug, pk, activity_id):
    project = get_project(request, pk)
    require_internal(request)
    activity = get_object_or_404(Activity, project=project, pk=activity_id)
    if request.method == 'POST' and request.POST.get('delete'):
        activity.delete()
        messages.success(request, f'Removed {activity.name}.')
        return redirect('programme', org_slug=org_slug, pk=pk)
    form = ActivityForm(request.POST or None, instance=activity, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('programme', org_slug=org_slug, pk=pk)
    return render(request, 'projects/activity_form.html', project_context(
        request, project, 'programme', form=form, activity=activity))


# ---------------------------------------------------------------------------
# Team & external portal invites
# ---------------------------------------------------------------------------

def project_team(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    members = project.members.select_related('user', 'subcontractor')
    invite_form = ProjectInviteForm(project=project)
    staff_form = AddStaffForm(project=project) if internal else None
    if request.method == 'POST':
        require_internal(request)
        action = request.POST.get('action')
        if action == 'invite':
            invite_form = ProjectInviteForm(request.POST, project=project)
            if invite_form.is_valid():
                inv = invite_form.save(commit=False)
                inv.project, inv.invited_by = project, request.user
                inv.save()
                send_project_invite(request, inv)
                messages.success(request, f'Portal invite sent to {inv.email}.')
                return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'add_staff':
            staff_form = AddStaffForm(request.POST, project=project)
            if staff_form.is_valid():
                ProjectMember.objects.create(project=project, user=staff_form.cleaned_data['user'],
                                             role=staff_form.cleaned_data['role'], added_by=request.user)
                messages.success(request, 'Added to the project team.')
                return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'remove':
            m = get_object_or_404(ProjectMember, project=project, pk=posted_id(request, 'member'))
            m.delete()
            log_activity(request, 'removed from project', m.user.full_name, project=project)
            messages.success(request, f'{m.user.full_name} removed from the project.')
            return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'revoke':
            inv = get_object_or_404(ProjectInvite, project=project, pk=posted_id(request, 'invite'))
            inv.status = ProjectInvite.STATUS_REVOKED
            inv.save(update_fields=['status'])
            messages.success(request, 'Invite revoked.')
            return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'resend':
            inv = get_object_or_404(ProjectInvite, project=project, pk=posted_id(request, 'invite'),
                                    status=ProjectInvite.STATUS_PENDING)
            send_project_invite(request, inv)
            messages.success(request, f'Invite re-sent to {inv.email}.')
            return redirect('project_team', org_slug=org_slug, pk=pk)
    invites = project.invites.filter(status=ProjectInvite.STATUS_PENDING) if internal else []
    return render(request, 'projects/team.html', project_context(
        request, project, 'team', members=members, invites=invites, invite_form=invite_form, staff_form=staff_form))


def send_project_invite(request, inv):
    link = notify.absolute(reverse('accept_project_invite', args=[inv.token]))
    notify.send(inv.email, f'{request.user.full_name} invited you to {inv.project.name}', 'project_invite', {
        'inviter': request.user, 'project': inv.project, 'invite': inv, 'link': link,
    }, org=request.org)
    log_activity(request, f'invited {inv.get_role_display().lower()}', inv.email, project=inv.project)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def task_list(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    tasks = Task.objects.filter(project=project).select_related('assignee')
    if not internal:
        tasks = tasks.filter(assignee=request.user)
    form = TaskForm(project=project) if internal else None
    if request.method == 'POST':
        require_internal(request)
        form = TaskForm(request.POST, project=project)
        if form.is_valid():
            t = form.save(commit=False)
            t.project, t.created_by = project, request.user
            t.save()
            notify_assignee(request, t.assignee, f'Task: {t.title}', project,
                            reverse('task_list', args=[org_slug, pk]) + f'#task-{t.pk}')
            log_activity(request, 'added task', t.title, project=project)
            return redirect('task_list', org_slug=org_slug, pk=pk)
    show = request.GET.get('show', 'open')
    if show == 'open':
        tasks = tasks.exclude(status=Task.STATUS_DONE)
    return render(request, 'projects/tasks.html', project_context(request, project, 'tasks', tasks=tasks,
                                                                  form=form, show=show))


@require_POST
def task_update(request, org_slug, pk, task_id):
    project = get_project(request, pk)
    task = get_object_or_404(Task, project=project, pk=task_id)
    if not is_internal(request) and task.assignee_id != request.user.pk:
        raise PermissionDenied
    status = request.POST.get('status')
    if status in dict(Task.STATUS_CHOICES):
        task.status = status
        task.completed_at = timezone.now() if status == Task.STATUS_DONE else None
        task.save()
        if status == Task.STATUS_DONE:
            log_activity(request, 'completed task', task.title, project=project)
    if request.POST.get('delete') and is_internal(request):
        task.delete()
    nxt = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = reverse('task_list', args=[org_slug, pk])
    return redirect(nxt)


def task_edit(request, org_slug, pk, task_id):
    project = get_project(request, pk)
    require_internal(request)
    task = get_object_or_404(Task, project=project, pk=task_id)
    form = TaskForm(request.POST or None, instance=task, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('task_list', org_slug=org_slug, pk=pk)
    return render(request, 'projects/task_form.html', project_context(request, project, 'tasks', form=form, task=task))


def notify_assignee(request, user, subject, project, path):
    notify.alert([user], subject, path, project=project, actor=request.user, org=request.org)


# ---------------------------------------------------------------------------
# Issues / snags
# ---------------------------------------------------------------------------

def issue_list(request, org_slug, pk):
    project = get_project(request, pk)
    issues = Issue.objects.filter(project=project).select_related('assignee')
    if not is_internal(request):
        issues = issues.filter(assignee=request.user)
    status = request.GET.get('status', 'open')
    if status == 'open':
        issues = issues.exclude(status=Issue.STATUS_CLOSED)
    elif status in dict(Issue.STATUS_CHOICES):
        issues = issues.filter(status=status)
    return render(request, 'projects/issues.html', project_context(request, project, 'issues', issues=issues,
                                                                   status=status))


def issue_create(request, org_slug, pk):
    project = get_project(request, pk)
    require_internal(request)
    from viewer.pins import attach_pending, pending_pin, viewer_url
    pending = pending_pin(request, project)  # opened from the drawing viewer to pin a new snag
    form = IssueForm(request.POST or None, request.FILES or None, project=project,
                     initial={'location': pending.location} if pending else None)
    if request.method == 'POST' and form.is_valid():
        i = form.save(commit=False)
        i.project, i.created_by = project, request.user
        i.save()
        path = reverse('issue_detail', args=[org_slug, pk, i.pk])
        notify_assignee(request, i.assignee, f'{i.reference}: {i.title}', project, path)
        log_activity(request, 'raised', f'{i.reference} {i.title}', project=project, url=path)
        if pending:
            pin = attach_pending(request, project, pending, issue=i)
            messages.success(request, f'{i.reference} raised and pinned to {pending.label}.')
            return redirect(viewer_url(request, project, pending.doc, pending.rev, pin))
        messages.success(request, f'{i.reference} raised.')
        return redirect(path)
    return render(request, 'projects/issue_form.html', project_context(request, project, 'issues', form=form,
                                                                       pending_pin=pending))


def issue_detail(request, org_slug, pk, issue_id):
    project = get_project(request, pk)
    issue = get_object_or_404(Issue.objects.select_related('assignee', 'created_by'), project=project, pk=issue_id)
    internal = is_internal(request)
    if not internal and issue.assignee_id != request.user.pk:
        raise PermissionDenied
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'comment':
            handle_comment(request, issue, project, issue.reference)
        elif action == 'ready':
            issue.status = Issue.STATUS_READY
            issue.save()
            log_activity(request, 'marked ready for inspection', issue.reference, project=project, url=request.path)
            if issue.created_by:
                notify_assignee(request, issue.created_by, f'{issue.reference} ready for inspection', project, request.path)
        elif action in ('close', 'reopen') and internal:
            issue.status = Issue.STATUS_CLOSED if action == 'close' else Issue.STATUS_OPEN
            issue.closed_at = timezone.now() if action == 'close' else None
            issue.save()
            log_activity(request, 'closed' if action == 'close' else 'reopened', issue.reference, project=project,
                         url=request.path)
        return redirect(request.path)
    return render(request, 'projects/issue_detail.html', project_context(
        request, project, 'issues', issue=issue, comments=comments_for(request, issue),
        comment_form=CommentForm(external=not internal)))


def issue_edit(request, org_slug, pk, issue_id):
    project = get_project(request, pk)
    require_internal(request)
    issue = get_object_or_404(Issue, project=project, pk=issue_id)
    form = IssueForm(request.POST or None, request.FILES or None, instance=issue, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('issue_detail', org_slug=org_slug, pk=pk, issue_id=issue.pk)
    return render(request, 'projects/issue_form.html', project_context(request, project, 'issues', form=form, issue=issue))


def issue_photo(request, org_slug, pk, issue_id):
    project = get_project(request, pk)
    issue = get_object_or_404(Issue, project=project, pk=issue_id)
    if not is_internal(request) and issue.assignee_id != request.user.pk:
        raise PermissionDenied
    return serve(request, issue.photo, project=project, label=f'{issue.reference} photo')


# Documents: the register, transmittals and bulk upload live in documents/views.py
