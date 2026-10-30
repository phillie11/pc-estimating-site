"""Regression tests for the independent review of PR #14 (H1, M2, M3, L4-L7)."""
from importlib import import_module
from unittest import mock

from django.apps import apps as global_apps
from django.test import TestCase

from documents import bulk, services, tasks
from documents.models import DistributionRule, Transmittal, TransmittalRecipient, UploadBatch, UploadRow
from projects.models import Document, DocumentRevision, Project, ProjectMember
from tenancy.context import org_context

from .base import DocBase, upload


class WipNeverLeaksTests(DocBase):
    """H1: a revision that was only ever work in progress stays internal whatever happens to it later."""

    def wip_doc(self):
        with org_context(self.org_a):
            d = Document(project=self.pa, title='GA', visibility=Document.VIS_SHARED, created_by=self.alice)
            d.set_number('SLN-ABC-ZZ-02-DR-A-1001')
            d.save()
            p1 = d.add_revision(DocumentRevision(revision='P01', suitability='S0', file=upload('p1.pdf'),
                                                 original_name='p1.pdf', uploaded_by=self.alice))
        return d, p1

    def get(self, user, d, rev):
        self.login(user)
        return self.client.get(self.purl('document_download', d.pk, rev.pk)).status_code

    def test_superseded_wip_stays_hidden(self):
        d, p1 = self.wip_doc()
        with org_context(self.org_a):
            d.add_revision(DocumentRevision(revision='P02', suitability='S0', file=upload('p2.pdf'),
                                            original_name='p2.pdf', uploaded_by=self.alice))
        self.assertEqual(self.fresh(p1).status, DocumentRevision.SUPERSEDED)
        for u in (self.cli, self.sub2):
            self.assertEqual(self.get(u, d, p1), 404)

    def test_withdrawn_wip_stays_hidden(self):
        d, p1 = self.wip_doc()
        with org_context(self.org_a):
            d.withdraw(p1)
        self.assertEqual(self.get(self.cli, d, p1), 404)

    def test_withdrawing_shared_revision_does_not_publish_the_wip_one(self):
        d, revs = self.doc(revisions=('P01',), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            wip = d.add_revision(DocumentRevision(revision='P02', status=DocumentRevision.WIP, file=upload('w.pdf'),
                                                  original_name='w.pdf'))
            d.set_current(revs['P01'])   # back to P01: P02 superseded
            d.withdraw(revs['P01'])      # P01 withdrawn: P02 takes over, but was never shared
        self.assertEqual(self.fresh(wip).status, DocumentRevision.WIP)
        self.assertEqual(self.get(self.cli, d, wip), 404)

    def test_shared_then_superseded_stays_visible(self):
        d, revs = self.doc(revisions=('P01', 'P02'), visibility=Document.VIS_SHARED)
        self.assertEqual(self.get(self.cli, d, revs['P01']), 200)

    def test_external_make_current_does_not_expose_staff_wip(self):
        self.login(self.sub1)
        self.client.post(self.purl('document_list'), {'title': 'Sub RAMS', 'reference': '', 'category': 'RAMS',
                                                      'file': upload('r1.pdf'), 'revision': 'P01'})
        with org_context(self.org_a):
            d = Document.objects.get(title='Sub RAMS')
            p1 = d.revisions.get()
            wip = d.add_revision(DocumentRevision(revision='P02', status=DocumentRevision.WIP, file=upload('w.pdf'),
                                                  original_name='w.pdf', uploaded_by=self.staff))
        self.client.post(self.purl('document_detail', d.pk), {'action': 'make_current', 'revision': p1.pk})
        self.assertEqual(self.fresh(wip).status, DocumentRevision.SUPERSEDED)
        for u in (self.sub1, self.cli):
            self.assertEqual(self.get(u, d, wip), 404)


class PdfMarkupTests(DocBase):
    """M2: people's text is escaped before it reaches ReportLab's markup parser."""
    NASTY = 'Ceiling <b>void & <img src="/etc/passwd"/> < 3'

    def test_register_sheet_with_markup_in_titles(self):
        self.doc(number='SLN-ABC-ZZ-02-DR-A-1002', title=self.NASTY)
        self.login(self.alice)
        r = self.client.get(self.purl('document_list') + '?export=pdf')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_transmittal_sheet_with_markup(self):
        _, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1003', title=self.NASTY)
        with org_context(self.org_a):
            self.pa.name = 'Bar & <i>kitchen'
            self.pa.save()
            tr = Transmittal(project=self.pa, title='Pack <i>1 & 2', purpose='INFORMATION',
                             cover_note='Line one <b>\nLine two & <img src="x"/>')
            tr.save(actor=self.alice)
            services.set_items(tr, [revs['P01']])
            services.set_recipients(tr, [self.sub1], [])
            services.issue(self.alice, tr)
            tasks.transmittal_sheet({'transmittal_id': tr.pk})
            self.assertTrue(Transmittal.objects.get(pk=tr.pk).sheet)

    def test_escaping_helper(self):
        from documents.pdf import markup
        self.assertEqual(markup('a < b & <img src="x"/>\nnext'), 'a &lt; b &amp; &lt;img src="x"/&gt;<br/>next')


class MigrationKeepsReferenceTests(TestCase):
    """M3: 0013 leaves `reference` as typed and picks the current revision by rank."""

    def test_reference_untouched_and_rank_fallback(self):
        from accounts.models import User
        from accounts.views import create_organization
        migration = import_module('projects.migrations.0013_document_register_data')
        u = User.objects.create_user(email='m@a.test', password='x', full_name='M')
        org = create_organization('Mig Co', u)
        with org_context(org):
            p = Project.objects.create(name='P', stage=Project.STAGE_LIVE)
            d = Document.objects.create(project=p, title='ISO', reference='sln-abc-zz-02-dr-a-1001 ',
                                        visibility='SHARED')
            c01 = DocumentRevision.objects.create(document=d, revision='C01', file=upload('a.pdf'), original_name='a')
            p02 = DocumentRevision.objects.create(document=d, revision='P02', file=upload('b.pdf'), original_name='b')
        # No revision flagged current, and P02 was uploaded last: C01 still ranks highest
        DocumentRevision._base_manager.update(is_current=False, status='', superseded_by=None, shared_at=None)
        migration.fill(global_apps, None)
        d = Document._base_manager.get(pk=d.pk)
        self.assertEqual(d.reference, 'sln-abc-zz-02-dr-a-1001 ')
        self.assertEqual((d.doc_type, d.number), ('DR', '1001'))
        self.assertEqual(DocumentRevision._base_manager.get(pk=p02.pk).superseded_by_id, c01.pk)
        self.assertEqual(DocumentRevision._base_manager.get(pk=c01.pk).status, 'PUBLISHED')
        migration.empty(global_apps, None)
        self.assertEqual(Document._base_manager.get(pk=d.pk).reference, 'sln-abc-zz-02-dr-a-1001 ')


class BannerTests(DocBase):
    """L4: externals aren't told about revisions they haven't been issued."""

    def test_unissued_newer_revision_is_not_named(self):
        d, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1003', title='Plan')
        with org_context(self.org_a):
            services.quick_issue(self.alice, self.pa, [revs['P01']])
            p02 = d.add_revision(DocumentRevision(revision='P02', file=upload('p2.pdf'), original_name='p2.pdf',
                                                  uploaded_by=self.alice))
        self.login(self.sub1)
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P01'].pk))
        self.assertContains(r, 'A newer revision is being prepared')
        self.assertNotContains(r, 'P02')
        self.assertNotContains(r, 'Superseded')
        r = self.client.get(self.purl('document_detail', d.pk))
        self.assertNotContains(r, 'P02')
        self.assertNotContains(r, '>Superseded<')
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, p02.pk)).status_code, 404)
        # Once P02 is issued to them, they're told plainly
        with org_context(self.org_a):
            services.quick_issue(self.alice, self.pa, [self.fresh(p02)])
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P01'].pk))
        self.assertContains(r, 'Superseded by P02')
        # Staff always see the real position
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('revision_detail', d.pk, revs['P01'].pk)), 'Superseded by P02')


