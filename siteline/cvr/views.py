"""
Commercial > CVR, Cashflow and Dayworks, and the company CVR. Every project view takes the project
from cvr.access (get_project() underneath); files only go out through projects.files.serve().
"""
import csv
from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from contracts.commercial import commercial_context
from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from costs.access import cost_projects
from projects.access import posted_id
from projects.files import serve
from tenancy.models import log_activity
from tenancy.permissions import admin_required, internal_required

from . import cashflow as cashflow_rules
from . import chart, figures, rules, services
from .access import can_set_rates, cvr_project, daywork_project, prepares_cvr
from .forms import (AdjustmentForm, CommentaryForm, LineForm, PhotoForm, RateForm, ReverseForm, SendForm,
                    SettingsForm, SheetForm, SignatureForm, StartForm, TermsForm)
from .models import CVRAdjustment, CVRPeriod, DayworkLine, DayworkPhoto, DayworkRate, DayworkSheet, sheet_totals

PAGE = 50


def _safe(value):
    try:
        from core.exports import safe  # Stream F moves the helper here
    except ImportError:
        from levelling.exports import safe
    return safe(value)


def _csv(filename, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for row in rows:
        w.writerow([_safe(v) if isinstance(v, str) else ('' if v is None else v) for v in row])
    return resp


def _month_param(value, default):
    try:
        return rules.month_start(date.fromisoformat(value + '-01' if len(value) == 7 else value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The monthly CVR for a job
# ---------------------------------------------------------------------------

def _previous_figures(project, month):
    """Last month's figures for the movement: its frozen ones if submitted, else live with its adjustments."""
    return _with_previous(project, None, month)[1]


def _with_previous(project, period, month, with_drift=False):
    """
    (this month's figures, last month's or None), the live ones worked out together. With `with_drift`, a
    third: a submitted or signed-off month's figures worked out live now (no adjustments), for
    figures.drift(), in the same queries.
    """
    prev_month = rules.add_months(month, -1)
    prev = CVRPeriod.objects.filter(project=project, month=prev_month).first()
    want = {}
    if period is not None and (not period.is_frozen or with_drift):
        want[period.month] = {project.pk: period} if not period.is_frozen else {}
    if prev is not None and not prev.is_frozen:
        want[prev_month] = {project.pk: prev}
    elif prev is None and prev_month >= services.first_month(project):
        want[prev_month] = {}
    live = figures.live_figures_months([project], list(want), want) if want else {}
    current = None
    if period is not None:
        current = figures.frozen_figures(period) if period.is_frozen else live[period.month][project.pk]
    if prev is not None and prev.is_frozen:
        previous = figures.frozen_figures(prev)
    else:
        previous = live[prev_month][project.pk] if prev_month in live else None
    if with_drift:
        now = live[period.month][project.pk] if period is not None and period.is_frozen else None
        return current, previous, now
    return current, previous


@internal_required
def cvr_list(request, org_slug, pk):
    project = cvr_project(request, pk)
    today = timezone.localdate()
    months = services.startable_months(project, today)
    can_prepare = True  # cvr_project() let them in: owners, admins and the job's PM or QS, who prepare it
    start_form = StartForm(request.POST or None, months=months)
    if request.method == 'POST':
        if not can_prepare:
            raise PermissionDenied
        if start_form.is_valid():
            month = date.fromisoformat(start_form.cleaned_data['month'])
            try:
                period, created = services.start_period(project, month, request.user, today)
            except services.CVRError as e:
                messages.error(request, str(e))
                return redirect('cvr_list', org_slug=org_slug, pk=pk)
            if created:
                log_activity(request, 'started', period.title, project=project, url=period.get_absolute_url(),
                             internal_only=True)
                messages.success(request, f'{period.reference} started. The figures come straight from '
                                          'applications, the cost ledger and the contract; add any accruals below.')
            return redirect(period)
    periods = list(CVRPeriod.objects.filter(project=project).select_related('signed_off_by'))
    if request.GET.get('export') == 'csv':
        rows = []
        for p in periods:
            f = figures.figures_for(p, project)
            rows.append([p.reference, p.label, p.get_status_display(), f.value, f.cost, f.margin, f.margin_pct,
                         f.final_account, f.final_cost, f.forecast_margin, f.forecast_margin_pct])
        return _csv(f'{project.reference}-cvr.csv', ['Ref', 'Month', 'Status', 'Value', 'Cost', 'Margin',
                                                      'Margin %', 'Forecast final account', 'Forecast final cost',
                                                      'Forecast margin', 'Forecast margin %'], rows)
    page = Paginator(periods, PAGE).get_page(request.GET.get('page'))
    drafts = {p.month: {project.pk: p} for p in page.object_list if not p.is_frozen}
    live = figures.live_figures_months([project], [today, *drafts], drafts)
    rows = [{'period': p, 'figures': figures.frozen_figures(p) if p.is_frozen else live[p.month][project.pk]}
            for p in page.object_list]
    now = live[rules.month_start(today)][project.pk] if rules.month_start(today) not in drafts else \
        figures.live_figures([project], today)[project.pk]
    return render(request, 'cvr/list.html', commercial_context(
        request, project, 'cvr', page=page, rows=rows, now=now, today=today, start_form=start_form,
        can_prepare=can_prepare, months=months, advisory=rules.ADVISORY))


def _period_ctx(request, project, period, **extra):
    current, previous, live_now = _with_previous(project, period, period.month, with_drift=True)
    adjustments = list(CVRAdjustment.objects.filter(period=period).select_related('created_by'))
    reversed_ids = {a.reverses_id for a in adjustments if a.reverses_id}
    for a in adjustments:
        a.is_reversed = a.pk in reversed_ids
    org = request.org.slug
    sources = {
        'applications': reverse('application_list', args=[org, project.pk]),
        'ledger': reverse('cost_ledger', args=[org, project.pk]) + f'?kind=ACTUAL&to={period.month_end.isoformat()}',
        'report': reverse('cost_report', args=[org, project.pk]),
        'contract': reverse('contract_detail', args=[org, project.pk]),
        'variations': reverse('variation_list', args=[org, project.pk]),
    }
    budget_after = None
    if not current.budget_to_date:
        from costs.models import LedgerEntry
        budget_after = LedgerEntry.objects.filter(project=project, kind=LedgerEntry.BUDGET, date__gt=period.month_end) \
            .order_by('date').values_list('date', flat=True).first()
    return commercial_context(
        request, project, 'cvr', period=period, f=current, budget_after=budget_after, previous=previous, move=current.movement(previous),
        adjustments=adjustments, transitions=available_transitions(period, request.user),
        can_prepare=True, drift=figures.drift(period, project, now=live_now),
        has_previous=services.previous_period(period) is not None, sources=sources,
        month_ended=rules.has_ended(period.month, timezone.localdate()), advisory=rules.ADVISORY, **extra)


@internal_required
def cvr_period(request, org_slug, pk, period_id):
    project = cvr_project(request, pk)
    period = get_object_or_404(CVRPeriod.objects.select_related('submitted_by', 'signed_off_by'), project=project,
                               pk=period_id)
    adj_form = AdjustmentForm(prefix='adj')
    commentary_form = CommentaryForm(instance=period, prefix='c')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('submit', 'send_back', 'sign_off'):
                apply_transition(period, action, request.user, note=request.POST.get('note', '')[:255])
                verb = {'submit': 'submitted for sign-off', 'send_back': 'sent back', 'sign_off': 'signed off'}[action]
                log_activity(request, verb, period.title, project=project, url=period.get_absolute_url(),
                             internal_only=True)
                messages.success(request, {
                    'submit': f'{period.reference} submitted. The figures are now fixed while it is checked.',
                    'send_back': f'{period.reference} is open again: the figures are worked out live.',
                    'sign_off': f'{period.reference} signed off and locked. Anything found later goes in a '
                                'later month as an adjustment.'}[action])
                return redirect(period)
            if not prepares_cvr(request.user, project):
                raise PermissionDenied
            if action == 'adjust':
                adj_form = AdjustmentForm(request.POST, prefix='adj')
                if adj_form.is_valid():
                    d = adj_form.cleaned_data
                    services.add_adjustment(period, d['kind'], d['amount'], d['reason'], request.user)
                    messages.success(request, 'Adjustment added.')
                    return redirect(period)
            elif action == 'commentary':
                commentary_form = CommentaryForm(request.POST, instance=period, prefix='c')
                if period.status != CVRPeriod.DRAFT:
                    messages.error(request, 'The commentary is fixed once it is submitted. Send it back to change it.')
                    return redirect(period)
                if commentary_form.is_valid():
                    commentary_form.save(commit=False).save(actor=request.user)
                    messages.success(request, 'Commentary saved.')
                    return redirect(period)
            elif action == 'bring_forward':
                n = services.bring_forward(period, request.user)
                messages.success(request, f'{n} adjustment{"s" if n != 1 else ""} brought forward from last month.')
                return redirect(period)
            else:
                return redirect(period)
        except (InvalidTransition, services.CVRError) as e:
            messages.error(request, str(e))
            return redirect(period)
        except RecordConflict:
            commentary_form.add_error(None, CommentaryForm.CONFLICT_MESSAGE)
    return render(request, 'cvr/period.html', _period_ctx(request, project, period, adj_form=adj_form,
                                                          commentary_form=commentary_form))


@internal_required
def cvr_reverse(request, org_slug, pk, period_id, adj_id):
    project = cvr_project(request, pk)
    period = get_object_or_404(CVRPeriod, project=project, pk=period_id)
    adj = get_object_or_404(CVRAdjustment, period=period, pk=adj_id)
    if not prepares_cvr(request.user, project):
        raise PermissionDenied
    form = ReverseForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.reverse_adjustment(adj, request.user, form.cleaned_data['reason'])
            messages.success(request, 'Adjustment reversed. Both stay on the record.')
        except services.CVRError as e:
            messages.error(request, str(e))
        return redirect(period)
    return render(request, 'cvr/reverse.html', commercial_context(request, project, 'cvr', period=period, adj=adj,
                                                                  form=form))


# ---------------------------------------------------------------------------
# Company CVR
# ---------------------------------------------------------------------------

STATUS_FILTERS = [('', 'All'), ('none', 'Not started'), (CVRPeriod.DRAFT, 'Being prepared'),
                  (CVRPeriod.SUBMITTED, 'Waiting for sign-off'), (CVRPeriod.SIGNED_OFF, 'Signed off')]


@internal_required
def company_cvr(request, org_slug):
    today = timezone.localdate()
    month = _month_param(request.GET.get('month', ''), rules.add_months(today, -1))
    month = min(month, rules.month_start(today))
    projects = list(cost_projects(request).filter(archived_at__isnull=True).order_by('name'))
    if not projects and not request.membership.is_admin:
        # Owners, admins and staff who run a job (its PM or QS) only: estimators and site staff are refused
        raise PermissionDenied('The company CVR is for owners, admins and the staff who run jobs.')
    q = request.GET.get('q', '').strip()
    if q:
        projects = [p for p in projects if q.lower() in p.name.lower() or q.lower() in (p.reference or '').lower()]
    periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=[x.pk for x in projects], month=month)}
    prev_month = rules.add_months(month, -1)
    prev_periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=[x.pk for x in projects],
                                                                      month=prev_month)}
    now, before = figures.month_figures_pair(projects, month, periods, prev_month, prev_periods)
    status = request.GET.get('status', '')
    rows = []
    for p in projects:
        f = now[p.pk]
        if not f.has_activity and p.pk not in periods:
            continue
        period = periods.get(p.pk)
        code = period.status if period else 'none'
        if status and status != code:
            continue
        prev = before.get(p.pk)
        rows.append({'project': p, 'period': period, 'f': f,
                     'move': rules.movement(f.forecast_margin, prev.forecast_margin if prev and prev.has_activity else None)})
    total = {k: sum((r['f'].derived[k] for r in rows), figures.ZERO)
             for k in ('value', 'cost', 'margin', 'final_account', 'final_cost', 'forecast_margin')}
    total['margin_pct'] = rules.percent_of(total['margin'], total['value'])
    total['forecast_margin_pct'] = rules.percent_of(total['forecast_margin'], total['final_account'])
    if request.GET.get('export') == 'csv':
        return _csv(f'cvr-{month:%Y-%m}.csv',
                    ['Job ref', 'Job', 'CVR', 'Status', 'Value', 'Cost', 'Margin', 'Margin %',
                     'Forecast final account', 'Forecast final cost', 'Forecast margin', 'Forecast margin %',
                     'Forecast margin movement'],
                    [[r['project'].reference, r['project'].name, r['period'].reference if r['period'] else '',
                      r['period'].get_status_display() if r['period'] else 'Not started', r['f'].value, r['f'].cost,
                      r['f'].margin, r['f'].margin_pct, r['f'].final_account, r['f'].final_cost,
                      r['f'].forecast_margin, r['f'].forecast_margin_pct, r['move']] for r in rows]
                    + [['', 'Total', '', '', total['value'], total['cost'], total['margin'], total['margin_pct'],
                        total['final_account'], total['final_cost'], total['forecast_margin'],
                        total['forecast_margin_pct'], '']])
    page = Paginator(rows, PAGE).get_page(request.GET.get('page'))
    month_choices = [rules.add_months(rules.month_start(today), -i) for i in range(0, 25)]
    return render(request, 'cvr/company.html', {
        'month': month, 'rows': page.object_list, 'page': page, 'total': total, 'status': status,
        'status_filters': STATUS_FILTERS, 'month_choices': month_choices, 'q': q, 'any_jobs': bool(projects),
        'filtered': bool(status or q), 'advisory': rules.ADVISORY})


