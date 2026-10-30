"""
The email outbox. tenancy.notify.send() renders an email and calls queue_email(),
which writes an OutboxEmail row, and the worker sends it (task `email.send`) with
retries, so a slow or failing mail server never holds up or breaks a request.

EMAIL_SYNC=1 (the default until a worker is running) sends straight away inside the
request, as before, and only falls back to the worker if that attempt fails. Every
email gets a row either way, so the platform admin shows what was sent and what failed.

Django's password reset emails don't come through here: PasswordResetForm sends
them itself, synchronously. That's deliberate: the person is waiting on the page for
it, the email has no organisation, and a failure should show up at once.
"""
import logging
import traceback
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import F
from django.utils import timezone

import jobs

from .models import Job, OutboxEmail
from .runner import backoff, scrub

logger = logging.getLogger(__name__)

TASK = 'email.send'


def queue_email(subject, body, to, org=None, reply_to=None, from_email=None):
    """Store an email and send it (now with EMAIL_SYNC, else from the worker). Returns the OutboxEmail."""
    email = OutboxEmail.objects.create(
        organization=org, to=list(to), reply_to=list(reply_to or []), subject=subject[:300], body=body,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL)
    if settings.EMAIL_SYNC:
        try:
            deliver(email)
            return email
        except Exception as exc:  # noqa: BLE001 - the worker retries it
            # The id, not the address: logs shouldn't collect people's addresses (the admin has them)
            logger.warning('Email %s failed; queued for retry: %s', email.pk, scrub(repr(exc)))
            run_after = timezone.now() + timedelta(seconds=backoff(1))
            jobs.enqueue(TASK, {'id': email.pk}, org=org, run_after=run_after, idempotency_key=email.job_key)
            return email
    jobs.enqueue(TASK, {'id': email.pk}, org=org, idempotency_key=email.job_key)
    return email


def deliver(email):
    """
    Send one stored email unless it has already gone. Raises if the mail server refuses it, so the
    job retries. An email that can never be sent (a newline in a header, a malformed address:
    Django raises BadHeaderError / ValueError) is marked failed at once instead.
    """
    if email.status == OutboxEmail.SENT:
        return
    try:
        msg = EmailMessage(email.subject, email.body, email.from_email, email.to, reply_to=email.reply_to or None)
        msg.send()
    except ValueError as exc:  # includes BadHeaderError: retrying won't help
        OutboxEmail.objects.filter(pk=email.pk).update(status=OutboxEmail.FAILED, attempts=F('attempts') + 1,
                                                       last_error=scrub(traceback.format_exc())[-2000:])
        email.status = OutboxEmail.FAILED
        logger.error('Email %s can never be sent, not retrying: %s', email.pk, scrub(repr(exc)))
        return
    except Exception:
        OutboxEmail.objects.filter(pk=email.pk).update(attempts=F('attempts') + 1,
                                                       last_error=scrub(traceback.format_exc())[-2000:])
        raise
    email.status, email.sent_at = OutboxEmail.SENT, timezone.now()
    OutboxEmail.objects.filter(pk=email.pk).update(status=email.status, sent_at=email.sent_at,
                                                   attempts=F('attempts') + 1, last_error='')


def resend(email):
    """Platform admin: try a failed email again (re-using its job, so it is never queued twice)."""
    OutboxEmail.objects.filter(pk=email.pk).exclude(status=OutboxEmail.SENT).update(status=OutboxEmail.QUEUED)
    job = Job.objects.filter(idempotency_key=email.job_key).first()
    if job is None:
        jobs.enqueue(TASK, {'id': email.pk}, org=email.organization, idempotency_key=email.job_key)
    else:
        jobs.retry(job)


def _gave_up(job):
    OutboxEmail.objects.filter(pk=job.payload.get('id')).exclude(status=OutboxEmail.SENT).update(
        status=OutboxEmail.FAILED)


@jobs.task(name=TASK, max_attempts=8, on_dead=_gave_up)  # 8 tries with backoff span about an hour
def send_email(payload):
    email = OutboxEmail.objects.filter(pk=payload['id']).first()
    if email is not None:  # gone if its workspace was deleted
        deliver(email)
