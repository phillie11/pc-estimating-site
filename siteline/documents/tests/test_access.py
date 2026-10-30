"""
Who sees which documents and revisions: transmittals share with externals, role sharing
keeps today's shared documents working, every role's view of each page, and isolation
between companies.
"""
from django.test import TestCase

from documents import services
from documents.models import CodeList, Transmittal, UploadBatch, pending_acknowledgements
from projects.models import Document, DocumentRevision
from tenancy.context import org_context
from tenancy.models import TenantContextMissing

from .base import DocBase


class ExternalVisibilityTests(DocBase):
    def issue(self, revs, users=(), companies=()):
        with org_context(self.org_a):
            t = Transmittal(project=self.pa, title='Issue', purpose=Transmittal.PURPOSE_CONSTRUCTION)
            t.save(actor=self.staff)
            services.set_items(t, revs)
            services.set_recipients(t, list(users), list(companies))
            services.issue(self.staff, t)
        return t

    def register(self, user):
        self.login(user)
        return self.client.get(self.purl('document_list'))

    def test_sub_sees_only_the_revisions_transmitted_to_them(self):
        d, revs = self.doc('SLN-ABC-ZZ-00-DR-X-5001', 'Joinery details', ('P01', 'P02', 'P03'))
        self.issue([revs['P02']], companies=[self.joiners])
        self.login(self.sub1)
        r = self.client.get(self.purl('document_detail', d.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'P02')
        self.assertNotContains(r, '>P01<')
        self.assertNotContains(r, '>P03<')
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, revs['P02'].pk)).status_code, 200)
        for code in ('P01', 'P03'):
            self.assertEqual(self.client.get(self.purl('document_download', d.pk, revs[code].pk)).status_code, 404)
            self.assertEqual(self.client.get(self.purl('revision_detail', d.pk, revs[code].pk)).status_code, 404)
        # P02 has since been superseded: they're told so
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P02'].pk))
        self.assertContains(r, 'Superseded by P03')
        self.assertContains(self.register(self.sub1b), 'Joinery details')  # same company
        for u in (self.sub2, self.cli, self.con):
            self.assertNotContains(self.register(u), 'Joinery details')
            self.assertEqual(self.client.get(self.purl('document_detail', d.pk)).status_code, 404)

    def test_shared_documents_keep_working_for_every_role(self):
        d, revs = self.doc('SLN-ABC-ZZ-00-DR-A-1001', 'GA', ('P01', 'C01'), visibility=Document.VIS_SHARED)
        for u in (self.cli, self.con, self.sub1, self.sub2):
            self.assertContains(self.register(u), 'GA')
            self.assertEqual(self.client.get(self.purl('document_download', d.pk, revs['C01'].pk)).status_code, 200)
        self.login(self.bidder)
        self.assertEqual(self.client.get(self.purl('document_list')).status_code, 403)
        self.login(self.outsider)
        self.assertEqual(self.client.get(self.purl('document_list')).status_code, 404)

    def test_sharing_with_some_roles_only(self):
        d, _ = self.doc('SLN-ABC-ZZ-00-DR-A-1001', 'Client-only GA', ('P01',), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            d.set_share_roles(['CLIENT', 'CONSULTANT'])
            d.save()
        self.assertContains(self.register(self.cli), 'Client-only GA')
        self.assertContains(self.register(self.con), 'Client-only GA')
        self.assertNotContains(self.register(self.sub1), 'Client-only GA')

    def test_work_in_progress_on_a_shared_document_stays_internal(self):
        d, revs = self.doc('SLN-ABC-ZZ-00-DR-A-1001', 'GA', ('P01',), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            from documents.tests.base import upload
            wip = d.add_revision(DocumentRevision(revision='P02', status=DocumentRevision.WIP, file=upload('p2.pdf'),
                                                  original_name='p2.pdf'))
        self.login(self.cli)
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, wip.pk)).status_code, 404)
        r = self.client.get(self.purl('document_detail', d.pk))
        self.assertNotContains(r, '>P02<')
        # Issuing it shares it
        self.issue([wip], users=[self.cli])
        self.assertEqual(self.fresh(wip).status, DocumentRevision.SHARED)
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, wip.pk)).status_code, 200)

    def test_internal_documents_and_overview(self):
        d, _ = self.doc('CP-01', 'Cost plan', ('P01',))
        self.assertNotContains(self.register(self.cli), 'Cost plan')
        r = self.client.get(self.purl('project_detail'))
        self.assertNotContains(r, 'Cost plan')
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('project_detail')), 'Cost plan')

    def test_externals_own_uploads_stay_visible_to_them(self):
        self.login(self.sub2)
        from documents.tests.base import upload
        self.client.post(self.purl('document_list'), {'title': 'Sparks RAMS', 'reference': '', 'category': 'RAMS',
                                                      'file': upload('r.pdf'), 'revision': 'P01'})
        self.assertContains(self.register(self.sub2), 'Sparks RAMS')


