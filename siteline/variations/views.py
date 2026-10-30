import csv
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from contracts.commercial import commercial_context
from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from projects.access import can_see_variations, get_project, is_internal
from projects.files import serve
from projects.forms import CommentForm
from projects.models import ProjectMember
from projects.views import comments_for, handle_comment
from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .forms import DecisionForm, VariationForm
from .models import Variation
from .workflow import is_project_client, may_decide


def _visible(request, project):
    if not can_see_variations(request):
        raise PermissionDenied
    qs = Variation.objects.filter(project=project).select_related('created_by', 'decided_by', 'rfi')
    if not is_internal(request):
        qs = qs.filter(visibility=Variation.VIS_SHARED)
    return qs


def sync_rfi_link(vo, actor):
    """Keep the RecordLink to the RFI in step with Variation.rfi, while the FK stays (ADR 0001 step 1)."""
    from changes.links import set_source
    set_source(vo, vo.rfi, 'rfis.rfi', actor=actor)


def variation_list(request, org_slug, pk):
    project = get_project(request, pk)
    vos = _visible(request, project)
    totals = {
        'submitted': vos.filter(status=Variation.STATUS_SUBMITTED).aggregate(s=Sum('value'))['s'] or Decimal('0'),
        'approved': vos.filter(status=Variation.STATUS_APPROVED).aggregate(s=Sum('approved_value'))['s'] or Decimal('0'),
        'draft': vos.filter(status=Variation.STATUS_DRAFT).aggregate(s=Sum('value'))['s'] or Decimal('0'),
        'rejected': vos.filter(status=Variation.STATUS_REJECTED).aggregate(s=Sum('value'))['s'] or Decimal('0'),
    }
    status = request.GET.get('status', '')
    if status in dict(Variation.STATUS_CHOICES):
        vos = vos.filter(status=status)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-variations.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Title', 'Cause', 'Status', 'Quoted', 'Approved', 'Time impact (days)', 'At risk',
                    'Client ref', 'Submitted', 'Decided', 'Decided by', 'EOT requested (days)', 'EOT granted (days)',
                    'Quotation due'])
        for v in vos:
            w.writerow([v.reference, v.title, v.get_cause_display(), v.get_status_display(),
                        v.value if v.value is not None else '', v.approved_value if v.approved_value is not None else '',
                        v.time_impact_days or '', 'Yes' if v.proceed_at_risk else '', v.client_reference,
                        v.submitted_at.date() if v.submitted_at else '', v.decided_at.date() if v.decided_at else '',
                        v.decided_by or '', v.eot_days_requested if v.eot_days_requested is not None else '',
                        v.eot_days_granted if v.eot_days_granted is not None else '', v.quotation_due or ''])
        return resp
    return render(request, 'variations/variation_list.html', commercial_context(
        request, project, 'variations', variations=vos, totals=totals, status=status,
        status_choices=[c for c in Variation.STATUS_CHOICES if is_internal(request) or c[0] in Variation.SHARED_STATUSES]))


@internal_required
def variation_create(request, org_slug, pk):
    project = get_project(request, pk)
    ctx = commercial_context(request, project, 'variations')
    form = VariationForm(request.POST or None, request.FILES or None, project=project, vocab=ctx['vocab'],
                         initial={'rfi': request.GET.get('rfi')})
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            vo = form.save(commit=False)
            vo.project = project
            vo.save(actor=request.user)
            sync_rfi_link(vo, request.user)
        log_activity(request, 'drafted', f'{vo.reference} {vo.title}', project=project, url=vo.get_absolute_url())
        messages.success(request, f'{vo.reference} saved as draft.')
        return redirect(vo)
    return render(request, 'variations/variation_form.html', {**ctx, 'form': form})


