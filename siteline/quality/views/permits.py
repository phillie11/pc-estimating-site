"""
Permits to work (PTW-): ask for one on a phone, the PM or an admin approves it by confirming each
precaution, it is active from its start until it runs out (worked out from the clock), and someone
closes it when the work is finished and the area is safe. Printable for the site board.
"""
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from tenancy import notify
from tenancy.models import log_activity

from .. import rules
from ..access import quality_project
from ..forms import ApproveForm, CloseForm, PermitForm, ReasonForm
from ..models import Permit, QualitySettings
from ..workflow import permit_approver_for
from .common import csv_response, ctx, first_error, page_of


def _filters(now):
    return {
        'open': ('Live', Q(status__in=Permit.OPEN_STATUSES)),
        'waiting': ('Waiting for approval', Q(status=rules.REQUESTED)),
        'active': ('Approved and in date', Q(status=rules.APPROVED, expires_at__gt=now)),
        'expired': ('Expired, not closed', Q(status=rules.APPROVED, expires_at__lte=now)),
        'closed': ('Closed', Q(status__in=(rules.CLOSED, rules.REFUSED, rules.WITHDRAWN))),
        'all': ('All', Q()),
    }


def permit_list(request, org_slug, pk):
    project = quality_project(request, pk)
    now = timezone.now()
    filters = _filters(now)
    status = request.GET.get('status', '')
    if status not in filters:
        status = 'open'
    base = Permit.objects.filter(project=project)
    from django.db.models import Count
    counts = base.aggregate(**{k: Count('pk', filter=q) for k, (_, q) in filters.items()})
    qs = base.filter(filters[status][1]).select_related('package__subcontractor', 'approved_by', 'created_by')
    kind = request.GET.get('type', '')
    if kind in rules.PERMIT_TYPES:
        qs = qs.filter(permit_type=kind)
    else:
        kind = ''
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(location__icontains=q) | Q(ref__icontains=q) | Q(holder__icontains=q))
    qs = qs.order_by('-starts_at', '-pk')
    warning = QualitySettings.for_org(request.org).expiry_warning_hours
    if request.GET.get('export') == 'csv':
        rows = []
        for p in qs:
            p._warning_hours = warning
            rows.append([p.ref, p.type_label, p.title, p.location, p.who, p.state_label,
                         timezone.localtime(p.starts_at).strftime('%Y-%m-%d %H:%M'),
                         timezone.localtime(p.expires_at).strftime('%Y-%m-%d %H:%M'),
                         p.created_by.full_name if p.created_by_id else '',
                         p.approved_by.full_name if p.approved_by_id else '',
                         timezone.localtime(p.closed_at).strftime('%Y-%m-%d %H:%M') if p.closed_at else ''])
        return csv_response(f'{project.reference}-permits-to-work.csv',
                            ['Ref', 'Type', 'Work', 'Where', 'Who', 'Status', 'Starts', 'Runs out', 'Asked for by',
                             'Approved by', 'Closed'], rows)
    page = page_of(request, qs)
    for p in page.object_list:
        p._warning_hours = warning
    return render(request, 'quality/permit_list.html', ctx(
        request, project, 'permits', page=page, permits=page.object_list, counts=counts, status=status, q=q,
        kind=kind, filters=[(k, label, counts[k]) for k, (label, _) in filters.items()], types=rules.PERMIT_TYPE_CHOICES,
        filtered=bool(q or kind or status not in ('open', 'all'))))


def _tell_approver(request, permit):
    approver = permit.ball_in_court_user
    if approver is not None and approver.pk != request.user.pk:
        notify.alert([approver], f'{permit.ref}: {permit.type_label} permit to approve', permit.get_absolute_url(),
                     project=permit.project, actor=request.user, org=request.org)


def permit_create(request, org_slug, pk):
    project = quality_project(request, pk)
    initial = {'permit_type': request.GET.get('type')} if request.GET.get('type') in rules.PERMIT_TYPES else {}
    form = PermitForm(request.POST or None, project=project, org=request.org, initial=initial)
    if request.method == 'POST' and form.is_valid():
        permit = form.save(commit=False)
        permit.project = project
        permit.ball_in_court_user = permit_approver_for(project, request.user)
        permit.save(actor=request.user)
        _tell_approver(request, permit)
        log_activity(request, 'asked for a permit to work', f'{permit.ref} {permit.title}', project=project,
                     url=permit.get_absolute_url(), internal_only=True)
        approver = permit.ball_in_court_user
        if approver is None:
            messages.warning(request, f'{permit.ref} asked for, but there is no one else to approve it. Ask an owner '
                                      'or admin to approve it. Work must not start until it is approved.')
        else:
            messages.success(request, f'{permit.ref} sent to {approver.full_name} to approve. Work must not start '
                                      'until it is approved.')
        return redirect(permit)
    return render(request, 'quality/permit_form.html', ctx(request, project, 'permits', form=form, permit=None,
                                                           types=rules.PERMIT_TYPES))