class RoleViewTests(DocBase):
    """Each role against each documents page: what they may open."""

    def test_pages_by_role(self):
        d, revs = self.doc(visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            batch = UploadBatch.objects.create(project=self.pa, created_by=self.staff)
        pages = {
            'register': self.purl('document_list'),
            'detail': self.purl('document_detail', d.pk),
            'revision': self.purl('revision_detail', d.pk, revs['P01'].pk),
            'transmittals': self.purl('transmittal_list'),
            'new transmittal': self.purl('transmittal_create'),
            'bulk': self.purl('bulk_upload'),
            'batch': self.purl('bulk_upload_batch', batch.pk),
            'settings': self.purl('register_settings'),
        }
        staff_only = {'new transmittal', 'bulk', 'batch', 'settings'}
        for user in (self.alice, self.admin, self.staff):
            self.login(user)
            for name, url in pages.items():
                self.assertEqual(self.client.get(url).status_code, 200, (user, name))
        for user in (self.cli, self.con, self.sub1):
            self.login(user)
            for name, url in pages.items():
                self.assertEqual(self.client.get(url).status_code, 403 if name in staff_only else 200, (user, name))
        self.login(self.bidder)
        for name, url in pages.items():
            self.assertEqual(self.client.get(url).status_code, 403, ('bidder', name))
        self.login(self.outsider)
        for name, url in pages.items():
            self.assertEqual(self.client.get(url).status_code, 404, ('outsider', name))

    def test_archived_project_is_read_only(self):
        d, _ = self.doc()
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.purl('document_list')).status_code, 200)
        self.assertEqual(self.client.post(self.purl('bulk_upload'), {}).status_code, 403)
        self.assertEqual(self.client.post(self.purl('document_detail', d.pk), {'action': 'visibility'}).status_code, 403)


class TenantIsolationTests(DocBase):
    def test_another_company_gets_404(self):
        d, revs = self.doc(visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            t = Transmittal(project=self.pa, title='x')
            t.save(actor=self.staff)
        self.login(self.bob)
        for url in (self.url('document_detail', self.pa.pk, d.pk, org=self.org_b),
                    self.url('document_download', self.pa.pk, d.pk, revs['P01'].pk, org=self.org_b),
                    self.url('transmittal_detail', self.pa.pk, t.pk, org=self.org_b),
                    self.url('document_list', self.pa.pk, org=self.org_b)):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.get(self.purl('document_list')).status_code, 404)  # not a member of Alpha

    def test_bravo_never_sees_alpha_rows(self):
        self.doc()
        with org_context(self.org_a):
            CodeList.objects.create(project=self.pa, field='originator', code='ABC')
            t = Transmittal(project=self.pa, title='x')
            t.save(actor=self.staff)
        with org_context(self.org_b):
            self.assertFalse(Document.objects.exists())
            self.assertFalse(Transmittal.objects.exists())
            self.assertFalse(CodeList.objects.exists())
            self.assertEqual(list(pending_acknowledgements(self.cli)), [])


class FailClosedTests(TestCase):
    def test_no_org_context_fails_closed(self):
        from documents.models import DistributionRule, RegisterSettings, TransmittalItem, TransmittalRecipient, UploadRow
        for model in (Transmittal, CodeList, DistributionRule, RegisterSettings, TransmittalItem, TransmittalRecipient,
                      UploadBatch, UploadRow):
            with self.assertRaises(TenantContextMissing, msg=model.__name__):
                list(model.objects.all())