class WithdrawnDownloadTests(DocBase):
    """Owner decision: externals see a revision was withdrawn but can't download it; staff can, with a banner."""

    def test_withdrawn(self):
        d, revs = self.doc(revisions=('P01', 'P02'), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            d.withdraw(revs['P02'])
        self.login(self.cli)
        r = self.client.get(self.purl('document_download', d.pk, revs['P02'].pk))
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, 'withdrawn', status_code=403)
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P02'].pk))
        self.assertContains(r, 'Withdrawn: do not use rev P02')
        self.assertNotContains(r, self.purl('document_download', d.pk, revs['P02'].pk))
        self.login(self.staff)
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, revs['P02'].pk)).status_code, 200)
        self.assertContains(self.client.get(self.purl('revision_detail', d.pk, revs['P02'].pk)), 'do not use')


class RecipientRuleTests(DocBase):
    def test_bidder_elsewhere_is_still_a_recipient_here(self):
        """L5: being a tendering sub on another project doesn't drop someone from this one's recipients."""
        with org_context(self.org_a):
            other = Project.objects.create(name='Other bid', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=other, user=self.sub2, role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.sparks)
            users, _ = services.recipient_choices(self.pa)
            self.assertIn(self.sub2, users)
            self.assertIn(self.sub1, users)
            self.assertNotIn(self.bidder, users)

    def test_record_receipt_needs_an_issued_transmittal(self):
        """L7: no recording receipt on a draft (or cancelled) transmittal."""
        with org_context(self.org_a):
            tr = Transmittal(project=self.pa, title='Draft')
            tr.save(actor=self.staff)
            services.set_recipients(tr, [self.cli], [])
            rec = TransmittalRecipient.objects.get(transmittal=tr)
        self.login(self.staff)
        self.client.post(tr.get_absolute_url(), {'action': 'record_receipt', 'recipient': rec.pk})
        self.assertIsNone(self.fresh(rec).acknowledged_at)
        with org_context(self.org_a):
            self.assertEqual(services.acknowledge(self.staff, tr, recipient=rec), 0)

    def test_people_who_left_the_project_are_dropped(self):
        """L7: distribution rules, last-sent lists and issue all skip people no longer on the project."""
        d, revs = self.doc(visibility=Document.VIS_SHARED, discipline='Architect')
        with org_context(self.org_a):
            DistributionRule.objects.create(project=self.pa, discipline='Architect', user=self.con)
            DistributionRule.objects.create(project=self.pa, discipline='Architect', user=self.cli)
            DistributionRule.objects.create(project=self.pa, discipline='Architect', subcontractor=self.sparks)
            ProjectMember.objects.filter(project=self.pa, user=self.con).delete()
            ProjectMember.objects.filter(project=self.pa, subcontractor=self.sparks).delete()
            users, companies = services.usual_recipients(self.pa, [d])
            self.assertEqual((users, companies), ([self.cli], []))
            # A draft addressed to someone who has since left: they're dropped when it's issued
            tr = Transmittal(project=self.pa, title='x')
            tr.save(actor=self.staff)
            services.set_items(tr, [revs['P01']])
            services.set_recipients(tr, [self.con, self.cli], [self.sparks])
            services.issue(self.staff, tr)
            self.assertEqual([r.name for r in tr.recipients.all()], ['Carla Client'])
            only_leavers = Transmittal(project=self.pa, title='y')
            only_leavers.save(actor=self.staff)
            services.set_items(only_leavers, [revs['P01']])
            services.set_recipients(only_leavers, [self.con], [])
            with self.assertRaisesMessage(services.IssueRefused, 'no longer on this project'):
                services.issue(self.staff, only_leavers)


