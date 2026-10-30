"""
PDF issue sheets with ReportLab (BSD licence): the drawing register by revision and date,
and the record of one transmittal. Both return bytes.
"""
import io
from xml.sax.saxutils import escape

from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

def markup(text):
    """
    People's text made safe for ReportLab's mini-markup: escaped (so "<b>", "&" and "<img src=...>"
    are printed, not obeyed: an <img> tag would read a file off the server), then newlines as <br/>.
    """
    return escape(str(text or '')).replace('\r\n', '\n').replace('\n', '<br/>')


def _para(text, style):
    """Every Paragraph goes through here, so nothing typed by a person is parsed as markup."""
    return Paragraph(markup(text), style)


GRID = colors.HexColor('#C9C4B8')
HEAD = colors.HexColor('#F1EEE6')


def _styles():
    s = getSampleStyleSheet()
    s['Normal'].fontSize = 8
    s['Normal'].leading = 10
    return s


def _table(rows, widths, head_rows=1):
    t = Table(rows, colWidths=widths, repeatRows=head_rows)
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 7.5),
        ('FONT', (0, 0), (-1, head_rows - 1), 'Helvetica-Bold', 7.5),
        ('BACKGROUND', (0, 0), (-1, head_rows - 1), HEAD),
        ('GRID', (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return t


def _doc(buf, title):
    return SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                             topMargin=12 * mm, bottomMargin=12 * mm, title=title)


def _footer(org_name):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, f'{org_name} · printed {timezone.localtime():%d %b %Y %H:%M}')
        canvas.drawRightString(landscape(A4)[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()
    return draw


def register_sheet(project, rows, issues):
    """
    The drawing register as an issue sheet. rows: [(document, display revision)].
    issues: [(date, {revision_id: code})] newest last, at most ~12 columns of issue dates;
    a cell shows the revision issued to anyone on that date.
    """
    buf = io.BytesIO()
    s = _styles()
    story = [_para(f'Drawing register: {project.name}', s['Title']),
             _para(f'{project.reference} · {len(rows)} documents', s['Normal']), Spacer(1, 4 * mm)]
    issue_cols = issues[-12:]
    head = ['Number', 'Title', 'Rev', 'Suit.', 'Status', 'Date'] + [d.strftime('%d/%m/%y') for d, _ in issue_cols]
    data = [head]
    for doc, rev in rows:
        by_date = []
        for _, codes in issue_cols:
            by_date.append(next((codes[r] for r in codes if r in doc._rev_ids), ''))
        data.append([doc.reference or '-', _para(doc.title, s['Normal']), rev.revision if rev else '-',
                     rev.suitability if rev else '', rev.get_status_display() if rev else '',
                     rev.uploaded_at.strftime('%d/%m/%y') if rev else ''] + by_date)
    if len(data) == 1:
        data.append(['', 'No documents on the register yet.', '', '', '', ''] + [''] * len(issue_cols))
    fixed = [52 * mm, 0, 12 * mm, 11 * mm, 20 * mm, 15 * mm]
    page_w = landscape(A4)[0] - 24 * mm
    col_w = 13 * mm
    fixed[1] = max(40 * mm, page_w - sum(fixed) - col_w * len(issue_cols))
    story.append(_table(data, fixed + [col_w] * len(issue_cols)))
    _doc(buf, f'Drawing register {project.reference}').build(
        story, onFirstPage=_footer(project.organization.name), onLaterPages=_footer(project.organization.name))
    return buf.getvalue()


def transmittal_sheet(transmittal):
    buf = io.BytesIO()
    s = _styles()
    p = transmittal.project
    issued = timezone.localtime(transmittal.issued_at) if transmittal.issued_at else None
    story = [
        _para(f'Transmittal {transmittal.reference}', s['Title']),
        _table([
            ['Project', f'{p.reference} {p.name}', 'Purpose', transmittal.get_purpose_display()],
            ['Subject', _para(transmittal.title, s['Normal']), 'Issued',
             issued.strftime('%d %b %Y %H:%M') if issued else 'Not issued'],
            ['From', transmittal.organization.name + (f' ({transmittal.issued_by.full_name})'
                                                      if transmittal.issued_by else ''),
             'Acknowledge by', transmittal.due_date.strftime('%d %b %Y') if transmittal.due_date else '-'],
        ], [28 * mm, 120 * mm, 30 * mm, 80 * mm], head_rows=0),
        Spacer(1, 4 * mm),
    ]
    if transmittal.cover_note:
        story += [_para(transmittal.cover_note, s['Normal']), Spacer(1, 4 * mm)]
    items = [['Number', 'Title', 'Rev', 'Suitability']] + [
        [i.reference or '-', _para(i.title, s['Normal']), i.revision_code, i.suitability or '-']
        for i in transmittal.items.all()]
    story += [_para('Documents', s['Heading3']), _table(items, [60 * mm, 150 * mm, 20 * mm, 30 * mm]),
              Spacer(1, 4 * mm)]
    recips = [['Recipient', 'Received']] + [
        [r.name, timezone.localtime(r.acknowledged_at).strftime('%d %b %Y %H:%M') if r.acknowledged_at else 'Not yet']
        for r in transmittal.recipients.all()]
    story += [_para('Recipients', s['Heading3']), _table(recips, [150 * mm, 60 * mm])]
    _doc(buf, f'Transmittal {transmittal.reference}').build(
        story, onFirstPage=_footer(transmittal.organization.name), onLaterPages=_footer(transmittal.organization.name))
    return buf.getvalue()
