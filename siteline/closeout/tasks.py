"""
Background work for close-out, for manuals and packs too big to build during the request. Each
task runs inside its job's organisation and is safe to run twice.

- closeout.compile_om: build an O&M manual zip (closeout.services.compile_om).
- closeout.merge_om_pdf: the same manual as one merged PDF with bookmarks (closeout/merge.py), made on request.
- closeout.pack_checksums: work out a handover pack's checksums. Files still being virus-checked
  are tried again a minute later, for up to two hours; then the pack is marked as failed.
"""
from datetime import timedelta

from django.utils import timezone

import jobs

from .models import HandoverPack, OMCompilation

SCAN_WAIT_TRIES = 120


def _om_gave_up(job):
    """The worker gave up: say so on the page rather than leaving it "being put together"."""
    OMCompilation.objects.filter(pk=job.payload.get('id'), status=OMCompilation.QUEUED).update(
        status=OMCompilation.FAILED, error='The manual could not be put together. Try again, or contact support.')


@jobs.task(name='closeout.compile_om', max_attempts=3, on_dead=_om_gave_up)
def compile_om(payload):
    from . import services
    comp = OMCompilation.objects.filter(pk=payload['id']).first()
    if comp is None or comp.status != OMCompilation.QUEUED:
        return
    services.compile_om(comp)


def _merge_gave_up(job):
    from . import merge
    merge.gave_up(job.payload.get('id'))


@jobs.task(name='closeout.merge_om_pdf', max_attempts=3, on_dead=_merge_gave_up)
def merge_om_pdf(payload):
    from . import merge
    comp = OMCompilation.objects.filter(pk=payload['id']).first()
    if comp is None or comp.merged_status != OMCompilation.MERGE_QUEUED:
        return
    merge.merge_om(comp)


@jobs.task(name='closeout.pack_checksums', max_attempts=3)
def pack_checksums(payload):
    from . import services
    pack = HandoverPack.objects.filter(pk=payload['id']).first()
    if pack is None or pack.status != HandoverPack.BUILDING:
        return
    if services.checksum_pack(pack):
        return
    tries = payload.get('tries', 0) + 1
    if tries > SCAN_WAIT_TRIES:
        HandoverPack.objects.filter(pk=pack.pk).update(
            status=HandoverPack.FAILED, error='Some files were still waiting for the virus check after two hours.')
        return
    jobs.enqueue('closeout.pack_checksums', {**payload, 'tries': tries}, org=pack.organization,
                 run_after=timezone.now() + timedelta(minutes=1))
