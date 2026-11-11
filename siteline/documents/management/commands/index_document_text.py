"""
Queue every PDF revision whose words haven't been read yet, so search can find documents by the
words inside them (documents/textindex.py). Run it after the PDF reader is first used or changed; new
uploads are read as they arrive.

    python manage.py index_document_text            # every company
    python manage.py index_document_text --org acme # one company

Each company is done inside its own org_context, and each revision is read by the
documents.extract_text background job (which waits for the virus check). Safe to run twice.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from documents import textindex
from documents.models import RevisionText
from projects.models import DocumentRevision
from tenancy.context import org_context
from tenancy.models import Organization


class Command(BaseCommand):
    help = 'Queue the PDF revisions not yet read for search inside documents.'

    def add_arguments(self, parser):
        parser.add_argument('--org', help='Only this company (its workspace name in the address, e.g. acme).')

    def handle(self, *args, **opts):
        if not textindex.available():
            self.stdout.write('Searching inside PDFs is not available (pypdf is not installed), so there is '
                              'nothing to do. See documents/textindex.py.')
            return
        orgs = Organization.objects.order_by('pk')
        if opts.get('org'):
            orgs = orgs.filter(slug=opts['org'])
        key = 'backfill-' + timezone.now().strftime('%Y%m%d%H%M')
        total = 0
        for org in orgs:
            with org_context(org):
                done = RevisionText.objects.values('revision_id')
                revs = DocumentRevision.objects.exclude(pk__in=done).only('pk', 'original_name', 'file', 'organization')
                n = 0
                for rev in revs.iterator():
                    if textindex.is_pdf(rev):
                        textindex.enqueue(rev, key=key)
                        n += 1
                if n:
                    self.stdout.write(f'{org.name}: {n} PDF revision{"s" if n != 1 else ""} queued.')
                total += n
        self.stdout.write(f'{total} queued in all. The worker reads them (run_worker, or run_worker --burst).')
