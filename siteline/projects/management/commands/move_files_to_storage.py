"""
Copy uploaded files from this server's disk (MEDIA_ROOT) into cloud storage.

Run once after setting STORAGE_BUCKET in .env:

    python manage.py move_files_to_storage --dry-run   # see what would be copied
    python manage.py move_files_to_storage             # copy

Safe to run again: files already in the bucket are skipped. Nothing is deleted
locally; remove MEDIA_ROOT yourself once the site has been checked.
"""
from django.apps import apps
from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import models


def stored_file_names():
    """Every file name referenced by any FileField, across every company."""
    for model in apps.get_models():
        fields = [f.name for f in model._meta.get_fields() if isinstance(f, models.FileField)]
        if not fields:
            continue
        manager = getattr(model, 'all_objects', model._default_manager)
        for row in manager.values_list(*fields):
            for name in row:
                if name:
                    yield name


class Command(BaseCommand):
    help = 'Copy uploaded files from local disk into cloud storage (STORAGE_BUCKET).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **opts):
        if not settings.STORAGE_BUCKET:
            raise CommandError('STORAGE_BUCKET is not set, so there is nowhere to copy to.')
        local = FileSystemStorage(location=settings.MEDIA_ROOT)
        copied = skipped = missing = 0
        for name in sorted(set(stored_file_names())):
            if not local.exists(name):
                missing += 1
                self.stderr.write(f'missing locally: {name}')
                continue
            if default_storage.exists(name):
                skipped += 1
                continue
            if not dry_run:
                with local.open(name, 'rb') as fh:
                    saved = default_storage.save(name, fh)
                if saved != name:
                    raise CommandError(f'Storage renamed {name} to {saved}; stopping so links do not break.')
            copied += 1
            self.stdout.write(f'{"would copy" if dry_run else "copied"}: {name}')
        self.stdout.write(self.style.SUCCESS(
            f'{"Would copy" if dry_run else "Copied"} {copied}, already there {skipped}, missing locally {missing}.'))
