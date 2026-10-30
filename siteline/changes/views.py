"""
Early warnings, change events and the change registers. Staff only: every view is
internal_required and goes through get_project(), and the records are always internal.
"""
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from contracts.commercial import commercial_context
from contracts.models import MainContract
from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from projects.access import get_project, posted_id, visible_projects
from projects.forms import CommentForm
from projects.models import Project
from projects.views import comments_for, handle_comment
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from . import clocks as clock_rules
from .forms import ChangeEventForm, ChangeEventLineForm, EarlyWarningForm, LinkSubVariationForm
from .links import came_from, led_to, set_source
from .models import ChangeEvent, ChangeEventLine, EarlyWarning

CAUSE_FOR_ORIGIN = {'RFI': 'DESIGN_CHANGE', 'INSTRUCTION': 'CLIENT_INSTRUCTION', 'SITE_ISSUE': 'SITE_CONDITION',
                    'CLIENT_REQUEST': 'CLIENT_INSTRUCTION', 'EARLY_WARNING': 'OTHER'}


def _contract(project):
    return MainContract.objects.filter(project=project).first()


def _with_clocks(ces, contracts, today):
    rows = []
    for ce in ces:
        clocks = clock_rules.clocks_for(ce, contracts.get(ce.project_id), today)
        rows.append({'ce': ce, 'clocks': clocks, 'attention': [c for c in clocks if c.needs_attention]})
    return rows


def _filtered(request, ews, ces):
    """Apply the register's filters (?show=, ?status=, ?origin=, ?attention=1). Shared by both registers."""
    show = request.GET.get('show', 'all')
    status = request.GET.get('status', 'live')
    origin = request.GET.get('origin', '')
    if status == 'live':
        ews = ews.filter(status=EarlyWarning.STATUS_OPEN)
        ces = ces.filter(status__in=ChangeEvent.LIVE_STATUSES)
    elif status in dict(ChangeEvent.STATUS_CHOICES) or status in dict(EarlyWarning.STATUS_CHOICES):
        ews = ews.filter(status=status)
        ces = ces.filter(status=status)
    if origin in dict(ChangeEvent.ORIGIN_CHOICES):
        ces = ces.filter(origin=origin)
        ews = ews.none()
    if request.GET.get('mine'):
        ews, ces = ews.filter(ball_in_court_user=request.user), ces.filter(ball_in_court_user=request.user)
    if show == 'ew':
        ces = ces.none()
    elif show == 'ce':
        ews = ews.none()
    return ews, ces, {'show': show, 'status': status, 'origin': origin, 'mine': bool(request.GET.get('mine')),
                      'attention': bool(request.GET.get('attention'))}


def _register_rows(request, ews, ces):
    ews, ces, filters = _filtered(request, ews, ces)
    ces = ces.select_related('project', 'ball_in_court_user').prefetch_related('lines')
    ews = ews.select_related('project', 'ball_in_court_user')
    contracts = {c.project_id: c for c in MainContract.objects.filter(project__in={ce.project_id for ce in ces})}
    rows = _with_clocks(ces, contracts, timezone.localdate())
    if filters['attention']:
        rows = [r for r in rows if r['attention']]
        ews = ews.none()
    return list(ews), rows, filters


@internal_required
def change_register(request, org_slug, pk):
    project = get_project(request, pk)
    ews, rows, filters = _register_rows(request, EarlyWarning.objects.filter(project=project),
                                        ChangeEvent.objects.filter(project=project))
    return render(request, 'changes/register.html', commercial_context(
        request, project, 'changes', early_warnings=ews, rows=rows, filters=filters, contract=_contract(project),
        origins=ChangeEvent.ORIGIN_CHOICES, statuses=_status_filters(), advisory=clock_rules.ADVISORY))


@internal_required
def company_change_register(request, org_slug):
    """Every early warning and change event on the live jobs this person can open."""
    projects = visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,))
    chosen = request.GET.get('project', '')
    if chosen.isdigit():
        projects = projects.filter(pk=int(chosen))
    ews, rows, filters = _register_rows(request, EarlyWarning.objects.filter(project__in=projects),
                                        ChangeEvent.objects.filter(project__in=projects))
    return render(request, 'changes/company_register.html', {
        'early_warnings': ews, 'rows': rows, 'filters': filters, 'origins': ChangeEvent.ORIGIN_CHOICES,
        'statuses': _status_filters(), 'advisory': clock_rules.ADVISORY, 'chosen_project': chosen,
        'projects': visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES).order_by('name'),
    })


def _status_filters():
    seen, out = set(), [('live', 'Live')]
    for code, label in EarlyWarning.STATUS_CHOICES + ChangeEvent.STATUS_CHOICES:
        if code not in seen:
            seen.add(code)
            out.append((code, label))
    return out + [('all', 'All')]


# --- early warnings ----------------------------------------------------------------

