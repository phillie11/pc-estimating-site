"""
Demo data for the drawing viewer, called by `seed_demo` inside the demo company's org_context
after documents/demo.py, on the Leeds ground floor GA (CLL-NLD-ZZ-00-DR-A-1001, current rev C01).

- Each GA revision gets a simple floor plan drawn with ReportLab, so revision compare has
  something to show: between P02 and C01 the kitchen/store partition moves, a door is added to
  the accessible WC and the bar servery gets longer.
- Pins: three snag pins and one RFI pin. One snag was pinned on rev P02, so it shows
  "placed on rev P02". Staff see all four; the joinery subcontractor (sub@) sees the skirting
  snag assigned to them and the shared RFI; the client sees the RFI only.
- Mark-ups: the PM's layer on C01 (clouds round the changes and a note), shared with everyone the
  drawing is shared with; the PM's earlier layer on P02, shared with the site team only; and the
  subcontractor's own private layer on C01 with a measurement.
"""
import hashlib
import io
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from projects.models import Document, Issue

from .models import DrawingPin, MarkupLayer

GA_NUMBER = 'CLL-NLD-ZZ-00-DR-A-1001'


def floor_plan(revision, later):
    """An A3 landscape ground floor plan at 1:50. `later` draws the C01 design changes."""
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    w, h = landscape(A3)
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setTitle(f'{GA_NUMBER} rev {revision}')
    # Grid
    c.setStrokeColorRGB(.75, .75, .75)
    c.setLineWidth(.5)
    for i, x in enumerate(range(80, 1000, 150)):
        c.line(x, 60, x, 700)
        c.drawCentredString(x, 710, chr(65 + i))
    for i, y in enumerate(range(100, 700, 150)):
        c.line(50, y, 1000, y)
        c.drawString(30, y - 3, str(i + 1))
    c.setStrokeColorRGB(0, 0, 0)
    # External walls
    c.setLineWidth(6)
    c.rect(80, 100, 900, 550)
    # Partitions
    c.setLineWidth(3)
    store_x = 760 if later else 700
    c.line(560, 100, 560, 650)            # bar / back of house
    c.line(560, 380, 980, 380)            # kitchen / WCs
    c.line(store_x, 380, store_x, 650)    # kitchen / store (moves in C01)
    c.line(80, 230, 560, 230)             # corridor C1
    c.line(780, 100, 780, 380)            # WC / accessible WC
    # Doors (a gap and a swing)
    c.setLineWidth(1.5)
    doors = [(560, 300, 'v'), (640, 380, 'h'), (300, 230, 'h')]
    if later:
        doors.append((860, 380, 'h'))     # new door to the accessible WC
    for x, y, d in doors:
        c.setStrokeColorRGB(1, 1, 1)
        c.setLineWidth(5)
        c.line(x, y, x + (0 if d == 'v' else 45), y + (45 if d == 'v' else 0))
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(1)
        c.arc(x - 45, y - 45, x + 45, y + 45, 0, 90)
    # Bar servery
    c.setLineWidth(2)
    c.rect(150, 420, 300 if later else 220, 50)
    # Room names
    c.setFont('Helvetica-Bold', 14)
    for x, y, name in [(260, 560, 'BAR'), (260, 160, 'CORRIDOR C1'), (620, 520, 'KITCHEN'),
                       ((store_x + 980) / 2 - 30, 520, 'STORE'), (640, 240, 'WC'), (840, 240, 'ACC. WC')]:
        c.drawString(x, y, name)
    c.setFont('Helvetica', 10)
    c.drawString(165, 440, 'Bar servery')
    # Title block
    c.setLineWidth(1)
    c.rect(1010, 60, 160, 640)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(1020, 670, 'Ground floor GA')
    c.setFont('Helvetica', 9)
    for i, line in enumerate([GA_NUMBER, 'Scale 1:50 @ A3', f'Revision {revision}',
                              'C01: store and WC changes' if later else 'For coordination',
                              'Demo drawing from seed_demo']):
        c.drawString(1020, 640 - i * 16, line)
    c.showPage()
    c.save()
    return buf.getvalue()


