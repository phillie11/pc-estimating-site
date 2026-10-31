"""
A day's site diary entry as a PDF (ReportLab, as documents/pdf.py). Everything a person typed
goes through documents.pdf.markup(), so it is printed, never obeyed as ReportLab markup.
"""
import io

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from documents.pdf import markup

GRID = colors.HexColor('#C9C4B8')
HEAD = colors.HexColor('#F1EEE6')


def _p(text, style):
    return Paragraph(markup(text), style)


def _table(rows, widths, s):
    rows = [[_p(c, s['Small']) for c in row] for row in rows]
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8),
        ('BACKGROUND', (0, 0), (-1, 0), HEAD),
        ('GRID', (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return t


def _styles():
    s = getSampleStyleSheet()
    s['Normal'].fontSize, s['Normal'].leading = 9, 11.5
    s.add(s['Normal'].clone('Small', fontSize=8, leading=10))
    s['Heading2'].fontSize, s['Heading2'].spaceBefore, s['Heading2'].spaceAfter = 11, 8, 3
    return s


def day_sheet(entry, org_name):
    buf = io.BytesIO()
    s = _styles()
    project = entry.project
    W = A4[0] - 24 * mm

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, f'{org_name} · {entry.ref} · printed {timezone.localtime():%d %b %Y %H:%M}')
        canvas.drawRightString(A4[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=14 * mm, title=f'{entry.title} - {project.name}')
    day = entry.entry_date
    written = f'Written by {entry.created_by.full_name if entry.created_by else "unknown"} on ' \
              f'{timezone.localtime(entry.created_at):%d %b %Y %H:%M}'
    if entry.written_late:
        written += ' (written late)'
    story = [_p(f'Site diary: {day:%A} {day.day} {day:%B %Y}', s['Title']),
             _p(f'{project.reference} {project.name}', s['Normal']),
             _p(f'{entry.ref} · {written} · {"Locked" if entry.locked else "Open for changes"}', s['Small']),
             Spacer(1, 4 * mm)]

    weather = ' / '.join(x for x in (entry.get_weather_am_display() and f'AM {entry.get_weather_am_display()}',
                                     entry.get_weather_pm_display() and f'PM {entry.get_weather_pm_display()}') if x)
    facts = [['Weather', ' '.join(x for x in (weather, entry.weather_note) if x) or '-'],
             ['Site hours', f'{entry.site_start or "?"} to {entry.site_finish or "?"}'
              if entry.site_start or entry.site_finish else '-'],
             ['Our people on site', f'{entry.own_staff_count}' + (f': {entry.own_staff}' if entry.own_staff else '')],
             ['Health and safety', ('INCIDENT REPORTED. ' if entry.hs_incident else 'No incident. ') + entry.hs_notes]]
    t = Table([[_p(a, s['Small']), _p(b, s['Normal'])] for a, b in facts], colWidths=[38 * mm, W - 38 * mm])
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.4, GRID), ('BACKGROUND', (0, 0), (0, -1), HEAD),
                           ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(t)

    def section(title, head, rows, widths, empty):
        story.append(_p(title, s['Heading2']))
        if rows:
            story.append(_table([head] + rows, [w * W for w in widths], s))
        else:
            story.append(_p(empty, s['Small']))

    labour = list(entry.labour.all())
    section(f'Labour on site ({sum(x.headcount for x in labour)} subcontract, {entry.own_staff_count} own)',
            ['Company', 'Trade', 'People', 'Working on'],
            [[x.name, x.trade, str(x.headcount), x.notes] for x in labour], (.34, .2, .1, .36), 'No subcontract labour recorded.')
    section('Plant', ['Plant or equipment', 'Qty', 'Hired from', 'Note'],
            [[x.description, str(x.quantity), x.supplier, x.notes] for x in entry.plant.all()], (.36, .08, .26, .3),
            'No plant recorded.')
    section('Deliveries', ['Time', 'Supplier', 'What arrived', 'Note'],
            [[x.arrived_at.strftime('%H:%M') if x.arrived_at else '', x.supplier, x.description, x.notes]
             for x in entry.deliveries.all()], (.1, .27, .35, .28), 'No deliveries recorded.')
    section('Visitors', ['Time', 'Name', 'Company', 'Reason'],
            [[x.arrived_at.strftime('%H:%M') if x.arrived_at else '', x.name, x.company, x.reason]
             for x in entry.visitors.all()], (.1, .27, .27, .36), 'No visitors recorded.')
    section('Instructions received', ['From', 'Instruction', 'Related'],
            [[x.given_by, x.description, ' '.join(r.reference for r in (x.rfi, x.change_event) if r)]
             for x in entry.instructions.select_related('rfi', 'change_event')], (.2, .62, .18),
            'No instructions recorded.')
    delays = list(entry.delays.all())
    section(f'Delays and disruption ({sum((d.hours_lost for d in delays), start=0)} hours lost)',
            ['Cause', 'Hours lost', 'What happened', 'Trades affected'],
            [[d.get_cause_display(), str(d.hours_lost), d.description, d.trades_affected] for d in delays],
            (.2, .1, .48, .22), 'No delays recorded.')
    story.append(_p('General notes', s['Heading2']))
    story.append(_p(entry.notes or 'None.', s['Normal']))
    photos = entry.photos.count()
    if photos:
        story.append(Spacer(1, 3 * mm))
        story.append(_p(f'{photos} site photo{"s" if photos != 1 else ""} filed with this entry (see the job\'s '
                        'photo gallery).', s['Small']))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
