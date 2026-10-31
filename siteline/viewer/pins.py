"""
Making and describing pins, shared by the viewer's own endpoints and the snag and RFI create
forms (which the viewer opens with ?pin_doc=&pin_rev=&pin_page=&pin_x=&pin_y= in the URL).
"""
from dataclasses import dataclass
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.http import Http404
from django.urls import reverse

from projects.models import Document, DocumentRevision

from .access import can_remove, can_view, get_doc_and_revision
from .models import DrawingPin, InvalidPin, PinLocked, parse_coord, parse_page

PIN_PARAMS = ('pin_doc', 'pin_rev', 'pin_page', 'pin_x', 'pin_y')


def viewer_url(request, project, doc, rev, pin=None):
    url = reverse('drawing_viewer', args=[request.org.slug, project.pk, doc.pk, rev.pk])
    return f'{url}?{urlencode({"pin": pin.pk})}' if pin is not None else url


def placement(request, project, doc_id, rev_id, page, x, y):
    """
    Check a position someone wants to pin at: a revision they may view (not withdrawn), a page in it
    and a point on the page. The viewer's own pin endpoint and the create forms both come here. Returns (doc, rev, page, x, y); raises Http404 for a revision they can't see and
    InvalidPin for a bad position.
    """
    doc, rev = get_doc_and_revision(request, project, doc_id, rev_id)
    if not can_view(request, rev):
        raise Http404
    if rev.is_withdrawn:
        raise InvalidPin('This revision has been withdrawn, so nothing can be pinned to it.')
    return doc, rev, parse_page(page, rev.page_count), parse_coord(x), parse_coord(y)


@dataclass
class PendingPin:
    """Where a snag or RFI being raised from the viewer will be pinned."""
    doc: Document
    rev: DocumentRevision
    page: int
    x: object
    y: object

    @property
    def label(self):
        return f'{self.doc.reference or self.doc.title} rev {self.rev.revision}'

    @property
    def location(self):
        return f'Drawing {self.label}, page {self.page}'

    def query(self):
        return urlencode({'pin_doc': self.doc.pk, 'pin_rev': self.rev.pk, 'pin_page': self.page,
                          'pin_x': self.x, 'pin_y': self.y})


def pending_pin(request, project):
    """The pin a create form was opened for, checked against what this person may see; None if none or invalid."""
    values = [request.GET.get(k, '') for k in PIN_PARAMS]
    if not all(values):
        return None
    doc_id, rev_id = values[0], values[1]
    if not (doc_id.isdigit() and rev_id.isdigit()):
        return None
    try:
        doc, rev, page, x, y = placement(request, project, int(doc_id), int(rev_id), *values[2:])
    except (Http404, InvalidPin):
        return None
    return PendingPin(doc, rev, page, x, y)


def create_pin(request, project, doc, rev, page, x, y, issue=None, rfi=None):
    """
    Pin a record. One already pinned on this drawing is moved to the new place rather than pinned
    twice, but only by someone who could remove that pin (staff, or whoever placed it): otherwise
    PinLocked. Two people pinning the same record at once: the unique constraint stops the second
    insert, which then becomes a move under the same rule.
    """
    record = {'issue': issue} if issue is not None else {'rfi': rfi}
    pin = DrawingPin.objects.filter(document=doc, **record).first()
    if pin is None:
        pin = DrawingPin(project=project, document=doc, created_by=request.user, revision=rev,
                         revision_code=rev.revision, page=page, x=x, y=y, **record)
        try:
            with transaction.atomic():
                pin.save()
            return pin
        except IntegrityError:
            pin = DrawingPin.objects.filter(document=doc, **record).first()
            if pin is None:
                raise
    if not can_remove(request, pin):
        raise PinLocked(PinLocked.message)
    pin.revision, pin.revision_code, pin.page, pin.x, pin.y = rev, rev.revision, page, x, y
    pin.save()
    return pin


def attach_pending(request, project, pending, issue=None, rfi=None):
    return create_pin(request, project, pending.doc, pending.rev, pending.page, pending.x, pending.y,
                      issue=issue, rfi=rfi)


def describe(request, project, pin, rev=None, seen=None):
    """What the viewer needs to draw a pin. `rev` is the revision on screen; `seen` the revision ids an external may see."""
    rec = pin.record
    other = rev is not None and pin.revision_id != rev.pk
    placed_on = ''
    if other:
        if pin.revision_id and (seen is None or pin.revision_id in seen):
            placed_on = f'placed on rev {pin.revision_code}'
        else:
            # Don't name a revision this person was never issued
            placed_on = 'placed on another revision'
    return {
        'id': pin.pk,
        'kind': pin.kind,
        'kind_label': 'Snag' if pin.issue_id else 'RFI',
        'ref': rec.reference,
        'title': rec.title,
        'status': rec.get_status_display(),
        'tone': rec.status_tone,
        'url': rec.get_absolute_url() if pin.rfi_id else reverse(
            'issue_detail', args=[request.org.slug, project.pk, pin.issue_id]),
        'page': pin.page,
        'x': float(pin.x),
        'y': float(pin.y),
        'placed_on': placed_on,
        'can_remove': can_remove(request, pin),
        'remove_url': reverse('pin_delete', args=[request.org.slug, project.pk, pin.document_id, pin.pk]),
    }


def record_choices(request, project, issues, rfis):
    """The existing snags and RFIs this person could pin, for the viewer's "link existing" list."""
    out = [{'value': f'issue:{i.pk}', 'label': f'{i.reference} {i.title}', 'group': 'Snags'} for i in issues]
    out += [{'value': f'rfi:{r.pk}', 'label': f'{r.reference} {r.title}', 'group': 'RFIs'} for r in rfis]
    return out

