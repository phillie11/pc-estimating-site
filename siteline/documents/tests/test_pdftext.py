"""
The real PDF reader (documents/pdftext.py, pypdf) on PDFs built in the tests: words found, scans marked
"no text", encrypted and broken files failing quietly, the page and time limits, and a hostile file
that asks for scripts, attachments and web addresses. The upload tests use the whole path with no
stand-in reader: upload, read, find.
"""
import io
import time
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from documents import pdftext, textindex
from documents.models import RevisionText
from documents.tasks import extract_text
from jobs.models import Job
from projects.models import Document
from tenancy.context import org_context

from .base import DocBase, upload


def real_pdf(pages=('Fire damper schedule',), encrypt=None, compress=1):
    """A real PDF (ReportLab) with each string on its own page; None makes a blank, scan-like page."""
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pageCompression=compress, encrypt=StandardEncryption(encrypt) if encrypt else None)
    for words in pages:
        if words:
            c.drawString(72, 700, words)
        c.showPage()
    c.save()
    return buf.getvalue()


def assemble(bodies):
    """A PDF from object bodies (bytes, numbered from 1), with a proper cross-reference table."""
    out, offsets = b'%PDF-1.4\n', []
    for n, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f'{n} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    size = len(bodies) + 1
    out += f'xref\n0 {size}\n0000000000 65535 f \n'.encode() + b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    return out + f'trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def stream_obj(data, extra=''):
    return f'<< {extra} /Length {len(data)} >>\nstream\n'.encode() + data + b'\nendstream'


def hostile_pdf():
    """One page of plain words, plus JavaScript, an open action, an attachment and a link to a web address."""
    return assemble([
        b'<< /Type /Catalog /Pages 2 0 R /OpenAction 6 0 R /Names << /JavaScript << /Names [(a) 6 0 R] >> '
        b'/EmbeddedFiles << /Names [(f.exe) 7 0 R] >> >> >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> /Annots [8 0 R] >>',
        stream_obj(b'BT /F1 12 Tf 20 100 Td (Plain words only) Tj ET'),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /S /JavaScript /JS (app.alert\\(1\\)) >>',
        b'<< /Type /Filespec /F (f.exe) /EF << /F 9 0 R >> >>',
        b'<< /Type /Annot /Subtype /Link /Rect [0 0 50 50] /A << /S /URI /URI (http://127.0.0.1:9/x) >> >>',
        stream_obj(b'MZ00')])


def read_pdf(data, max_pages=500, seconds=30):
    return pdftext.extract(io.BytesIO(data), max_pages, time.monotonic() + seconds)


