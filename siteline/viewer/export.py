"""
Vector export of mark-ups: the drawing's own PDF pages with each mark-up shape drawn on them as real PDF
lines, curves, filled arrow heads and text, not as a picture (pypdf, with the drawing operators written here).

WHERE A SHAPE GOES. Stored positions (viewer/markup.py) are fractions of the page as the viewer shows it,
from 0 to 1 across and down, origin top left, and line weight and text size are thousandths of the page
width. pdf.js shows a page cropped to its CropBox (inside the MediaBox) and turned by its /Rotate, so
`page_matrix()` does the same: it maps a point measured in points from the top left of the displayed page
(y down) to the page's own coordinates, for /Rotate 0, 90, 180 and 270 and for any page size or origin.
Nothing is guessed: if the page box is empty the page is left as it was.

WHAT DIFFERS FROM THE SCREEN, and why. The browser draws with the system font and soft white halos; a PDF
has only its standard fonts here, so notes and measurement labels are Helvetica Bold (text outside Latin-1
becomes "?"), halos are solid white, and the vertical position of a note's first line is set from the font
size (top of the text at the stored point, baseline 0.8 of the size below it). Positions, line weights, box
and cloud outlines, arrow heads and measurement ticks follow the same formulas as viewer/static/viewer/markup.js.

HOSTILE FILES AND TEXT. The drawing is read like any uploaded PDF: only once the virus check has cleared it
(viewer.tasks), up to MAX_FILE_MB and MAX_PAGES, encrypted files refused, a deadline checked per page. Pages are
copied into a new document, so open actions, scripts, attachments and outlines are not carried over, and link
actions, additional actions, form fields and attachment, sound, movie, screen, 3D and rich-media annotations
are removed from each page. Shapes are checked again (markup.clean_shapes) and every number is clamped; text is written
as a hex string, so nothing in a label can be read as PDF syntax, and only a fixed set of drawing operators
is ever written (the tests check this).
"""
import io
import math
import time

import pypdf
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
from reportlab.pdfbase.pdfmetrics import stringWidth

from . import markup

MAX_FILE_MB = 100
MAX_PAGES = 300
MAX_SECONDS = 5 * 60
FONT_NAME = '/SLMarkupHB'
FONT = 'Helvetica-Bold'
RGB = {'red': '#D7263D', 'blue': '#1F5FD1', 'green': '#1E8C45', 'orange': '#E8590C', 'purple': '#7A3CC2',
       'black': '#15191F'}
KAPPA = 0.5522847498  # a quarter circle as a Bezier curve
STRIPPED_ANNOTATIONS = ('/FileAttachment', '/Sound', '/Movie', '/Screen', '/RichMedia', '/3D', '/Widget')


class ExportFailed(Exception):
    """A drawing that can't be exported: the message is shown to the person as it stands."""


# ---------------------------------------------------------------------------
# Where the displayed page sits on the page itself
# ---------------------------------------------------------------------------

def page_box(media, crop):
    """(x0, y0, x1, y1) of what the viewer shows: the CropBox inside the MediaBox, or the MediaBox. None if empty."""
    def norm(r):
        if r is None:
            return None
        x0, x1 = sorted((float(r[0]), float(r[2])))
        y0, y1 = sorted((float(r[1]), float(r[3])))
        return (x0, y0, x1, y1) if all(map(math.isfinite, (x0, y0, x1, y1))) else None
    media, crop = norm(media), norm(crop)
    box = media
    if crop is not None:
        box = crop if media is None else (max(crop[0], media[0]), max(crop[1], media[1]),
                                          min(crop[2], media[2]), min(crop[3], media[3]))
    if box is None or box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        box = media
    if box is None or box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        return None
    return box


def displayed_size(box, rotation):
    w, h = box[2] - box[0], box[3] - box[1]
    return (h, w) if rotation in (90, 270) else (w, h)


def page_matrix(box, rotation):
    """
    (a, b, c, d, e, f) of a PDF `cm`: displayed (dx, dy), in points from the top left and y down, to the
    page's coordinates (x = a*dx + c*dy + e, y = b*dx + d*dy + f). The page is turned clockwise by `rotation`.
    """
    x0, y0, x1, y1 = box
    return {0: (1, 0, 0, -1, x0, y1), 90: (0, 1, 1, 0, x0, y0),
            180: (-1, 0, 0, 1, x1, y0), 270: (0, -1, -1, 0, x1, y1)}[rotation]


