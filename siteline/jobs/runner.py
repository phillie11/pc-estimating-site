"""
The worker loop behind `manage.py run_worker`. See jobs/__init__.py for the design.

One worker runs one job at a time. Run more worker processes for more throughput;
claiming is safe across processes and machines.
"""
import logging
import os
import random
import re
import socket
import time
import traceback
import uuid
from contextlib import nullcontext
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from tenancy.context import org_context, unscoped

from . import get_task
from .models import Job, OutboxEmail

logger = logging.getLogger(__name__)


def backoff(attempt):
    """Seconds to wait after the nth failed attempt: base, 2x base, 4x base... capped, plus up to 10% jitter."""
    delay = min(settings.JOBS_BACKOFF_BASE * 2 ** max(attempt - 1, 0), settings.JOBS_BACKOFF_MAX)
    return delay + random.uniform(0, delay / 10)  # noqa: S311 - jitter, not security


EMAIL_ADDRESS = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')


def scrub(text):
    """Mask email addresses, so stored errors and logs don't collect people's addresses (SMTP errors quote them)."""
    return EMAIL_ADDRESS.sub('<email>', text)


class Worker:
    # Pauses before retrying a failed write of a job's result. Giving up leaves the job "running" until
    # its lock times out, and then it runs again (a duplicate email), so ride out a short outage. Under
    # ECS's 30-second stopTimeout in total.
    db_retry_delays = (1, 5, 15)

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
        """
        Retention. Sent emails lose their body after JOBS_EMAIL_BODY_DAYS (it holds invite links that
        still work) but keep who/what/when. Done jobs and sent emails go after JOBS_KEEP_DAYS; dead jobs
        and failed emails, kept longer for someone to look at, after JOBS_KEEP_FAILED_DAYS.
        Returns (jobs deleted, emails deleted, bodies blanked).
        """
        now = timezone.now()
        keep, keep_failed = now - timedelta(days=settings.JOBS_KEEP_DAYS), now - timedelta(days=settings.JOBS_KEEP_FAILED_DAYS)
        jobs, _ = Job.objects.filter(Q(status=Job.DONE, finished_at__lt=keep) |
                                     Q(status=Job.DEAD, finished_at__lt=keep_failed)).delete()
        emails, _ = OutboxEmail.objects.filter(Q(status=OutboxEmail.SENT, sent_at__lt=keep) |
                                               Q(status=OutboxEmail.FAILED, created_at__lt=keep_failed)).delete()
        blanked = OutboxEmail.objects.filter(
            status=OutboxEmail.SENT, sent_at__lt=now - timedelta(days=settings.JOBS_EMAIL_BODY_DAYS)).exclude(
            body='').update(body='')
        return jobs, emails, blanked

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
            with self.scope(job, t), (transaction.atomic() if t.atomic else nullcontext()):
                t.fn(job.payload)
        except Exception as exc:  # noqa: BLE001 - any task error is recorded and retried
            self._failed(job, t, exc)
            return False
        if self._record(job, status=Job.DONE, locked_at=None, locked_by='', last_error='', finished_at=timezone.now()):
            logger.info('Job %s %s done (attempt %s)', job.pk, job.name, job.attempts)
        else:
            logger.warning('Job %s %s finished after its lock was taken over; the new owner decides', job.pk, job.name)
        return True

    def _record(self, job, **fields):
        """
        Write a claimed job's new state, only while we still hold its lock (if it was recovered as stale
        meanwhile, its new owner decides). Returns the number of rows changed: 0 means the lock was lost.
        Retries through a short database outage (db_retry_delays), then raises DatabaseError.
        """
        for delay in (*self.db_retry_delays, None):
            try:
                return Job.objects.filter(pk=job.pk, locked_by=self.id).update(updated_at=timezone.now(), **fields)
            except DatabaseError:
                if delay is None:
                    raise
                logger.warning('Could not record the result of job %s; retrying in %ss', job.pk, delay, exc_info=True)
                self.reset_connection()
                time.sleep(delay)  # not self.sleep(): SIGTERM must not skip recording the result

    def _failed(self, job, t, exc):
        now = timezone.now()
        error = scrub(''.join(traceback.format_exception(exc)))[-5000:]
        dead = job.attempts >= job.max_attempts
        run_after = now if dead else now + timedelta(seconds=backoff(job.attempts))
        if not self._record(job, status=Job.DEAD if dead else Job.FAILED, run_after=run_after, locked_at=None,
                            locked_by='', last_error=error, finished_at=now if dead else None):
            logger.warning('Job %s %s failed after its lock was taken over; the new owner decides', job.pk, job.name)
            return
        if dead:
            logger.error('Job %s %s is dead after %s attempts: %s', job.pk, job.name, job.attempts, scrub(repr(exc)))
            job.status, job.last_error = Job.DEAD, error
            self._on_dead(job, t)
        else:
            logger.warning('Job %s %s failed (attempt %s of %s), retrying at %s: %s',
                           job.pk, job.name, job.attempts, job.max_attempts, run_after, scrub(repr(exc)))

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
                if job is not None:
                    ran += 1
                    self.run(job)
            except DatabaseError:
                # The database went away (restart, failover, network). A job whose result couldn't be
                # recorded stays "running" and is retried once its lock times out. --once / --burst
                # exit with the error; the long-running worker waits and carries on.
                if once or burst:
                    raise
                logger.exception('Database error in the worker loop; retrying in %ss', poll)
                self.reset_connection()
                self.sleep(poll)
                continue
            if job is not None:
                if once or (max_jobs and ran >= max_jobs):
                    break
                continue
            if once or burst:
                break
            self.sleep(poll)
        return ran

    @staticmethod
    def reset_connection():
        if not connection.in_atomic_block:  # (tests run the worker inside a transaction)
            connection.close()  # the next query reconnects

    def sleep(self, seconds):
        """Sleep in short steps so SIGTERM ends an idle worker straight away."""
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))


def run_inline(job_id):
    """JOBS_RUN_INLINE: run a just-committed job in this process. Errors are recorded, never raised."""
    worker = Worker(f'inline:{os.getpid()}:{uuid.uuid4().hex[:8]}')  # unique per call: web servers run threads
    try:
        job = worker.claim(job_id)
        if job is not None:
            worker.run(job)
    except Exception:  # noqa: BLE001 - the job stays queued for a worker
        logger.exception('Running job %s inline failed', job_id)
