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

from .models import AIRun, CLARIFICATIONS, QUOTE_MAPPING, RFI_DRAFTING, TENDER_BRIEF

logger = logging.getLogger(__name__)


def queue(run):
    jobs.enqueue('ai.run', {'run_id': run.pk}, org=run.organization, idempotency_key=f'ai-run:{run.pk}',
                 max_attempts=2)


def _stand_in(run):
    """A request-shaped object for the requester's current, active membership here, or None if they have none."""
    from types import SimpleNamespace

    from tenancy.models import Membership
    user = run.requested_by
    m = Membership.objects.filter(organization=run.organization, user=user, is_active=True).first() if user else None
    if m is None or not m.is_internal:
        return None
    return SimpleNamespace(user=user, membership=m, org=run.organization)


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
        stand_in = _stand_in(run)  # the requester as they are now, not as they were when they pressed the button
        if run.feature == CLARIFICATIONS:
            from handoverpack import access as pack_access
            from handoverpack import clarifications
            from handoverpack.models import HandoverPack
            pack = HandoverPack.objects.filter(project=project).select_related('workbook', 'project').first()
            if pack is None or not pack_access.can_edit(project):
                raise service.Unavailable('The handover pack can\'t be changed now.')
            if stand_in is None or not pack_access.can_see(stand_in, project):
                raise service.Unavailable('You can no longer open this handover pack, so nothing was drafted.')
            n = clarifications.draft(pack, run.requested_by, run=run)
            message = (f'{n} draft clarification{"s" if n != 1 else ""} added for you to check.' if n else
                       'The AI model suggested nothing new.')
        elif run.feature == TENDER_BRIEF:
            from . import access, brief as briefs
            if not access.can_edit_brief(project):
                raise service.Unavailable(f'{project.name} is no longer an open bid, so its documents weren\'t read.')
            if stand_in is None or not stand_in.membership.can_estimate:
                raise service.Unavailable('You can no longer work on this bid, so its documents weren\'t read.')
            _, notes = briefs.read(project, run.requested_by, [int(i) for i in run.params.get('revisions', [])],
                                   run=run)
            message = ' '.join(['Bid brief drafted for you to check.', *notes])
        elif run.feature == RFI_DRAFTING:
            from . import access, rfis
            if not access.can_edit_brief(project):
                raise service.Unavailable(f'{project.name} is no longer an open bid, so no RFIs were drafted.')
            if stand_in is None or not stand_in.membership.can_estimate:
                raise service.Unavailable('You can no longer work on this bid, so no RFIs were drafted.')
            saved, notes = rfis.draft(project, run.requested_by, [int(i) for i in run.params.get('revisions', [])],
                                      use_brief=bool(run.params.get('use_brief')), run=run)
            n = len(saved)
            message = ' '.join([(f'{n} draft RFI{"s" if n != 1 else ""} added, internal, for you to check and issue.'
                                 if n else 'The AI model found nothing new to ask.'), *notes])
        elif run.feature == QUOTE_MAPPING:
            from directory.models import Package
            from levelling import services as levelling
            from levelling.models import LevellingSheet

            from . import quotemap
            if project.stage not in project.PIPELINE_STAGES:
                raise service.Unavailable(f'{project.name} is no longer an open bid, so the quote wasn\'t mapped.')
            if stand_in is None or not stand_in.membership.can_estimate:
                raise service.Unavailable('You can no longer work on this bid, so the quote wasn\'t mapped.')
            sheet = LevellingSheet.objects.filter(project=project, pk=run.params.get('sheet')).first()
            package = Package.objects.filter(project=project, pk=run.params.get('package')).exclude(
                status__in=levelling.NOT_PRICING).first()
            if sheet is None or package is None or package.trade_id != sheet.trade_id:
                raise service.Unavailable('That quote or levelling sheet has gone, so nothing was mapped.')
            draft, notes = quotemap.read(sheet, package, run.requested_by, run=run)
            n = draft.lines.count()
            message = ' '.join([f'{n} line{"s" if n != 1 else ""} mapped from {package.subcontractor}\'s quote for '
                                'you to check. Nothing is in the levelling until you accept it.', *notes])
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
