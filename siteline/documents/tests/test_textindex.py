"""
Searching inside PDFs (documents/textindex.py): reading at upload (inline for small files, the
job for big ones or while the virus check runs), the hostile-file gates, what the document page
says, the register's search box, the global search for each role, the back-fill command, and
tenancy. These tests plug in a stand-in reader that reads WORDS(...) out of the file; the real
reader (pypdf) is tested on real PDFs in test_pdftext.py. "Not available" is tested with no reader.
"""
import io
import re
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from documents import textindex
from documents.models import RevisionText
from jobs.models import Job
from projects.models import Document, DocumentRevision, FileScan
from tenancy.context import TenantContextMissing, org_context

from .base import DocBase, pdf_bytes, upload


def words_pdf(text):
    return pdf_bytes() + f'% WORDS({text})\n'.encode()


def stand_in(fh, max_pages, deadline):
    """A stand-in PDF reader for the tests: the words are whatever is in WORDS(...)."""
    m = re.search(rb'WORDS\((.*?)\)', fh.read())
    if m and m.group(1) == b'BROKEN':
        raise textindex.ExtractionFailed('broken')
    if m and m.group(1) == b'CRASH':
        raise RuntimeError('reader crashed')
    return (m.group(1).decode() if m else ''), 1, False



class PureTests(SimpleTestCase):
    def test_normalise(self):
        self.assertEqual(textindex.normalise('  Fire\x00 damper\n\tschedule\u200b '), ('Fire damper schedule', False))
        text, cut = textindex.normalise('x' * (textindex.MAX_CHARS + 5))
        self.assertEqual((len(text), cut), (textindex.MAX_CHARS, True))

    def test_snippet(self):
        text = ('General notes. ' * 10) + 'All fire dampers to be tested and certified before ceiling close. ' + \
               ('More notes. ' * 10)
        s = textindex.snippet(text, 'FIRE DAMPERS')
        self.assertIn('fire dampers to be tested', s)
        self.assertTrue(s.startswith('…') and s.endswith('…'))
        self.assertLess(len(s), 200)
        self.assertEqual(textindex.snippet(text, 'sprinkler'), '')
        self.assertEqual(textindex.snippet('fire', 'fire'), 'fire')

    def test_on_by_default_and_off_without_a_reader(self):
        self.assertTrue(textindex.available())
        with mock.patch.object(textindex, 'EXTRACTOR', None):
            self.assertFalse(textindex.available())


class TextBase(DocBase):
    READER = staticmethod(stand_in)

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(textindex, 'EXTRACTOR', self.READER))

    def upload_doc(self, words, user=None, name='spec.pdf', **data):
        self.login(user or self.staff)
        base = {'title': 'M&E spec', 'reference': 'SLN-ABC-ZZ-02-SP-A-2001', 'category': 'SPEC',
                'visibility': 'SHARED', 'file': upload(name, words_pdf(words)), 'revision': 'P01',
                'suitability': 'S2'}
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.purl('document_list'), {**base, **data})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        with org_context(self.org_a):
            return Document.objects.get(reference=base['reference'] if 'reference' not in data else data['reference'])

    def row(self, doc):
        with org_context(self.org_a):
            return RevisionText.objects.filter(revision__document=doc).first()


class UploadTests(TextBase):
    def test_small_pdf_is_read_when_the_upload_commits(self):
        doc = self.upload_doc('All fire dampers to be tested')
        row = self.row(doc)
        self.assertEqual((row.status, row.text), (RevisionText.DONE, 'All fire dampers to be tested'))
        r = self.client.get(self.purl('document_detail', doc.pk))
        self.assertContains(r, 'The words in rev P01 can be searched.')

    def test_scan_still_running_goes_to_the_job(self):
        with mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.fetch_scan_tag', return_value=None):
            doc = self.upload_doc('Acoustic ceiling tiles')
        self.assertIsNone(self.row(doc))
        with org_context(self.org_a):
            job = Job.objects.get(name='documents.extract_text')
        self.assertEqual(self.client.get(self.purl('document_detail', doc.pk)).status_code, 200)
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'still being read for search')
        # The job waits, then reads once the scan is clean
        from documents.tasks import extract_text
        with org_context(self.org_a), mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.fetch_scan_tag', return_value=None):
            extract_text(job.payload)
            self.assertEqual(Job.objects.filter(name='documents.extract_text').count(), 2)
        with org_context(self.org_a):
            extract_text(job.payload)
            self.assertEqual(RevisionText.objects.get().text, 'Acoustic ceiling tiles')
            extract_text(job.payload)  # a second run changes nothing
            self.assertEqual(RevisionText.objects.count(), 1)

    def test_big_files_go_to_the_job(self):
        with mock.patch.object(textindex, 'INLINE_MB', 0):
            doc = self.upload_doc('Big spec')
        self.assertIsNone(self.row(doc))
        with org_context(self.org_a):
            self.assertTrue(Job.objects.filter(name='documents.extract_text').exists())

    def test_blocked_files_are_never_opened(self):
        calls = []
        with mock.patch.object(textindex, 'EXTRACTOR', lambda *a: calls.append(a) or ('x', 1, False)), \
                mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.fetch_scan_tag', return_value=FileScan.INFECTED):
            doc = self.upload_doc('Nasty')
        self.assertEqual((self.row(doc).status, calls), (RevisionText.BLOCKED, []))

    def test_too_big_failed_and_scanned_files(self):
        with mock.patch.object(textindex, 'MAX_FILE_MB', 0):
            doc = self.upload_doc('Huge')
        self.assertEqual(self.row(doc).status, RevisionText.TOO_BIG)
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'too big to read for search')
        for words, status in (('BROKEN', RevisionText.FAILED), ('CRASH', RevisionText.FAILED),
                              ('', RevisionText.NO_TEXT)):
            with org_context(self.org_a):
                Document.objects.filter(reference='SLN-ABC-ZZ-02-SP-A-2001').delete()
            doc = self.upload_doc(words)  # the upload itself always works
            self.assertEqual(self.row(doc).status, status, words)
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'probably a scan')

    def test_non_pdfs_are_left_alone(self):
        doc = self.upload_doc('Notes', name='notes.docx')
        self.assertIsNone(self.row(doc))
        self.assertNotContains(self.client.get(self.purl('document_detail', doc.pk)), 'data-text-note')

    def test_new_revisions_are_read_too(self):
        doc = self.upload_doc('First issue')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.purl('document_detail', doc.pk), {
                'file': upload('spec-p02.pdf', words_pdf('Second issue with smoke detectors')), 'revision': 'P02',
                'suitability': 'S2', 'make_current': 'on'})
        with org_context(self.org_a):
            self.assertEqual(sorted(RevisionText.objects.values_list('text', flat=True)),
                             ['First issue', 'Second issue with smoke detectors'])


