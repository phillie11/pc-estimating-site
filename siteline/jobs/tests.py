"""
The job queue: enqueue inside transactions, idempotency keys, claiming, retries and
backoff, dead jobs, lock recovery, the organisation a task runs in, and SIGTERM.
"""
import io
import logging
import os
import signal
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

import jobs
from projects.models import Project
from tenancy.context import get_current_org, org_context
from tenancy.tests import Base, make_user

from .models import Job, OutboxEmail
from .runner import Worker, backoff

calls = []


@jobs.task(name='test.record')
def record(payload):
    calls.append(payload)


@jobs.task(name='test.fail', max_attempts=3, on_dead=lambda job: calls.append(('dead', job.pk)))
def fail(payload):
    raise RuntimeError('boom')


@jobs.task(name='test.project_names')
def project_names(payload):
    calls.append(sorted(Project.objects.values_list('name', flat=True)))


@jobs.task(name='test.every_project', cross_company=True)
def every_project(payload):
    calls.append(sorted(Project.objects.values_list('name', flat=True)))


@jobs.task(name='test.sigterm')
def sigterm(payload):
    os.kill(os.getpid(), signal.SIGTERM)  # as ECS does on a deploy, mid-job
    calls.append('finished')


def quiet(test):
    """The worker logs every job and failure; keep test output readable."""
    log = logging.getLogger('jobs')
    test.addCleanup(log.setLevel, log.level)
    log.setLevel(logging.CRITICAL)


def burst():
    out = io.StringIO()
    call_command('run_worker', '--burst', stdout=out)
    return out.getvalue()


class JobTestCase(Base):
    def setUp(self):
        super().setUp()
        quiet(self)
        calls.clear()

    def make_due(self, job):
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now() - timedelta(seconds=1))


