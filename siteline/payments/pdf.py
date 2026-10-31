"""
The application for payment as a PDF, on the contractor's letterhead (company settings: name,
logo, address, phone, website, company and VAT numbers). ReportLab, as documents/pdf.py, and every
piece of text goes through its markup() helper so nothing typed by a person is parsed as markup.
"""
import io

from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from documents.pdf import markup

from . import rules

GRID = colors.HexColor('#C9C4B8')
HEAD = colors.HexColor('#F1EEE6')


def _p(text, style):
    """Every Paragraph goes through markup(): people's text is printed, never obeyed."""
    return Paragraph(markup(text), style)


def _money(value, symbol):
    v = rules.money(value)
    sign = '-' if v < 0 else ''
    return f'{sign}{symbol}{abs(v):,}'


def _date(day):
    return f'{day.day} {day:%B %Y}' if day else '-'


def _styles():
    s = getSampleStyleSheet()
    s['Normal'].fontSize = 9
    s['Normal'].leading = 12
    s.add(ParagraphStyle('Small', parent=s['Normal'], fontSize=7.5, leading=9.5, textColor=colors.HexColor('#555555')))
    s.add(ParagraphStyle('Right', parent=s['Normal'], alignment=2))
    s.add(ParagraphStyle('RightBold', parent=s['Normal'], alignment=2, fontName='Helvetica-Bold'))
    s.add(ParagraphStyle('Org', parent=s['Normal'], fontName='Helvetica-Bold', fontSize=14, leading=17))
    return s


def _logo(org):
    if not org.logo:
        return None
    try:
        with org.logo.open('rb') as fh:
            data = io.BytesIO(fh.read())
        img = Image(data)
        ratio = img.imageWidth / float(img.imageHeight or 1)
        h = 16 * mm
        img.drawHeight, img.drawWidth = h, min(h * ratio, 50 * mm)
        return img
    except Exception:  # noqa: BLE001 - a missing or unreadable logo must not stop the application printing
        return None


def letterhead(org, s):
    lines = [_p(org.name, s['Org'])]
    if org.address:
        lines.append(_p(org.address, s['Small']))
    contact = ' · '.join(x for x in (org.phone, org.website) if x)
    if contact:
        lines.append(_p(contact, s['Small']))
    reg = ' · '.join(x for x in ((f'Company no. {org.company_number}' if org.company_number else ''),
                                 (f'VAT no. {org.vat_number}' if org.vat_number else '')) if x)
    if reg:
        lines.append(_p(reg, s['Small']))
    logo = _logo(org)
    t = Table([[lines, logo or '']], colWidths=[130 * mm, 56 * mm])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
                           ('LINEBELOW', (0, 0), (-1, 0), 0.8, GRID), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
    return t


