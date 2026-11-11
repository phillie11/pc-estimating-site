"""
Background work for the drawing viewer. Each task runs inside its job's organisation and is safe to run twice.

- viewer.markup_export: the vector PDF of a drawing with its mark-ups (viewer/export.py). The drawing is only
  read once the virus check has cleared it; until then the task asks again a minute later, for up to two hours.
"""
from datetime import timedelta

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

import jobs
from projects.files import scan_status
from projects.models import FileScan
from tenancy import notify

from . import export
from .models import MarkupExport

SCAN_WAIT_TRIES = 120


def fail(row, why):
    MarkupExport.objects.filter(pk=row.pk, status=MarkupExport.QUEUED).update(status=MarkupExport.FAILED,
                                                                              error=why[:300])


def _gave_up(job):
    MarkupExport.objects.filter(pk=job.payload.get('id'), status=MarkupExport.QUEUED).update(
        status=MarkupExport.FAILED, error='The export could not be made. Try again, or contact support.')


@jobs.task(name='viewer.markup_export', max_attempts=2, on_dead=_gave_up)
def markup_export(payload):
    row = MarkupExport.objects.filter(pk=payload['id']).select_related(
        'revision__document', 'project', 'requested_by').first()
    if row is None or row.status != MarkupExport.QUEUED:
        return
    rev = row.revision
    status = scan_status(rev.file, row.organization)
    if status is None:
        tries = payload.get('tries', 0) + 1
        if tries <= SCAN_WAIT_TRIES:
            jobs.enqueue('viewer.markup_export', {**payload, 'tries': tries}, org=row.organization,
                         run_after=timezone.now() + timedelta(minutes=1))
        else:
            fail(row, 'The drawing was still waiting for the virus check after two hours.')
        return
    if status != FileScan.CLEAN:
        fail(row, 'This drawing did not pass the virus check, so it can\'t be exported.')
        return
    if (rev.size or 0) > export.MAX_FILE_MB * 1024 * 1024:
        fail(row, f'This drawing is over {export.MAX_FILE_MB} MB, so it can\'t be exported with mark-ups.')
        return
    try:
        with rev.file.open('rb') as fh:
            data, pages, drawn = export.run(row, fh)
    except export.ExportFailed as e:
        fail(row, str(e))
        return
    row.file.save(f'{row.pk}.pdf', ContentFile(data), save=False)
    row.status, row.size, row.pages, row.shapes_drawn = MarkupExport.READY, len(data), pages, drawn
    row.error, row.ready_at = '', timezone.now()
    row.save()
    url = reverse('markup_export_file', args=[row.organization.slug, row.project_id, row.document_id, rev.pk])
    notify.alert([row.requested_by], f'Your marked-up PDF is ready: {rev.document.reference or rev.document.title} '
                                     f'rev {rev.revision}', url, project=row.project, email=False)
