"""pdf_page_count counts page objects without parsing untrusted PDFs."""
import io

from django.test import SimpleTestCase

from documents.services import pdf_page_count


class PageCountTests(SimpleTestCase):
    def test_counts_page_objects_not_the_pages_tree(self):
        pdf = b'%PDF-1.7\n1 0 obj <</Type /Pages /Count 2>> endobj\n2 0 obj <</Type/Page>> endobj\n3 0 obj <</Type /Page /Parent 1 0 R>> endobj\n'
        self.assertEqual(pdf_page_count(io.BytesIO(pdf)), 2)

    def test_not_a_pdf_or_no_visible_pages_gives_none(self):
        self.assertIsNone(pdf_page_count(io.BytesIO(b'PK\x03\x04 a zip, not a pdf /Type /Page')))
        self.assertIsNone(pdf_page_count(io.BytesIO(b'%PDF-1.7\n<</Type /ObjStm>> compressed pages\n')))

    def test_unreadable_file_gives_none(self):
        class Broken(io.BytesIO):
            def read(self, *a):
                raise OSError('gone')
        self.assertIsNone(pdf_page_count(Broken(b'%PDF')))
