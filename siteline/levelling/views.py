"""
Quote levelling pages, under a bid's Sub quotes tab. Staff who work in estimating only:
subcontractors (tendering or appointed), clients and consultants never reach any of it.
"""
from decimal import Decimal

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from directory.models import Package, Trade
from projects.access import get_project, posted_id
from projects.views import project_context
from tenancy.models import log_activity
from tenancy.permissions import estimating_required

from . import exports, importer, services
from .forms import AdjustmentForm, CarryForm, ExclusionForm, ExclusionStatusForm, ImportForm, LineForm
from .models import Adjustment, Exclusion, ScopeLine


def _sheet_url(project, trade):
    return reverse('levelling_sheet', args=[project.organization.slug, project.pk, trade.pk])


def _load(request, pk, trade_id):
    """The project first (so an archived project refuses a write before anything else), then the trade."""
    project = get_project(request, pk)
    trade = get_object_or_404(Trade, pk=trade_id)
    if not Package.objects.filter(project=project, trade=trade).exists():
        from django.http import Http404
        raise Http404
    return project, trade


def _editable(project):
    """Levelling is part of the bid: once it is won (or lost) it is kept as it was, read only."""
    return project.stage in project.PIPELINE_STAGES and not project.is_archived


def _closed(request, project, url):
    messages.error(request, f'{project.name} is {project.get_stage_display().lower()}, so its levelling is read only.')
    return redirect(url)


@estimating_required
def levelling_index(request, org_slug, pk):
    """Every trade out to subcontractors on this bid, and how far its levelling has got."""
    project = get_project(request, pk)
    trades = {}
    for p in Package.objects.filter(project=project, trade__isnull=False).select_related('trade'):
        trades.setdefault(p.trade_id, p.trade)
    rows, carried_total = [], Decimal('0.00')
    for trade in sorted(trades.values(), key=lambda t: t.name):
        view = services.build(services.sheet_for(project, trade))
        carried = view.carried
        if carried and carried.levelled is not None:
            carried_total += carried.levelled
        if view.appointed:
            label, tone = f'Appointed: {view.appointed.subcontractor}', 'good'
        elif carried:
            label, tone = f'Carried: {carried.package.subcontractor}', 'good'
        elif view.levelled_count >= 2:
            label, tone = 'Levelled, nobody carried yet', 'info'
        else:
            label, tone = 'Not levelled', 'warn'
        rows.append({'trade': trade, 'view': view, 'label': label, 'tone': tone, 'url': _sheet_url(project, trade),
                     'lowest': view.ranked[0] if view.ranked else None})
    return render(request, 'levelling/index.html', project_context(
        request, project, 'packages', rows=rows, carried_total=carried_total,
        carried_count=sum(1 for r in rows if r['view'].carried), editable=_editable(project)))


@estimating_required
def levelling_sheet(request, org_slug, pk, trade_id):
    project, trade = _load(request, pk, trade_id)
    sheet = services.sheet_for(project, trade)
    url = _sheet_url(project, trade)
    editable = _editable(project)
    view = services.build(sheet)
    columns = [c.package for c in view.columns]
    forms = {}
    if request.method == 'POST':
        if not editable:
            return _closed(request, project, url)
        response = _post(request, project, trade, sheet, view, columns, url, forms)
        if response is not None:
            return response
        view = services.build(sheet if sheet.pk else services.sheet_for(project, trade))
    return _render(request, project, trade, view, editable, forms)


