"""
The levelling sheet for the tender file: a CSV (every cell spreadsheet-safe) and a one-to-few
page ReportLab PDF summary. Everything a person typed reaches the PDF through
documents.pdf.markup, so it is printed, never parsed as markup.
"""
import csv
import io
import re

from django.utils import timezone

from documents.pdf import markup

FORMULA_STARTS = ('=', '+', '-', '@', '\t', '\r', '\n')
NUMBER = re.compile(r'-?\d+(\.\d+)?%?')


def safe(value):
    """Text a spreadsheet won't run as a formula, even after leading spaces. Money goes out as plain 2dp text."""
    if value is None:
        return ''
    if not isinstance(value, str):
        return str(value)
    if NUMBER.fullmatch(value):  # a plain number such as -120.00 or -1.5% is data, not a formula
        return value
    if value[:1] in FORMULA_STARTS or value.lstrip()[:1] in FORMULA_STARTS:
        return "'" + value
    return value


def money(v):
    return '' if v is None else f'{v:.2f}'


def pct(v):
    return '' if v is None else f'{v}%'


def _cell_text(cell):
    if cell.plug is not None and cell.value is None:
        return f'plug {money(cell.plug.amount)}'
    if cell.missing:
        return 'MISSING'
    if cell.cell is not None and cell.cell.included:
        return 'incl'
    return money(cell.value)