def rotation_of(page):
    try:
        value = int(page.get('/Rotate', 0) or 0)
    except (TypeError, ValueError):
        return 0
    value %= 360
    return value if value in (0, 90, 180, 270) else 0


# ---------------------------------------------------------------------------
# The drawing operators
# ---------------------------------------------------------------------------

def _n(value):
    value = float(value)
    return f'{value:.3f}' if math.isfinite(value) else '0'


def _unit(value):
    """A stored position, clamped to the page."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0


def _text(value):
    """A label as a PDF hex string: Latin-1/Windows text, anything else as "?", never any PDF syntax."""
    try:
        return '<' + markup.clean_text(value, required=False).encode('cp1252', 'replace').hex().upper() + '>'
    except markup.InvalidMarkup:
        return '<>'


def _colour(name):
    hexa = RGB.get(name, RGB['red'])
    r, g, b = (int(hexa[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return f'{_n(r)} {_n(g)} {_n(b)}'


def _label_size(w):
    return max(11, 16 * w / 1000)


def _line(ax, ay, bx, by):
    return [f'{_n(ax)} {_n(ay)} m {_n(bx)} {_n(by)} l S']


def _arrow_head(ax, ay, bx, by, width):
    ang, length = math.atan2(by - ay, bx - ax), max(10, width * 4)
    p1 = (bx - length * math.cos(ang - 0.45), by - length * math.sin(ang - 0.45))
    p2 = (bx - length * math.cos(ang + 0.45), by - length * math.sin(ang + 0.45))
    return [f'{_n(bx)} {_n(by)} m {_n(p1[0])} {_n(p1[1])} l {_n(p2[0])} {_n(p2[1])} l h f']


def _tick(ax, ay, bx, by, x, y, width):
    ang, length = math.atan2(by - ay, bx - ax) + math.pi / 2, max(6, width * 3)
    return [f'{_n(x - length * math.cos(ang))} {_n(y - length * math.sin(ang))} m '
            f'{_n(x + length * math.cos(ang))} {_n(y + length * math.sin(ang))} l S']


def _text_ops(text, x, y, size, colour, halo):
    """Text with its first baseline at y (display space is y down, so the text matrix flips it upright)."""
    ops = ['BT', f'{FONT_NAME} {_n(size)} Tf', f'1 0 0 -1 {_n(x)} {_n(y)} Tm']
    if halo:
        ops += ['1 Tr', '1 1 1 RG', f'{_n(max(2, size / 5))} w', f'{text} Tj', '0 Tr']
    ops += [f'{colour} rg', f'{text} Tj', 'ET']
    return ops


def _cloud(x, y, w, h, scallop):
    if w < 2 or h < 2:
        return [f'{_n(x)} {_n(y)} {_n(w)} {_n(h)} re S']
    ops = []
    for (ex, ey, fx, fy) in ((x, y, x + w, y), (x + w, y, x + w, y + h), (x + w, y + h, x, y + h), (x, y + h, x, y)):
        length = math.hypot(fx - ex, fy - ey)
        n = max(1, round(length / (scallop * 2)))
        ang, step = math.atan2(fy - ey, fx - ex), length / n
        rad = step / 2
        ux, uy, nx, ny = math.cos(ang), math.sin(ang), math.sin(ang), -math.cos(ang)
        for i in range(n):
            cx, cy = ex + ux * (step * i + rad), ey + uy * (step * i + rad)
            p0, q, p1 = (cx - ux * rad, cy - uy * rad), (cx + nx * rad, cy + ny * rad), (cx + ux * rad, cy + uy * rad)
            k = KAPPA * rad
            ops.append(f'{_n(p0[0])} {_n(p0[1])} m '
                       f'{_n(p0[0] + nx * k)} {_n(p0[1] + ny * k)} {_n(q[0] - ux * k)} {_n(q[1] - uy * k)} '
                       f'{_n(q[0])} {_n(q[1])} c '
                       f'{_n(q[0] + ux * k)} {_n(q[1] + uy * k)} {_n(p1[0] + nx * k)} {_n(p1[1] + ny * k)} '
                       f'{_n(p1[0])} {_n(p1[1])} c')
    ops.append('S')
    return ops


def shape_ops(s, w, h):
    """The operators for one shape on a displayed page w by h points (display space, y down)."""
    kind = s.get('t')
    colour = _colour(s.get('c'))
    width = max(1, (s.get('w') or 3) * w / 1000)
    ops = [f'{colour} RG {colour} rg {_n(width)} w']

    def X(pt):
        return _unit(pt[0]) * w

    def Y(pt):
        return _unit(pt[1]) * h
    try:
        if kind == 'pen':
            pts = s['pts']
            ops.append(f'{_n(X(pts[0]))} {_n(Y(pts[0]))} m')
            ops += [f'{_n(X(p))} {_n(Y(p))} l' for p in pts[1:]]
            if len(pts) == 1:
                ops.append(f'{_n(X(pts[0]) + 0.1)} {_n(Y(pts[0]))} l')
            ops.append('S')
        elif kind in ('line', 'arrow', 'measure'):
            ax, ay, bx, by = X(s['a']), Y(s['a']), X(s['b']), Y(s['b'])
            ops += _line(ax, ay, bx, by)
            if kind == 'arrow':
                ops += _arrow_head(ax, ay, bx, by, width)
            if kind == 'measure':
                ops += _tick(ax, ay, bx, by, ax, ay, width) + _tick(ax, ay, bx, by, bx, by, width)
                if s.get('text'):
                    size = _label_size(w)
                    text = _text(s['text'])
                    shown = markup.clean_text(s['text'], required=False)
                    tw = stringWidth(shown.encode('cp1252', 'replace').decode('cp1252'), FONT, size)
                    pad, cx, cy = size * 0.35, (ax + bx) / 2, (ay + by) / 2
                    box = f'{_n(cx - tw / 2 - pad)} {_n(cy - size * 0.7)} {_n(tw + pad * 2)} {_n(size * 1.4)} re'
                    ops += ['q', '1 1 1 rg', f'{box} f', f'{colour} RG 1 w', f'{box} S', 'Q']
                    ops += _text_ops(text, cx - tw / 2, cy + size * 0.35, size, colour, halo=False)
        elif kind in ('rect', 'cloud'):
            x0, y0 = min(X(s['a']), X(s['b'])), min(Y(s['a']), Y(s['b']))
            bw, bh = abs(X(s['a']) - X(s['b'])), abs(Y(s['a']) - Y(s['b']))
            ops += _cloud(x0, y0, bw, bh, max(6, 14 * w / 1000)) if kind == 'cloud' else [
                f'{_n(x0)} {_n(y0)} {_n(bw)} {_n(bh)} re S']
        elif kind == 'text':
            size = max(8, (s.get('s') or 18) * w / 1000)
            ops += _text_ops(_text(s['text']), X(s['at']), Y(s['at']) + size * 0.8, size, colour, halo=True)
        else:
            return []
    except (KeyError, IndexError, TypeError, ValueError):
        return []  # a shape that isn't the shape we store is left out, never half drawn
    return ops


def page_ops(shapes, w, h):
    """All of one page's shapes, in order, as the bytes of a content stream in display space."""
    ops = ['1 J 1 j']
    drawn = 0
    for s in shapes:
        one = shape_ops(s, w, h)
        if one:
            ops += ['q'] + one + ['Q']
            drawn += 1
    return ('\n'.join(ops) + '\n').encode('ascii'), drawn