def _post(request, project, trade, sheet, view, columns, url, forms):
    """One action from the sheet. Returns a redirect, or None to show the page again with forms' errors."""
    action = request.POST.get('action')
    actor = request.user
    if action == 'add_line':
        form = LineForm(request.POST, prefix='line')
        if form.is_valid():
            sheet = services.ensure_saved(sheet)
            d = form.cleaned_data
            services.add_line(sheet, d['ref'], d['description'], d['qty'], d['unit'])
            messages.success(request, 'Line added.')
            return redirect(url + '#lines')
        forms['line'] = form
        return None
    if not sheet.pk:
        messages.error(request, 'Add scope lines or import them first.')
        return redirect(url)
    if action == 'delete_line':
        line = get_object_or_404(ScopeLine, sheet=sheet, pk=posted_id(request, 'line'))
        line.delete()
        messages.success(request, 'Line removed, with its prices and any plug for it.')
        return redirect(url + '#lines')
    if action == 'adjust':
        form = AdjustmentForm(request.POST, packages=columns, prefix='adj')
        if form.is_valid():
            d = form.cleaned_data
            services.add_adjustment(sheet, d['package'], d['kind'], d['amount'], d['reason'], actor)
            log_activity(request, 'adjusted quote', f'{d["package"].subcontractor} · {trade}', project=project, url=url)
            messages.success(request, f'Adjustment added to {d["package"].subcontractor}.')
            return redirect(url + '#adjustments')
        forms['adj'] = form
        return None
    if action == 'delete_adjustment':
        adj = get_object_or_404(Adjustment, sheet=sheet, pk=posted_id(request, 'adjustment'))
        if adj.kind == Adjustment.EXCLUSION:
            messages.error(request, 'Change that one in the exclusions register.')
        else:
            adj.delete()
            messages.success(request, 'Adjustment removed.')
        return redirect(url + '#adjustments')
    if action == 'plug':
        line = get_object_or_404(ScopeLine, sheet=sheet, pk=posted_id(request, 'line'))
        pkg = get_object_or_404(Package, pk=posted_id(request, 'package'), pk__in=[p.pk for p in columns])
        basis = request.POST.get('basis')
        if basis not in (Adjustment.AVERAGE, Adjustment.HIGHEST):
            return redirect(url)
        try:
            adj = services.plug(sheet, line, pkg, basis, actor)
            messages.success(request, f'{pkg.subcontractor}: plugged at £{adj.amount:,}, recorded as an adjustment.')
        except services.Refused as e:
            messages.error(request, str(e))
        return redirect(url + '#lines')
    if action == 'exclusion':
        form = ExclusionForm(request.POST, packages=columns, prefix='exc')
        if form.is_valid():
            d = form.cleaned_data
            exc = Exclusion(sheet=sheet, package=d['package'], text=d['text'], status=d['status'], amount=d['amount'],
                            created_by=actor)
            services.save_exclusion(exc, actor)
            messages.success(request, 'Added to the exclusions register.')
            return redirect(url + '#exclusions')
        forms['exc'] = form
        return None
    if action == 'exclusion_status':
        exc = get_object_or_404(Exclusion, sheet=sheet, pk=posted_id(request, 'exclusion'))
        form = ExclusionStatusForm(request.POST)
        if form.is_valid():
            exc.status, exc.amount = form.cleaned_data['status'], form.cleaned_data['amount']
            services.save_exclusion(exc, actor)
            messages.success(request, 'Exclusion updated.')
        else:
            messages.error(request, 'Enter what we have allowed for it, or choose another option.')
        return redirect(url + '#exclusions')
    if action == 'delete_exclusion':
        get_object_or_404(Exclusion, sheet=sheet, pk=posted_id(request, 'exclusion')).delete()
        messages.success(request, 'Removed from the register.')
        return redirect(url + '#exclusions')
    if action == 'copy_exclusions':
        pkg = get_object_or_404(Package, pk=posted_id(request, 'package'), pk__in=[p.pk for p in columns])
        n = services.copy_exclusions(sheet, pkg, actor)
        messages.success(request, f'{n} exclusion{"s" if n != 1 else ""} brought in from {pkg.subcontractor}\'s quote.'
                         if n else 'Nothing new to bring in.')
        return redirect(url + '#exclusions')
    if action == 'correction':
        pkg = get_object_or_404(Package, pk=posted_id(request, 'package'), pk__in=[p.pk for p in columns])
        col = view.column(pkg.pk)
        if col and col.correction:
            Adjustment.objects.create(sheet=sheet, package=pkg, kind=Adjustment.CORRECTION, amount=col.correction,
                                      reason=f'Their lines add up to £{col.lines_total:,}, not the £{col.quoted:,} '
                                             'quoted', created_by=actor)
            messages.success(request, f'Arithmetic correction of £{col.correction:,} added to {pkg.subcontractor}.')
        return redirect(url + '#adjustments')
    if action == 'carry':
        form = CarryForm(request.POST, packages=columns, prefix='carry')
        if form.is_valid():
            pkg = form.cleaned_data['package']
            try:
                services.carry(sheet, pkg, form.cleaned_data['reason'], actor)
            except services.Refused as e:
                form.add_error('reason' if 'Say why' in str(e) else 'package', str(e))
                forms['carry'] = form
                return None
            log_activity(request, 'carried in tender', f'{pkg.subcontractor} for {trade}', project=project, url=url)
            messages.success(request, f'{pkg.subcontractor} carried in the tender for {trade}. Nobody is told: '
                                      'appointment still happens after the bid is won.')
            return redirect(url)
        forms['carry'] = form
        return None
    if action == 'uncarry':
        services.uncarry(sheet)
        messages.success(request, 'Nobody is carried for this trade now.')
        return redirect(url)
    return redirect(url)


