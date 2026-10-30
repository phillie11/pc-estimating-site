"""
The email outbox end to end: notify.send() in a request writes an OutboxEmail,
the worker sends it, failures retry and give up, and EMAIL_SYNC keeps the old
send-during-the-request behaviour.
"""
import io
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from tenancy import notify
from tenancy.context import org_context
from tenancy.models import Notification, TeamInvite
from tenancy.tests import Base

from . import outbox
from .models import Job, OutboxEmail
from .tests import quiet


def burst():
    call_command('run_worker', '--burst', stdout=io.StringIO())


def smtp_down():
    return mock.patch('jobs.outbox.EmailMessage.send', side_effect=OSError('SMTP down'))


class OutboxTestCase(Base):
    def setUp(self):
        super().setUp()
        quiet(self)


@override_settings(EMAIL_SYNC=False)
class WorkerSendsEmailTests(OutboxTestCase):
    def test_a_request_queues_the_email_and_the_worker_sends_it(self):
        self.login(self.alice)
        r = self.client.post(self.url('team', self.org_a), {'email': 'new@a.test', 'role': 'MEMBER'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(mail.outbox, [])  # nothing sent during the request
        email = OutboxEmail.objects.get()
        self.assertEqual((email.to, email.status, email.organization), (['new@a.test'], OutboxEmail.QUEUED, self.org_a))
        self.assertIn(TeamInvite.objects.get(email='new@a.test').token, email.body)
        job = Job.objects.get()
        self.assertEqual((job.name, job.organization, job.idempotency_key), ('email.send', self.org_a, f'email:{email.pk}'))
        burst()
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual((sent.to, sent.subject), (['new@a.test'], '[Alpha Fitout] Alice A invited you to Alpha Fitout'))
        email.refresh_from_db()
        self.assertEqual((email.status, email.attempts), (OutboxEmail.SENT, 1))
        self.assertIsNotNone(email.sent_at)
        self.assertEqual(Job.objects.get().status, Job.DONE)

    def test_alert_creates_the_notification_now_and_queues_the_email(self):
        with org_context(self.org_a):
            notify.alert([self.alice], 'RFI-001 assigned to you', '/alpha/rfis/1/', org=self.org_a)
            self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(mail.outbox, [])
        burst()
        self.assertEqual(mail.outbox[0].to, ['alice@a.test'])

    def test_a_rolled_back_request_sends_nothing(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
            raise RuntimeError('the view failed after notifying')
        self.assertFalse(OutboxEmail.objects.exists() or Job.objects.exists())
        burst()
        self.assertEqual(mail.outbox, [])

    @override_settings(JOBS_BACKOFF_BASE=60)
    def test_a_failed_send_is_retried(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
        with smtp_down():
            burst()
        email, job = OutboxEmail.objects.get(), Job.objects.get()
        self.assertEqual((email.status, email.attempts, job.status), (OutboxEmail.QUEUED, 1, Job.FAILED))
        self.assertIn('SMTP down', email.last_error)
        self.assertGreater(job.run_after, timezone.now() + timedelta(seconds=55))
        Job.objects.update(run_after=timezone.now())
        burst()
        email.refresh_from_db()
        self.assertEqual((email.status, email.attempts, len(mail.outbox)), (OutboxEmail.SENT, 2, 1))

    def test_after_the_last_attempt_the_email_is_marked_failed_and_can_be_sent_again(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
        Job.objects.update(max_attempts=1)
        with smtp_down():
            burst()
        email = OutboxEmail.objects.get()
        self.assertEqual((email.status, Job.objects.get().status), (OutboxEmail.FAILED, Job.DEAD))
        outbox.resend(email)  # the admin's "Send again"
        self.assertEqual(Job.objects.count(), 1)  # the same job, re-queued
        burst()
        email.refresh_from_db()
        self.assertEqual((email.status, len(mail.outbox)), (OutboxEmail.SENT, 1))

    def test_a_second_run_of_the_same_job_does_not_send_twice(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
        email = OutboxEmail.objects.get()
        outbox.send_email({'id': email.pk})
        outbox.send_email({'id': email.pk})  # e.g. a worker died after sending, before marking the job done
        self.assertEqual(len(mail.outbox), 1)

    def test_reply_to_is_kept(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a,
                    reply_to='pm@a.test')
        burst()
        self.assertEqual(mail.outbox[0].reply_to, ['pm@a.test'])

    @override_settings(JOBS_RUN_INLINE=True)
    def test_inline_mode_sends_once_the_request_commits(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
            self.assertEqual(mail.outbox, [])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(OutboxEmail.objects.get().status, OutboxEmail.SENT)


@override_settings(EMAIL_SYNC=True)
class SyncEmailTests(OutboxTestCase):
    def test_sync_mode_sends_during_the_request_and_records_it(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(OutboxEmail.objects.get().status, OutboxEmail.SENT)
        self.assertFalse(Job.objects.exists())

    def test_sync_failure_is_handed_to_the_worker_without_breaking_the_request(self):
        with smtp_down():
            notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'x'}, org=self.org_a)
        email, job = OutboxEmail.objects.get(), Job.objects.get()
        self.assertEqual((email.status, email.attempts), (OutboxEmail.QUEUED, 1))
        self.assertGreater(job.run_after, timezone.now())  # retried after the first backoff, not straight away
        Job.objects.update(run_after=timezone.now())
        burst()
        self.assertEqual(len(mail.outbox), 1)