def permit_edit(request, org_slug, pk, permit_id):
    project = quality_project(request, pk)
    permit = get_object_or_404(Permit, project=project, pk=permit_id)
    if permit.status != rules.REQUESTED:
        messages.error(request, f'{permit.ref} has been {permit.get_status_display().lower()}, so it can\'t be '
                                'changed. Ask for a new permit instead.')
        return redirect(permit)
    form = PermitForm(request.POST or None, instance=permit, project=project, org=request.org)
    if request.method == 'POST' and form.is_valid():
        try:
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            messages.success(request, f'{permit.ref} saved.')
            return redirect(permit)
    return render(request, 'quality/permit_form.html', ctx(request, project, 'permits', form=form, permit=permit,
                                                           types=rules.PERMIT_TYPES))


def permit_detail(request, org_slug, pk, permit_id):
    project = quality_project(request, pk)
    permit = get_object_or_404(Permit.objects.select_related('package__subcontractor', 'created_by', 'approved_by',
                                                             'closed_by', 'ball_in_court_user'),
                               project=project, pk=permit_id)
    settings = QualitySettings.for_org(request.org)
    permit._warning_hours = settings.expiry_warning_hours
    names = {t.name for t in available_transitions(permit, request.user)} if not project.is_archived else set()
    return render(request, 'quality/permit_detail.html', ctx(
        request, project, 'permits', permit=permit, transitions=names, precautions=permit.precaution_list(),
        approve_form=ApproveForm(permit=permit), refuse_form=ReasonForm(),
        close_form=CloseForm(permit=permit, minutes=settings.fire_watch_minutes),
        fire_watch_minutes=settings.fire_watch_minutes, advisory=rules.ADVISORY))


@require_POST
def permit_action(request, org_slug, pk, permit_id):
    project = quality_project(request, pk)
    permit = get_object_or_404(Permit, project=project, pk=permit_id)
    action = request.POST.get('action')
    minutes = QualitySettings.for_org(request.org).fire_watch_minutes
    try:
        if action == 'approve':
            form = ApproveForm(request.POST, permit=permit)
            if not form.is_valid():
                messages.error(request, first_error(form))
                return redirect(permit)
            apply_transition(permit, 'approve', request.user, precautions=form.cleaned_data['precautions'],
                             extra=form.cleaned_data['extra'])
            if permit.created_by_id and permit.created_by_id != request.user.pk:
                notify.alert([permit.created_by], f'{permit.ref}: {permit.type_label} permit approved',
                             permit.get_absolute_url(), project=project, actor=request.user, org=request.org)
            end = timezone.localtime(permit.expires_at)
            messages.success(request, f'{permit.ref} approved. It runs out at {end:%H:%M} on {end:%a} {end.day} '
                                      f'{end:%b}; close it when the work is done.')
        elif action == 'refuse':
            form = ReasonForm(request.POST)
            if not form.is_valid():
                messages.error(request, 'Say why the permit is refused, so it can be put right.')
                return redirect(permit)
            apply_transition(permit, 'refuse', request.user, reason=form.cleaned_data['reason'],
                             note=form.cleaned_data['reason'][:255])
            if permit.created_by_id and permit.created_by_id != request.user.pk:
                notify.alert([permit.created_by], f'{permit.ref}: {permit.type_label} permit refused',
                             permit.get_absolute_url(), project=project, actor=request.user, org=request.org)
            messages.success(request, f'{permit.ref} refused. The person who asked for it has been told why.')
        elif action == 'close':
            form = CloseForm(request.POST, permit=permit, minutes=minutes)
            if not form.is_valid():
                messages.error(request, first_error(form))
                return redirect(permit)
            apply_transition(permit, 'close', request.user, note=form.cleaned_data['note'][:255],
                             remark=form.cleaned_data['note'],
                             fire_watch_minutes=minutes if permit.is_hot_works else None)
            messages.success(request, f'{permit.ref} closed. Work under it must not restart without a new permit.')
        elif action == 'withdraw':
            apply_transition(permit, 'withdraw', request.user)
            messages.success(request, f'{permit.ref} withdrawn. It stays on the register.')
        else:
            messages.error(request, 'That action is not available.')
            return redirect(permit)
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
        return redirect('permit_detail', org_slug=org_slug, pk=pk, permit_id=permit_id)
    log_activity(request, {'approve': 'approved permit', 'refuse': 'refused permit', 'close': 'closed permit',
                           'withdraw': 'withdrew permit'}[action], f'{permit.ref} {permit.title}', project=project,
                 url=permit.get_absolute_url(), internal_only=True)
    return redirect(permit)


def permit_print(request, org_slug, pk, permit_id):
    """The permit on one page for the site board: print it from the browser, or save it as a PDF."""
    project = quality_project(request, pk)
    permit = get_object_or_404(Permit.objects.select_related('package__subcontractor', 'created_by', 'approved_by',
                                                             'closed_by'), project=project, pk=permit_id)
    settings = QualitySettings.for_org(request.org)
    permit._warning_hours = settings.expiry_warning_hours
    return render(request, 'quality/permit_print.html', {
        'project': project, 'permit': permit, 'precautions': permit.precaution_list(),
        'fire_watch_minutes': settings.fire_watch_minutes, 'advisory': rules.ADVISORY, 'now': timezone.now()})