def _render(request, project, trade, view, editable, forms):
    columns = [c.package for c in view.columns]
    pricing = services.trade_packages(project, trade)
    carry_initial = {'package': view.sheet.carried_id} if view.sheet.carried_id else {}
    return render(request, 'levelling/sheet.html', project_context(
        request, project, 'packages', trade=trade, view=view, editable=editable,
        line_form=forms.get('line') or LineForm(prefix='line'),
        adj_form=forms.get('adj') or AdjustmentForm(packages=columns, prefix='adj'),
        exc_form=forms.get('exc') or ExclusionForm(packages=columns, prefix='exc'),
        carry_form=forms.get('carry') or CarryForm(packages=[c.package for c in view.columns if c.carryable],
                                                   prefix='carry', initial=carry_initial),
        import_form=forms.get('import') or ImportForm(packages=pricing, prefix='imp'),
        quote_files=[p for p in pricing if p.quote_file and importer._ext(p.quote_file.name) in importer.ALLOWED],
        exclusion_choices=Exclusion.STATUS_CHOICES))


@estimating_required
def levelling_rates(request, org_slug, pk, trade_id, package_id):
    """Type one subcontractor's rates or sums against each line, from a quote that came in on paper or PDF."""
    project, trade = _load(request, pk, trade_id)
    sheet = services.sheet_for(project, trade)
    pkg = get_object_or_404(Package, project=project, trade=trade, pk=package_id)
    url = _sheet_url(project, trade)
    if pkg.status in services.NOT_PRICING:
        messages.error(request, f'{pkg.subcontractor} declined to price this.')
        return redirect(url)
    lines = list(sheet.lines.all()) if sheet.pk else []
    from .models import QuoteCell
    cells = {c.line_id: c for c in QuoteCell.objects.filter(line__in=lines, package=pkg)} if lines else {}
    errors = {}
    if request.method == 'POST':
        if not _editable(project):
            return _closed(request, project, url)
        parsed = {}
        for line in lines:
            raw_rate = request.POST.get(f'rate_{line.pk}', '').strip()
            raw_sum = request.POST.get(f'sum_{line.pk}', '').strip()
            rate, total = importer.parse_number(raw_rate), importer.parse_number(raw_sum)
            if (raw_rate and rate is None) or (raw_sum and total is None):
                errors[line.pk] = 'Enter a number, like 1250.50.'
            parsed[line.pk] = (rate, total, request.POST.get(f'incl_{line.pk}') == '1')
        if not errors:
            for line in lines:
                rate, total, incl = parsed[line.pk]
                services.set_cell(line, pkg, rate=rate, amount=total, included=incl and rate is None and total is None)
            log_activity(request, 'entered rates', f'{pkg.subcontractor} · {trade}', project=project, url=url)
            messages.success(request, f'{pkg.subcontractor}\'s prices saved.')
            return redirect(url + '#lines')
    rows = []
    for line in lines:
        c = cells.get(line.pk)
        rows.append({'line': line, 'cell': c, 'error': errors.get(line.pk),
                     'rate': request.POST.get(f'rate_{line.pk}', '') if errors else (c.rate if c and c.rate is not None else ''),
                     'sum': request.POST.get(f'sum_{line.pk}', '') if errors else (c.amount if c and c.amount is not None else ''),
                     'incl': (request.POST.get(f'incl_{line.pk}') == '1') if errors else bool(c and c.included)})
    return render(request, 'levelling/rates.html', project_context(
        request, project, 'packages', trade=trade, package=pkg, rows=rows, editable=_editable(project),
        sheet_url=url, has_errors=bool(errors)))