@admin_required
def cvr_settings(request, org_slug):
    obj = services.settings_for(request.org)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Commercial reporting settings saved.')
        return redirect('cvr_settings', org_slug=org_slug)
    return render(request, 'cvr/settings.html', {'form': form})


# ---------------------------------------------------------------------------
# Cashflow
# ---------------------------------------------------------------------------

@internal_required
def cashflow(request, org_slug, pk):
    project = cvr_project(request, pk)
    today = timezone.localdate()
    data = cashflow_rules.build(project, today)
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-cashflow.csv',
                    ['Month', 'In (actual)', 'In (forecast)', 'Out (actual)', 'Out (forecast)', 'Net', 'Running total'],
                    [[f'{m.month:%Y-%m}', m.in_actual, m.in_forecast, m.out_actual, m.out_forecast, m.net, m.cumulative]
                     for m in data['months']])
    has_money = any(m.money_in or m.money_out for m in data['months'])
    return render(request, 'cvr/cashflow.html', commercial_context(
        request, project, 'cashflow', data=data, chart=chart.geometry(data['months'], today) if has_money else None,
        has_money=has_money, today=today, advisory=rules.ADVISORY))


# ---------------------------------------------------------------------------
# Dayworks
# ---------------------------------------------------------------------------

def _sheet(project, sheet_id):
    return get_object_or_404(DayworkSheet.objects.select_related('created_by', 'change_event'), project=project,
                             pk=sheet_id)


