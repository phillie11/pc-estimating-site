"""
The fire-stopping register as rows, for the CSV download and for the close-out golden thread pack.

    from quality.exports import register
    header, rows = register(project)            # every penetration on the job, inside its org_context
    header, rows = register(project, queryset)  # a filtered list (the register page's CSV)

Rows are plain values (text, numbers, ISO dates); the caller makes CSV cells formula-safe. Only
staff should be given them: the register is internal (quality/access.py).
"""
from django.db.models import Count, Q
from django.utils import timezone

from .models import Penetration, QualityPhoto

HEADER = ['Ref', 'Where', 'Through', 'Fire rating (minutes)', 'What passes through', 'Size', 'System or product',
          'Manufacturer\'s detail', 'Installed by', 'Sealed on', 'Status', 'Inspected by', 'Inspected on',
          'Inspection note', 'Drawing', 'Drawing revision', 'Drawing page', 'Photos before', 'Photos after']


def register(project, queryset=None):
    qs = queryset if queryset is not None else Penetration.objects.filter(project=project)
    qs = qs.select_related('package__subcontractor', 'drawing', 'drawing_revision', 'inspected_by').annotate(
        x_before=Count('photos', filter=Q(photos__stage=QualityPhoto.BEFORE)),
        x_after=Count('photos', filter=Q(photos__stage=QualityPhoto.AFTER))).order_by('number')
    rows = []
    for p in qs:
        rows.append([
            p.ref, p.location, p.get_element_display(), p.fire_rating or '',
            p.get_service_display() if p.service else '', p.size, p.system, p.detail_reference, p.installed_by,
            p.installed_on.isoformat() if p.installed_on else '', p.get_status_display(),
            p.inspected_by.full_name if p.inspected_by_id else '',
            timezone.localdate(p.inspected_at).isoformat() if p.inspected_at else '', p.inspection_note,
            (p.drawing.reference or p.drawing.title) if p.drawing_id else '',
            p.drawing_revision.revision if p.drawing_revision_id else '', p.pin_page or '',
            p.x_before, p.x_after,
        ])
    return HEADER, rows
