"""
Background work for document control. Each task runs inside its job's organisation and is
safe to run twice.

- documents.transmittal_sheet: the PDF record of an issued transmittal.
- documents.file_facts: sha256 and page count for a stored revision or bulk-upload row.
  The file is only read once GuardDuty has cleared it (the bucket refuses to serve it
  before then), so until then the task re-queues itself a minute later, for up to two hours.
"""
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

import jobs
from projects.files import scan_status
from projects.models import DocumentRevision, FileScan

from .models import Transmittal, UploadRow

SCAN_WAIT_TRIES = 120


@jobs.task(name='documents.transmittal_sheet', max_attempts=5)
def transmittal_sheet(payload):
    from .pdf import transmittal_sheet as build
    tr = Transmittal.objects.filter(pk=payload['transmittal_id']).first()
    if tr is None or not tr.issued_at:
        return
    if tr.sheet and tr.sheet_generated_at and tr.sheet_generated_at >= tr.issued_at:
        return  # already made: a second run changes nothing
    pdf = build(tr)
    tr.sheet.save(f'{tr.reference}.pdf', ContentFile(pdf), save=False)
    tr.sheet_generated_at = timezone.now()
    tr.save(actor=None, audit_action='sheet generated', update_fields=['sheet', 'sheet_generated_at'])


@jobs.task(name='documents.file_facts', max_attempts=3)
def file_facts(payload):
    from .services import file_facts as facts
    model = {'revision': DocumentRevision, 'row': UploadRow}[payload['kind']]
    obj = model.objects.filter(pk=payload['id']).first()
    if obj is None or (obj.sha256 and (obj.page_count is not None or not obj.file.name.lower().endswith('.pdf'))):
        return
    status = scan_status(obj.file, obj.organization)
    if status is None:
        tries = payload.get('tries', 0) + 1
        if tries <= SCAN_WAIT_TRIES:
            jobs.enqueue('documents.file_facts', {**payload, 'tries': tries}, org=obj.organization,
                         run_after=timezone.now() + timedelta(minutes=1))
        return
    if status != FileScan.CLEAN:
        return  # blocked: never read it
    with obj.file.open('rb') as fh:
        sha, pages = facts(fh, obj.file.name)
    fields = {'sha256': sha, 'page_count': pages}
    if model is UploadRow:
        fields['processed'] = True
    model.objects.filter(pk=obj.pk).update(**fields)
    if model is UploadRow and obj.committed_revision_id:
        DocumentRevision.objects.filter(pk=obj.committed_revision_id, sha256='').update(sha256=sha, page_count=pages)