class EnqueueTests(JobTestCase):
    def test_unknown_task_and_bad_payload_are_refused_at_enqueue(self):
        with self.assertRaises(ValueError):
            jobs.enqueue('test.no_such_task')
        with self.assertRaises(TypeError):
            jobs.enqueue('test.record', {'thing': object()})
        with self.assertRaises(ValueError):
            jobs.enqueue('test.every_project', org=self.org_a)
        self.assertFalse(Job.objects.exists())

    def test_a_rolled_back_transaction_leaves_no_job(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            jobs.enqueue('test.record', {'n': 1})
            raise RuntimeError('the request failed')
        self.assertFalse(Job.objects.exists())

    @override_settings(JOBS_RUN_INLINE=True)
    def test_inline_mode_runs_the_job_only_once_the_transaction_commits(self):
        with self.captureOnCommitCallbacks() as callbacks:
            job = jobs.enqueue('test.record', {'n': 1}, org=self.org_a)
            self.assertEqual(calls, [])
        self.assertEqual(Job.objects.get(pk=job.pk).status, Job.QUEUED)
        for callback in callbacks:
            callback()
        self.assertEqual(calls, [{'n': 1}])
        self.assertEqual(Job.objects.get(pk=job.pk).status, Job.DONE)

    def test_idempotency_key_returns_the_first_job(self):
        first = jobs.enqueue('test.record', {'n': 1}, idempotency_key='invite:7')
        again = jobs.enqueue('test.record', {'n': 2}, idempotency_key='invite:7')
        self.assertEqual((again.pk, Job.objects.count()), (first.pk, 1))
        jobs.enqueue('test.record', {'n': 3})  # no key: never deduplicated
        jobs.enqueue('test.record', {'n': 4})
        self.assertEqual(Job.objects.count(), 3)

    def test_idempotency_race_keeps_the_callers_transaction_usable(self):
        """Another process inserted the key between our lookup and our insert."""
        first = jobs.enqueue('test.record', idempotency_key='race')
        with transaction.atomic():
            with mock.patch('django.db.models.query.QuerySet.first', return_value=None):
                again = jobs.enqueue('test.record', idempotency_key='race')
            self.assertEqual(again.pk, first.pk)
            with org_context(self.org_a):
                self.assertEqual(Project.objects.count(), 1)  # the transaction wasn't broken


class WorkerTests(JobTestCase):
    def test_burst_runs_due_jobs_in_order_and_leaves_future_ones(self):
        later = jobs.enqueue('test.record', {'n': 'later'}, run_after=timezone.now() + timedelta(hours=1))
        jobs.enqueue('test.record', {'n': 2})
        jobs.enqueue('test.record', {'n': 1}, run_after=timezone.now() - timedelta(minutes=1))
        self.assertIn('after 2 job(s)', burst())
        self.assertEqual(calls, [{'n': 1}, {'n': 2}])
        self.assertEqual(Job.objects.get(pk=later.pk).status, Job.QUEUED)
        done = Job.objects.filter(status=Job.DONE)
        self.assertEqual(done.count(), 2)
        self.assertTrue(all(j.finished_at and j.attempts == 1 and not j.locked_by for j in done))

    def test_once_runs_a_single_job(self):
        jobs.enqueue('test.record', {'n': 1})
        jobs.enqueue('test.record', {'n': 2})
        call_command('run_worker', '--once', stdout=io.StringIO())
        self.assertEqual(calls, [{'n': 1}])

    def test_two_workers_never_take_the_same_job(self):
        jobs.enqueue('test.record')
        a, b = Worker('a'), Worker('b')
        self.assertIsNotNone(a.claim())
        self.assertIsNone(b.claim())

    def test_skip_locked_claim(self):
        """The PostgreSQL / MySQL 8 path (CI runs it for real on PostgreSQL; SQLite ignores FOR UPDATE)."""
        job = jobs.enqueue('test.record')
        with mock.patch.object(connection.features, 'has_select_for_update_skip_locked', True):
            claimed = Worker('a').claim()
            self.assertIsNone(Worker('b').claim())
        self.assertEqual((claimed.pk, claimed.status, claimed.locked_by, claimed.attempts), (job.pk, Job.RUNNING, 'a', 1))

    def test_a_task_runs_inside_its_jobs_organisation(self):
        jobs.enqueue('test.project_names', org=self.org_a)
        jobs.enqueue('test.project_names', org=self.org_b)
        burst()
        self.assertEqual(calls, [['Alpha secret job'], ['Bravo job']])
        self.assertIsNone(get_current_org())

    def test_a_job_with_no_organisation_fails_closed(self):
        job = jobs.enqueue('test.project_names')
        with org_context(self.org_a):  # e.g. run inline inside a request: the request's org must not leak in
            w = Worker()
            self.assertFalse(w.run(w.claim()))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)
        self.assertIn('TenantContextMissing', job.last_error)
        self.assertEqual(calls, [])

    def test_a_cross_company_task_sees_every_company(self):
        jobs.enqueue('test.every_project')
        burst()
        self.assertEqual(calls, [['Alpha secret job', 'Bravo job']])

    @override_settings(JOBS_BACKOFF_BASE=60, JOBS_BACKOFF_MAX=3600)
    def test_failures_back_off_then_the_job_dies(self):
        job = jobs.enqueue('test.fail')
        expected = [60, 120]
        for attempt, delay in enumerate(expected, start=1):
            before = timezone.now()
            self.assertIn('after 1 job(s)', burst())  # the retry isn't due yet, so burst stops
            job.refresh_from_db()
            self.assertEqual((job.status, job.attempts), (Job.FAILED, attempt))
            self.assertIn('RuntimeError: boom', job.last_error)
            wait = (job.run_after - before).total_seconds()
            self.assertTrue(delay - 1 <= wait <= delay * 1.1 + 5, wait)
            self.make_due(job)
        burst()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (Job.DEAD, 3))
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(calls, [('dead', job.pk)])
        self.assertIn('after 0 job(s)', burst())  # dead jobs are left alone

    @override_settings(JOBS_BACKOFF_BASE=30, JOBS_BACKOFF_MAX=600)
    def test_backoff_is_capped(self):
        self.assertTrue(30 <= backoff(1) <= 33)
        self.assertTrue(600 <= backoff(30) <= 660)

    def test_a_job_whose_task_no_longer_exists_is_retried_not_lost(self):
        job = Job.objects.create(name='test.removed')
        burst()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)
        self.assertIn('No task is registered', job.last_error)

    @override_settings(JOBS_LOCK_TIMEOUT=600)
    def test_jobs_left_running_by_a_dead_worker_are_recovered(self):
        old = timezone.now() - timedelta(hours=1)
        lost = Job.objects.create(name='test.record', status=Job.RUNNING, attempts=1, locked_at=old, locked_by='gone:1')
        last_try = Job.objects.create(name='test.fail', status=Job.RUNNING, attempts=3, max_attempts=3, locked_at=old,
                                      locked_by='gone:2')
        busy = Job.objects.create(name='test.record', status=Job.RUNNING, attempts=1, locked_at=timezone.now(),
                                  locked_by='alive:3')
        self.assertEqual(Worker().recover_stale(), 2)
        lost.refresh_from_db(), last_try.refresh_from_db(), busy.refresh_from_db()
        self.assertEqual((lost.status, lost.locked_by), (Job.FAILED, ''))
        self.assertIn('gone:1', lost.last_error)
        self.assertEqual(last_try.status, Job.DEAD)
        self.assertEqual(calls, [('dead', last_try.pk)])  # on_dead runs for recovered jobs too
        self.assertEqual(busy.status, Job.RUNNING)
        burst()  # the recovered job runs again
        lost.refresh_from_db()
        self.assertEqual((lost.status, lost.attempts), (Job.DONE, 2))

    @override_settings(JOBS_LOCK_TIMEOUT=600)
    def test_a_slow_worker_finishing_after_recovery_does_not_overwrite_the_new_run(self):
        jobs.enqueue('test.record')
        slow, fresh = Worker('slow'), Worker('fresh')
        job = slow.claim()
        Job.objects.filter(pk=job.pk).update(locked_at=timezone.now() - timedelta(hours=1))
        fresh.recover_stale()
        self.assertEqual(fresh.claim().pk, job.pk)
        slow.run(job)
        job.refresh_from_db()
        self.assertEqual((job.status, job.locked_by), (Job.RUNNING, 'fresh'))

    def test_sigterm_finishes_the_current_job_then_stops(self):
        jobs.enqueue('test.sigterm')
        jobs.enqueue('test.record', {'n': 'next'})
        before = signal.getsignal(signal.SIGTERM)
        out = io.StringIO()
        call_command('run_worker', '--poll', '0.1', stdout=out)  # no --burst: only the signal ends it
        self.assertEqual(calls, ['finished'])
        self.assertIn('SIGTERM', out.getvalue())
        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1)  # left for the next worker
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    @override_settings(JOBS_KEEP_DAYS=30, JOBS_KEEP_FAILED_DAYS=90, JOBS_EMAIL_BODY_DAYS=7)
    def test_prune_retention(self):
        now = timezone.now()
        days = lambda n: now - timedelta(days=n)  # noqa: E731
        for status, finished in ((Job.DONE, days(31)), (Job.DEAD, days(91)),  # deleted
                                 (Job.DONE, now), (Job.DEAD, days(31))):  # kept: dead ones are kept longer
            Job.objects.create(name='test.record', status=status, finished_at=finished)
        email = lambda **kw: OutboxEmail.objects.create(to=['a@x.test'], subject='Invite', body='/invite/abc/', **kw)  # noqa: E731
        email(status=OutboxEmail.SENT, sent_at=days(31))  # deleted
        old_failed = email(status=OutboxEmail.FAILED)
        OutboxEmail.objects.filter(pk=old_failed.pk).update(created_at=days(91))  # deleted
        failed = email(status=OutboxEmail.FAILED)  # kept, body and all, for "Send again"
        week_old = email(status=OutboxEmail.SENT, sent_at=days(8))  # kept, body blanked
        fresh = email(status=OutboxEmail.SENT, sent_at=now)
        self.assertEqual(Worker().prune(), (2, 2, 1))
        self.assertEqual(sorted(Job.objects.values_list('status', flat=True)), [Job.DEAD, Job.DONE])
        bodies = dict(OutboxEmail.objects.values_list('pk', 'body'))
        self.assertEqual(bodies, {failed.pk: '/invite/abc/', week_old.pk: '', fresh.pk: '/invite/abc/'})
        self.assertEqual(OutboxEmail.objects.get(pk=week_old.pk).subject, 'Invite')

    def test_jobs_status(self):
        jobs.enqueue('test.record', run_after=timezone.now() - timedelta(hours=2))
        Job.objects.create(name='test.fail', status=Job.DEAD, finished_at=timezone.now(), last_error='x\nOSError: down')
        out = io.StringIO()
        call_command('jobs_status', stdout=out)
        text = out.getvalue()
        self.assertIn('queued 1', text)
        self.assertIn('Due now: 1 (oldest waiting 2h 0m)', text)
        self.assertIn('OSError: down', text)


