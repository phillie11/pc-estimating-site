"""
Regression tests from the review of the job queue: SMTP timeouts, database errors
while recording a result, lost locks, idempotency keys reused for other work,
emails that can never be sent, addresses in stored errors and logs, inline worker
ids and atomic tasks.
"""
import io
import smtplib
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core import checks, mail
from django.core.mail.backends.smtp import EmailBackend
from django.core.management import call_command
from django.db import OperationalError
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone

import jobs
from tenancy.tests import Base

from . import outbox
from .models import Job, OutboxEmail
from .runner import Worker, run_inline
from .tests import quiet

calls = []


@jobs.task(name='test.hardening.ok')
def ok(payload):
    calls.append('ran')


@jobs.task(name='test.hardening.fail_once', max_attempts=1, on_dead=lambda job: calls.append(('dead', job.pk)))
def fail_once(payload):
    raise RuntimeError('boom')


def write_then_fail(payload):
    OutboxEmail.objects.create(to=['a@x.test'], subject=payload['subject'], body='b')
    raise RuntimeError('half-way')


jobs.task(name='test.hardening.atomic', atomic=True)(write_then_fail)
jobs.task(name='test.hardening.not_atomic')(lambda payload: write_then_fail(payload))


def fail_update_marking(status, times):
    """Make the first `times` updates that set `status` raise, as if the database went away."""
    real, left = QuerySet.update, [times]

    def update(qs, **fields):
        if fields.get('status') == status and left[0]:
            left[0] -= 1
            raise OperationalError('server closed the connection unexpectedly')
        return real(qs, **fields)
    return mock.patch.object(QuerySet, 'update', update)


