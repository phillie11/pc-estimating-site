"""
Fire-stopping penetrations on drawings, following the viewer's pin pattern (viewer/pins.py): the
penetration form is opened from the drawing viewer with ?pin_doc=&pin_rev=&pin_page=&pin_x=&pin_y=,
checked by viewer.pins.pending_pin(), and the position is stored on the penetration itself (the
viewer's DrawingPin links only snags and RFIs). The viewer shows them through viewer_pins() below,
alongside its own pins, for staff who can use Quality on the job.
"""
from django.urls import reverse

from .access import can_use_quality
from .models import Penetration


def apply_pending(penetration, pending):
    """Copy a checked viewer position (viewer.pins.PendingPin) onto a penetration (not saved)."""
    penetration.drawing, penetration.drawing_revision = pending.doc, pending.rev
    penetration.pin_page, penetration.pin_x, penetration.pin_y = pending.page, pending.x, pending.y


def viewer_url(request, penetration):
    """The viewer on the revision it was pinned on, centred on it; '' if it isn't pinned."""
    if not penetration.is_pinned or penetration.drawing_revision_id is None:
        return ''
    url = reverse('drawing_viewer', args=[request.org.slug, penetration.project_id, penetration.drawing_id,
                                          penetration.drawing_revision_id])
    return f'{url}?pin=fs-{penetration.pk}'


def new_url(request, project):
    """'New fire-stopping' from the viewer's pin dialog, for staff on the job; '' for anyone else."""
    if project.is_archived or not can_use_quality(request, project):
        return ''
    return reverse('penetration_create', args=[request.org.slug, project.pk])


def viewer_pins(request, project, doc, rev=None):
    """The job's penetrations pinned on this drawing, in the shape viewer.pins.describe() gives."""
    if not can_use_quality(request, project):
        return []
    out = []
    for p in Penetration.objects.filter(project=project, drawing=doc, pin_page__isnull=False) \
            .select_related('drawing_revision'):
        other = rev is not None and p.drawing_revision_id != rev.pk
        if other:
            placed_on = f'placed on rev {p.drawing_revision.revision}' if p.drawing_revision_id \
                else 'placed on another revision'
        else:
            placed_on = ''
        out.append({
            'id': f'fs-{p.pk}', 'kind': 'firestopping', 'kind_label': 'Fire-stopping', 'ref': p.ref,
            'title': p.title, 'status': p.get_status_display(), 'tone': p.status_tone, 'url': p.get_absolute_url(),
            'page': p.pin_page, 'x': float(p.pin_x), 'y': float(p.pin_y), 'placed_on': placed_on,
            'can_remove': False, 'remove_url': '',
        })
    return out
