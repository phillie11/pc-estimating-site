"""
Delete visitors' QR sign-ins older than each company's retention period (safety/retention.py).
Schedule it daily; it does the work itself, so no background worker is needed:

    cd ~/siteline && .venv/bin/python manage.py purge_signins

Sign-ins by staff and portal users, and incidents, are never deleted. --dry-run counts without deleting.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from safety.retention import purge_visitor_signins
from tenancy.context import org_context
from tenancy.models import Organization


class Command(BaseCommand):
    help = "Delete visitors' site sign-ins older than each company's retention period (default 12 months)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Count what would be deleted; delete nothing.')

    def handle(self, *args, dry_run=False, **opts):
        now = timezone.now()
        total = 0
        # Every company, suspended or not: retention applies to data kept whether or not anyone pays
        for org in Organization.objects.order_by('pk'):
            with org_context(org):
                n, cutoff = purge_visitor_signins(org, now=now, dry_run=dry_run)
            if n:
                self.stdout.write(f'{org.slug}: {n} visitor sign-in{"s" if n != 1 else ""} from before '
                                  f'{timezone.localdate(cutoff):%d %b %Y} {"would be " if dry_run else ""}deleted')
            total += n
        self.stdout.write(f'{"Would delete" if dry_run else "Deleted"} {total} visitor sign-in{"s" if total != 1 else ""}.')