DW_FILTERS = [('', 'All'), ('open', 'To do'), (DayworkSheet.DRAFT, 'Not signed yet'), (DayworkSheet.SIGNED, 'Signed'),
              (DayworkSheet.IN_CHANGE, 'On the change register'), (DayworkSheet.VOID, 'Cancelled')]


@internal_required
def daywork_list(request, org_slug, pk):
    project = daywork_project(request, pk)
    qs = DayworkSheet.objects.filter(project=project).select_related('created_by', 'change_event')
    all_count = qs.count()
    show = request.GET.get('show', '')
    if show == 'open':
        qs = qs.filter(status__in=(DayworkSheet.DRAFT, DayworkSheet.SIGNED))
    elif show in dict(DayworkSheet.STATUS_CHOICES):
        qs = qs.filter(status=show)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(ref__icontains=q) | Q(location__icontains=q))
    sheets = list(qs)
    totals = sheet_totals(sheets)
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-dayworks.csv',
                    ['Ref', 'Date', 'Work', 'Where', 'Status', 'Signed by', 'Change event', 'Priced total'],
                    [[s.reference, s.work_date.isoformat(), s.title, s.location, s.get_status_display(),
                      s.signed_by_name, s.change_event.reference if s.change_event else '', totals[s.pk]]
                     for s in sheets])
    page = Paginator(sheets, PAGE).get_page(request.GET.get('page'))
    for s in page.object_list:
        s.total = totals[s.pk]
    return render(request, 'cvr/daywork_list.html', commercial_context(
        request, project, 'dayworks', page=page, sheets=page.object_list, show=show, q=q, filters=DW_FILTERS,
        all_count=all_count, filtered=bool(show or q), can_set_rates=can_set_rates(request, project),
        has_rates=DayworkRate.objects.filter(project=project).exists()))


