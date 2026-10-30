"""
Summarise the job queue and the email outbox.

    python manage.py jobs_status

Look here (or at the platform admin's Jobs page) when emails seem late: a growing
"due now" count with no worker running means run_worker is down.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from jobs.models import Job, OutboxEmail


class Command(BaseCommand):
    help = 'Show counts of background jobs and outbox emails by status, overdue jobs and recent failures.'

    def handle(self, *args, **opts):
        now = timezone.now()
        out = self.stdout.write
        by_status = dict(Job.objects.order_by().values_list('status').annotate(n=Count('pk')))
        out('Jobs: ' + ', '.join(f'{s} {by_status.get(s, 0)}' for s, _ in Job.STATUS_CHOICES))
        due = Job.objects.filter(status__in=Job.READY, run_after__lte=now)
        oldest = due.order_by('run_after').values_list('run_after', flat=True).first()
        out(f'Due now: {due.count()}' + (f' (oldest waiting {self.age(now - oldest)})' if oldest else ''))
        stale = Job.objects.filter(status=Job.RUNNING,
                                   locked_at__lt=now - timedelta(seconds=settings.JOBS_LOCK_TIMEOUT)).count()
        if stale:
            out(self.style.WARNING(f'Running past the lock timeout: {stale} (a worker died; the next one retries them)'))
        emails = dict(OutboxEmail.objects.order_by().values_list('status').annotate(n=Count('pk')))
        out('Emails: ' + ', '.join(f'{s} {emails.get(s, 0)}' for s, _ in OutboxEmail.STATUS_CHOICES))
        out(f'Email mode: {"sent during the request (EMAIL_SYNC=1)" if settings.EMAIL_SYNC else "sent by the worker"}')
        dead = Job.objects.filter(status=Job.DEAD, finished_at__gte=now - timedelta(days=7)).order_by('-finished_at')
        for job in dead[:10]:
            last = (job.last_error.strip().splitlines() or [''])[-1][:160]
            out(self.style.ERROR(f'  dead #{job.pk} {job.name} {job.finished_at:%Y-%m-%d %H:%M}: {last}'))

    @staticmethod
    def age(delta):
        minutes = int(delta.total_seconds() // 60)
        return f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m'
