"""
Run background jobs (see jobs/__init__.py).

    python manage.py run_worker            # keep running: the ECS `worker` service, or a PythonAnywhere always-on task
    python manage.py run_worker --burst    # run every job that is due, then exit (a scheduled task, or tests)
    python manage.py run_worker --once     # run at most one job

SIGTERM (ECS stopping the task on a deploy) and Ctrl-C finish the current job and
exit. ECS waits 30 seconds (stopTimeout) before killing the container; a job cut off
by that is retried by the next worker once its lock times out.
"""
import signal

from django.core.management.base import BaseCommand

from jobs.runner import Worker


class Command(BaseCommand):
    help = 'Run queued background jobs (emails and other tasks).'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--once', action='store_true', help='Run at most one job, then exit.')
        mode.add_argument('--burst', action='store_true', help='Run every job that is due, then exit.')
        parser.add_argument('--max-jobs', type=int, default=None, help='Exit after this many jobs.')
        parser.add_argument('--poll', type=float, default=None, help='Seconds between checks when idle.')

    def handle(self, *args, once=False, burst=False, max_jobs=None, poll=None, **opts):
        worker = Worker()

        def stop(signum, frame):
            if not worker.stopping:
                self.stdout.write(f'{signal.Signals(signum).name}: finishing the current job, then stopping')
            worker.stopping = True

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if not (once or burst):
                self.stdout.write(f'Worker {worker.id} started')
            ran = worker.work(once=once, burst=burst, max_jobs=max_jobs, poll=poll)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        self.stdout.write(self.style.SUCCESS(f'Worker {worker.id} stopped after {ran} job(s)'))