@internal_required
def early_warning_create(request, org_slug, pk):
    project = get_project(request, pk)
    ctx = commercial_context(request, project, 'changes')
    form = EarlyWarningForm(request.POST or None, project=project, vocab=ctx['vocab'],
                            initial={'ball_in_court_user': request.user.pk})
    if request.method == 'POST' and form.is_valid():
        ew = form.save(commit=False)
        ew.project = project
        ew.save(actor=request.user)
        log_activity(request, 'raised early warning', f'{ew.reference} {ew.title}', project=project,
                     url=ew.get_absolute_url(), internal_only=True)
        messages.success(request, f'{ew.reference} raised.')
        return redirect(ew)
    return render(request, 'changes/early_warning_form.html', {**ctx, 'form': form})


@internal_required
def early_warning_edit(request, org_slug, pk, ew_id):
    project = get_project(request, pk)
    ew = get_object_or_404(EarlyWarning, project=project, pk=ew_id)
    ctx = commercial_context(request, project, 'changes', ew=ew)
    form = EarlyWarningForm(request.POST or None, instance=ew, project=project, vocab=ctx['vocab'])
    if request.method == 'POST' and form.is_valid():
        try:
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            return redirect(ew)
    return render(request, 'changes/early_warning_form.html', {**ctx, 'form': form})