@internal_required
def daywork_create(request, org_slug, pk):
    project = daywork_project(request, pk)
    form = SheetForm(request.POST or None, initial={'work_date': timezone.localdate()})
    if request.method == 'POST' and form.is_valid():
        sheet = services.new_sheet(project, request.user, **{k: form.cleaned_data[k] for k in (
            'title', 'work_date', 'location', 'instructed_by', 'description')})
        log_activity(request, 'started daywork sheet', f'{sheet.reference} {sheet.title}', project=project,
                     url=sheet.get_absolute_url(), internal_only=True)
        messages.success(request, f'{sheet.reference} started. Add the labour, plant and materials.')
        return redirect(sheet)
    return render(request, 'cvr/daywork_form.html', commercial_context(request, project, 'dayworks', form=form,
                                                                       creating=True))


@internal_required
def daywork_edit(request, org_slug, pk, sheet_id):
    project = daywork_project(request, pk)
    sheet = _sheet(project, sheet_id)
    if not sheet.is_editable:
        messages.error(request, f'{sheet.reference} is {sheet.get_status_display().lower()}, so it can no longer be '
                                'changed.')
        return redirect(sheet)
    form = SheetForm(request.POST or None, instance=sheet)
    if request.method == 'POST' and form.is_valid():
        try:
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            return redirect(sheet)
    return render(request, 'cvr/daywork_form.html', commercial_context(request, project, 'dayworks', form=form,
                                                                       sheet=sheet))


