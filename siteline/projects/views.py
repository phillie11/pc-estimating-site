import csv
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import admin_required, internal_required

from . import analytics, gantt
from .access import can_see_variations, get_project, is_internal, require_internal, visible_projects
from .files import serve
from .forms import (ActivityForm, AddStaffForm, CommentForm, DocumentForm, IssueForm, ProjectForm, ProjectInviteForm,
                    RevisionForm, TaskForm)
from .models import Activity, Comment, Document, DocumentRevision, Issue, Project, ProjectInvite, ProjectMember, Task


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def project_context(request, project, tab, **extra):
    """Context every project tab needs: the project, tabs and badge counts."""
    internal = is_internal(request)
    tabs = [('overview', 'Overview', reverse('project_detail', args=[request.org.slug, project.pk]), None)]
    from rfis.models import RFI
    rfi_qs = RFI.objects.filter(project=project, status=RFI.STATUS_OPEN)
    task_qs = Task.objects.filter(project=project).exclude(status=Task.STATUS_DONE)
    issue_qs = Issue.objects.filter(project=project).exclude(status=Issue.STATUS_CLOSED)
    if not internal:
        rfi_qs = rfi_qs.filter(is_shared=True)
        task_qs = task_qs.filter(assignee=request.user)
        issue_qs = issue_qs.filter(assignee=request.user)
    tabs += [
        ('programme', 'Programme', reverse('programme', args=[request.org.slug, project.pk]), None),
        ('tasks', 'Tasks', reverse('task_list', args=[request.org.slug, project.pk]), task_qs.count()),
        ('issues', 'Snags & issues', reverse('issue_list', args=[request.org.slug, project.pk]), issue_qs.count()),
        ('documents', 'Documents', reverse('document_list', args=[request.org.slug, project.pk]), None),
        ('rfis', 'RFIs', reverse('rfi_list', args=[request.org.slug, project.pk]), rfi_qs.count()),
    ]
    if can_see_variations(request):
        from variations.models import Variation
        vo_qs = Variation.objects.filter(project=project, status=Variation.STATUS_SUBMITTED)
        tabs.append(('variations', 'Variations', reverse('variation_list', args=[request.org.slug, project.pk]), vo_qs.count()))
    if internal:
        tabs.append(('packages', 'Packages', reverse('package_list', args=[request.org.slug, project.pk]), None))
    tabs.append(('team', 'Team', reverse('project_team', args=[request.org.slug, project.pk]), None))
    return {'project': project, 'tab': tab, 'tabs': tabs, 'project_member': getattr(request, 'project_member', None),
            **extra}


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
            and type(obj).__name__ not in ('Variation', 'Task', 'Issue')
        log_activity(request, 'commented on', label, project=project, url=request.path,
                     internal_only=c.internal_only or not target_shared)
        return True
    return False


def _money(v):
    return '' if v is None else f'{v:.2f}'


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def project_list(request, org_slug):
    view = request.GET.get('view', 'active')
    q = request.GET.get('q', '').strip()
    qs = visible_projects(request)
    groups = {
        'active': Project.PIPELINE_STAGES + Project.DELIVERY_STAGES,
        'pipeline': Project.PIPELINE_STAGES,
        'live': Project.DELIVERY_STAGES,
        'closed': Project.CLOSED_STAGES,
    }
    if view in groups:
        qs = qs.filter(stage__in=groups[view])
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(client_name__icontains=q) | Q(site_address__icontains=q))
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
    return render(request, 'projects/project_list.html', {'projects': qs, 'view': view, 'q': q})


@internal_required
def project_create(request, org_slug):
    org = request.org
    initial = {'stage': request.GET.get('stage', Project.STAGE_LEAD), 'owner': request.user}
    form = ProjectForm(request.POST or None, org=org, initial=initial)
    if request.method == 'POST' and form.is_valid():
        if form.cleaned_data['stage'] not in Project.CLOSED_STAGES and not org.can_add_project():
            messages.error(request, f'Your {org.plan_config["name"]} plan allows {org.plan_config["max_active_projects"]} '
                                    'active projects. Close some or upgrade.')
        else:
            p = form.save(commit=False)
            p.created_by = request.user
            p.save()
            log_activity(request, 'created project', p.name, project=p, url=p.get_absolute_url())
            messages.success(request, f'{p.reference} created.')
            return redirect(p)
    return render(request, 'projects/project_form.html', {'form': form})