@internal_required
def early_warning_detail(request, org_slug, pk, ew_id):
    project = get_project(request, pk)
    ew = get_object_or_404(EarlyWarning, project=project, pk=ew_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'comment':
                handle_comment(request, ew, project, ew.reference)
            elif action in ('close', 'reopen'):
                apply_transition(ew, action, request.user)
                log_activity(request, {'close': 'closed', 'reopen': 'reopened'}[action], ew.reference,
                             project=project, url=ew.get_absolute_url(), internal_only=True)
            elif action == 'escalate':
                # Escalating raises a change event from it, linked back, and marks the warning escalated
                with transaction.atomic():
                    ce = ChangeEvent(project=project, title=ew.title, origin='EARLY_WARNING',
                                     description=ew.description, ball_in_court_user=ew.ball_in_court_user,
                                     aware_on=timezone.localdate(ew.created_at))
                    ce.save(actor=request.user)
                    set_source(ce, ew, 'changes.earlywarning', actor=request.user)
                    apply_transition(ew, 'escalate', request.user, note=f'Became {ce.reference}')
                log_activity(request, 'escalated', f'{ew.reference} to {ce.reference}', project=project,
                             url=ce.get_absolute_url(), internal_only=True)
                messages.success(request, f'{ew.reference} escalated: {ce.reference} raised from it. Price it below.')
                return redirect(ce)
        except (InvalidTransition, RecordConflict) as e:
            messages.error(request, str(e))
        return redirect(ew)
    return render(request, 'changes/early_warning_detail.html', commercial_context(
        request, project, 'changes', ew=ew, transitions=available_transitions(ew, request.user),
        change_events=led_to(ew, 'changes.changeevent'), comments=comments_for(request, ew),
        comment_form=CommentForm(external=False)))


# --- change events -----------------------------------------------------------------

def _save_sources(ce, form, actor):
    set_source(ce, form.cleaned_data.get('source_rfi'), 'rfis.rfi', actor=actor)
    set_source(ce, form.cleaned_data.get('source_early_warning'), 'changes.earlywarning', actor=actor)


@internal_required
def change_event_create(request, org_slug, pk):
    project = get_project(request, pk)
    contract = _contract(project)
    ctx = commercial_context(request, project, 'changes', contract=contract)
    initial = {'ball_in_court_user': request.user.pk, 'aware_on': timezone.localdate()}
    if request.GET.get('rfi', '').isdigit():
        initial.update(origin='RFI', source_rfi=int(request.GET['rfi']))
    form = ChangeEventForm(request.POST or None, project=project, contract=contract, vocab=ctx['vocab'],
                           initial=initial)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            ce = form.save(commit=False)
            ce.project = project
            ce.save(actor=request.user)
            _save_sources(ce, form, request.user)
        log_activity(request, 'raised change event', f'{ce.reference} {ce.title}', project=project,
                     url=ce.get_absolute_url(), internal_only=True)
        messages.success(request, f'{ce.reference} raised. Add its estimated cost and revenue below.')
        return redirect(ce)
    return render(request, 'changes/change_event_form.html', {**ctx, 'form': form})


@internal_required
def change_event_edit(request, org_slug, pk, ce_id):
    project = get_project(request, pk)
    ce = get_object_or_404(ChangeEvent, project=project, pk=ce_id)
    contract = _contract(project)
    ctx = commercial_context(request, project, 'changes', ce=ce, contract=contract)
    rfi = next(iter(came_from(ce, 'rfis.rfi')), None)
    ew = next(iter(came_from(ce, 'changes.earlywarning')), None)
    form = ChangeEventForm(request.POST or None, instance=ce, project=project, contract=contract, vocab=ctx['vocab'],
                           initial={'source_rfi': rfi.object_id if rfi else None,
                                    'source_early_warning': ew.object_id if ew else None})
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                form.save(commit=False).save(actor=request.user)
                _save_sources(ce, form, request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            return redirect(ce)
    return render(request, 'changes/change_event_form.html', {**ctx, 'form': form})


def raise_variation(ce, actor):
    """Draft the client variation this change event becomes, priced at its revenue, linked both ways."""
    from variations.models import Variation
    from variations.views import sync_rfi_link
    rfi_idx = next(iter(came_from(ce, 'rfis.rfi')), None)
    revenue = ce.revenue_total
    with transaction.atomic():
        vo = Variation(project=ce.project, title=ce.title[:200], cause=CAUSE_FOR_ORIGIN.get(ce.origin, 'OTHER'),
                       # The client will read this once it's submitted: no internal refs (the link records it)
                       description=ce.description, value=revenue or None,
                       time_impact_days=ce.time_impact_days, rfi_id=rfi_idx.object_id if rfi_idx else None)
        vo.save(actor=actor)
        set_source(vo, ce, 'changes.changeevent', actor=actor)
        sync_rfi_link(vo, actor)
    return vo


@internal_required
def change_event_detail(request, org_slug, pk, ce_id):
    project = get_project(request, pk)
    ce = get_object_or_404(ChangeEvent.objects.prefetch_related('lines__trade', 'lines__package__subcontractor'),
                           project=project, pk=ce_id)
    contract = _contract(project)
    line_form = ChangeEventLineForm(project=project)
    sv_form = LinkSubVariationForm(project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'comment':
                handle_comment(request, ce, project, ce.reference)
            elif action in ('notify', 'quote', 'agree', 'reject', 'implement'):
                apply_transition(ce, action, request.user)
                log_activity(request, f'marked {ce.get_status_display().lower()}', ce.reference, project=project,
                             url=ce.get_absolute_url(), internal_only=True)
            elif action == 'add_line':
                line_form = ChangeEventLineForm(request.POST, project=project)
                if not line_form.is_valid():
                    return render(request, 'changes/change_event_detail.html',
                                  _ce_ctx(request, project, ce, contract, line_form, sv_form))
                line = line_form.save(commit=False)
                line.change_event = ce
                with transaction.atomic():  # the line and its audit entry together
                    line.save()
                    ce.save(actor=request.user, audit_action='priced',
                            audit_note=f'{line.get_kind_display()} {line.description}: {line.amount}')
            elif action == 'delete_line':
                line = get_object_or_404(ChangeEventLine, change_event=ce, pk=posted_id(request, 'line'))
                note = f'Removed {line.get_kind_display().lower()} {line.description}: {line.amount}'
                with transaction.atomic():
                    line.delete()
                    ce.save(actor=request.user, audit_action='priced', audit_note=note)
            elif action == 'raise_variation':
                existing = led_to(ce, 'variations.variation')
                if existing:
                    messages.error(request, f'{ce.reference} already became {existing[0].ref}.')
                else:
                    vo = raise_variation(ce, request.user)
                    log_activity(request, 'drafted', f'{vo.reference} from {ce.reference}', project=project,
                                 url=vo.get_absolute_url(), internal_only=True)
                    messages.success(request, f'Draft {vo.reference} created from {ce.reference}. Check it, then '
                                              'submit it to the client.')
                    return redirect(vo)
            elif action == 'link_sub_variation':
                sv_form = LinkSubVariationForm(request.POST, project=project)
                if sv_form.is_valid():
                    sv = sv_form.cleaned_data['sub_variation']
                    sv.change_event = ce
                    sv.save(update_fields=['change_event'])
                    ce.save(actor=request.user, audit_action='linked', audit_note=f'Sub variation {sv.reference}')
            elif action == 'unlink_sub_variation':
                from delivery.models import SubVariation
                sv = get_object_or_404(SubVariation, project=project, change_event=ce, pk=posted_id(request, 'sv'))
                sv.change_event = None
                sv.save(update_fields=['change_event'])
                ce.save(actor=request.user, audit_action='unlinked', audit_note=f'Sub variation {sv.reference}')
        except (InvalidTransition, RecordConflict) as e:
            messages.error(request, str(e))
        return redirect(ce)
    return render(request, 'changes/change_event_detail.html',
                  _ce_ctx(request, project, ce, contract, line_form, sv_form))


def _ce_ctx(request, project, ce, contract, line_form, sv_form):
    return commercial_context(
        request, project, 'changes', ce=ce, contract=contract, line_form=line_form, sv_form=sv_form,
        clocks=clock_rules.clocks_for(ce, contract, timezone.localdate()), advisory=clock_rules.ADVISORY,
        transitions=available_transitions(ce, request.user), sources=came_from(ce),
        variations=led_to(ce, 'variations.variation'),
        sub_variations=ce.sub_variations.select_related('subcontractor'),
        comments=comments_for(request, ce), comment_form=CommentForm(external=False))
