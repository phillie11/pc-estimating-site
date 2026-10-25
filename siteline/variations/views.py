import csv
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from projects.access import can_decide_variations, can_see_variations, get_project, is_internal
from projects.files import serve
from projects.forms import CommentForm
from projects.models import ProjectMember
from projects.views import comments_for, handle_comment, project_context
from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .forms import DecisionForm, VariationForm
from .models import Variation


def _visible(request, project):
    if not can_see_variations(request):
        raise PermissionDenied
    qs = Variation.objects.filter(project=project).select_related('raised_by', 'decided_by', 'rfi')
    if not is_internal(request):
        qs = qs.exclude(status=Variation.STATUS_DRAFT)
    return qs


def variation_list(request, org_slug, pk):
    project = get_project(request, pk)
    vos = _visible(request, project)
    totals = {
        'submitted': vos.filter(status=Variation.STATUS_SUBMITTED).aggregate(s=Sum('value'))['s'] or Decimal('0'),
        'approved': vos.filter(status=Variation.STATUS_APPROVED).aggregate(s=Sum('approved_value'))['s'] or Decimal('0'),
        'draft': vos.filter(status=Variation.STATUS_DRAFT).aggregate(s=Sum('value'))['s'] or Decimal('0'),
        'rejected': vos.filter(status=Variation.STATUS_REJECTED).aggregate(s=Sum('value'))['s'] or Decimal('0'),
    }
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-variations.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Title', 'Cause', 'Status', 'Quoted', 'Approved', 'Time impact (days)', 'At risk',
                    'Client ref', 'Submitted', 'Decided', 'Decided by'])
        for v in vos:
            w.writerow([v.reference, v.title, v.get_cause_display(), v.get_status_display(),
                        v.value if v.value is not None else '', v.approved_value if v.approved_value is not None else '',
                        v.time_impact_days or '', 'Yes' if v.proceed_at_risk else '', v.client_reference,
                        v.submitted_at.date() if v.submitted_at else '', v.decided_at.date() if v.decided_at else '',
                        v.decided_by or ''])
        return resp
    return render(request, 'variations/variation_list.html', project_context(
        request, project, 'variations', variations=vos, totals=totals))


@internal_required
def variation_create(request, org_slug, pk):
    project = get_project(request, pk)
    form = VariationForm(request.POST or None, request.FILES or None, project=project,
                         initial={'rfi': request.GET.get('rfi')})
    if request.method == 'POST' and form.is_valid():
        vo = form.save(commit=False)
        vo.project, vo.raised_by = project, request.user
        vo.save()
        log_activity(request, 'drafted', f'{vo.reference} {vo.title}', project=project, url=vo.get_absolute_url())
        messages.success(request, f'{vo.reference} saved as draft.')
        return redirect(vo)
    return render(request, 'variations/variation_form.html', project_context(request, project, 'variations', form=form))


@internal_required
def variation_edit(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(Variation, project=project, pk=vo_id)
    if vo.status not in (Variation.STATUS_DRAFT, Variation.STATUS_SUBMITTED):
        messages.error(request, 'Decided variations cannot be edited.')
        return redirect(vo)
    form = VariationForm(request.POST or None, request.FILES or None, instance=vo, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect(vo)
    return render(request, 'variations/variation_form.html', project_context(request, project, 'variations',
                                                                             form=form, vo=vo))


def variation_detail(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(_visible(request, project), pk=vo_id)
    internal = is_internal(request)
    can_decide = can_decide_variations(request) and vo.status == Variation.STATUS_SUBMITTED
    decision_form = DecisionForm(initial={'client_reference': vo.client_reference})
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'comment':
            handle_comment(request, vo, project, vo.reference)
        elif action == 'submit' and internal and vo.status == Variation.STATUS_DRAFT:
            vo.status, vo.submitted_at = Variation.STATUS_SUBMITTED, timezone.now()
            vo.save()
            clients = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CLIENT).select_related('user')
            for c in clients:
                notify.send(c.user.email, f'{vo.reference} submitted for approval', 'variation_submitted', {
                    'vo': vo, 'project': project, 'actor': request.user, 'link': notify.absolute(vo.get_absolute_url()),
                }, org=request.org)
            log_activity(request, 'submitted for approval', f'{vo.reference} {vo.title}', project=project,
                         url=vo.get_absolute_url(), internal_only=True)
            messages.success(request, f'{vo.reference} submitted.' + ('' if clients else
                             ' No client is on this project yet, so nobody was notified. Invite them from the Team tab.'))
        elif action == 'decide':
            if not (can_decide or (request.membership.is_admin and vo.status == Variation.STATUS_SUBMITTED)):
                raise PermissionDenied
            decision_form = DecisionForm(request.POST)
            if decision_form.is_valid() and not can_decide and not decision_form.cleaned_data['decision_notes'].strip():
                decision_form.add_error('decision_notes', 'Say how the client gave this decision (e.g. email from J. Smith, 3 Nov).')
            if decision_form.is_valid():
                d = decision_form.cleaned_data
                vo.status = d['decision']
                vo.approved_value = (d['approved_value'] if d['approved_value'] is not None else vo.value) \
                    if vo.status == Variation.STATUS_APPROVED else None
                vo.client_reference = d['client_reference'] or vo.client_reference
                vo.decision_notes = d['decision_notes']
                vo.decided_at, vo.decided_by = timezone.now(), request.user
                vo.save()
                log_activity(request, 'approved' if vo.status == Variation.STATUS_APPROVED else 'rejected',
                             f'{vo.reference} {vo.title}', project=project, url=vo.get_absolute_url(), internal_only=True)
                if vo.raised_by and vo.raised_by != request.user:
                    notify.send(vo.raised_by.email, f'{vo.reference} {vo.get_status_display().lower()}', 'variation_decided', {
                        'vo': vo, 'project': project, 'actor': request.user, 'link': notify.absolute(vo.get_absolute_url()),
                    }, org=request.org)
                messages.success(request, f'{vo.reference} {vo.get_status_display().lower()}.')
            else:
                return render(request, 'variations/variation_detail.html', _ctx(request, project, vo, decision_form, can_decide))
        elif action == 'withdraw' and internal and vo.status in (Variation.STATUS_DRAFT, Variation.STATUS_SUBMITTED):
            vo.status = Variation.STATUS_WITHDRAWN
            vo.save()
            log_activity(request, 'withdrew', vo.reference, project=project, url=vo.get_absolute_url(),
                         internal_only=True)
        return redirect(vo)
    return render(request, 'variations/variation_detail.html', _ctx(request, project, vo, decision_form, can_decide))


def _ctx(request, project, vo, decision_form, can_decide):
    return project_context(request, project, 'variations', vo=vo, decision_form=decision_form, can_decide=can_decide,
                           comments=comments_for(request, vo),
                           comment_form=CommentForm(external=not is_internal(request)))


def variation_attachment(request, org_slug, pk, vo_id):
    project = get_project(request, pk)
    vo = get_object_or_404(_visible(request, project), pk=vo_id)
    return serve(vo.attachment)
