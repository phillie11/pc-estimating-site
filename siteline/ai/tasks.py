"""
The worker side of AI drafting. A page queues an AIRun and the job `ai.run`; `manage.py run_worker` runs it
inside the run's company (org_context), so every query in it is that company's only.

A call costs money, so a run is never repeated by accident: the job runs at most once in effect. If the worker
dies mid-call and the job is picked up again after JOBS_LOCK_TIMEOUT, the run is already marked running, so it
is marked "not done" with a plain message for the person to start again, rather than calling the model twice.
"""
import logging

from django.utils import timezone

import jobs

from .models import AIRun, CLARIFICATIONS, TENDER_BRIEF

logger = logging.getLogger(__name__)


def queue(run):
    jobs.enqueue('ai.run', {'run_id': run.pk}, org=run.organization, idempotency_key=f'ai-run:{run.pk}',
                 max_attempts=2)


def _finish(run, status, message):
    run.status, run.message, run.finished_at = status, message[:500], timezone.now()
    run.save(update_fields=['status', 'message', 'finished_at'])


@jobs.task(name='ai.run', max_attempts=2)
def run_job(payload):
    from . import service
    run = AIRun.objects.filter(pk=payload['run_id']).select_related('project', 'requested_by').first()
    if run is None or run.status in (AIRun.DONE, AIRun.FAILED):
        return
    if run.status == AIRun.RUNNING:
        _finish(run, AIRun.FAILED, 'This was interrupted before it finished. Start it again.')
        return
    if AIRun.objects.filter(pk=run.pk, status=AIRun.QUEUED).update(status=AIRun.RUNNING) != 1:
        return
    project = run.project
    try:
        if project.is_archived:
            raise service.Unavailable(f'{project.name} is archived.')
        if run.feature == CLARIFICATIONS:
            from handoverpack import access as pack_access
            from handoverpack import clarifications
            from handoverpack.models import HandoverPack
            pack = HandoverPack.objects.filter(project=project).select_related('workbook', 'project').first()
            if pack is None or not pack_access.can_edit(project):
                raise service.Unavailable('The handover pack can\'t be changed now.')
            n = clarifications.draft(pack, run.requested_by, run=run)
            message = (f'{n} draft clarification{"s" if n != 1 else ""} added for you to check.' if n else
                       'The AI model suggested nothing new.')
        elif run.feature == TENDER_BRIEF:
            from . import brief as briefs
            _, notes = briefs.read(project, run.requested_by, [int(i) for i in run.params.get('revisions', [])],
                                   run=run)
            message = ' '.join(['Bid brief drafted for you to check.', *notes])
        else:
            raise service.Unavailable('Unknown kind of draft.')
    except (service.Unavailable, service.CallFailed) as e:
        _finish(run, AIRun.FAILED, str(e))
        return
    except Exception:
        _finish(run, AIRun.FAILED, 'Something went wrong on Siteline\'s side. Nothing was saved. Try again later.')
        logger.exception('ai: run %s failed', run.pk)
        return
    _finish(run, AIRun.DONE, message)
