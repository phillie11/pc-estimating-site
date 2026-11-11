"""
The O&M manual's contents PDF and the handover pack's manifest PDF (ReportLab, as diary/pdf.py).
Everything a person typed goes through documents.pdf.markup(), so it is printed, never obeyed.
"""
import io
import re

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from documents.pdf import markup

GRID = colors.HexColor('#C9C4B8')
HEAD = colors.HexColor('#F1EEE6')
W = A4[0] - 24 * mm


def _styles():
    s = getSampleStyleSheet()
    s['Normal'].fontSize, s['Normal'].leading = 9, 11.5
    s.add(s['Normal'].clone('Small', fontSize=7.5, leading=9.5))
    s.add(s['Normal'].clone('Mono', fontName='Courier', fontSize=6.5, leading=8))
    s['Title'].fontSize, s['Title'].leading = 20, 24
    s['Heading2'].fontSize, s['Heading2'].spaceBefore, s['Heading2'].spaceAfter = 11, 8, 3
    return s


def _p(text, style):
    return Paragraph(markup(text), style)


def _table(rows, widths, s, mono_cols=(), header=True):
    body = []
    for i, row in enumerate(rows):
        body.append([_p(c, s['Mono'] if (i and j in mono_cols) else s['Small']) for j, c in enumerate(row)])
    t = Table(body, colWidths=widths, repeatRows=1 if header else 0, splitInRow=1)
    t.setStyle(TableStyle(([('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8), ('BACKGROUND', (0, 0), (-1, 0), HEAD)]
                           if header else []) + [
        ('GRID', (0, 0), (-1, -1), 0.4, GRID), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return t


def _doc(buf, title):
    return SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm,
                             bottomMargin=14 * mm, title=title)


def _footer(text):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, text)
        canvas.drawRightString(A4[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()
    return draw


def om_index(project, org_name, number, sections, left_out):
    """
    sections: [(number, title, contents lines, [(zip path, title, size, sha256)])]
    left_out: [(section title, file title, reason)]
    """
    buf = io.BytesIO()
    s = _styles()
    story = [Spacer(1, 30 * mm), _p('Operation and maintenance manual', s['Title']), Spacer(1, 4 * mm),
             _p(project.name, s['Heading2']), _p(project.site_address, s['Normal']), Spacer(1, 8 * mm),
             _p(f'Compiled by {org_name} on {timezone.localdate():%d %B %Y}. Version {number}.', s['Normal']),
             Spacer(1, 4 * mm),
             _p('This manual is a folder of files. Each section below is a folder in the download, and every file is '
                'listed with its SHA-256 checksum, so a copy can be checked against this index.', s['Normal']),
             PageBreak(), _p('Contents', s['Heading2'])]
    story.append(_table([['Section', 'What it covers', 'Files']] + [
        [f'{n:02d} {title}', '\n'.join(lines) or '-', str(len(files)) if files else 'None yet']
        for n, title, lines, files in sections], [55 * mm, W - 75 * mm, 20 * mm], s))
    for n, title, lines, files in sections:
        if not files:
            continue
        story += [_p(f'{n:02d} {title}', s['Heading2']),
                  _table([['File in the download', 'Document', 'Size', 'SHA-256']] + [
                      [path, label, _size(size), sha] for path, label, size, sha in files],
                      [55 * mm, 45 * mm, 16 * mm, W - 116 * mm], s, mono_cols=(3,))]
    if left_out:
        story += [_p('Not included', s['Heading2']),
                  _p('These files could not be added yet. Compile the manual again once they are cleared.', s['Normal']),
                  _table([['Section', 'File', 'Why']] + [list(x) for x in left_out],
                         [55 * mm, 70 * mm, W - 125 * mm], s)]
    _doc(buf, f'O&M manual - {project.name}').build(
        story, onFirstPage=_footer(f'{org_name} · O&M manual v{number}'),
        onLaterPages=_footer(f'{org_name} · {project.name} · O&M manual v{number}'))
    return buf.getvalue()


def om_front_matter(project, org, number, entries, left_out):
    """
    The cover page and contents page(s) of the one-PDF manual.
    entries: [(section number, section title, [(document title, kind, value)])] where kind is
      'page'      value is the page number in the merged PDF where the document starts
      'separate'  value is its path in the zip (not a PDF, so not merged)
      'skipped'   value is the plain reason it could not be merged
    left_out: [(section title, file title, reason)] the zip left out too (virus check, missing)
    """
    buf = io.BytesIO()
    s = _styles()
    accent = colors.HexColor(org.brand_colour if re.fullmatch(r'#[0-9A-Fa-f]{6}', org.brand_colour or '') else '#E8590C')
    s.add(s['Normal'].clone('Company', fontSize=13, leading=16, textColor=accent, fontName='Helvetica-Bold'))
    story = [Spacer(1, 40 * mm), _p(org.name, s['Company']), Spacer(1, 6 * mm),
             _p('Operation and maintenance manual', s['Title']), Spacer(1, 4 * mm),
             _p(project.name, s['Heading2']), _p(project.site_address, s['Normal']), Spacer(1, 8 * mm),
             _table([['Project reference', project.reference], ['Version', str(number)],
                     ['Date', f'{timezone.localdate():%d %B %Y}']], [45 * mm, W - 45 * mm], s, header=False),
             PageBreak(), _p('Contents', s['Heading2'])]
    for n, title, rows in entries:
        story.append(_p(f'{n:02d} {title}', s['Heading2']))
        if not rows:
            story.append(_p('No files in this section.', s['Small']))
            continue
        body = []
        for name, kind, value in rows:
            where = {'page': f'Page {value}', 'separate': f'Supplied separately, see the zip ({value})',
                     'skipped': f'Not included: {value}'}[kind]
            body.append([name, where])
        story.append(_table(body, [W - 85 * mm, 85 * mm], s, header=False))
    if left_out:
        story += [_p('Left out of the zip as well', s['Heading2']),
                  _p('These files had not cleared the virus check, or could not be found, when the manual was compiled.',
                     s['Normal']),
                  _table([['Section', 'File', 'Why']] + [list(x) for x in left_out],
                         [55 * mm, 70 * mm, W - 125 * mm], s)]

    def cover(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(accent)
        canvas.rect(0, A4[1] - 12 * mm, A4[0], 12 * mm, stroke=0, fill=1)
        canvas.restoreState()
    _doc(buf, f'O&M manual - {project.name}').build(
        story, onFirstPage=cover, onLaterPages=_footer(f'{org.name} · {project.name} · O&M manual v{number}'))
    return buf.getvalue()


def pack_manifest(pack, items, org_name):
    buf = io.BytesIO()
    s = _styles()
    project = pack.project
    when = pack.issued_at or pack.ready_at or pack.created_at
    story = [_p(f'Handover pack {pack.number}: {project.name}', s['Title']),
             _p(f'{org_name} · {timezone.localtime(when):%d %B %Y %H:%M}', s['Normal']), Spacer(1, 3 * mm),
             _p('Each item is the exact file handed over, with its SHA-256 checksum. To check a copy, work out its '
                'SHA-256 and compare it with the one below.', s['Normal']),
             _p('Advisory: this pack supports the golden thread of building information. Whether the Building '
                'Safety Act applies, and what the golden thread must hold, is for the client and their advisers.',
                s['Small']), Spacer(1, 3 * mm)]
    if pack.note:
        story += [_p(pack.note, s['Normal']), Spacer(1, 3 * mm)]
    story.append(_table([['#', 'Item', 'Ref / rev', 'File', 'SHA-256']] + [
        [str(i), f'{it.get_category_display()}: {it.title}', ' '.join(x for x in (it.reference, it.revision) if x),
         it.file_name, it.sha256 or it.get_state_display()] for i, it in enumerate(items, 1)],
        [8 * mm, 58 * mm, 28 * mm, 38 * mm, W - 132 * mm], s, mono_cols=(4,)))
    if pack.manifest_sha256:
        story += [Spacer(1, 3 * mm), _p(f'Manifest checksum (SHA-256 of the item list): {pack.manifest_sha256}',
                                        s['Small'])]
    _doc(buf, f'Handover pack {pack.number} - {project.name}').build(
        story, onFirstPage=_footer(f'{org_name} · handover pack {pack.number}'),
        onLaterPages=_footer(f'{org_name} · handover pack {pack.number}'))
    return buf.getvalue()


def _size(n):
    n = n or 0
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024
