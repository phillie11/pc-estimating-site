import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from projects.access import get_project, is_internal, portal_projects
from projects.files import serve
from projects.forms import CommentForm
from projects.views import comments_for, handle_comment, notify_assignee, project_context
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .forms import ReassignForm, RFIForm, RFIResponseForm
from .models import RFI


def _visible(request, project):
    qs = RFI.objects.filter(project=project).select_related('assigned_to', 'raised_by')
    if not is_internal(request):
        qs = qs.filter(is_shared=True)
    return qs


def rfi_list(request, org_slug, pk):
    project = get_project(request, pk)
    rfis = _visible(request, project)
    status = request.GET.get('status', 'live')
    if status == 'live':
        rfis = rfis.filter(status__in=RFI.LIVE_STATUSES)
    elif status == 'mine':
        rfis = rfis.filter(assigned_to=request.user, status=RFI.STATUS_OPEN)
    elif status in dict(RFI.STATUS_CHOICES):
        rfis = rfis.filter(status=status)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-rfi-register.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Subject', 'Category', 'Priority', 'Status', 'Raised by', 'Raised', 'Ball in court',
                    'Required by', 'Responded', 'Days open', 'Response'])
        for r in rfis:
            w.writerow([r.reference, r.subject, r.get_category_display(), r.get_priority_display(), r.get_status_display(),
                        r.raised_by or '', r.created_at.date(), r.assigned_to or '', r.due_date or '',
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
        rfi.project, rfi.raised_by = project, request.user
        if external:
            rfi.is_shared = True
        rfi.save()
        notify_assignee(request, rfi.assigned_to, f'{rfi.reference}: {rfi.subject}', project, rfi.get_absolute_url())
        log_activity(request, 'raised', f'{rfi.reference} {rfi.subject}', project=project, url=rfi.get_absolute_url(),
                     internal_only=not rfi.is_shared)
        messages.success(request, f'{rfi.reference} raised.')
        return redirect(rfi)
    return render(request, 'rfis/rfi_form.html', project_context(request, project, 'rfis', form=form))


def rfi_detail(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(_visible(request, project), pk=rfi_id)
    internal = is_internal(request)
    can_respond = rfi.status == RFI.STATUS_OPEN and (internal or rfi.assigned_to_id == request.user.pk)
    response_form = RFIResponseForm(external=not internal)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'comment':
            handle_comment(request, rfi, project, rfi.reference)
        elif action == 'respond':
            if not can_respond:
                raise PermissionDenied
            response_form = RFIResponseForm(request.POST, external=not internal)
            if response_form.is_valid():
                rfi.response = response_form.cleaned_data['response']
                rfi.responded_by, rfi.responded_at = request.user, timezone.now()
                rfi.status = RFI.STATUS_CLOSED if response_form.cleaned_data.get('close') else RFI.STATUS_RESPONDED
                if rfi.status == RFI.STATUS_CLOSED:
                    rfi.closed_at = timezone.now()
                rfi.save()
                log_activity(request, 'responded to', rfi.reference, project=project, url=rfi.get_absolute_url(),
                             internal_only=not rfi.is_shared)
                notify_assignee(request, rfi.raised_by, f'{rfi.reference} answered', project, rfi.get_absolute_url())
                messages.success(request, 'Response recorded.')
                return redirect(rfi)
            return render(request, 'rfis/rfi_detail.html', _ctx(request, project, rfi, response_form, can_respond))
        elif action in ('close', 'reopen', 'void') and internal:
            rfi.status = {'close': RFI.STATUS_CLOSED, 'reopen': RFI.STATUS_OPEN, 'void': RFI.STATUS_VOID}[action]
            rfi.closed_at = timezone.now() if action in ('close', 'void') else None
            rfi.save()
            log_activity(request, {'close': 'closed', 'reopen': 'reopened', 'void': 'voided'}[action], rfi.reference,
                         project=project, url=rfi.get_absolute_url(), internal_only=not rfi.is_shared)
        elif action == 'reassign' and internal:
            form = ReassignForm(request.POST, project=project)
            if form.is_valid():
                rfi.assigned_to = form.cleaned_data['assigned_to']
                rfi.save()
                notify_assignee(request, rfi.assigned_to, f'{rfi.reference}: {rfi.subject}', project, rfi.get_absolute_url())
                log_activity(request, f'passed to {rfi.assigned_to}', rfi.reference, project=project,
                             url=rfi.get_absolute_url(), internal_only=not rfi.is_shared)
        return redirect(rfi)
    return render(request, 'rfis/rfi_detail.html', _ctx(request, project, rfi, response_form, can_respond))


def _ctx(request, project, rfi, response_form, can_respond):
    return project_context(
        request, project, 'rfis', rfi=rfi, response_form=response_form, can_respond=can_respond,
        comments=comments_for(request, rfi), comment_form=CommentForm(external=not is_internal(request)),
        reassign_form=ReassignForm(project=project, initial={'assigned_to': rfi.assigned_to_id}),
        variations=rfi.variations.all() if is_internal(request) else [],
    )


@internal_required
def rfi_edit(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(RFI, project=project, pk=rfi_id)
    form = RFIForm(request.POST or None, request.FILES or None, instance=rfi, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect(rfi)
    return render(request, 'rfis/rfi_form.html', project_context(request, project, 'rfis', form=form, rfi=rfi))


def rfi_attachment(request, org_slug, pk, rfi_id):
    project = get_project(request, pk)
    rfi = get_object_or_404(_visible(request, project), pk=rfi_id)
    return serve(request, rfi.attachment, project=project, label=f'{rfi.reference} attachment')


def rfi_register(request, org_slug):
    """Every live RFI across the projects this user can see."""
    rfis = RFI.objects.filter(project__in=portal_projects(request), status__in=RFI.LIVE_STATUSES) \
        .select_related('project', 'assigned_to')
    if not is_internal(request):
        rfis = rfis.filter(is_shared=True)
    if request.GET.get('mine'):
        rfis = rfis.filter(assigned_to=request.user)
    return render(request, 'rfis/register.html', {'rfis': rfis.order_by('due_date'), 'today': timezone.localdate(),
                                                  'mine': bool(request.GET.get('mine'))})
