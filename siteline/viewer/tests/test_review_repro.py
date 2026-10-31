"""
Failing tests for the defects found reviewing origin/claude/r2-viewer.

Run from siteline/ with the branch checked out:
    cp <this file> viewer/tests/test_review_repro.py
    MEDIA_ROOT=$(mktemp -d) DEBUG=1 python manage.py test viewer.tests.test_review_repro
Every test here fails on the branch as reviewed and should pass once fixed.
"""
from decimal import Decimal
from unittest import mock

from django.test import RequestFactory, override_settings

from projects.models import DocumentRevision
from tenancy.context import org_context
from viewer.models import DrawingPin
from viewer.tests.test_viewer import ViewerBase


class ExternalMovesStaffPinTests(ViewerBase):
    """
    F2. pin_create -> pins.create_pin moves an existing pin for the same (document, record) without
    checking who may change it. An external who cannot remove a staff pin (can_remove is False,
    no Remove button) can still relocate it anywhere on any page/revision they can view.
    """

    def test_subcontractor_cannot_move_staff_pin_on_shared_rfi(self):
        pin = self.pin(rfi=self.rfi_shared, x='0.1', y='0.1', by=self.staff)
        self.login(self.sub2)  # not involved with the RFI beyond it being shared
        r = self.post_pin(f'rfi:{self.rfi_shared.pk}', rev=self.p01, page='2', x='0.9', y='0.9')
        with org_context(self.org_a):
            pin.refresh_from_db()
        # Branch: 201, and the staff pin is now on P01 page 2 at (0.9, 0.9)
        self.assertIn(r.status_code, (403, 400))
        self.assertEqual((pin.x, pin.y, pin.page, pin.revision_id), (Decimal('0.1'), Decimal('0.1'), 1, self.p02.pk))

    def test_assignee_cannot_move_staff_pin_on_their_snag(self):
        pin = self.pin(issue=self.snag1, x='0.2', y='0.2', by=self.staff)
        self.login(self.sub1)
        r = self.post_pin(f'issue:{self.snag1.pk}', x='0.8', y='0.8')
        with org_context(self.org_a):
            pin.refresh_from_db()
        self.assertIn(r.status_code, (403, 400))
        self.assertEqual((pin.x, pin.y), (Decimal('0.2'), Decimal('0.2')))


