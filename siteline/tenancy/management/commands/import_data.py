"""
Load a file made by export_data into an empty, freshly migrated database.

    python manage.py import_data ~/siteline-data.json.gz

Runs loaddata inside unscoped() (natural-key and many-to-many lookups use the
tenant-scoped managers) and in one transaction, so a failure loads nothing.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from tenancy.context import unscoped


class Command(BaseCommand):
    help = 'loaddata of a whole-database export from export_data, in one transaction.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='file made by export_data')

    def handle(self, *args, path, **opts):
        with unscoped(), transaction.atomic():
            call_command('loaddata', path, verbosity=opts['verbosity'])
