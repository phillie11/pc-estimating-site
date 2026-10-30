"""
Export every company's data for a database move or a full backup.

    python manage.py export_data --output ~/siteline-data.json.gz

dumpdata on its own stops with TenantContextMissing: with --all it reads rows
through the base managers, but many-to-many fields (a subcontractor's trades)
still go through the tenant-scoped default manager. This runs it inside
unscoped(), with the options the move needs. See infra/scripts/migrate-data.md.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand

from tenancy.context import unscoped

EXCLUDE = ['contenttypes', 'auth.permission', 'sessions', 'axes']


class Command(BaseCommand):
    help = 'dumpdata of every organisation, for moving or backing up the whole database.'

    def add_arguments(self, parser):
        parser.add_argument('--output', '-o', required=True, help='file to write, e.g. siteline-data.json.gz')

    def handle(self, *args, output, **opts):
        with unscoped():
            call_command('dumpdata', all=True, use_natural_foreign_keys=True, exclude=EXCLUDE,
                         output=output, verbosity=opts['verbosity'])
