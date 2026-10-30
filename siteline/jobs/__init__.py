"""
Background jobs: a small queue kept in the app's own database.

    import jobs

    @jobs.task(name='tenders.close_package', max_attempts=3)
    def close_package(payload):
        TradePackage.objects.get(pk=payload['package_id']).close()   # runs inside org_context(job.organization)

    jobs.enqueue('tenders.close_package', {'package_id': p.pk}, org=p.organization)

`python manage.py run_worker` claims and runs them (jobs/runner.py). The email
outbox (jobs/outbox.py) is built on it.

Why a table and not Celery/Redis, django-tasks or procrastinate: Siteline runs on
PythonAnywhere (MySQL) until the AWS cutover and on PostgreSQL after it, CI tests
SQLite and PostgreSQL, and there is no broker on either host. procrastinate is
PostgreSQL only; django-tasks' database backend would work but is pre-1.0 and its
API is still moving. The volume here is a few thousand emails a day, which any of
the three databases queues comfortably, so a few hundred lines we can read beat a dependency.

How it behaves, and the trade-offs:

- enqueue() writes the Job row in the caller's transaction, so the job exists
  exactly when the caller's work commits: a rollback drops it, and no worker can
  see it before the commit. (A transaction.on_commit() hook would do the same but
  loses the job if the process dies between commit and hook.) With
  JOBS_RUN_INLINE=1 the job is also run in-process once the transaction commits,
  for local development without a worker.
- Delivery is at least once. A job whose worker dies is picked up again after
  JOBS_LOCK_TIMEOUT, and a task that did its work but crashed before the row was
  marked done runs again. Write tasks so a second run is harmless (the outbox
  checks the email's status before sending). There is no heartbeat, so the lock
  timeout must be longer than the slowest task.
- Workers poll (JOBS_POLL_SECONDS) rather than LISTEN/NOTIFY, which only
  PostgreSQL has. A job waits up to one poll interval.
- Claiming uses SELECT ... FOR UPDATE SKIP LOCKED where the database has it
  (PostgreSQL, MySQL 8) so several workers never wait on each other; otherwise
  (SQLite, older MySQL) a conditional UPDATE takes the job, which is just as safe
  but makes rival workers retry.
- Failures retry with exponential backoff and jitter (JOBS_BACKOFF_BASE doubling
  up to JOBS_BACKOFF_MAX) and become `dead` after max_attempts, which a person
  looks at in the platform admin (`Run again`). `manage.py jobs_status` summarises.
- Tenant separation: the runner enters org_context(job.organization). A job with
  no organisation runs with none, so a TenantModel query in it fails closed. A task
  registered with cross_company=True runs inside unscoped() and cannot be given an
  organisation; keep those rare and greppable, like unscoped() itself.
- Payloads are JSON: pass ids, not model instances, and look rows up in the task.
"""
from dataclasses import dataclass
from typing import Callable

from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class Task:
    name: str
    fn: Callable
    max_attempts: int | None = None
    cross_company: bool = False
    on_dead: Callable | None = None  # called with the Job once it is marked dead


_registry: dict[str, Task] = {}


def task(name, max_attempts=None, cross_company=False, on_dead=None):
    """Register `fn(payload)` as the task `name`. Names are stored in the database, so don't rename them lightly."""
    def register(fn):
        existing = _registry.get(name)
        if existing and existing.fn.__qualname__ != fn.__qualname__:
            raise ImproperlyConfigured(f'Two tasks are registered as {name!r}')
        _registry[name] = Task(name, fn, max_attempts, cross_company, on_dead)
        return fn
    return register


def get_task(name):
    return _registry.get(name)


def enqueue(name, payload=None, org=None, run_after=None, idempotency_key=None, max_attempts=None):
    """
    Queue a job and return it. Safe inside a transaction: the job only exists if it commits.
    With an idempotency_key, a second enqueue with the same key returns the first job instead.
    """
    import json

    from django.conf import settings
    from django.core.serializers.json import DjangoJSONEncoder
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from .models import Job

    t = get_task(name)
    if t is None:
        raise ValueError(f'No task is registered as {name!r}')
    if t.cross_company and org is not None:
        raise ValueError(f'{name!r} is a cross-company task; it cannot be queued for one organisation')
    payload = {} if payload is None else payload
    json.dumps(payload, cls=DjangoJSONEncoder)  # fail here, in the caller, not in the worker
    if idempotency_key and (existing := Job.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    job = Job(name=name, payload=payload, organization=org, run_after=run_after or timezone.now(),
              idempotency_key=idempotency_key or None,
              max_attempts=max_attempts or t.max_attempts or settings.JOBS_MAX_ATTEMPTS)
    try:
        with transaction.atomic():  # a savepoint, so a duplicate key doesn't break the caller's transaction
            job.save()
    except IntegrityError:
        if idempotency_key:
            return Job.objects.get(idempotency_key=idempotency_key)
        raise
    if settings.JOBS_RUN_INLINE:
        from .runner import run_inline
        transaction.on_commit(lambda: run_inline(job.pk))
    return job


def retry(job):
    """Queue a failed or dead job to run again now, with a fresh set of attempts."""
    from django.utils import timezone

    from .models import Job
    now = timezone.now()
    return Job.objects.filter(pk=job.pk).exclude(status=Job.RUNNING).update(
        status=Job.QUEUED, attempts=0, run_after=now, locked_at=None, locked_by='', updated_at=now)
