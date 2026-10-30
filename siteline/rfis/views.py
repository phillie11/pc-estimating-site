import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from projects.access import get_project, is_internal, portal_projects
from projects.files import serve
from projects.forms import CommentForm
from projects.views import comments_for, handle_comment, notify_assignee, project_context
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .forms import ReassignForm, RFIForm, RFIResponseForm
from .models import RFI


def _visible(request, project):
    qs = RFI.objects.filter(project=project).select_related('ball_in_court_user', 'created_by')
    if not is_internal(request):
        qs = qs.filter(visibility=RFI.VIS_SHARED)
    return qs


def rfi_list(request, org_slug, pk):
    project = get_project(request, pk)
    rfis = _visible(request, project)
    status = request.GET.get('status', 'live')
    if status == 'live':
        rfis = rfis.filter(status__in=RFI.LIVE_STATUSES)
    elif status == 'mine':
        rfis = rfis.filter(ball_in_court_user=request.user, status=RFI.STATUS_OPEN)
    elif status in dict(RFI.STATUS_CHOICES):
        rfis = rfis.filter(status=status)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-rfi-register.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Subject', 'Category', 'Priority', 'Status', 'Raised by', 'Raised', 'Ball in court',
                    'Required by', 'Responded', 'Days open', 'Response'])
        for r in rfis:
            w.writerow([r.reference, r.title, r.get_category_display(), r.get_priority_display(), r.get_status_display(),
                        r.created_by or '', r.created_at.date(), r.ball_in_court_user or '', r.due_date or '',
                        r.responded_at.date() if r.responded_at else '', r.days_open, r.response])
        return resp
    return render(request, 'rfis/rfi_list.html', project_context(request, project, 'rfis', rfis=rfis, status=status,
                                                                 today=timezone.localdate()))


def rfi_create(request, org_slug, pk):
    project = get_project(request, pk)
    external = not is_internal(request)
    form = RFIForm(request.POST or None, request.FILES or None, project=project, external=external)
    if request.method == 'POST' and form.is_valid():
        rfi = form.save(commit=False)
        rfi.project = project
        if external:
            rfi.is_shared = True
        rfi.save(actor=request.user)
        notify_assignee(request, rfi.ball_in_court_user, f'{rfi.reference}: {rfi.title}', project, rfi.get_absolute_url())
        log_activity(request, 'raised', f'{rfi.reference} {rfi.title}', project=project, url=rfi.get_absolute_url(),
                     internal_only=not rfi.is_shared)
        messages.success(request, f'{rfi.reference} raised.')
        return redirect(rfi)
    return render(request, 'rfis/rfi_form.html', project_context(request, project, 'rfis', form=form))


def rfi_detail(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(_visible(request, project), pk=rfi_id)
    internal = is_internal(request)
    can_respond = rfi.status == RFI.STATUS_OPEN and (internal or rfi.ball_in_court_user_id == request.user.pk)
    response_form = RFIResponseForm(external=not internal)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'comment':
                handle_comment(request, rfi, project, rfi.reference)
            elif action == 'respond':
                if not can_respond:
                    raise PermissionDenied
                response_form = RFIResponseForm(request.POST, external=not internal)
                if not response_form.is_valid():
                    return render(request, 'rfis/rfi_detail.html', _ctx(request, project, rfi, response_form, can_respond))
                name = 'respond_and_close' if response_form.cleaned_data.get('close') else 'respond'
                apply_transition(rfi, name, request.user, response=response_form.cleaned_data['response'])
                log_activity(request, 'responded to', rfi.reference, project=project, url=rfi.get_absolute_url(),
                             internal_only=not rfi.is_shared)
                notify_assignee(request, rfi.created_by, f'{rfi.reference} answered', project, rfi.get_absolute_url())
                messages.success(request, 'Response recorded.')
            elif action in ('close', 'reopen', 'void') and internal:
                apply_transition(rfi, action, request.user)
                log_activity(request, {'close': 'closed', 'reopen': 'reopened', 'void': 'voided'}[action], rfi.reference,
                             project=project, url=rfi.get_absolute_url(), internal_only=not rfi.is_shared)
            elif action == 'reassign' and internal:
                form = ReassignForm(request.POST, project=project)
                if form.is_valid():
                    rfi.ball_in_court_user = form.cleaned_data['ball_in_court_user']
                    rfi.save(actor=request.user, audit_action='reassigned')
                    notify_assignee(request, rfi.ball_in_court_user, f'{rfi.reference}: {rfi.title}', project,
                                    rfi.get_absolute_url())
                    log_activity(request, f'passed to {rfi.ball_in_court_user}', rfi.reference, project=project,
                                 url=rfi.get_absolute_url(), internal_only=not rfi.is_shared)
        except (InvalidTransition, RecordConflict) as e:
            messages.error(request, str(e))
        return redirect(rfi)
    return render(request, 'rfis/rfi_detail.html', _ctx(request, project, rfi, response_form, can_respond))


def _ctx(request, project, rfi, response_form, can_respond):
    return project_context(
        request, project, 'rfis', rfi=rfi, response_form=response_form, can_respond=can_respond,
        comments=comments_for(request, rfi), comment_form=CommentForm(external=not is_internal(request)),
        reassign_form=ReassignForm(project=project, initial={'ball_in_court_user': rfi.ball_in_court_user_id}),
        variations=rfi.variations.all() if is_internal(request) else [],
    )


@internal_required
def rfi_edit(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(RFI, project=project, pk=rfi_id)
    form = RFIForm(request.POST or None, request.FILES or None, instance=rfi, project=project)
    if request.method == 'POST' and form.is_valid():
        try:
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            return redirect(rfi)
    return render(request, 'rfis/rfi_form.html', project_context(request, project, 'rfis', form=form, rfi=rfi))


def rfi_attachment(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(_visible(request, project), pk=rfi_id)
    return serve(request, rfi.attachment, project=project, label=f'{rfi.reference} attachment')


def rfi_register(request, org_slug):
    """Every live RFI across the projects this user can see."""
    rfis = RFI.objects.filter(project__in=portal_projects(request), status__in=RFI.LIVE_STATUSES) \
        .select_related('project', 'ball_in_court_user')
    if not is_internal(request):
        rfis = rfis.filter(visibility=RFI.VIS_SHARED)
    if request.GET.get('mine'):
        rfis = rfis.filter(ball_in_court_user=request.user)
    # The filters the projects dashboard's RFI numbers link to
    narrowed = []
    if request.GET.get('status') == 'open':
        rfis = rfis.filter(status=RFI.STATUS_OPEN)
        narrowed.append('open (not yet answered)')
    if request.GET.get('overdue'):
        rfis = rfis.filter(due_date__lt=timezone.localdate())
        narrowed.append('past their due date')
    if request.GET.get('scope') == 'live':
        from projects.models import Project
        rfis = rfis.filter(project__stage__in=Project.DELIVERY_STAGES)
        narrowed.append('on live jobs')
    return render(request, 'rfis/register.html', {'rfis': rfis.order_by('due_date'), 'today': timezone.localdate(),
                                                  'mine': bool(request.GET.get('mine')), 'narrowed': narrowed})
