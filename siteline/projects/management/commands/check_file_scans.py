"""
Look up GuardDuty's verdict on stored files and alert people about bad ones.

Downloads are already blocked until a file is scanned clean; this catches an
infected or unscannable upload straight away, before anyone tries to open it,
and tells the person who uploaded it as well as the project's staff.

    python manage.py check_file_scans              # run hourly as a PythonAnywhere task
    python manage.py check_file_scans --retrigger  # once, for files uploaded before scanning was on

GuardDuty only scans files uploaded after Malware Protection was enabled.
--retrigger rewrites each still-unscanned file in place (same name, content and
type), which counts as a new upload, so GuardDuty scans it. Run it before adding
the bucket policy that refuses unscanned files, because it needs to read them.
"""
import os

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import models

from projects import files
from projects.models import FileScan
from tenancy.models import Organization

UPLOADER_FIELDS = ('uploaded_by', 'submitted_by', 'created_by', 'raised_by', 'asked_by', 'issued_by')


def retrigger_scan(fieldfile):
    """Copy a stored file onto itself so GuardDuty sees a new upload and scans it."""
    storage = fieldfile.storage
    client = storage.connection.meta.client
    head = client.head_object(Bucket=storage.bucket_name, Key=fieldfile.name)
    client.copy_object(Bucket=storage.bucket_name, Key=fieldfile.name,
                       CopySource={'Bucket': storage.bucket_name, 'Key': fieldfile.name},
                       MetadataDirective='REPLACE', ContentType=head.get('ContentType', 'binary/octet-stream'),
                       Metadata=head.get('Metadata', {}))


def project_of(row):
    if getattr(row, 'project_id', None):
        return row.project
    for parent in ('document', 'addendum'):
        obj = getattr(row, parent, None)
        if obj is not None and getattr(obj, 'project_id', None):
            return obj.project
    return None


def uploader_of(row):
    return next((getattr(row, f) for f in UPLOADER_FIELDS if getattr(row, f + '_id', None)), None)


class Command(BaseCommand):
    help = 'Record malware scan results for stored files and alert on infected or unscannable ones.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=1000, help='Most files to look up in one run.')
        parser.add_argument('--retrigger', action='store_true',
                            help='Ask GuardDuty to scan files that have no verdict yet (uploaded before scanning).')

    def handle(self, *args, limit=1000, retrigger=False, **opts):
        if not files.scanning_enabled():
            raise CommandError('Cloud storage with malware scanning is not switched on (STORAGE_BUCKET).')
        done = set(FileScan.all_objects.values_list('path', flat=True))
        counts = {'clean': 0, 'blocked': 0, 'pending': 0}
        looked_up = 0
        for model in apps.get_models():
            fields = [f.name for f in model._meta.get_fields() if isinstance(f, models.FileField)]
            if not fields:
                continue
            manager = getattr(model, 'all_objects', model._default_manager)
            for row in manager.all().iterator():
                for name in fields:
                    fieldfile = getattr(row, name)
                    if not fieldfile or fieldfile.name in done or looked_up >= limit:
                        continue
                    looked_up += 1
                    status = files.fetch_scan_tag(fieldfile)
                    if status is None:
                        counts['pending'] += 1
                        if retrigger:
                            retrigger_scan(fieldfile)
                        continue
                    org = row if isinstance(row, Organization) else row.organization
                    FileScan.all_objects.get_or_create(path=fieldfile.name, defaults={'status': status, 'organization': org})
                    done.add(fieldfile.name)
                    if status == FileScan.CLEAN:
                        counts['clean'] += 1
                        continue
                    counts['blocked'] += 1
                    uploader = uploader_of(row)
                    files.alert_blocked(org, fieldfile.name, status, str(row) or os.path.basename(fieldfile.name),
                                  project_of(row), extra=[uploader] if uploader else ())
                    self.stderr.write(f'{status}: {fieldfile.name}')
        self.stdout.write(self.style.SUCCESS(
            f'Clean {counts["clean"]}, blocked {counts["blocked"]}, still being scanned {counts["pending"]}.'))
        if retrigger and counts['pending']:
            self.stdout.write(f'Asked GuardDuty to scan {counts["pending"]} file(s). Run this again in a few minutes '
                              '(without --retrigger) to collect the results.')
