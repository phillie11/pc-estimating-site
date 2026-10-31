"""
Cost control pages, under the job's Commercial tab (Costs), and the company's cost code library
under Settings. Every job view fetches the job through costs.access.costs_project().
"""
import csv

from django.contrib import messages
from django.db.models import Prefetch, ProtectedError, RestrictedError, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from contracts.commercial import commercial_context
from projects.access import posted_id
from projects.files import serve
from tenancy.permissions import admin_required, internal_required

from . import ledger, services
from .access import costs_project, settings_for
from .forms import (ActualForm, AdjustmentForm, BudgetSetupForm, CostCodeForm, CostSettingsForm, ForecastForm,
                    OrderForm, ReverseForm, RevisionForm, TransferForm)
from .models import ActualCost, BudgetChange, BudgetLine, Commitment, CostCode, LedgerEntry
from .report import cost_report
from .services import CostError


def costs_context(request, project, sub, **extra):
    org, pk = request.org.slug, project.pk
    nav = [
        ('report', 'Cost report', reverse('cost_report', args=[org, pk])),
        ('budget', 'Budget', reverse('cost_budget', args=[org, pk])),
        ('commitments', 'Orders', reverse('cost_commitments', args=[org, pk])),
        ('actuals', 'Actual costs', reverse('cost_actuals', args=[org, pk])),
        ('ledger', 'Ledger', reverse('cost_ledger', args=[org, pk])),
    ]
    return commercial_context(request, project, 'costs', costs_nav=nav, costs_sub=sub,
                              read_only=project.is_archived, **extra)


