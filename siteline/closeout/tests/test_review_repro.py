"""
Review of the close-out branch: one test per defect found, each failing before its fix.
"""
import shutil
import tempfile

from django.test import override_settings

from projects.models import Document, DocumentRevision
from tenancy.context import org_context

from .. import services
from ..models import HandoverPack
from .base import CloseoutTestBase, pdf

MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class InternalDrawingsStayOutOfThePackTests(CloseoutTestBase):
    """Issuing a pack lets the client download every item: internal-only drawings must not be in it."""

    def test_internal_and_never_issued_drawings_are_not_handed_to_the_client(self):
        with org_context(self.org_a):
            internal = Document.objects.create(project=self.pa, title='Internal cost markup', reference='INT-01',
                                               category='DRAWING')
            internal.add_revision(DocumentRevision(revision='P01', file=pdf('int.pdf', b'%PDF-1.4 internal'),
                                                   original_name='int.pdf'))
            shared = Document.objects.create(project=self.pa, title='GA plan', reference='GA-01', category='DRAWING',
                                             visibility=Document.VIS_SHARED)
            shared.add_revision(DocumentRevision(revision='C01', file=pdf('ga.pdf', b'%PDF-1.4 ga'),
                                                 original_name='ga.pdf'))
            pack = services.build_pack(self.pa, self.pat)
            titles = set(pack.items.values_list('title', flat=True))
        self.assertIn('GA plan', titles)
        self.assertNotIn('Internal cost markup', titles)
        self.assertEqual(pack.status, HandoverPack.READY)