class CreateFormPinsWithdrawnRevisionTests(ViewerBase):
    """
    F3. pin_create refuses a withdrawn revision, and the viewer hides "Add pin" on one, but
    pins.pending_pin -> placement -> can_view lets staff through on a withdrawn revision, so
    issue_create / rfi_create with ?pin_rev=<withdrawn> pin the new record to it.
    """

    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.p03 = self.ga.add_revision(DocumentRevision(revision='P03', file=self._pdf('P03.pdf'),
                                                             original_name='P03.pdf', size=10, uploaded_by=self.alice))
            self.ga.withdraw(self.p03)
            self.p03.refresh_from_db()

    @staticmethod
    def _pdf(name):
        from documents.tests.base import upload
        return upload(name)

    def q(self):
        return f'?pin_doc={self.ga.pk}&pin_rev={self.p03.pk}&pin_page=1&pin_x=0.5&pin_y=0.5'

    def test_new_snag_is_not_pinned_to_withdrawn_revision(self):
        self.assertTrue(self.p03.is_withdrawn)
        self.login(self.staff)
        url = self.purl('issue_create') + self.q()
        self.assertNotContains(self.client.get(url), 'This will be pinned')  # branch: shows the banner
        self.client.post(url, {'title': 'On withdrawn', 'issue_type': 'SNAG', 'priority': 'NORMAL', 'location': 'x'})
        with org_context(self.org_a):
            self.assertFalse(DrawingPin.objects.filter(revision=self.p03).exists())  # branch: pin created

    def test_new_rfi_is_not_pinned_to_withdrawn_revision(self):
        self.login(self.staff)
        url = self.purl('rfi_create') + self.q()
        r = self.client.post(url, {'title': 'On withdrawn', 'question': 'q', 'category': 'DESIGN',
                                   'priority': 'NORMAL', 'is_shared': 'on', 'cost_impact': 'TBC'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertFalse(DrawingPin.objects.filter(revision=self.p03).exists())


class _FakeBody:
    """boto3's StreamingBody: bytes are only fetched from S3 as chunks are read."""
    def __init__(self, obj):
        self.obj, self.closed = obj, False

    def iter_chunks(self, chunk_size=1024):
        sent = 0
        while sent < self.obj.size:
            n = min(chunk_size, self.obj.size - sent)
            self.obj.downloaded += n
            self.obj.largest_chunk = max(self.obj.largest_chunk, n)
            sent += n
            yield (b'%PDF' + b'\0' * (n - 4)) if sent == n else b'\0' * n

    def close(self):
        self.closed = True


class _FakeObject:
    """Stands in for boto3's s3.Object: records how much was downloaded, and when."""
    def __init__(self, size):
        self.size, self.downloaded, self.largest_chunk = size, 0, 0
        self.content_length, self.content_encoding = size, None
        self.body = _FakeBody(self)

    def load(self, **kw):
        pass

    def get(self, **kw):
        return {'Body': self.body, 'ContentLength': self.size, 'ContentType': 'binary/octet-stream'}

    def download_fileobj(self, fh, ExtraArgs=None, Config=None):
        chunk = b'%PDF' + b'\0' * (1024 * 1024 - 4)
        for _ in range(self.size // len(chunk)):
            fh.write(chunk)
            self.downloaded += len(chunk)


class ThroughAppBuffersWholeFileTests(ViewerBase):
    """
    F1. serve(..., through_app=True) with S3Storage used fieldfile.open('rb'): django-storages'
    S3File downloads the WHOLE object into an unrolled SpooledTemporaryFile, and FileResponse's
    Content-Length seek forced the full download before a byte was sent. It must stream the
    object's body in bounded chunks, fetching nothing before the first chunk is read.
    """

    @override_settings(STORAGE_BUCKET='siteline-test')
    def test_s3_stream_does_not_buffer_whole_object_in_memory(self):
        from storages.backends.s3 import S3Storage
        from projects.files import serve

        size = 20 * 1024 * 1024
        fake = _FakeObject(size)
        storage = S3Storage(bucket_name='siteline-test', access_key='x', secret_key='y', region_name='eu-west-2')
        req = RequestFactory().get('/')
        req.user, req.org = self.staff, self.org_a
        with org_context(self.org_a):
            rev = DocumentRevision.objects.get(pk=self.p02.pk)
        rev.file.storage = storage
        with mock.patch('projects.files.scanning_enabled', return_value=False), \
                mock.patch.object(S3Storage, 'bucket', new_callable=mock.PropertyMock,
                                  return_value=mock.Mock(Object=mock.Mock(return_value=fake))), \
                org_context(self.org_a):
            resp = serve(req, rev.file, 'GA.pdf', project=self.pa, label='GA', through_app=True)
            self.assertEqual(fake.downloaded, 0, 'object downloaded before the response started')
            first = next(iter(resp.streaming_content))
            self.assertTrue(first.startswith(b'%PDF'))
            self.assertLessEqual(fake.downloaded, 64 * 1024, 'more than one chunk fetched for the first read')
            resp.close()
        self.assertLess(fake.downloaded, size, 'whole object downloaded')
        self.assertLessEqual(fake.largest_chunk, 64 * 1024)
        self.assertTrue(fake.body.closed, 'S3 body left open after the response closed')
        # Same headers as a file served from this server's disk
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertEqual(resp['Content-Length'], str(size))
        self.assertTrue(resp['Content-Disposition'].startswith('inline'))
        self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', resp['Content-Security-Policy'])
        self.assertEqual(resp['Cache-Control'], 'private, no-store')

    def test_s3_storage_spills_to_disk_past_5mb(self):
        """Backstop for anything else that reads through S3File: the cloud storage settings cap memory at 5 MB."""
        from pathlib import Path
        from django.conf import settings
        text = (Path(settings.BASE_DIR) / 'config' / 'settings.py').read_text()
        block = text[text.index("'BACKEND': 'storages.backends.s3.S3Storage'"):]
        self.assertIn("'max_memory_size': 5 * 1024 * 1024", block[:block.index('\n    }')])