class HardeningTests(Base):
    def setUp(self):
        super().setUp()
        quiet(self)
        calls.clear()

    def worker(self, worker_id='w'):
        w = Worker(worker_id)
        w.db_retry_delays = (0, 0, 0)
        return w

    # M1: a mail server that stops answering must not hang the worker for good
    def test_smtp_has_a_timeout_well_under_the_lock_timeout(self):
        self.assertEqual(settings.EMAIL_TIMEOUT, 30)
        self.assertEqual(EmailBackend().timeout, 30)
        self.assertEqual([m for m in checks.run_checks() if m.id == 'jobs.W001'], [])
        for timeout in (None, 0, settings.JOBS_LOCK_TIMEOUT // 2):
            with override_settings(EMAIL_TIMEOUT=timeout):
                self.assertEqual([m.id for m in checks.run_checks() if m.id == 'jobs.W001'], ['jobs.W001'], timeout)

    # M2: a database error while recording the result
    def test_a_short_outage_while_recording_done_is_ridden_out(self):
        job = jobs.enqueue('test.hardening.ok')
        with fail_update_marking(Job.DONE, times=2):
            self.assertEqual(self.worker().work(burst=True), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, calls), (Job.DONE, ['ran']))  # ran once, not left "running" to run again

    def test_the_long_running_worker_survives_a_longer_outage(self):
        job = jobs.enqueue('test.hardening.ok')
        w = self.worker()
        with fail_update_marking(Job.DONE, times=10), \
                mock.patch.object(Worker, 'sleep', lambda self, s: setattr(self, 'stopping', True)):
            self.assertEqual(w.work(poll=0), 1)  # no exception out of the loop
        job.refresh_from_db()
        self.assertEqual((job.status, job.locked_by), (Job.RUNNING, 'w'))  # lock recovery retries it later

    def test_a_database_error_recording_a_failure_is_retried_too(self):
        job = jobs.enqueue('test.hardening.fail_once')
        with fail_update_marking(Job.DEAD, times=1):
            self.worker().work(burst=True)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DEAD)
        self.assertEqual(calls, [('dead', job.pk)])

    # L1: no "dead" handling for a job whose lock was taken over
    @override_settings(JOBS_LOCK_TIMEOUT=600)
    def test_on_dead_runs_once_when_the_lock_was_lost(self):
        job = jobs.enqueue('test.hardening.fail_once')
        slow = self.worker('slow')
        claimed = slow.claim()
        Job.objects.filter(pk=job.pk).update(locked_at=timezone.now() - timedelta(hours=1))
        self.worker('fresh').recover_stale()  # marks it dead (last attempt) and calls on_dead
        slow.run(claimed)  # then the slow worker's task fails
        self.assertEqual(calls, [('dead', job.pk)])
        self.assertIn('never finished', Job.objects.get(pk=job.pk).last_error)  # the recovery's note stands

    # L2: one idempotency key, one piece of work
    def test_idempotency_key_reused_for_other_work_is_refused(self):
        first = jobs.enqueue('test.hardening.ok', org=self.org_a, idempotency_key='k')
        self.assertEqual(jobs.enqueue('test.hardening.ok', org=self.org_a, idempotency_key='k').pk, first.pk)
        with self.assertRaises(ValueError):
            jobs.enqueue('test.hardening.ok', org=self.org_b, idempotency_key='k')
        with self.assertRaises(ValueError):
            jobs.enqueue('test.hardening.fail_once', org=self.org_a, idempotency_key='k')
        with self.assertRaises(ValueError):
            jobs.enqueue('test.hardening.ok', idempotency_key='k')
        self.assertEqual(Job.objects.count(), 1)

    # L3: an email that can never be sent isn't retried
    @override_settings(EMAIL_SYNC=False)
    def test_a_bad_header_fails_the_email_at_once(self):
        email = outbox.queue_email('Hello\nBcc: everyone@x.test', 'body', ['x@a.test'], org=self.org_a)
        call_command('run_worker', '--burst', stdout=io.StringIO())
        email.refresh_from_db()
        self.assertEqual((email.status, email.attempts), (OutboxEmail.FAILED, 1))
        self.assertEqual(Job.objects.get().status, Job.DONE)  # no retries queued
        self.assertEqual(mail.outbox, [])

    @override_settings(EMAIL_SYNC=True)
    def test_a_bad_header_in_sync_mode_is_not_handed_to_the_worker(self):
        email = outbox.queue_email('Hello\nthere', 'body', ['x@a.test'], org=self.org_a)
        self.assertEqual(OutboxEmail.objects.get(pk=email.pk).status, OutboxEmail.FAILED)
        self.assertFalse(Job.objects.exists())

    # L4: no email addresses in stored errors or logs
    @override_settings(EMAIL_SYNC=False)
    def test_addresses_are_kept_out_of_stored_errors_and_logs(self):
        email = outbox.queue_email('Hello', 'body', ['someone.private@client.test'], org=self.org_a)
        refused = smtplib.SMTPRecipientsRefused({'someone.private@client.test': (550, b'mailbox unavailable')})
        with mock.patch('jobs.outbox.EmailMessage.send', side_effect=refused), \
                self.assertLogs('jobs', 'WARNING') as logs:
            call_command('run_worker', '--burst', stdout=io.StringIO())
        email.refresh_from_db()
        job = Job.objects.get()
        for text in (job.last_error, email.last_error, '\n'.join(logs.output)):
            self.assertNotIn('someone.private@client.test', text)
        self.assertIn('<email>', job.last_error)
        self.assertIn('SMTPRecipientsRefused', job.last_error)

    @override_settings(EMAIL_SYNC=True)
    def test_sync_failure_log_names_the_email_not_the_address(self):
        with mock.patch('jobs.outbox.EmailMessage.send', side_effect=OSError('refused for who@client.test')), \
                self.assertLogs('jobs', 'WARNING') as logs:
            email = outbox.queue_email('Hello', 'body', ['who@client.test'], org=self.org_a)
        text = '\n'.join(logs.output)
        self.assertIn(f'Email {email.pk}', text)
        self.assertNotIn('who@client.test', text)

    # L5: each inline run is its own worker
    def test_inline_worker_ids_are_unique(self):
        ids = []
        with mock.patch.object(Worker, 'claim', autospec=True, side_effect=lambda w, job_id=None: ids.append(w.id)):
            run_inline(1)
            run_inline(2)
        self.assertEqual(len(set(ids)), 2)

    # L7: opt-in atomic tasks
    def test_an_atomic_task_rolls_back_its_writes_when_it_fails(self):
        jobs.enqueue('test.hardening.atomic', {'subject': 'atomic'})
        jobs.enqueue('test.hardening.not_atomic', {'subject': 'plain'})
        self.worker().work(burst=True)
        self.assertEqual(list(OutboxEmail.objects.values_list('subject', flat=True)), ['plain'])
        self.assertEqual(set(Job.objects.values_list('status', flat=True)), {Job.FAILED})