def _safe(value):
    """A text CSV cell that a spreadsheet won't run as a formula."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


# --- Cost report ------------------------------------------------------------------------

@internal_required
def report(request, org_slug, pk):
    project = costs_project(request, pk)
    data = cost_report(project)
    rows = data['rows']
    group = request.GET.get('group', '')
    show = request.GET.get('show', '')
    groups = dict(CostCode.GROUP_CHOICES)
    if group in groups:
        rows = [r for r in rows if r.code.group == group]
    if show == 'over':
        rows = [r for r in rows if r.overspent]
    if request.GET.get('export') == 'csv':
        return _report_csv(project, data)
    present = {r.code.group for r in data['rows']}
    return render(request, 'costs/report.html', costs_context(
        request, project, 'report', data=data, rows=rows, group=group, show=show,
        group_choices=[(g, label) for g, label in CostCode.GROUP_CHOICES if g in present],
        filtered=bool(group or show), has_budget=services.has_budget(project),
        forecast_history=[r for r in data['rows'] if r.history],
        overspent=sum(1 for r in data['rows'] if r.overspent)))


def _report_csv(project, data):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-cost-report.csv"'
    w = csv.writer(resp)
    w.writerow(['Cost code', 'Name', 'Group', 'Budget', 'Committed', 'Actual to date', 'Forecast final cost',
                'Forecast set by hand', 'Variance'])
    for r in data['rows']:
        w.writerow([_safe(r.code.code), _safe(r.code.name), r.code.get_group_display(), r.budget, r.committed,
                    r.actual, r.forecast, 'Yes' if r.override else '', r.variance])
    t = data['total']
    w.writerow(['', 'Total', '', t.budget, t.committed, t.actual, t.forecast, '', t.variance])
    w.writerow([])
    w.writerow(['', 'Forecast final account', '', data['final_account']])
    w.writerow(['', 'Forecast final cost', '', data['final_cost']])
    w.writerow(['', 'Forecast margin', '', data['margin']])
    w.writerow(['', 'Forecast margin %', '', data['margin_pct'] if data['margin_pct'] is not None else ''])
    return resp


@internal_required
def forecast(request, org_slug, pk):
    project = costs_project(request, pk)
    if request.method != 'POST':
        return redirect('cost_report', org_slug=org_slug, pk=pk)
    form = ForecastForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        services.set_forecast(project, request.user, d['cost_code'], d['amount'], d['reason'])
        messages.success(request, f'Forecast for {d["cost_code"]} ' +
                         ('set.' if d['amount'] is not None else 'back to the standard forecast.'))
    else:
        messages.error(request, 'Forecast not saved: ' + ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect(reverse('cost_report', args=[org_slug, pk]) + '#forecasts')


# --- Budget -----------------------------------------------------------------------------

@internal_required
def budget(request, org_slug, pk):
    project = costs_project(request, pk)
    changes = BudgetChange.objects.filter(project=project).select_related('created_by').prefetch_related(
        Prefetch('lines', queryset=BudgetLine.objects.select_related('cost_code')))
    kind = request.GET.get('kind', '')
    if kind in dict(BudgetChange.KIND_CHOICES):
        changes = changes.filter(kind=kind)
    total = LedgerEntry.objects.filter(project=project, kind=ledger.BUDGET).aggregate(s=Sum('amount'))['s'] or 0
    original = BudgetChange.objects.filter(project=project, kind=BudgetChange.ORIGINAL).first()
    return render(request, 'costs/budget.html', costs_context(
        request, project, 'budget', changes=changes, kind=kind, kind_choices=BudgetChange.KIND_CHOICES,
        current_total=total, original=original))


@internal_required
def budget_setup(request, org_slug, pk):
    project = costs_project(request, pk)
    if services.has_budget(project):
        messages.info(request, 'This job already has a budget. Change it with a transfer or revision.')
        return redirect('cost_budget', org_slug=org_slug, pk=pk)
    settings_for(request.org)  # the default code library on first use
    codes = CostCode.objects.filter(is_active=True).order_by('code')
    from contracts.commercial import commercial_position
    initial = {'basis_value': project.value or commercial_position(project)['contract'], 'margin_pct': '10'}
    form = BudgetSetupForm(request.POST or None, codes=codes, initial=initial)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            services.set_original_budget(project, request.user, d['basis_value'], d['margin_pct'], form.lines(),
                                         reason=d['reason'])
        except CostError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, 'Budget set. From now on it changes only by a transfer or revision, '
                                      'so the original stays on record.')
            return redirect('cost_report', org_slug=org_slug, pk=pk)
    return render(request, 'costs/budget_setup.html', costs_context(request, project, 'budget', form=form))


@internal_required
def budget_change(request, org_slug, pk):
    project = costs_project(request, pk)
    if not services.has_budget(project):
        messages.info(request, 'Set the budget first.')
        return redirect('cost_budget_setup', org_slug=org_slug, pk=pk)
    kind = 'revision' if request.GET.get('kind') == 'revision' or request.POST.get('kind') == 'revision' else 'transfer'
    form_class = RevisionForm if kind == 'revision' else TransferForm
    form = form_class(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            if kind == 'revision':
                change = services.revise_budget(project, request.user, d['cost_code'], d['amount'], d['reason'])
            else:
                change = services.transfer_budget(project, request.user, d['from_code'], d['to_code'], d['amount'],
                                                  d['reason'])
        except CostError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f'{change.reference} recorded: {change.get_kind_display().lower()}.')
            return redirect('cost_budget', org_slug=org_slug, pk=pk)
    return render(request, 'costs/budget_change.html', costs_context(request, project, 'budget', form=form,
                                                                     kind=kind))


# --- Commitments ------------------------------------------------------------------------

def _guess_code(package, codes):
    trade = (str(package.trade) if package.trade else '').lower()
    for c in codes:
        if trade and c.name.lower() == trade:
            return c
    for c in codes:
        if c.code == '290':
            return c
    return next((c for c in codes if c.group == CostCode.PACKAGES), codes[0] if codes else None)


@internal_required
def commitments(request, org_slug, pk):
    from delivery.models import SubVariation
    from directory.models import Package
    project = costs_project(request, pk)
    settings_for(request.org)
    orders = Commitment.objects.filter(project=project).select_related('cost_code', 'package__trade') \
        .annotate(adjusted=Sum('adjustments__amount'))
    kind = request.GET.get('kind', '')
    if kind in dict(Commitment.KIND_CHOICES):
        orders = orders.filter(kind=kind)
    codes = list(CostCode.objects.filter(is_active=True).order_by('code'))
    to_commit = []
    if not project.is_archived:
        for p in Package.objects.filter(project=project, status=Package.APPOINTED, commitment__isnull=True) \
                .select_related('subcontractor', 'trade'):
            to_commit.append((p, _guess_code(p, codes)))
    sv_waiting = SubVariation.objects.filter(
        project=project, status=SubVariation.APPROVED, approved_value__isnull=False, cost_adjustment__isnull=True,
        package__commitment__isnull=False).select_related('package__commitment', 'subcontractor')
    rows = [(o, o.original_value + (o.adjusted or 0)) for o in orders]
    return render(request, 'costs/commitments.html', costs_context(
        request, project, 'commitments', rows=rows, kind=kind, kind_choices=Commitment.KIND_CHOICES,
        to_commit=to_commit, codes=codes, sv_waiting=sv_waiting if not project.is_archived else [],
        total=sum((v for _, v in rows), 0)))


@internal_required
def commit_package(request, org_slug, pk):
    from directory.models import Package
    project = costs_project(request, pk)
    if request.method != 'POST':
        return redirect('cost_commitments', org_slug=org_slug, pk=pk)
    package = get_object_or_404(Package, project=project, pk=posted_id(request, 'package'))
    code = get_object_or_404(CostCode, is_active=True, pk=posted_id(request, 'cost_code'))
    try:
        c = services.commit_package(package, code, request.user)
    except CostError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f'{c.ref} committed: {c.supplier} at £{c.original_value:,.2f} against {code}.')
    return redirect('cost_commitments', org_slug=org_slug, pk=pk)


@internal_required
def order_create(request, org_slug, pk):
    project = costs_project(request, pk)
    settings_for(request.org)
    form = OrderForm(request.POST or None, initial={'order_date': timezone.localdate()})
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            c = services.place_order(project, request.user, d['cost_code'], d['supplier'], d['value'],
                                     reference=d['reference'], description=d['description'],
                                     order_date=d['order_date'])
        except CostError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f'{c.ref} recorded.')
            return redirect(c.get_absolute_url())
    return render(request, 'costs/order_form.html', costs_context(request, project, 'commitments', form=form))


@internal_required
def commitment_detail(request, org_slug, pk, commitment_id):
    project = costs_project(request, pk)
    c = get_object_or_404(Commitment.objects.select_related('cost_code', 'package__trade', 'created_by'),
                          project=project, pk=commitment_id)
    form = AdjustmentForm(request.POST if request.POST.get('action') == 'adjust' else None)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'adjust':
                if form.is_valid():
                    services.adjust_commitment(c, request.user, form.cleaned_data['amount'],
                                               form.cleaned_data['reason'])
                    messages.success(request, f'{c.ref} adjusted.')
                    return redirect(c.get_absolute_url())
            elif action == 'add_sv':
                sv = get_object_or_404(services.addable_sub_variations(c), pk=posted_id(request, 'sub_variation'))
                services.add_sub_variation(c, sv, request.user)
                messages.success(request, f'{sv.reference} added to {c.ref}.')
                return redirect(c.get_absolute_url())
        except CostError as e:
            messages.error(request, str(e))
    adjustments = c.adjustments.select_related('created_by', 'sub_variation')
    actuals = c.actuals.select_related('cost_code')
    return render(request, 'costs/commitment_detail.html', costs_context(
        request, project, 'commitments', c=c, form=form, adjustments=adjustments, actuals=actuals,
        addable=services.addable_sub_variations(c) if not project.is_archived else [],
        invoiced=sum((a.amount for a in actuals), 0)))


# --- Actual costs -----------------------------------------------------------------------

@internal_required
def actuals(request, org_slug, pk):
    project = costs_project(request, pk)
    rows = ActualCost.objects.filter(project=project).select_related('cost_code', 'commitment', 'created_by')
    kind = request.GET.get('kind', '')
    if kind in dict(ActualCost.KIND_CHOICES):
        rows = rows.filter(kind=kind)
    code = request.GET.get('code', '')
    if code.isdigit():
        rows = rows.filter(cost_code_id=int(code))
    rows = list(rows)
    # Which actuals the ledger has reversed, and the entry to reverse for the rest
    from django.contrib.contenttypes.models import ContentType
    ct = ContentType.objects.get_for_model(ActualCost)
    entries = {e.source_id: e for e in LedgerEntry.objects.filter(
        project=project, source_type=ct, source_id__in=[a.pk for a in rows], reverses__isnull=True)}
    reversed_ids = set(LedgerEntry.objects.filter(project=project, source_type=ct, reverses__isnull=False)
                       .values_list('source_id', flat=True))
    table = [(a, entries.get(a.pk), a.pk in reversed_ids) for a in rows]
    total = sum((a.amount for a, _, rev in table if not rev), 0)
    return render(request, 'costs/actuals.html', costs_context(
        request, project, 'actuals', table=table, kind=kind, kind_choices=ActualCost.KIND_CHOICES, total=total,
        code=code, filtered=bool(kind or code)))


@internal_required
def actual_create(request, org_slug, pk):
    project = costs_project(request, pk)
    settings_for(request.org)
    initial = {'cost_date': timezone.localdate()}
    order = request.GET.get('order', '')
    if order.isdigit():
        c = Commitment.objects.filter(project=project, pk=int(order)).first()
        if c:
            initial.update(commitment=c, cost_code=c.cost_code, supplier=c.supplier)
    form = ActualForm(request.POST or None, request.FILES or None, project=project, initial=initial)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            services.record_actual(project, request.user, d['cost_code'], d['kind'], d['supplier'], d['amount'],
                                   reference=d['reference'], cost_date=d['cost_date'], description=d['description'],
                                   commitment=d['commitment'], file=d.get('file'))
        except CostError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, 'Cost recorded.')
            return redirect('cost_actuals', org_slug=org_slug, pk=pk)
    return render(request, 'costs/actual_form.html', costs_context(request, project, 'actuals', form=form))


@internal_required
def actual_file(request, org_slug, pk, actual_id):
    project = costs_project(request, pk)
    a = get_object_or_404(ActualCost, project=project, pk=actual_id)
    return serve(request, a.file, download_name=a.original_name or None, project=project,
                 label=f'Cost {a.supplier} {a.reference}'.strip())


# --- Ledger -----------------------------------------------------------------------------

@internal_required
def ledger_list(request, org_slug, pk):
    project = costs_project(request, pk)
    entries = LedgerEntry.objects.filter(project=project).select_related('cost_code', 'reverses') \
        .order_by('-date', '-pk')
    kind = request.GET.get('kind', '')
    if kind in ledger.KINDS:
        entries = entries.filter(kind=kind)
    code = request.GET.get('code', '')
    if code.isdigit():
        entries = entries.filter(cost_code_id=int(code))
    reversed_ids = set(LedgerEntry.objects.filter(project=project, reverses__isnull=False)
                       .values_list('reverses_id', flat=True))
    codes = CostCode.objects.filter(pk__in=LedgerEntry.objects.filter(project=project).values('cost_code'))
    entries = list(entries[:500])
    return render(request, 'costs/ledger.html', costs_context(
        request, project, 'ledger', entries=entries, kind=kind, code=code, codes=codes,
        kind_choices=LedgerEntry.KIND_CHOICES, reversed_ids=reversed_ids, filtered=bool(kind or code),
        net=sum((e.amount for e in entries), 0)))


@internal_required
def ledger_reverse(request, org_slug, pk, entry_id):
    project = costs_project(request, pk)
    entry = get_object_or_404(LedgerEntry, project=project, pk=entry_id)
    back = request.POST.get('next') or reverse('cost_ledger', args=[org_slug, pk])
    if not back.startswith(f'/{org_slug}/projects/{pk}/'):
        back = reverse('cost_ledger', args=[org_slug, pk])
    if request.method != 'POST':
        return redirect(back)
    if entry.kind != ledger.ACTUAL:
        messages.error(request, 'Only actual costs are reversed here. Correct a budget with a transfer or revision, '
                                'and an order with an adjustment.')
        return redirect(back)
    form = ReverseForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Say why the cost is being reversed.')
        return redirect(back)
    try:
        ledger.reverse(entry, request.user, form.cleaned_data['reason'])
    except ledger.LedgerError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, 'Cost reversed. The original entry and its reversal both stay on the ledger.')
    return redirect(back)


# --- Settings: cost codes and margin threshold ------------------------------------------

@admin_required
def cost_settings(request, org_slug):
    settings_obj = settings_for(request.org)
    form = CostSettingsForm(request.POST if request.POST.get('action') == 'threshold' else None, instance=settings_obj)
    code_form = CostCodeForm(request.POST if request.POST.get('action') == 'add_code' else None,
                             prefix='new', initial={'is_active': True})
    if request.method == 'POST':
        if request.POST.get('action') == 'threshold' and form.is_valid():
            form.save()
            messages.success(request, 'Margin warning saved.')
            return redirect('cost_settings', org_slug=org_slug)
        if request.POST.get('action') == 'add_code' and code_form.is_valid():
            code = code_form.save()
            messages.success(request, f'Cost code {code} added.')
            return redirect('cost_settings', org_slug=org_slug)
    codes = CostCode.objects.order_by('code')
    group = request.GET.get('group', '')
    if group in dict(CostCode.GROUP_CHOICES):
        codes = codes.filter(group=group)
    status = request.GET.get('status', '')
    if status == 'retired':
        codes = codes.filter(is_active=False)
    elif status == 'active':
        codes = codes.filter(is_active=True)
    return render(request, 'costs/settings.html', {
        'form': form, 'code_form': code_form, 'codes': codes, 'group': group, 'status': status,
        'group_choices': CostCode.GROUP_CHOICES, 'settings_obj': settings_obj})


@admin_required
def cost_code_edit(request, org_slug, code_id):
    settings_for(request.org)
    code = get_object_or_404(CostCode, pk=code_id)
    if request.method == 'POST' and request.POST.get('action') == 'delete':
        if code.has_money:
            messages.error(request, f'{code} has money on it, so it can\'t be deleted. Untick "In use" to retire it.')
            return redirect('cost_code_edit', org_slug=org_slug, code_id=code.pk)
        name = str(code)
        try:
            code.delete()
        except (ProtectedError, RestrictedError):
            messages.error(request, f'{code} is used on a job, so it can\'t be deleted. Untick "In use" to retire it.')
            return redirect('cost_code_edit', org_slug=org_slug, code_id=code.pk)
        messages.success(request, f'Cost code {name} deleted.')
        return redirect('cost_settings', org_slug=org_slug)
    form = CostCodeForm(request.POST or None, instance=code)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Cost code {code} saved.')
        return redirect('cost_settings', org_slug=org_slug)
    return render(request, 'costs/code_form.html', {'form': form, 'code': code})