def overlay(shapes, box, rotation):
    """(content stream bytes, shapes drawn) for a page: the displayed-space drawing under one `cm`."""
    w, h = displayed_size(box, rotation)
    body, drawn = page_ops(shapes, w, h)
    cm = ' '.join(_n(v) for v in page_matrix(box, rotation))
    return f'q\n{cm} cm\n'.encode('ascii') + body + b'Q\n', drawn


# ---------------------------------------------------------------------------
# Putting it on the pages
# ---------------------------------------------------------------------------

def _clean_page(page):
    """Take the parts of a page that can act on the reader out of the copy."""
    for key in ('/AA',):
        if key in page:
            del page[key]
    annots = page.get('/Annots')
    if annots is None:
        return
    keep = ArrayObject()
    for ref in annots:
        try:
            annot = ref.get_object()
            if annot.get('/Subtype') in STRIPPED_ANNOTATIONS:
                continue
            for key in ('/A', '/AA'):
                if key in annot:
                    del annot[key]
            keep.append(ref)
        except Exception:  # noqa: BLE001 - an annotation we can't read is left out
            continue
    page[NameObject('/Annots')] = keep


def _stamp(writer, page, content):
    """Append `content` after the page's own drawing (which is wrapped in q ... Q so it can't disturb it)."""
    raw = page.raw_get('/Contents') if '/Contents' in page else None
    existing = raw.get_object() if raw is not None else None
    parts = list(existing) if isinstance(existing, ArrayObject) else ([raw] if raw is not None else [])
    q, qq = DecodedStreamObject(), DecodedStreamObject()
    q.set_data(b'q\n')
    qq.set_data(b'\nQ\n')
    ours = DecodedStreamObject()
    ours.set_data(content)
    page[NameObject('/Contents')] = ArrayObject([writer._add_object(q), *parts, writer._add_object(qq),
                                                 writer._add_object(ours)])
    # Our own copies of the resources and the font list: pages often share them, and only this page needs our font
    resources = DictionaryObject(page['/Resources'].get_object() if '/Resources' in page else {})
    fonts = DictionaryObject(resources['/Font'].get_object() if '/Font' in resources else {})
    resources[NameObject('/Font')] = fonts
    page[NameObject('/Resources')] = resources
    fonts[NameObject(FONT_NAME)] = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica-Bold'), NameObject('/Encoding'): NameObject('/WinAnsiEncoding')})