def _fixed(sheet):
    return services.CVRError(f'{sheet.reference} is {sheet.get_status_display().lower()}, so its lines and photos '
                             'are fixed.')


def _lock_open(sheet):
    """
    In the caller's transaction: lock the sheet's row and check it is still being written, so a line or
    photo can't land on a sheet someone signed after this page read it.
    """
    status = DayworkSheet.objects.select_for_update().filter(pk=sheet.pk).values_list('status', flat=True).first()
    if status not in DayworkSheet.EDITABLE:
        raise _fixed(sheet)


def _touch(sheet, actor, action, note):
    try:
        sheet.save(actor=actor, audit_action=action, audit_note=str(note)[:255])
    except RecordConflict:
        sheet = DayworkSheet.objects.get(pk=sheet.pk)
        if not sheet.is_editable:  # signed meanwhile: the caller's transaction rolls the change back
            raise _fixed(sheet)
        sheet.save(actor=actor, audit_action=action, audit_note=str(note)[:255])


def _store_photo(sheet, f, caption, user):
    import io
    import os

    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps
    photo = DayworkPhoto(organization_id=sheet.organization_id, sheet=sheet, caption=caption,
                         original_name=os.path.basename(f.name)[:255], uploaded_by=user)
    photo.image.save(os.path.basename(f.name), f, save=False)
    f.seek(0)
    img = ImageOps.exif_transpose(Image.open(f))
    img.thumbnail((480, 480))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=80)
    photo.thumbnail.save('thumb.jpg', ContentFile(buf.getvalue()), save=False)
    photo.save()
    return photo


