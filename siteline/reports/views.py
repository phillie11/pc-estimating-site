"""
Company reports (/<org>/reports/), for owners and admins: portfolio margin, cash position, overdue
across jobs, safety and subcontractor performance. Figures come from reports/data.py, which reads
each module's own functions. Every report has the table on screen, a chart where it helps, and a
CSV (?export=csv) through core.exports, which needs a recent password or code like any export.
"""
from datetime import date

from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone

from core.exports import csv_response
from tenancy.permissions import admin_required

from . import chart, data

ADVISORY = 'Advisory: check your contract.'
SAFETY_ADVISORY = 'Advisory: check with your safety adviser.'


def _month(value, today):
    from cvr import rules
    this = rules.month_start(today)
    try:
        y, m = (int(x) for x in (value or '').split('-'))
        return min(date(y, m, 1), this)
    except ValueError:
        return this


@admin_required
def reports_home(request, org_slug):
    return render(request, 'reports/home.html', {})


@admin_required
def report_margin(request, org_slug):
    from cvr import rules
    today = timezone.localdate()
    month = _month(request.GET.get('month'), today)
    rep = data.portfolio_margin(month)
    rows = rep['rows']
    if request.GET.get('export') == 'csv':
        return csv_response(f'portfolio-margin-{month:%Y-%m}.csv', [
            'Job ref', 'Job', 'Project manager', 'CVR', 'Value to date', 'Cost to date', 'Margin to date',
            'Forecast final account', 'Forecast final cost', 'Forecast margin', 'Forecast margin %',
            'Forecast margin moved since last month'], [
            [r['project'].reference, r['project'].name, r['project'].manager.full_name if r['project'].manager else '',
             r['period'].get_status_display() if r['period'] else 'Not started', r['f'].value, r['f'].cost,
             r['f'].margin, r['f'].final_account, r['f'].final_cost, r['f'].forecast_margin,
             r['f'].forecast_margin_pct, r['move']] if r['has_figures'] else
            [r['project'].reference, r['project'].name, r['project'].manager.full_name if r['project'].manager else '',
             'No figures yet', '', '', '', r['f'].final_account, '', '', '', ''] for r in rows])
    with_figures = [r for r in rows if r['has_figures']]
    currency = request.org.currency_symbol
    from cvr.chart import short
    graph = chart.bars([(r['project'].name, r['f'].forecast_margin,
                         f"{r['project'].name}: forecast margin {short(r['f'].forecast_margin)}"
                         + (f" ({r['f'].forecast_margin_pct}%)" if r['f'].forecast_margin_pct is not None else ''),
                         short(r['f'].forecast_margin).replace('£', currency)) for r in with_figures[:20]])
    return render(request, 'reports/margin.html', {
        'month': month, 'rows': rows, 'total': rep['total'], 'counted': rep['counted'], 'graph': graph, 'charted': min(len(with_figures), 20),
        'month_choices': [rules.add_months(rules.month_start(today), -i) for i in range(0, 25)],
        'advisory': ADVISORY})


@admin_required
def report_cash(request, org_slug):
    today = timezone.localdate()
    rep = data.cash_position(today)
    if request.GET.get('export') == 'csv':
        header = ['Job ref', 'Job']
        for m in rep['months']:
            header += [f'{m:%b %Y} in', f'{m:%b %Y} out', f'{m:%b %Y} net']
        header.append('Net over the three months')
        body = []
        for r in rep['rows']:
            line = [r['project'].reference, r['project'].name]
            for c in r['cells']:
                line += [c['in'], c['out'], c['net']]
            body.append(line + [r['net']])
        total = ['', 'Total']
        for t in rep['totals']:
            total += [t['in'], t['out'], t['net']]
        return csv_response(f'cash-position-{today:%Y-%m-%d}.csv', header, body + [total + [rep['grand']['net']]])
    from cvr.chart import short
    currency = request.org.currency_symbol
    graph = chart.bars([(f"{t['month']:%B %Y}", t['net'], f"{t['month']:%B %Y}: in {short(t['in'])}, out "
                         f"{short(t['out'])}, net {short(t['net'])}", short(t['net']).replace('£', currency))
                        for t in rep['totals']], label_width=140) if rep['rows'] else None
    return render(request, 'reports/cash.html', {**rep, 'graph': graph, 'today': today, 'advisory': ADVISORY})


@admin_required
def report_overdue(request, org_slug):
    today, now = timezone.localdate(), timezone.now()
    sections = data.overdue(today, now)
    if request.GET.get('export') == 'csv':
        return csv_response(f'overdue-{today:%Y-%m-%d}.csv',
                            ['What', 'Kind', 'Ref', 'Title', 'Job ref', 'Job', 'Was due', 'Days late', 'Detail'],
                            [[s.title, i.kind, i.ref, i.title, i.project.reference, i.project.name, i.due, i.days_late,
                              i.detail] for s in sections for i in s.items])
    return render(request, 'reports/overdue.html', {
        'sections': sections, 'total': sum(s.count for s in sections), 'today': today, 'advisory': ADVISORY,
        'limit': data.LIST_LIMIT})


@admin_required
def report_safety(request, org_slug):
    today = timezone.localdate()
    rep = data.safety(today)
    if request.GET.get('export') == 'csv':
        return csv_response(f'safety-{today:%Y-%m}.csv',
                            ['Month'] + [label for _, label in rep['kinds']] + ['Total', 'RIDDOR reportable'],
                            [[f"{r['month']:%Y-%m}"] + r['by_kind'] + [r['total'], r['riddor']] for r in rep['rows']])
    graph = chart.bars([(f"{r['month']:%b %Y}", r['total'],
                         f"{r['month']:%B %Y}: {r['total']} incident{'s' if r['total'] != 1 else ''}, "
                         f"{r['riddor']} RIDDOR reportable", str(r['total'])) for r in rep['rows']],
                       label_width=90) if rep['total'] else None
    return render(request, 'reports/safety.html', {**rep, 'graph': graph, 'advisory': SAFETY_ADVISORY})


@admin_required
def report_subcontractors(request, org_slug):
    today = timezone.localdate()
    rows = data.subcontractors(today)
    q = request.GET.get('q', '').strip()
    if q:
        rows = [r for r in rows if q.lower() in r['name'].lower()]
    if request.GET.get('export') == 'csv':
        return csv_response(f'subcontractor-performance-{today:%Y-%m-%d}.csv', [
            'Subcontractor', 'Appointed packages', 'Jobs', 'Submittals due or made', 'Made on time', 'On time %',
            'Snags and defects', 'Per package', 'Closed', 'Average days to close'], [
            [r['name'], r['packages'], r['jobs'], r['submittals_due'], r['on_time'], r['on_time_pct'], r['snags'],
             r['per_package'], r['closed'], r['avg_days']] for r in rows])
    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    return render(request, 'reports/subcontractors.html', {'rows': page.object_list, 'page': page, 'q': q,
                                                           'any': bool(rows) or bool(q)})