class NotAvailableTests(TextBase):
    READER = None

    def test_nothing_is_queued_and_the_page_says_so(self):
        doc = self.upload_doc('Fire dampers')
        self.assertIsNone(self.row(doc))
        with org_context(self.org_a):
            self.assertFalse(Job.objects.filter(name='documents.extract_text').exists())
        self.login(self.cli)
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)),
                            'Searching the words inside PDFs isn&#x27;t available at the moment.')

    def test_command_says_there_is_nothing_to_do(self):
        out = io.StringIO()
        call_command('index_document_text', stdout=out)
        self.assertIn('not available', out.getvalue())


class FindingTests(TextBase):
    def setUp(self):
        super().setUp()
        # Shared spec: P01 shared, P02 work in progress (never shared) with an extra word
        self.spec = self.upload_doc('Intumescent collars to all pipe penetrations')
        with org_context(self.org_a):
            self.p02 = self.spec.add_revision(DocumentRevision(
                revision='P02', suitability='S0', file=upload('p02.pdf', words_pdf('Sprinkler heads relocated')),
                original_name='p02.pdf', size=10, uploaded_by=self.staff), make_current=False)
        with org_context(self.org_a):
            textindex.read(self.fresh(self.p02))
        # Internal report
        self.report = self.upload_doc('Margin review of intumescent works', reference='SLN-ABC-ZZ-02-RP-A-3001',
                                      title='Cost review', category='REPORT', visibility='INTERNAL')

    def register(self, q):
        return self.client.get(self.purl('document_list'), {'q': q})

    def test_register_search_finds_words_inside(self):
        self.login(self.staff)
        r = self.register('intumescent')
        self.assertContains(r, self.purl('document_detail', self.spec.pk))
        self.assertContains(r, self.purl('document_detail', self.report.pk))
        self.assertContains(r, 'found inside the file')
        self.assertContains(self.register('sprinkler'), self.purl('document_detail', self.spec.pk))
        self.assertNotContains(self.register('in'), 'found inside the file')  # too short to search inside
        self.login(self.cli)
        r = self.register('intumescent')
        self.assertContains(r, self.purl('document_detail', self.spec.pk))
        self.assertNotContains(r, self.purl('document_detail', self.report.pk))
        # The word only in work in progress finds nothing for the client
        self.assertNotContains(self.register('sprinkler'), self.purl('document_detail', self.spec.pk))

    def hits(self, user, q):
        self.login(user)
        r = self.client.get(self.url('search'), {'q': q})
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_global_search_for_each_role(self):
        spec, report = self.purl('document_detail', self.spec.pk), self.purl('document_detail', self.report.pk)
        page = self.hits(self.staff, 'pipe penetrations')
        self.assertTrue(spec in page)
        self.assertTrue('Found in the file: Intumescent collars to all pipe penetrations' in page)
        self.assertTrue(report in self.hits(self.alice, 'margin review'))
        self.assertTrue(spec in self.hits(self.staff, 'sprinkler'))
        for user in (self.cli, self.con, self.sub1):
            self.assertTrue(spec in self.hits(user, 'pipe penetrations'), user.email)
            self.assertFalse(spec in self.hits(user, 'sprinkler'), user.email)       # WIP revision
            self.assertFalse(report in self.hits(user, 'margin review'), user.email)  # internal document
        for user in (self.bidder, self.outsider):
            self.assertFalse(spec in self.hits(user, 'pipe penetrations'), user.email)

    def test_by_title_needs_no_quote(self):
        self.assertFalse('Found in the file' in self.hits(self.staff, 'M&E spec'))

    def test_another_company_finds_nothing(self):
        self.login(self.bob)
        r = self.client.get(self.url('search', org=self.org_b), {'q': 'pipe penetrations'})
        self.assertFalse(f'/documents/{self.spec.pk}/' in r.content.decode())
        with org_context(self.org_b):
            self.assertFalse(RevisionText.objects.exists())
        self.assertEqual(self.client.get(self.url('search'), {'q': 'pipe penetrations'}).status_code, 404)

    def test_command_queues_what_is_left(self):
        with org_context(self.org_a):
            RevisionText.objects.filter(revision=self.p02).delete()
        out = io.StringIO()
        call_command('index_document_text', stdout=out)
        self.assertIn('Alpha Fitout: 1 PDF revision queued.', out.getvalue())
        with org_context(self.org_a):
            self.assertEqual(Job.objects.filter(name='documents.extract_text', payload__revision_id=self.p02.pk).count(),
                             1)
        call_command('index_document_text', '--org', self.org_b.slug, stdout=io.StringIO())


class FailClosedTests(TestCase):
    def test_no_org_context_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(RevisionText.objects.all())