@require_POST
@estimating_required
def levelling_import(request, org_slug, pk, trade_id):
    project, trade = _load(request, pk, trade_id)
    url = _sheet_url(project, trade)
    if not _editable(project):
        return _closed(request, project, url)
    sheet = services.sheet_for(project, trade)
    pricing = services.trade_packages(project, trade)
    if request.POST.get('action') == 'from_quote':
        pkg = get_object_or_404(Package, pk=posted_id(request, 'package'), pk__in=[p.pk for p in pricing])
        return _import_quote_file(request, project, sheet, pkg, url)
    form = ImportForm(request.POST, request.FILES, packages=pricing, prefix='imp')
    if not form.is_valid():
        errors = '; '.join(e for errs in form.errors.values() for e in errs)
        messages.error(request, f'Not imported: {errors}')
        return redirect(url + '#import')
    f = form.cleaned_data['file']
    try:
        rows, skipped = importer.read(f)
    except importer.WorkbookRefused as e:
        messages.error(request, f'Not imported: {e}')
        return redirect(url + '#import')
    sheet = services.ensure_saved(sheet)
    if form.cleaned_data['kind'] == ImportForm.SCOPE:
        added, updated = services.import_scope(sheet, rows)
        log_activity(request, 'imported scope lines', f'{trade}: {added} added, {updated} updated', project=project,
                     url=url)
        messages.success(request, f'{added} line{"s" if added != 1 else ""} added, {updated} updated'
                         + (f', {skipped} heading or total row{"s" if skipped != 1 else ""} skipped' if skipped else '')
                         + '. Lines already on the sheet are never removed by an import.')
        return redirect(url + '#lines')
    return _apply_priced(request, project, sheet, form.cleaned_data['package'], rows, url)


def _apply_priced(request, project, sheet, pkg, rows, url):
    if not sheet.lines.exists():
        messages.error(request, 'Import our scope lines first, then each subcontractor\'s priced copy.')
        return redirect(url + '#import')
    result = services.import_priced(sheet, pkg, rows)
    log_activity(request, 'imported priced copy', f'{pkg.subcontractor} · {sheet.trade}', project=project, url=url)
    msg = f'{pkg.subcontractor}: {result.filled} line{"s" if result.filled != 1 else ""} priced.'
    if result.sums_differ:
        msg += f' {len(result.sums_differ)} line{"s" if len(result.sums_differ) != 1 else ""} where qty × rate ' \
               'isn\'t their sum: their sum is used. Check them for an arithmetic correction.'
    if result.unmatched:
        names = ', '.join((r.ref or r.description)[:40] for r in result.unmatched[:5])
        msg += f' {len(result.unmatched)} priced row{"s" if len(result.unmatched) != 1 else ""} in their copy ' \
               f'match nothing in our scope ({names}{"…" if len(result.unmatched) > 5 else ""}): check for extras ' \
               'or changed descriptions.'
    messages.success(request, msg)
    return redirect(url + '#lines')


def _import_quote_file(request, project, sheet, pkg, url):
    """Fill a column from the workbook the subcontractor attached to their portal quote."""
    from projects.files import scan_status
    from projects.models import FileScan
    if not pkg.quote_file or importer._ext(pkg.quote_file.name) not in importer.ALLOWED:
        messages.error(request, f'{pkg.subcontractor}\'s quote document isn\'t an .xlsx or CSV file.')
        return redirect(url + '#import')
    if scan_status(pkg.quote_file, request.org) != FileScan.CLEAN:
        messages.error(request, 'That file hasn\'t passed its virus check yet. Try again in a minute.')
        return redirect(url + '#import')
    try:
        with pkg.quote_file.open('rb') as fh:
            rows, _ = importer.read(fh)
    except importer.WorkbookRefused as e:
        messages.error(request, f'Not imported: {e}')
        return redirect(url + '#import')
    except (OSError, ValueError):
        messages.error(request, 'That quote document couldn\'t be opened.')
        return redirect(url + '#import')
    return _apply_priced(request, project, services.ensure_saved(sheet), pkg, rows, url)


@estimating_required
def levelling_csv(request, org_slug, pk, trade_id):
    project, trade = _load(request, pk, trade_id)
    view = services.build(services.sheet_for(project, trade))
    resp = HttpResponse(exports.csv_bytes(view), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-{slugify(trade.name)}-levelling.csv"'
    return resp


@estimating_required
def levelling_pdf(request, org_slug, pk, trade_id):
    project, trade = _load(request, pk, trade_id)
    view = services.build(services.sheet_for(project, trade))
    resp = HttpResponse(exports.pdf_bytes(view), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-{slugify(trade.name)}-levelling.pdf"'
    return resp