class AdminTests(JobTestCase):
    def test_platform_admin_only(self):
        job = jobs.enqueue('test.fail')
        Job.objects.filter(pk=job.pk).update(status=Job.DEAD, attempts=3)
        OutboxEmail.objects.create(to=['a@x.test'], subject='Hello', body='b', organization=self.org_a)
        staff = make_user('staff@platform.test')
        staff.is_staff = True
        staff.save()
        self.client.force_login(staff, backend='django.contrib.auth.backends.ModelBackend')
        for name in ('admin:jobs_job_changelist', 'admin:jobs_outboxemail_changelist'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        root = make_user('root@platform.test')
        root.is_staff = root.is_superuser = True
        root.save()
        self.client.force_login(root, backend='django.contrib.auth.backends.ModelBackend')
        for name in ('admin:jobs_job_changelist', 'admin:jobs_outboxemail_changelist'):
            self.assertContains(self.client.get(reverse(name)), 'Hello' if 'outbox' in name else 'test.fail')
        self.assertEqual(self.client.get(reverse('admin:jobs_job_change', args=[job.pk])).status_code, 200)
        self.client.post(reverse('admin:jobs_job_changelist'),
                         {'action': 'run_again', '_selected_action': [job.pk]})
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (Job.QUEUED, 0))
        changelist = reverse('admin:jobs_outboxemail_changelist')
        self.assertContains(self.client.get(changelist, {'q': 'a@x.test'}), 'Hello')
        email = OutboxEmail.objects.get()
        OutboxEmail.objects.update(status=OutboxEmail.FAILED)
        self.client.post(changelist, {'action': 'send_again', '_selected_action': [email.pk]})
        email.refresh_from_db()
        self.assertEqual(email.status, OutboxEmail.QUEUED)
        self.assertTrue(Job.objects.filter(idempotency_key=email.job_key, status=Job.QUEUED).exists())