def csv_bytes(view):
    buf = io.StringIO()
    w = csv.writer(buf)
    sheet = view.sheet
    names = [c.package.subcontractor.company_name for c in view.columns]
    w.writerow([safe(f'Quote levelling: {sheet.trade} on {sheet.project.name} ({sheet.project.reference})')])
    w.writerow([safe(f'Printed {timezone.localtime():%d %b %Y %H:%M}')])
    w.writerow([])
    w.writerow(['Ref', 'Description', 'Qty', 'Unit'] + [safe(n) for n in names])
    for row in view.lines:
        line = row.line
        w.writerow([safe(line.ref), safe(line.description), '' if line.qty is None else str(line.qty.normalize()),
                    safe(line.unit)] + [safe(_cell_text(c)) for c in row.cells])
    w.writerow([])
    pad = ['', '', '']

    def total(label, values):
        w.writerow([safe(label)] + pad + [safe(v) for v in values])

    total('Lines total', [money(c.lines_total) for c in view.columns])
    total('Quoted total', [money(c.quoted) for c in view.columns])
    total('Adjustments', [money(c.result.adjustments) for c in view.columns])
    total('Levelled total', [money(c.levelled) for c in view.columns])
    total('Rank', [str(c.result.rank or '') + ('=' if c.result.tied else '') for c in view.columns])
    total('Variance from lowest', [money(c.result.var_lowest) for c in view.columns])
    total('Variance from lowest %', [pct(c.result.var_lowest_pct) for c in view.columns])
    total('Variance from average', [money(c.result.var_average) for c in view.columns])
    total('Variance from average %', [pct(c.result.var_average_pct) for c in view.columns])
    total('Missing items', [str(c.missing) for c in view.columns])
    total('Unpriced exclusions', [str(len(c.unpriced_exclusions)) for c in view.columns])
    carried = view.carried
    total('Carried in tender', ['YES' if carried and c is carried else '' for c in view.columns])
    w.writerow([])
    w.writerow(['Adjustments'])
    w.writerow(['Subcontractor', 'Type', 'Amount', 'Reason'])
    for c in view.columns:
        for a in c.adjustments:
            w.writerow([safe(c.package.subcontractor.company_name), safe(a.get_kind_display()), money(a.amount),
                        safe(a.reason)])
    w.writerow([])
    w.writerow(['Exclusions and qualifications'])
    w.writerow(['Subcontractor', 'Exclusion', 'Status', 'Priced by us'])
    for c in view.columns:
        for e in c.exclusions:
            w.writerow([safe(c.package.subcontractor.company_name), safe(e.text), safe(e.get_status_display()),
                        money(e.amount)])
    if carried:
        w.writerow([])
        w.writerow(['Carried in tender', safe(carried.package.subcontractor.company_name), money(carried.levelled),
                    safe(sheet.carried_reason)])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def pdf_bytes(view):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    sheet, project = view.sheet, view.sheet.project
    s = getSampleStyleSheet()
    s['Normal'].fontSize, s['Normal'].leading = 8, 10

    def para(text, style='Normal'):
        return Paragraph(markup(text), s[style])

    def table(rows, widths):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, -1), 'Helvetica', 7.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7.5),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1EEE6')),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#C9C4B8')), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ]))
        return t

    def gbp(v):
        return '-' if v is None else f'£{v:,.2f}'

    carried = view.carried
    story = [para(f'Quote levelling: {sheet.trade}', 'Title'),
             para(f'{project.reference} {project.name}' + (f' · {project.client_name}' if project.client_name else '')),
             para(f'{len(view.columns)} quotes · {len(view.lines)} scope lines · printed '
                  f'{timezone.localtime():%d %b %Y %H:%M}'), Spacer(1, 4 * mm)]
    rows = [['Subcontractor', 'Quoted', 'Adjustments', 'Levelled', 'Rank', 'v lowest', 'v average', 'Missing',
             'Unpriced excl.']]
    for c in sorted(view.columns, key=lambda c: (c.result.rank is None, c.result.rank or 0,
                                                 c.package.subcontractor.company_name)):
        r = c.result
        name = c.package.subcontractor.company_name + (' (carried)' if c is carried else '')
        rows.append([para(name), gbp(c.quoted), gbp(r.adjustments), gbp(r.levelled),
                     f'{r.rank or "-"}{"=" if r.tied else ""}',
                     '-' if r.var_lowest is None else f'{gbp(r.var_lowest)} ({r.var_lowest_pct or 0}%)',
                     '-' if r.var_average is None else f'{gbp(r.var_average)} ({r.var_average_pct or 0}%)',
                     str(c.missing), str(len(c.unpriced_exclusions))])
    if len(rows) == 1:
        rows.append([para('No quotes in yet.')] + [''] * 8)
    story += [table(rows, [62 * mm, 25 * mm, 25 * mm, 25 * mm, 12 * mm, 36 * mm, 36 * mm, 15 * mm, 20 * mm]),
              Spacer(1, 4 * mm)]
    if view.summary.lowest is not None:
        story.append(para(f'Lowest levelled {gbp(view.summary.lowest)} · average {gbp(view.summary.average)}'))
    if carried:
        story += [Spacer(1, 2 * mm), para(f'Carried in tender: {carried.package.subcontractor.company_name} at '
                                          f'{gbp(carried.levelled)} levelled.', 'Heading4')]
        if sheet.carried_reason:
            story.append(para(f'Why: {sheet.carried_reason}'))
    else:
        story += [Spacer(1, 2 * mm), para('Nobody carried in the tender yet.', 'Heading4')]
    adj = [['Subcontractor', 'Type', 'Amount', 'Reason']]
    for c in view.columns:
        for a in c.adjustments:
            adj.append([para(c.package.subcontractor.company_name), a.get_kind_display(), gbp(a.amount),
                        para(a.reason)])
    if len(adj) > 1:
        story += [Spacer(1, 4 * mm), para('Adjustments', 'Heading3'),
                  table(adj, [60 * mm, 40 * mm, 28 * mm, 140 * mm])]
    exc = [['Subcontractor', 'Exclusion or qualification', 'Status', 'Priced by us']]
    for c in view.columns:
        for e in c.exclusions:
            exc.append([para(c.package.subcontractor.company_name), para(e.text), e.get_status_display(),
                        gbp(e.amount)])
    if len(exc) > 1:
        story += [Spacer(1, 4 * mm), para('Exclusions and qualifications', 'Heading3'),
                  table(exc, [60 * mm, 140 * mm, 35 * mm, 28 * mm])]
        if view.unpriced_total:
            story.append(para(f'Risk: {view.unpriced_total} exclusion{"s" if view.unpriced_total != 1 else ""} '
                              'not priced or accepted yet.'))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, f'{project.organization.name} · internal: not for subcontractors')
        canvas.drawRightString(landscape(A4)[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                      bottomMargin=12 * mm, title=f'Levelling {project.reference} {sheet.trade}').build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