@internal_required
def daywork_detail(request, org_slug, pk, sheet_id):
    from changes.models import ChangeEvent
    project = daywork_project(request, pk)
    sheet = _sheet(project, sheet_id)
    kind = request.GET.get('kind') or rules.LABOUR
    line_form = LineForm(project=project, kind=kind, prefix='line')
    photo_form = PhotoForm(prefix='photo')
    live_ces = ChangeEvent.objects.filter(project=project, status__in=ChangeEvent.LIVE_STATUSES).order_by('-number')
    send_form = SendForm(change_events=live_ces, prefix='send')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('add_line', 'delete_line', 'add_photo', 'delete_photo') and not sheet.is_editable:
                raise _fixed(sheet)
            if action == 'add_line':
                line_form = LineForm(request.POST, project=project, prefix='line')
                if line_form.is_valid():
                    with transaction.atomic():
                        _lock_open(sheet)
                        line = line_form.save(commit=False)
                        line.organization_id, line.sheet, line.created_by = sheet.organization_id, sheet, request.user
                        line.unit_rate = line_form.cleaned_data['unit_rate']
                        line.description = line_form.cleaned_data['description']
                        line.unit = line_form.cleaned_data.get('unit') or ''
                        line.save()
                        services.refresh_terms(sheet)
                        _touch(sheet, request.user, 'priced', f'Added {line.get_kind_display().lower()}: '
                                                              f'{line.description} {line.quantity} x {line.unit_rate}')
                    messages.success(request, f'Added: {line.description}, £{line.amount:,.2f}.')
                    return redirect(reverse('daywork_detail', args=[org_slug, pk, sheet.pk]) + f'?kind={line.kind}')
            elif action == 'delete_line':
                line = get_object_or_404(DayworkLine, sheet=sheet, pk=posted_id(request, 'line'))
                with transaction.atomic():
                    _lock_open(sheet)
                    note = f'Removed {line.get_kind_display().lower()}: {line.description}'
                    line.delete()
                    _touch(sheet, request.user, 'priced', note)
                return redirect(sheet)
            elif action == 'add_photo':
                photo_form = PhotoForm(request.POST, request.FILES, prefix='photo')
                if photo_form.is_valid():
                    with transaction.atomic():
                        _lock_open(sheet)
                        _store_photo(sheet, photo_form.cleaned_data['photo'], photo_form.cleaned_data['caption'],
                                     request.user)
                        _touch(sheet, request.user, 'photo', 'Photo added')
                    messages.success(request, 'Photo added.')
                    return redirect(sheet)
            elif action == 'delete_photo':
                photo = get_object_or_404(DayworkPhoto, sheet=sheet, pk=posted_id(request, 'photo'))
                with transaction.atomic():
                    _lock_open(sheet)
                    photo.delete()
                    _touch(sheet, request.user, 'photo', 'Photo removed')
                return redirect(sheet)
            elif action == 'send':
                send_form = SendForm(request.POST, change_events=live_ces, prefix='send')
                if send_form.is_valid():
                    chosen = send_form.cleaned_data['change_event']
                    ce = get_object_or_404(live_ces, pk=int(chosen)) if chosen else None
                    ce, created = services.send_to_change(sheet, request.user, ce)
                    log_activity(request, 'added daywork to the change register',
                                 f'{sheet.reference} on {ce.reference}', project=project, url=ce.get_absolute_url(),
                                 internal_only=True)
                    messages.success(request, (f'{ce.reference} raised from {sheet.reference}' if created else
                                               f'{sheet.reference} added to {ce.reference}') +
                                     '. Its revenue and cost lines are priced from the sheet.')
                    return redirect(ce)
            elif action == 'cancel':
                apply_transition(sheet, 'cancel', request.user, note=request.POST.get('note', '')[:255])
                messages.success(request, f'{sheet.reference} cancelled. It stays on the register.')
                return redirect(sheet)
        except (services.CVRError, InvalidTransition) as e:
            messages.error(request, str(e))
            return redirect(sheet)
    lines = list(sheet.lines.select_related('rate'))
    if sheet.is_editable:
        services.refresh_terms(sheet)
    price = sheet.price(lines)
    groups = [(kind_code, label, [ln for ln in lines if ln.kind == kind_code], price['kinds'][kind_code])
              for kind_code, label in DayworkLine.KIND_CHOICES]
    return render(request, 'cvr/daywork_detail.html', commercial_context(
        request, project, 'dayworks', sheet=sheet, lines=lines, groups=groups, price=price, line_form=line_form,
        photo_form=photo_form, send_form=send_form, photos=list(sheet.photos.all()), kind=kind,
        rates=list(DayworkRate.objects.filter(project=project, is_active=True)),
        can_set_rates=can_set_rates(request, project),
        transitions={t.name for t in available_transitions(sheet, request.user)}))