def project_detail(request, org_slug, pk):
    project = get_project(request, pk)
    from rfis.models import RFI
    from variations.models import Variation
    internal = is_internal(request)
    rfis = RFI.objects.filter(project=project)
    if not internal:
        rfis = rfis.filter(is_shared=True)
    stats = {
        'rfis_open': rfis.filter(status=RFI.STATUS_OPEN).count(),
        'rfis_overdue': rfis.filter(status=RFI.STATUS_OPEN, due_date__lt=timezone.localdate()).count(),
        'tasks_open': Task.objects.filter(project=project, **({} if internal else {'assignee': request.user}))
        .exclude(status=Task.STATUS_DONE).count(),
        'issues_open': Issue.objects.filter(project=project, **({} if internal else {'assignee': request.user}))
        .exclude(status=Issue.STATUS_CLOSED).count(),
        'docs': Document.objects.filter(project=project).count() if internal
        else Document.objects.filter(project=project, visibility=Document.VIS_SHARED).count(),
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
    return render(request, 'projects/project_detail.html', project_context(
        request, project, 'overview', stats=stats, commercial=commercial, activity=activity[:12],
        members=project.members.select_related('user')[:12],
        stage_choices=Project.STAGE_CHOICES,
    ))


@internal_required
def project_edit(request, org_slug, pk):
    project = get_project(request, pk)
    old_stage = project.stage
    form = ProjectForm(request.POST or None, instance=project, org=request.org)
    if request.method == 'POST' and form.is_valid():
        p = form.save()
        if p.stage != old_stage:
            log_activity(request, f'moved to {p.get_stage_display()}', p.name, project=p, url=p.get_absolute_url())
        messages.success(request, 'Project saved.')
        return redirect(p)
    return render(request, 'projects/project_form.html', {'form': form, 'project': project})


@require_POST
@admin_required
def project_delete(request, org_slug, pk):
    project = get_project(request, pk)
    if request.POST.get('confirm') != project.reference:
        messages.error(request, f'Type {project.reference} to confirm deletion.')
        return redirect('project_edit', org_slug=org_slug, pk=pk)
    name = project.name
    project.delete()
    log_activity(request, 'deleted project', name)
    messages.success(request, f'{name} deleted.')
    return redirect('project_list', org_slug=org_slug)


# ---------------------------------------------------------------------------
# Bid pipeline board
# ---------------------------------------------------------------------------

@internal_required
def pipeline(request, org_slug):
    qs = visible_projects(request)
    columns = []
    for code in Project.BOARD_STAGES:
        items = qs.filter(stage=code).order_by('tender_due', '-updated_at')
        total = items.aggregate(s=Sum('value'))['s'] or Decimal('0')
        columns.append({'code': code, 'label': dict(Project.STAGE_CHOICES)[code], 'items': items, 'total': total})
    recent_closed = qs.filter(stage__in=[Project.STAGE_LOST, Project.STAGE_DECLINED]).order_by('-decided_at')[:10]
    return render(request, 'projects/pipeline.html', {'columns': columns, 'recent_closed': recent_closed,
                                                      'stage_choices': Project.STAGE_CHOICES})


@require_POST
@internal_required
def project_stage(request, org_slug, pk):
    project = get_project(request, pk)
    try:
        stage = json.loads(request.body or '{}').get('stage') if request.content_type == 'application/json' \
            else request.POST.get('stage')
    except json.JSONDecodeError:
        stage = None
    if stage not in dict(Project.STAGE_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Unknown stage'}, status=400)
    if project.stage in Project.CLOSED_STAGES and stage not in Project.CLOSED_STAGES and not request.org.can_add_project():
        return JsonResponse({'ok': False, 'error': 'Active project limit reached for your plan'}, status=402)
    if project.stage != stage:
        project.stage = stage
        project.save()
        log_activity(request, f'moved to {project.get_stage_display()}', project.name, project=project,
                     url=project.get_absolute_url())
    if request.content_type == 'application/json':
        return JsonResponse({'ok': True, 'stage': stage})
    return redirect(project)


@internal_required
def tender_analytics(request, org_slug):
    data = analytics.tender_analytics(visible_projects(request), request.GET.get('period', '12'),
                                      today=timezone.localdate())
    return render(request, 'projects/analytics.html', {**data, 'periods': analytics.PERIODS})


# ---------------------------------------------------------------------------
# Programmes (Gantt)
# ---------------------------------------------------------------------------

@internal_required
def programme_overview(request, org_slug):
    """Every won and live job on one chart, from start on site to completion."""
    show = request.GET.get('show', 'live')
    stages = Project.DELIVERY_STAGES + ((Project.STAGE_COMPLETE,) if show == 'all' else ())
    projects = visible_projects(request).filter(stage__in=stages)
    undated = [p for p in projects if not (p.start_date and p.completion_date)]
    chart = gantt.build(projects, start=lambda p: p.start_date, finish=lambda p: p.completion_date,
                        today=timezone.localdate())
    if chart:
        today = timezone.localdate()
        chart['rows'].sort(key=lambda r: (r['item'].start_date, r['item'].completion_date))
        for r in chart['rows']:
            p = r['item']
            length = (p.completion_date - p.start_date).days + 1
            elapsed = min(max((today - p.start_date).days + 1, 0), length)
            r['elapsed'] = f'{elapsed / length * 100:.1f}' if length > 0 else '0'
    return render(request, 'projects/programme_overview.html', {'chart': chart, 'undated': undated, 'show': show})


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
            m = get_object_or_404(ProjectMember, project=project, pk=request.POST.get('member'))
            m.delete()
            log_activity(request, 'removed from project', m.user.full_name, project=project)
            messages.success(request, f'{m.user.full_name} removed from the project.')
            return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'revoke':
            inv = get_object_or_404(ProjectInvite, project=project, pk=request.POST.get('invite'))
            inv.status = ProjectInvite.STATUS_REVOKED
            inv.save(update_fields=['status'])
            messages.success(request, 'Invite revoked.')
            return redirect('project_team', org_slug=org_slug, pk=pk)
        elif action == 'resend':
            inv = get_object_or_404(ProjectInvite, project=project, pk=request.POST.get('invite'),
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
                            reverse('task_list', args=[org_slug, pk]))
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
    if user and user != request.user:
        notify.send(user.email, subject, 'assigned', {
            'actor': request.user, 'project': project, 'subject': subject, 'link': notify.absolute(path),
        }, org=request.org)


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
    form = IssueForm(request.POST or None, request.FILES or None, project=project)
    if request.method == 'POST' and form.is_valid():
        i = form.save(commit=False)
        i.project, i.created_by = project, request.user
        i.save()
        path = reverse('issue_detail', args=[org_slug, pk, i.pk])
        notify_assignee(request, i.assignee, f'{i.reference}: {i.title}', project, path)
        log_activity(request, 'raised', f'{i.reference} {i.title}', project=project, url=path)
        messages.success(request, f'{i.reference} raised.')
        return redirect(path)
    return render(request, 'projects/issue_form.html', project_context(request, project, 'issues', form=form))


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
    return serve(issue.photo)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def _visible_documents(request, project):
    qs = Document.objects.filter(project=project).prefetch_related('revisions')
    if not is_internal(request):
        qs = qs.filter(visibility=Document.VIS_SHARED)
    return qs


def document_list(request, org_slug, pk):
    project = get_project(request, pk)
    docs = _visible_documents(request, project)
    category = request.GET.get('category', '')
    if category in dict(Document.CATEGORY_CHOICES):
        docs = docs.filter(category=category)
    q = request.GET.get('q', '').strip()
    if q:
        docs = docs.filter(Q(title__icontains=q) | Q(reference__icontains=q))
    form = DocumentForm(external=not is_internal(request))
    if request.method == 'POST':
        form = DocumentForm(request.POST, request.FILES, external=not is_internal(request))
        if form.is_valid():
            doc = form.save(commit=False)
            doc.project, doc.created_by = project, request.user
            if not is_internal(request):
                doc.visibility = Document.VIS_SHARED
            doc.save()
            f = form.cleaned_data['file']
            DocumentRevision.objects.create(document=doc, file=f, original_name=f.name[:200], size=f.size,
                                            revision=form.cleaned_data['revision'], notes=form.cleaned_data['notes'],
                                            uploaded_by=request.user)
            log_activity(request, 'uploaded', f'{doc.title} rev {form.cleaned_data["revision"]}', project=project,
                         url=reverse('document_detail', args=[org_slug, pk, doc.pk]),
                         internal_only=doc.visibility == Document.VIS_INTERNAL)
            messages.success(request, f'{doc.title} uploaded.')
            return redirect('document_list', org_slug=org_slug, pk=pk)
    return render(request, 'projects/documents.html', project_context(
        request, project, 'documents', documents=docs, form=form, category=category, q=q,
        categories=Document.CATEGORY_CHOICES))


def document_detail(request, org_slug, pk, doc_id):
    project = get_project(request, pk)
    doc = get_object_or_404(_visible_documents(request, project), pk=doc_id)
    form = RevisionForm()
    if request.method == 'POST':
        if request.POST.get('action') == 'visibility' and is_internal(request):
            doc.visibility = Document.VIS_SHARED if doc.visibility == Document.VIS_INTERNAL else Document.VIS_INTERNAL
            doc.save(update_fields=['visibility', 'updated_at'])
            return redirect(request.path)
        if request.POST.get('action') == 'delete' and is_internal(request):
            doc.delete()
            messages.success(request, 'Document deleted.')
            return redirect('document_list', org_slug=org_slug, pk=pk)
        if not is_internal(request) and doc.created_by_id != request.user.pk:
            raise PermissionDenied
        form = RevisionForm(request.POST, request.FILES)
        if form.is_valid():
            rev = form.save(commit=False)
            rev.document, rev.uploaded_by = doc, request.user
            rev.original_name, rev.size = rev.file.name[:200], rev.file.size
            rev.save()
            doc.save(update_fields=['updated_at'])
            log_activity(request, 'uploaded new revision of', f'{doc.title} rev {rev.revision}', project=project,
                         url=request.path, internal_only=doc.visibility == Document.VIS_INTERNAL)
            messages.success(request, f'Revision {rev.revision} uploaded.')
            return redirect(request.path)
    return render(request, 'projects/document_detail.html', project_context(
        request, project, 'documents', doc=doc, revisions=doc.revisions.select_related('uploaded_by'), form=form,
        can_revise=is_internal(request) or doc.created_by_id == request.user.pk))


def document_download(request, org_slug, pk, doc_id, rev_id):
    project = get_project(request, pk)
    doc = get_object_or_404(_visible_documents(request, project), pk=doc_id)
    rev = get_object_or_404(DocumentRevision, document=doc, pk=rev_id)
    return serve(rev.file, rev.original_name)