@internal_required
def variation_edit(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(Variation, project=project, pk=vo_id)
    if vo.status not in (Variation.STATUS_DRAFT, Variation.STATUS_SUBMITTED):
        messages.error(request, 'Decided variations cannot be edited.')
        return redirect(vo)
    ctx = commercial_context(request, project, 'variations', vo=vo)
    form = VariationForm(request.POST or None, request.FILES or None, instance=vo, project=project, vocab=ctx['vocab'])
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                form.save(commit=False).save(actor=request.user)
                sync_rfi_link(vo, request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            return redirect(vo)
    return render(request, 'variations/variation_form.html', {**ctx, 'form': form})


def _notify_submitted(request, project, vo):
    clients = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CLIENT).select_related('user')
    for c in clients:
        notify.send(c.user.email, f'{vo.reference} submitted for approval', 'variation_submitted', {
            'vo': vo, 'project': project, 'actor': request.user, 'link': notify.absolute(vo.get_absolute_url()),
        }, org=request.org)
    notify.alert([c.user for c in clients], f'{vo.reference} submitted for your approval', vo.get_absolute_url(),
                 project=project, actor=request.user, org=request.org, email=False)
    return clients


def variation_detail(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(_visible(request, project), pk=vo_id)
    internal = is_internal(request)
    can_decide = vo.status == Variation.STATUS_SUBMITTED and is_project_client(vo, request.user)
    decision_form = DecisionForm(initial={'client_reference': vo.client_reference,
                                          'eot_days_granted': vo.eot_days_requested})
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'comment':
                handle_comment(request, vo, project, vo.reference)
            elif action == 'submit':
                apply_transition(vo, 'submit', request.user)
                clients = _notify_submitted(request, project, vo)
                log_activity(request, 'submitted for approval', f'{vo.reference} {vo.title}', project=project,
                             url=vo.get_absolute_url(), internal_only=True)
                messages.success(request, f'{vo.reference} submitted.' + ('' if clients else
                                 ' No client is on this project yet, so nobody was notified. Invite them from the Team tab.'))
            elif action == 'decide':
                if vo.status != Variation.STATUS_SUBMITTED or not may_decide(vo, request.user):
                    raise PermissionDenied
                decision_form = DecisionForm(request.POST)
                if decision_form.is_valid() and not can_decide and not decision_form.cleaned_data['decision_notes'].strip():
                    decision_form.add_error('decision_notes', 'Say how the client gave this decision (e.g. email from J. Smith, 3 Nov).')
                if not decision_form.is_valid():
                    return render(request, 'variations/variation_detail.html',
                                  _ctx(request, project, vo, decision_form, can_decide))
                d = decision_form.cleaned_data
                approve = d['decision'] == Variation.STATUS_APPROVED
                if approve and d['eot_days_granted'] is not None:
                    vo.eot_days_granted = d['eot_days_granted']
                apply_transition(vo, 'approve' if approve else 'reject', request.user, note=d['decision_notes'][:200],
                                 approved_value=d['approved_value'], client_reference=d['client_reference'],
                                 decision_notes=d['decision_notes'])
                log_activity(request, 'approved' if approve else 'rejected', f'{vo.reference} {vo.title}',
                             project=project, url=vo.get_absolute_url(), internal_only=True)
                if vo.created_by and vo.created_by != request.user:
                    notify.send(vo.created_by.email, f'{vo.reference} {vo.get_status_display().lower()}', 'variation_decided', {
                        'vo': vo, 'project': project, 'actor': request.user, 'link': notify.absolute(vo.get_absolute_url()),
                    }, org=request.org)
                    notify.alert([vo.created_by], f'{vo.reference} {vo.get_status_display().lower()}', vo.get_absolute_url(),
                                 project=project, actor=request.user, org=request.org, email=False)
                messages.success(request, f'{vo.reference} {vo.get_status_display().lower()}.')
            elif action == 'withdraw':
                apply_transition(vo, 'withdraw', request.user)
                log_activity(request, 'withdrew', vo.reference, project=project, url=vo.get_absolute_url(),
                             internal_only=True)
        except (InvalidTransition, RecordConflict) as e:
            messages.error(request, str(e))
        return redirect(vo)
    return render(request, 'variations/variation_detail.html', _ctx(request, project, vo, decision_form, can_decide))


def _ctx(request, project, vo, decision_form, can_decide):
    internal = is_internal(request)
    extra = {}
    if internal:
        from changes.links import came_from
        extra['change_events'] = came_from(vo, 'changes.changeevent')
        extra['sub_variations'] = vo.sub_variations.select_related('subcontractor')
    return commercial_context(request, project, 'variations', vo=vo, decision_form=decision_form,
                              can_decide=can_decide, comments=comments_for(request, vo),
                              comment_form=CommentForm(external=not internal), **extra)


def variation_attachment(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(_visible(request, project), pk=vo_id)
    return serve(request, vo.attachment, project=project, label=f'{vo.reference} attachment')