@internal_required
def daywork_sign(request, org_slug, pk, sheet_id):
    project = daywork_project(request, pk)
    sheet = _sheet(project, sheet_id)
    if sheet.status != DayworkSheet.DRAFT:
        messages.error(request, f'{sheet.reference} is {sheet.get_status_display().lower()}.')
        return redirect(sheet)
    form = SignatureForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            apply_transition(sheet, 'sign', request.user, note=f'Signed by {d["name"]}', signature=d['signature'],
                             signer=d['name'].strip(), role=d['role'])
        except InvalidTransition as e:
            form.add_error(None, str(e))
        except RecordConflict:
            messages.error(request, f'{sheet.reference} was changed by someone else while it was being signed. Check '
                                    'the lines with them and sign again.')
            return redirect('daywork_sign', org_slug=org_slug, pk=pk, sheet_id=sheet.pk)
        else:
            log_activity(request, 'recorded the signature on', sheet.reference, project=project,
                         url=sheet.get_absolute_url(), internal_only=True)
            messages.success(request, f'{sheet.reference} signed by {sheet.signed_by_name}. Next: add it to the '
                                      'change register so it is valued.')
            return redirect(sheet)
    lines = list(sheet.lines.all())
    services.refresh_terms(sheet)  # what it will be signed at (workflow._sign)
    return render(request, 'cvr/daywork_sign.html', commercial_context(
        request, project, 'dayworks', sheet=sheet, form=form, lines=lines, price=sheet.price(lines)))


@internal_required
def daywork_signature(request, org_slug, pk, sheet_id):
    project = daywork_project(request, pk)
    sheet = _sheet(project, sheet_id)
    if not sheet.signature:
        raise Http404
    return serve(request, sheet.signature, download_name=f'{sheet.reference}-signature.png', project=project,
                 label=f'{sheet.reference} signature')


@internal_required
def daywork_photo(request, org_slug, pk, sheet_id, photo_id, size):
    project = daywork_project(request, pk)
    sheet = _sheet(project, sheet_id)
    photo = get_object_or_404(DayworkPhoto, sheet=sheet, pk=photo_id)
    field = photo.thumbnail if size == 'thumb' and photo.thumbnail else photo.image
    if size not in ('thumb', 'full'):
        raise Http404
    return serve(request, field, project=project, label=f'{sheet.reference} photo')


@internal_required
def daywork_rates(request, org_slug, pk):
    project = daywork_project(request, pk)
    editable = can_set_rates(request, project)
    terms = services.terms_for(project)
    terms_form = TermsForm(instance=terms, prefix='terms')
    rate_form = RateForm(prefix='rate')
    if request.method == 'POST':
        if not editable:
            raise PermissionDenied
        action = request.POST.get('action')
        if action == 'terms':
            terms_form = TermsForm(request.POST, instance=terms, prefix='terms')
            if terms_form.is_valid():
                t = terms_form.save(commit=False)
                t.organization_id, t.project, t.updated_by = project.organization_id, project, request.user
                t.save()
                messages.success(request, 'Percentage additions saved. Sheets not yet signed follow them; signed ones '
                                          'keep theirs.')
                return redirect('daywork_rates', org_slug=org_slug, pk=pk)
        elif action == 'add_rate':
            rate_form = RateForm(request.POST, prefix='rate')
            if rate_form.is_valid():
                r = rate_form.save(commit=False)
                r.organization_id, r.project = project.organization_id, project
                r.save()
                messages.success(request, f'Rate added: {r}.')
                return redirect('daywork_rates', org_slug=org_slug, pk=pk)
        elif action in ('retire', 'restore'):
            r = get_object_or_404(DayworkRate, project=project, pk=posted_id(request, 'rate'))
            r.is_active = action == 'restore'
            r.save(update_fields=['is_active'])
            return redirect('daywork_rates', org_slug=org_slug, pk=pk)
    return render(request, 'cvr/daywork_rates.html', commercial_context(
        request, project, 'dayworks', terms=terms, terms_form=terms_form, rate_form=rate_form, editable=editable,
        rates=list(DayworkRate.objects.filter(project=project)), advisory=rules.ADVISORY))