class ReaderTests(SimpleTestCase):
    def test_reads_the_words_page_by_page(self):
        text, pages, truncated = read_pdf(real_pdf(['Fire damper schedule', 'Intumescent collars']))
        self.assertIn('Fire damper schedule', text)
        self.assertIn('Intumescent collars', text)
        self.assertEqual((pages, truncated), (2, False))

    def test_a_scan_has_no_text(self):
        text, pages, truncated = read_pdf(real_pdf([None, None]))
        self.assertEqual((text.strip(), pages, truncated), ('', 2, False))

    def test_stops_at_the_page_limit(self):
        text, pages, truncated = read_pdf(real_pdf([f'Sheet number {n}' for n in range(1, 8)]), max_pages=3)
        self.assertEqual((pages, truncated), (3, True))
        self.assertIn('Sheet number 3', text)
        self.assertNotIn('Sheet number 4', text)

    def test_a_past_deadline_returns_what_it_has_as_truncated(self):
        self.assertEqual(read_pdf(real_pdf(['One', 'Two']), seconds=-1), ('', 0, True))

    def test_the_deadline_is_also_checked_inside_a_page(self):
        # The clock reads "in time" for the file-level check and the page-level check, then runs out
        ticks = iter([0, 0] + [10 ** 9] * 1000)
        with mock.patch('documents.pdftext.time.monotonic', lambda: next(ticks)):
            text, pages, truncated = pdftext.extract(io.BytesIO(real_pdf(['Never read'])), 5, 100)
        self.assertEqual((text, truncated), ('', True))

    def test_encrypted_broken_and_non_pdf_files_fail_quietly(self):
        good = real_pdf()
        for data in (real_pdf(['Secret'], encrypt='pw'), b'not a pdf at all', b'', b'%PDF-1.4\n1 0 obj << /Foo',
                     good[:len(good) // 2], b'\x00' * 5000):
            with self.assertRaises(textindex.ExtractionFailed, msg=data[:20]):
                read_pdf(data)

    def test_anything_unexpected_from_pypdf_is_converted(self):
        for exc in (RecursionError('deep'), MemoryError(), ValueError('odd'), ZeroDivisionError()):
            with mock.patch('pypdf.PdfReader', side_effect=exc), self.assertRaises(textindex.ExtractionFailed):
                read_pdf(real_pdf())

    def test_a_page_with_a_huge_content_stream_is_skipped_but_the_others_are_read(self):
        data = real_pdf(['Normal page words', 'Second page words'], compress=0)
        with mock.patch.object(pdftext, 'MAX_CONTENT_BYTES', 40):
            text, pages, truncated = read_pdf(data)
        self.assertEqual((text, pages, truncated), ('', 2, False))  # both over 40 bytes here: both skipped
        text, pages, _ = read_pdf(data)
        self.assertIn('Second page words', text)

    def test_a_compressed_stream_that_inflates_past_the_cap_is_skipped(self):
        import zlib
        stream = zlib.compress(b'BT /F1 12 Tf (' + b'a' * 6_000_000 + b') Tj ET')
        self.assertLess(len(stream), 100_000)  # small as stored, big when decoded
        data = assemble([
            b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
            stream_obj(stream, '/Filter /FlateDecode'), b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'])
        self.assertEqual(read_pdf(data), ('', 1, False))

    def test_scripts_attachments_and_web_addresses_are_ignored(self):
        with mock.patch('socket.socket.connect', side_effect=AssertionError('network used')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('program started')):
            text, pages, truncated = read_pdf(hostile_pdf())
        self.assertEqual((text.strip(), pages, truncated), ('Plain words only', 1, False))


class UploadTests(DocBase):
    """The whole path with the real reader: upload, read, find."""

    def upload_real(self, data, ref='SLN-ABC-ZZ-02-SP-A-2001'):
        self.login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.purl('document_list'), {
                'title': 'M&E spec', 'reference': ref, 'category': 'SPEC', 'visibility': 'SHARED',
                'file': upload('spec.pdf', data), 'revision': 'P01', 'suitability': 'S2'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            doc = Document.objects.get(reference=ref)
            return doc, RevisionText.objects.filter(revision__document=doc).first()

    def test_search_finds_a_real_pdf_by_its_words(self):
        doc, row = self.upload_real(real_pdf(['Acoustic ceiling tiles to level two']))
        self.assertEqual(row.status, RevisionText.DONE)
        self.assertContains(self.client.get(self.purl('document_list'), {'q': 'ceiling tiles'}),
                            self.purl('document_detail', doc.pk))
        self.assertContains(self.client.get(self.url('search'), {'q': 'ceiling tiles'}),
                            'Found in the file: Acoustic ceiling tiles to level two')
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'can be searched.')

    def test_scan_encrypted_and_garbage_are_marked_and_the_upload_still_works(self):
        for n, (data, status) in enumerate((
                (real_pdf([None]), RevisionText.NO_TEXT),
                (real_pdf(['Secret'], encrypt='pw'), RevisionText.FAILED),
                (b'garbage bytes, not a PDF', RevisionText.FAILED))):
            doc, row = self.upload_real(data, ref=f'SLN-ABC-ZZ-02-SP-A-20{n:02d}')
            self.assertEqual(row.status, status, n)
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'be read for search')

    def test_a_long_pdf_is_read_up_to_the_page_limit(self):
        with mock.patch.object(textindex, 'MAX_PAGES', 3):
            doc, row = self.upload_real(real_pdf([f'Detail sheet {n}' for n in range(1, 7)]))
        self.assertEqual((row.status, row.pages_read, row.truncated), (RevisionText.DONE, 3, True))
        self.assertContains(self.client.get(self.purl('document_detail', doc.pk)), 'the first 3 pages')

    def test_a_deadline_already_passed_gives_a_truncated_row(self):
        with mock.patch.object(textindex, 'TIME_LIMIT_SECONDS', -1):
            doc, row = self.upload_real(real_pdf(['One', 'Two']))
        self.assertEqual((row.status, row.truncated), (RevisionText.NO_TEXT, True))

    def test_the_backfill_command_reads_rows_that_were_never_read(self):
        with mock.patch.object(textindex, 'EXTRACTOR', None):
            doc, row = self.upload_real(real_pdf(['Existing spec words']))
        self.assertIsNone(row)
        out = io.StringIO()
        call_command('index_document_text', stdout=out)
        self.assertIn('Alpha Fitout: 1 PDF revision queued.', out.getvalue())
        with org_context(self.org_a):
            for job in Job.objects.filter(name='documents.extract_text'):
                extract_text(job.payload)
            self.assertEqual(RevisionText.objects.get().text, 'Existing spec words')