class BulkLockingTests(DocBase):
    """L6: commit and cancel lock the batch and its rows, and only touch rows still waiting."""

    def test_commit_and_cancel_lock(self):
        from django.db.models.query import QuerySet
        self.login(self.staff)
        self.client.post(self.purl('bulk_upload'), {'files': [upload('SLN-ABC-ZZ-02-DR-A-1001_P01.pdf'),
                                                              upload('SLN-ABC-ZZ-02-DR-A-1002_P01.pdf'),
                                                              upload('SLN-ABC-ZZ-02-DR-A-1003_P01.pdf')]})
        with org_context(self.org_a):
            batch = UploadBatch.objects.get()
            rows = list(batch.rows.order_by('pk'))
            locked = []
            real = QuerySet.select_for_update

            def spy(qs, *a, **k):
                locked.append(qs.model)
                return real(qs, *a, **k)
            with mock.patch.object(QuerySet, 'select_for_update', spy):
                bulk.commit(batch, [rows[0].pk], self.staff)
            self.assertIn(UploadBatch, locked)
            self.assertIn(UploadRow, locked)
            # A row someone else committed meanwhile is not committed again
            UploadRow.objects.filter(pk=rows[1].pk).update(state=UploadRow.COMMITTED)
            added, _ = bulk.commit(batch, [rows[1].pk], self.staff)
            self.assertEqual(added, [])
            locked.clear()
            with mock.patch.object(QuerySet, 'select_for_update', spy):
                bulk.cancel(batch)
            self.assertIn(UploadBatch, locked)
            self.assertIn(UploadRow, locked)
            self.assertEqual(UploadRow.objects.get(pk=rows[1].pk).state, UploadRow.COMMITTED)
            self.assertEqual(UploadRow.objects.get(pk=rows[2].pk).state, UploadRow.SKIPPED)
            # A cancelled batch takes no more commits
            added, _ = bulk.commit(batch, [rows[2].pk], self.staff)
            self.assertEqual(added, [])
