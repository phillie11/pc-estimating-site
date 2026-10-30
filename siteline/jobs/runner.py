"""
The worker loop behind `manage.py run_worker`. See jobs/__init__.py for the design.

One worker runs one job at a time. Run more worker processes for more throughput;
claiming is safe across processes and machines.
"""
import logging
import os
import random
import socket
import time
import traceback
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import F
from django.utils import timezone

from tenancy.context import org_context, unscoped

from . import get_task
from .models import Job, OutboxEmail

logger = logging.getLogger(__name__)


def backoff(attempt):
    """Seconds to wait after the nth failed attempt: base, 2x base, 4x base... capped, plus up to 10% jitter."""
    delay = min(settings.JOBS_BACKOFF_BASE * 2 ** max(attempt - 1, 0), settings.JOBS_BACKOFF_MAX)
    return delay + random.uniform(0, delay / 10)  # noqa: S311 - jitter, not security


class Worker:
    def __init__(self, worker_id=None):
        self.id = worker_id or f'{socket.gethostname()}:{os.getpid()}'[:100]
        self.stopping = False  # set by the SIGTERM handler; the loop stops after the current job

    # --- claiming ------------------------------------------------------------------

    def claim(self, job_id=None):
        """Take the next due job (or that one job) and mark it running, or return None."""
        now = timezone.now()
        due = Job.objects.filter(status__in=Job.READY, run_after__lte=now).order_by('run_after', 'pk')
        if job_id is not None:
            due = due.filter(pk=job_id)
        taken = dict(status=Job.RUNNING, locked_at=now, locked_by=self.id, attempts=F('attempts') + 1, updated_at=now)
        if connection.features.has_select_for_update_skip_locked:
            # PostgreSQL, MySQL 8: rows another worker is claiming are skipped, not waited for
            with transaction.atomic():
                pk = due.select_for_update(skip_locked=True).values_list('pk', flat=True).first()
                if pk is None:
                    return None
                Job.objects.filter(pk=pk).update(**taken)
        else:
            # SQLite and older MySQL: the conditional UPDATE is the lock. If another worker took the
            # row first it matches nothing and we try the next candidate.
            for pk in due.values_list('pk', flat=True)[:10]:
                if Job.objects.filter(pk=pk, status__in=Job.READY, run_after__lte=now).update(**taken):
                    break
            else:
                return None
        return Job.objects.select_related('organization').get(pk=pk)

    def recover_stale(self):
        """Jobs whose worker died (killed, OOM, deploy) are retried, or marked dead if they were on their last try."""
        now = timezone.now()
        cutoff = now - timedelta(seconds=settings.JOBS_LOCK_TIMEOUT)
        recovered = 0
        for job in Job.objects.filter(status=Job.RUNNING, locked_at__lt=cutoff):
            note = f'Worker {job.locked_by} took this job at {job.locked_at:%Y-%m-%d %H:%M:%S} UTC and never finished it.'
            dead = job.attempts >= job.max_attempts
            if Job.objects.filter(pk=job.pk, status=Job.RUNNING, locked_by=job.locked_by).update(
                    status=Job.DEAD if dead else Job.FAILED, run_after=now, locked_at=None, locked_by='',
                    last_error=note, updated_at=now, finished_at=now if dead else None):
                recovered += 1
                logger.warning('Recovered stale job %s: %s', job.pk, note)
                if dead:
                    job.refresh_from_db()
                    self._on_dead(job, get_task(job.name))
        return recovered

    def prune(self):
        """Delete finished jobs and sent emails after JOBS_KEEP_DAYS (they hold names and email bodies)."""
        cutoff = timezone.now() - timedelta(days=settings.JOBS_KEEP_DAYS)
        jobs, _ = Job.objects.filter(status=Job.DONE, finished_at__lt=cutoff).delete()
        emails, _ = OutboxEmail.objects.filter(status=OutboxEmail.SENT, sent_at__lt=cutoff).delete()
        return jobs, emails

    # --- running ------------------------------------------------------------------

    def scope(self, job, t):
        if t is not None and t.cross_company:
            return unscoped()
        # With no organisation this clears any current one (e.g. a request's, when run inline),
        # so tenant queries in the task fail closed rather than reading the wrong company
        return org_context(job.organization)

    def run(self, job):
        """Run a claimed job. Returns True if it succeeded. Never raises for a task's error."""
        t = get_task(job.name)
        try:
            if t is None:  # e.g. a newer web container queued a task this older worker doesn't know yet
                raise LookupError(f'No task is registered as {job.name!r}')
            with self.scope(job, t):
                t.fn(job.payload)
        except Exception as exc:  # noqa: BLE001 - any task error is recorded and retried
            self._failed(job, t, exc)
            return False
        now = timezone.now()
        # Only if we still hold the lock: if it was recovered as stale meanwhile, the job's new owner decides
        Job.objects.filter(pk=job.pk, locked_by=self.id).update(
            status=Job.DONE, locked_at=None, locked_by='', last_error='', updated_at=now, finished_at=now)
        logger.info('Job %s %s done (attempt %s)', job.pk, job.name, job.attempts)
        return True

    def _failed(self, job, t, exc):
        now = timezone.now()
        error = ''.join(traceback.format_exception(exc))[-5000:]
        dead = job.attempts >= job.max_attempts
        run_after = now if dead else now + timedelta(seconds=backoff(job.attempts))
        Job.objects.filter(pk=job.pk, locked_by=self.id).update(
            status=Job.DEAD if dead else Job.FAILED, run_after=run_after, locked_at=None, locked_by='',
            last_error=error, updated_at=now, finished_at=now if dead else None)
        if dead:
            logger.error('Job %s %s is dead after %s attempts: %s', job.pk, job.name, job.attempts, exc)
            job.status, job.last_error = Job.DEAD, error
            self._on_dead(job, t)
        else:
            logger.warning('Job %s %s failed (attempt %s of %s), retrying at %s: %s',
                           job.pk, job.name, job.attempts, job.max_attempts, run_after, exc)

    def _on_dead(self, job, t):
        if t is None or t.on_dead is None:
            return
        try:
            with self.scope(job, t):
                t.on_dead(job)
        except Exception:  # noqa: BLE001
            logger.exception('on_dead for job %s %s failed', job.pk, job.name)

    # --- the loop -----------------------------------------------------------------

    def work(self, once=False, burst=False, max_jobs=None, poll=None):
        """
        Run jobs until stopped. once: at most one job. burst: every job that is due, then return.
        Returns the number of jobs run.
        """
        poll = settings.JOBS_POLL_SECONDS if poll is None else poll
        ran, next_recover, next_prune = 0, 0, 0
        while not self.stopping:
            try:
                if not connection.in_atomic_block:  # (tests run the worker inside a transaction)
                    close_old_connections()  # drop connections the database or CONN_MAX_AGE has closed
                if time.monotonic() >= next_recover:  # once a minute
                    self.recover_stale()
                    next_recover = time.monotonic() + 60
                if time.monotonic() >= next_prune:  # once an hour
                    self.prune()
                    next_prune = time.monotonic() + 3600
                job = self.claim()
            except DatabaseError:
                if once or burst:
                    raise
                logger.exception('Database error in the worker loop; retrying in %ss', poll)
                connection.close()
                self.sleep(poll)
                continue
            if job is not None:
                self.run(job)
                ran += 1
                if once or (max_jobs and ran >= max_jobs):
                    break
                continue
            if once or burst:
                break
            self.sleep(poll)
        return ran

    def sleep(self, seconds):
        """Sleep in short steps so SIGTERM ends an idle worker straight away."""
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))


def run_inline(job_id):
    """JOBS_RUN_INLINE: run a just-committed job in this process. Errors are recorded, never raised."""
    worker = Worker(f'inline:{os.getpid()}')
    try:
        job = worker.claim(job_id)
        if job is not None:
            worker.run(job)
    except Exception:  # noqa: BLE001 - the job stays queued for a worker
        logger.exception('Running job %s inline failed', job_id)
