"""
Demo pins for the drawing viewer, called by `seed_demo` inside the demo company's org_context
after documents/demo.py: on the Leeds ground floor GA (CLL-NLD-ZZ-00-DR-A-1001, current rev C01)
three snag pins and one RFI pin. One snag was pinned on rev P02, so it shows "placed on rev P02".

Who sees which: staff see all four; the joinery subcontractor (sub@) sees the skirting snag
assigned to them and the shared RFI; the client sees the RFI only.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from projects.models import Document, Issue

from .models import DrawingPin

GA_NUMBER = 'CLL-NLD-ZZ-00-DR-A-1001'


def seed(project, pm, sub_user, rfi):
    doc = Document.objects.filter(project=project, reference=GA_NUMBER).first()
    if doc is None:
        return []
    revs = {r.revision: r for r in doc.revisions.all()}
    current, earlier = revs.get('C01'), revs.get('P02') or revs.get('C01')
    skirting = Issue.objects.filter(project=project, title='Damaged skirting to corridor C1').first()
    cables = Issue.objects.filter(project=project, title='Trailing cables at kitchen entrance').first()
    tile = Issue.objects.create(project=project, title='Chipped floor tile at bar entrance', issue_type='SNAG',
                                location=f'Drawing {GA_NUMBER} rev C01, page 1 (bar entrance)', assignee=pm,
                                status=Issue.STATUS_READY, due_date=timezone.localdate() + timedelta(days=3),
                                created_by=pm)
    placed = [
        (skirting, None, earlier, '0.240000', '0.420000'),
        (cables, None, current, '0.700000', '0.300000'),
        (tile, None, current, '0.520000', '0.640000'),
        (None, rfi, current, '0.380000', '0.250000'),
    ]
    pins = []
    for issue, rfi_, rev, x, y in placed:
        if (issue is None and rfi_ is None) or rev is None:
            continue
        pins.append(DrawingPin.objects.create(
            project=project, document=doc, revision=rev, revision_code=rev.revision, page=1, x=Decimal(x),
            y=Decimal(y), issue=issue, rfi=rfi_, created_by=pm))
    return pins
