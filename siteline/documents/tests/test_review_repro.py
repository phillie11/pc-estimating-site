"""
Review repros for searching inside PDFs (stream G review). Each test failed on origin/claude/r3b-docs.
"""
from documents import textindex
from projects.models import DocumentRevision
from tenancy.context import org_context

from .test_textindex import TextBase


class WithdrawnTextTests(TextBase):
    """
    Externals can't open a withdrawn revision (no download, no viewer), so its words must not be
    quoted to them in the global search, nor find the document in the register.
    """

    def setUp(self):
        super().setUp()
        self.spec = self.upload_doc('Wrong file: tender margin 14 percent on joinery package')
        with org_context(self.org_a):
            rev = self.spec.revisions.get()
            self.assertEqual(textindex.matching_revisions(DocumentRevision.objects.all(), 'margin').count(), 1)
            DocumentRevision.objects.filter(pk=rev.pk).update(status=DocumentRevision.WITHDRAWN)

    def search(self, user, q):
        self.login(user)
        return self.client.get(self.url('search'), {'q': q}).content.decode()

    def test_global_search_does_not_quote_a_withdrawn_revision_to_externals(self):
        detail = self.purl('document_detail', self.spec.pk)
        self.assertIn(detail, self.search(self.staff, 'tender margin'))  # staff can still open it
        for user in (self.cli, self.con, self.sub1):
            page = self.search(user, 'tender margin')
            self.assertNotIn('tender margin 14 percent', page, user.email)
            self.assertNotIn(detail, page, user.email)

    def test_register_does_not_find_a_withdrawn_revision_by_its_words_for_externals(self):
        detail = self.purl('document_detail', self.spec.pk)
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('document_list'), {'q': 'tender margin'}), detail)
        self.login(self.cli)
        r = self.client.get(self.purl('document_list'), {'q': 'tender margin'})
        self.assertNotContains(r, detail)
        self.assertNotContains(r, 'found inside the file')