def export_pdf(source, shapes, out, now=time.monotonic):
    """
    Write the drawing in `source` (an open binary file) to `out` with `shapes` drawn on it.
    Returns (pages, shapes drawn). Raises ExportFailed with a plain reason.
    """
    deadline = now() + MAX_SECONDS
    by_page = {}
    for s in shapes:
        by_page.setdefault(s['p'], []).append(s)
    try:
        with pypdf.apply_configuration(jbig2dec_binary=None):
            reader = pypdf.PdfReader(source, strict=False)
            if reader.is_encrypted:
                raise ExportFailed('This drawing is password protected, so mark-ups can\'t be drawn on it.')
            count = len(reader.pages)
            if count == 0:
                raise ExportFailed('This drawing has no pages.')
            if count > MAX_PAGES:
                raise ExportFailed(f'This drawing has more than {MAX_PAGES} pages. Use the picture or one-page PDF '
                                   'export in the viewer instead.')
            writer = pypdf.PdfWriter()
            drawn_total = 0
            for index in range(count):
                if now() >= deadline:
                    raise ExportFailed('The export took too long. Try again, or use the one-page export in the viewer.')
                page = writer.add_page(reader.pages[index])
                _clean_page(page)
                mine = by_page.get(index + 1)
                if not mine:
                    continue
                box = page_box(page.get('/MediaBox'), page.get('/CropBox'))
                if box is None:
                    continue
                content, drawn = overlay(mine, box, rotation_of(page))
                if drawn:
                    _stamp(writer, page, content)
                    drawn_total += drawn
            writer.add_metadata({'/Title': 'Drawing with mark-ups'})
            writer.write(out)
            return count, drawn_total
    except ExportFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - a hostile file can fail in any way
        raise ExportFailed('This drawing could not be read, so the mark-ups could not be drawn on it.') from exc


def check(shapes):
    """The shapes as stored, checked again before they are drawn: [] for anything that isn't valid."""
    try:
        return markup.clean_shapes(shapes)
    except markup.InvalidMarkup:
        return []


def run(export, source):
    """Export `source` for a MarkupExport row and return the bytes."""
    out = io.BytesIO()
    pages, drawn = export_pdf(source, check(export.shapes), out)
    return out.getvalue(), pages, drawn