def _grid(rows, widths, s, head=False, total_rows=()):
    t = Table(rows, colWidths=widths)
    style = [('GRID', (0, 0), (-1, -1), 0.4, GRID), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
             ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
    if head:
        style.append(('BACKGROUND', (0, 0), (-1, 0), HEAD))
    for r in total_rows:
        style.append(('BACKGROUND', (0, r), (-1, r), HEAD))
    t.setStyle(TableStyle(style))
    return t


def application_pdf(app, contract=None, words=None):
    """The application as PDF bytes."""
    words = words or {}
    org, project = app.organization, app.project
    symbol = org.currency_symbol or '£'
    s = _styles()
    buf = io.BytesIO()
    story = [letterhead(org, s), Spacer(1, 6 * mm),
             _p(f'Application for payment {app.reference}', s['Title']),
             _p(app.title, s['Heading3'])]
    to = project.client_name or ''
    if contract is not None and contract.administrator_id:
        admin = contract.administrator
        to = f'{admin.user.full_name}, {words.get("administrator", "contract administrator")}' + (
            f' ({admin.company_name})' if admin.company_name else (f', {to}' if to else ''))
    details = [
        [_p('Project', s['Normal']), _p(f'{project.reference} {project.name}', s['Normal'])],
        [_p('To', s['Normal']), _p(to or '-', s['Normal'])],
        [_p('Contract', s['Normal']), _p(contract.form_label if contract else 'No contract set up: the Scheme for '
                                         'Construction Contracts applies', s['Normal'])],
        [_p(words.get('valuation_date', 'Valuation date').capitalize(), s['Normal']),
         _p(_date(app.valuation_date), s['Normal'])],
        [_p('Submitted', s['Normal']),
         _p(_date(timezone.localdate(app.submitted_at)) if app.submitted_at else 'Draft: not yet submitted', s['Normal'])],
    ]
    story += [_grid(details, [40 * mm, 146 * mm], s), Spacer(1, 5 * mm)]

    lines = list(app.lines.all())
    if lines:
        rows = [[_p('Work done to date, by section', s['Normal']), _p('Value to date', s['Right'])]]
        rows += [[_p(line.section, s['Normal']), _p(_money(line.value, symbol), s['Right'])] for line in lines]
        story += [_grid(rows, [140 * mm, 46 * mm], s, head=True), Spacer(1, 4 * mm)]

    stage = dict(rules.STAGE_CHOICES).get(app.retention_stage, '')
    figures = [
        ('Work done to date', app.work_done),
        ('Approved variations to date', app.variations_value),
        ('Materials on site', app.materials_on_site),
        ('Materials off site', app.materials_off_site),
        ('Gross valuation', app.gross_valuation),
        (f'Less retention at {app.retention_percent.normalize()}% ({stage.lower()})', -app.retention),
        ('Net valuation', app.net_valuation),
        ('Less previous amounts certified or notified', -app.previous_certified),
        ('Amount due this time (excluding VAT)', app.amount_due),
    ]
    rows = [[_p(label, s['Normal']), _p(_money(v, symbol), s['Right'])] for label, v in figures]
    if app.reverse_charge:
        rows.append([_p('VAT', s['Normal']), _p('Reverse charge', s['Right'])])
        rows.append([_p('Total this application', s['Normal']), _p(_money(app.total, symbol), s['RightBold'])])
    else:
        rows.append([_p(f'VAT at {app.vat_rate.normalize()}%', s['Normal']), _p(_money(app.vat, symbol), s['Right'])])
        rows.append([_p('Total including VAT', s['Normal']), _p(_money(app.total, symbol), s['RightBold'])])
    story += [_grid(rows, [140 * mm, 46 * mm], s, total_rows=(4, 6, 8, len(rows) - 1)), Spacer(1, 3 * mm)]
    if app.reverse_charge:
        story.append(_p(rules.REVERSE_CHARGE_NOTE, s['Normal']))
    story.append(_p('No Construction Industry Scheme (CIS) deduction is shown here.', s['Small']))
    story.append(Spacer(1, 5 * mm))

    cycle = app.cycle or (app.live_cycle(contract) if app.valuation_date else None)
    if cycle is not None:
        dates = [[_p('Payment cycle', s['Normal']), _p('Date', s['Normal'])],
                 [_p('Due date', s['Normal']), _p(_date(cycle.due_date), s['Normal'])],
                 [_p(f'Payment notice ({words.get("payment_document", "payment notice")}) by', s['Normal']),
                  _p(_date(cycle.payment_notice_by), s['Normal'])],
                 [_p('Pay less notice by', s['Normal']), _p(_date(cycle.pay_less_by), s['Normal'])],
                 [_p('Final date for payment', s['Normal']), _p(_date(cycle.final_date), s['Normal'])]]
        story += [_grid(dates, [100 * mm, 86 * mm], s, head=True), Spacer(1, 2 * mm),
                  _p('If no payment notice is given by its deadline, this application stands as the notified sum '
                     '(Housing Grants, Construction and Regeneration Act 1996 s110B). Days are counted leaving out '
                     'Christmas Day, Good Friday and bank holidays. ' + rules.ADVISORY, s['Small'])]
    if app.notes:
        story += [Spacer(1, 4 * mm), _p('Notes', s['Heading4']), _p(app.notes, s['Normal'])]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, f'{org.name} · {app.reference} · printed {timezone.localtime():%d %b %Y %H:%M}')
        canvas.drawRightString(A4[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()

    SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                      bottomMargin=14 * mm, title=f'Application for payment {app.reference}').build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