def _redraw_ga(doc):
    for rev in doc.revisions.all():
        body = floor_plan(rev.revision, later=rev.revision.startswith('C'))
        rev.file.save(f'{GA_NUMBER}_{rev.revision}.pdf', ContentFile(body), save=False)
        rev.size, rev.sha256, rev.page_count = len(body), hashlib.sha256(body).hexdigest(), 1
        rev.save(update_fields=['file', 'size', 'sha256', 'page_count'])


def seed(project, pm, sub_user, rfi):
    doc = Document.objects.filter(project=project, reference=GA_NUMBER).first()
    if doc is None:
        return []
    doc.scale = '1:50 @ A3'
    doc.save(update_fields=['scale'])
    _redraw_ga(doc)
    revs = {r.revision: r for r in doc.revisions.all()}
    current, earlier = revs.get('C01'), revs.get('P02') or revs.get('C01')
    skirting = Issue.objects.filter(project=project, title='Damaged skirting to corridor C1').first()
    cables = Issue.objects.filter(project=project, title='Trailing cables at kitchen entrance').first()
    tile = Issue.objects.create(project=project, title='Chipped floor tile at bar entrance', issue_type='SNAG',
                                location=f'Drawing {GA_NUMBER} rev C01, page 1 (bar entrance)', assignee=pm,
                                status=Issue.STATUS_READY, due_date=timezone.localdate() + timedelta(days=3),
                                created_by=pm)
    placed = [
        (skirting, None, earlier, '0.240000', '0.720000'),
        (cables, None, current, '0.560000', '0.400000'),
        (tile, None, current, '0.300000', '0.380000'),
        (None, rfi, current, '0.250000', '0.290000'),
    ]
    pins = []
    for issue, rfi_, rev, x, y in placed:
        if (issue is None and rfi_ is None) or rev is None:
            continue
        pins.append(DrawingPin.objects.create(
            project=project, document=doc, revision=rev, revision_code=rev.revision, page=1, x=Decimal(x),
            y=Decimal(y), issue=issue, rfi=rfi_, created_by=pm))
    if current is not None:
        _seed_layers(project, doc, current, earlier, pm, sub_user)
    return pins


def _layer(project, doc, rev, owner, share, shapes):
    return MarkupLayer.objects.create(project=project, document=doc, revision=rev, owner=owner, share=share,
                                      data={'v': 1, 'shapes': shapes}, shape_count=len(shapes))


def _seed_layers(project, doc, current, earlier, pm, sub_user):
    # Page fractions on the A3 sheet (1190.55 x 841.89 pt; y measured down from the top)
    _layer(project, doc, current, pm, MarkupLayer.EXTERNAL, [
        {'t': 'cloud', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.57, 0.18], 'b': [0.68, 0.56]},
        {'t': 'cloud', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.69, 0.50], 'b': [0.78, 0.58]},
        {'t': 'arrow', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.50, 0.10], 'b': [0.60, 0.20]},
        {'t': 'text', 'p': 1, 'c': 'red', 's': 16, 'at': [0.36, 0.07],
         'text': 'Store partition moved 60 mm on plan: check the extract duct route'},
        {'t': 'text', 'p': 1, 'c': 'red', 's': 14, 'at': [0.70, 0.60], 'text': 'New door to accessible WC'},
    ])
    if earlier is not None and earlier.pk != current.pk:
        _layer(project, doc, earlier, pm, MarkupLayer.TEAM, [
            {'t': 'rect', 'p': 1, 'c': 'blue', 'w': 2, 'a': [0.12, 0.27], 'b': [0.32, 0.36]},
            {'t': 'text', 'p': 1, 'c': 'blue', 's': 14, 'at': [0.12, 0.23], 'text': 'Servery too short for two tills?'},
        ])
    _layer(project, doc, current, sub_user, MarkupLayer.PRIVATE, [
        {'t': 'measure', 'p': 1, 'c': 'green', 'w': 3, 'a': [0.126, 0.47], 'b': [0.378, 0.47],
         'text': '5.29 m'},
        {'t': 'pen', 'p': 1, 'c': 'green', 'w': 3,
         'pts': [[0.13, 0.50], [0.17, 0.52], [0.22, 0.51], [0.27, 0.53], [0.31, 0.52], [0.37, 0.53]]},
    ])
