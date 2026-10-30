"""Transmittals: numbering, issue, notifications, the issue-sheet job, acknowledgement and ball in court."""
from django.core import mail

from core.models import AuditEntry, RecordIndex
from documents import services
from documents.models import DistributionRule, Transmittal, TransmittalRecipient, pending_acknowledgements
from jobs.runner import Worker
from projects.models import Document, DocumentRevision
from tenancy.context import org_context
from tenancy.models import Notification

from .base import DocBase


class TransmittalBase(DocBase):
    def setUp(self):
        super().setUp()
        self.ga, self.ga_revs = self.doc('SLN-ABC-ZZ-02-DR-A-1001', 'GA plan', ('P01', 'P02'))
        self.spec, self.spec_revs = self.doc('SLN-ABC-ZZ-ZZ-SP-A-0001', 'Spec', ('P01',))

    def create(self, revs, users=(), companies=(), then='issue', **extra):
        self.login(self.staff)
        data = {'title': 'For construction', 'purpose': 'CONSTRUCTION', 'cover_note': 'Please build from these.',
                'revisions': [r.pk for r in revs], 'users': [u.pk for u in users],
                'companies': [c.pk for c in companies], 'then': then, **extra}
        r = self.client.post(self.purl('transmittal_create'), data)
        with org_context(self.org_a):
            return r, Transmittal.objects.order_by('-pk').first()


class IssueTests(TransmittalBase):
    def test_issue_numbers_shares_notifies_and_sets_ball(self):
        mail.outbox.clear()
        r, tr = self.create([self.ga_revs['P02']], users=[self.cli], companies=[self.joiners])
        self.assertRedirects(r, tr.get_absolute_url())
        self.assertEqual((tr.ref, tr.status, tr.issued_by, tr.purpose), ('TR-001', 'ISSUED', self.staff, 'CONSTRUCTION'))
        self.assertEqual(self.fresh(self.ga_revs['P02']).status, DocumentRevision.SHARED)  # WIP until issued
        self.assertEqual(self.fresh(self.ga_revs['P01']).status, DocumentRevision.SUPERSEDED)
        for u in (self.cli, self.sub1, self.sub1b):
            self.assertTrue(Notification.all_objects.filter(user=u, title__startswith='TR-001 for construction').exists())
        for u in (self.con, self.sub2, self.bidder):
            self.assertFalse(Notification.all_objects.filter(user=u).exists())
        self.assertEqual({e.to[0] for e in mail.outbox}, {'client@c.test', 'sub1@s.test', 'sub1b@s.test'})
        self.assertEqual((tr.ball_in_court_user, tr.ball_in_court_company), (self.cli, self.joiners))
        with org_context(self.org_a):
            idx = RecordIndex.objects.get(ref='TR-001')
            self.assertTrue(idx.is_open)
            self.assertEqual(idx.ball_in_court_user, self.cli)
            item = tr.items.get()
            self.assertEqual((item.reference, item.revision_code), ('SLN-ABC-ZZ-02-DR-A-1001', 'P02'))
            self.assertTrue(AuditEntry.objects.filter(record_idx=idx, action='issue').exists())

    def test_numbering_is_per_project(self):
        self.create([self.ga_revs['P02']], users=[self.cli])
        _, tr = self.create([self.spec_revs['P01']], users=[self.cli])
        self.assertEqual(tr.ref, 'TR-002')
        with org_context(self.org_a):
            from projects.models import Project
            pa2 = Project.objects.create(name='Second', stage=Project.STAGE_LIVE)
            t = Transmittal(project=pa2, title='x')
            t.save(actor=self.staff)
        self.assertEqual(t.ref, 'TR-001')

    def test_issue_sheet_is_made_by_a_job(self):
        _, tr = self.create([self.ga_revs['P02'], self.spec_revs['P01']], users=[self.cli])
        self.assertFalse(tr.sheet)
        page = self.client.get(tr.get_absolute_url())
        self.assertContains(page, 'Issue sheet being prepared')
        Worker('t').work(burst=True)
        tr = self.fresh(tr)
        self.assertTrue(tr.sheet.name.endswith('.pdf'))
        r = self.client.get(self.purl('transmittal_sheet', tr.pk))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(b''.join(r.streaming_content).startswith(b'%PDF'))
        # Running it again changes nothing
        name = tr.sheet.name
        from documents.tasks import transmittal_sheet
        with org_context(self.org_a):
            transmittal_sheet({'transmittal_id': tr.pk})
        self.assertEqual(self.fresh(tr).sheet.name, name)

    def test_draft_then_issue_and_refusals(self):
        _, tr = self.create([], users=[self.cli], then='draft')
        self.assertEqual(tr.status, 'DRAFT')
        r = self.client.post(tr.get_absolute_url(), {'action': 'issue'}, follow=True)
        self.assertContains(r, 'Add at least one document')
        self.assertEqual(self.fresh(tr).status, 'DRAFT')
        r = self.client.post(self.purl('transmittal_edit', tr.pk), {
            'title': 'Now with docs', 'purpose': 'INFORMATION', 'revisions': [self.spec_revs['P01'].pk],
            'users': [self.cli.pk], 'then': 'issue', 'version': self.fresh(tr).version})
        self.assertEqual(self.fresh(tr).status, 'ISSUED')
        r = self.client.get(self.purl('transmittal_edit', tr.pk), follow=True)
        self.assertContains(r, 'can&#x27;t be changed')

    def test_withdrawn_revisions_cannot_be_issued(self):
        with org_context(self.org_a):
            self.ga.withdraw(self.ga_revs['P02'])
        r, tr = self.create([self.ga_revs['P02']], users=[self.cli])
        self.assertIsNone(tr)  # not even offered on the form
        self.assertContains(r, 'Select a valid choice')
        # and the service refuses one that sneaks onto a draft
        with org_context(self.org_a):
            t = Transmittal(project=self.pa, title='x')
            t.save(actor=self.staff)
            services.set_items(t, [self.fresh(self.ga_revs['P02'])])
            services.set_recipients(t, [self.cli], [])
            with self.assertRaisesMessage(services.IssueRefused, 'withdrawn'):
                services.issue(self.staff, t)

    def test_cancel_a_draft(self):
        _, tr = self.create([self.spec_revs['P01']], users=[self.cli], then='draft')
        self.client.post(tr.get_absolute_url(), {'action': 'cancel'})
        self.assertEqual(self.fresh(tr).status, 'CANCELLED')

    def test_only_staff_issue(self):
        _, tr = self.create([self.spec_revs['P01']], users=[self.cli], then='draft')
        self.login(self.cli)
        self.assertEqual(self.client.get(self.purl('transmittal_create')).status_code, 403)
        self.assertEqual(self.client.get(tr.get_absolute_url()).status_code, 404)  # a draft isn't theirs yet

    def test_transmitted_document_cannot_be_deleted(self):
        self.create([self.spec_revs['P01']], users=[self.cli])
        r = self.client.post(self.purl('document_detail', self.spec.pk), {'action': 'delete'}, follow=True)
        self.assertContains(r, 'stays on the register as a record')
        with org_context(self.org_a):
            self.assertTrue(Document.objects.filter(pk=self.spec.pk).exists())


class AcknowledgeTests(TransmittalBase):
    def setUp(self):
        super().setUp()
        _, self.tr = self.create([self.ga_revs['P02']], users=[self.cli], companies=[self.joiners])

    def test_each_recipient_acknowledges_then_it_completes(self):
        self.login(self.cli)
        with org_context(self.org_a):
            self.assertEqual(list(pending_acknowledgements(self.cli)), [self.tr])
        r = self.client.get(self.tr.get_absolute_url())
        self.assertContains(r, 'Confirm receipt')
        self.client.post(self.tr.get_absolute_url(), {'action': 'acknowledge'})
        tr = self.fresh(self.tr)
        self.assertEqual((tr.status, tr.ball_in_court_user, tr.ball_in_court_company), ('ISSUED', None, self.joiners))
        # Anyone from the company acknowledges for it
        self.login(self.sub1b)
        with org_context(self.org_a):
            self.assertEqual(list(pending_acknowledgements(self.sub1b)), [self.tr])
            self.assertEqual(list(pending_acknowledgements(self.sub2)), [])
        self.client.post(self.tr.get_absolute_url(), {'action': 'acknowledge'})
        tr = self.fresh(self.tr)
        self.assertEqual((tr.status, tr.ball_in_court_company), ('ACKNOWLEDGED', None))
        with org_context(self.org_a):
            rec = TransmittalRecipient.objects.get(transmittal=tr, subcontractor=self.joiners)
            self.assertEqual(rec.acknowledged_by, self.sub1b)
            self.assertFalse(RecordIndex.objects.get(ref='TR-001').is_open)
            self.assertEqual(list(pending_acknowledgements(self.sub1)), [])
        self.assertTrue(Notification.all_objects.filter(user=self.staff, title='TR-001 received by everyone').exists())

    def test_staff_can_record_receipt(self):
        self.login(self.staff)
        with org_context(self.org_a):
            rec = TransmittalRecipient.objects.get(transmittal=self.tr, user=self.cli)
        self.client.post(self.tr.get_absolute_url(), {'action': 'record_receipt', 'recipient': rec.pk,
                                                      'note': 'Signed delivery note'})
        rec = self.fresh(rec)
        self.assertEqual((rec.acknowledged_by, rec.acknowledged_note), (self.staff, 'Signed delivery note'))

    def test_externals_cannot_record_receipt_for_others(self):
        self.login(self.sub1)
        with org_context(self.org_a):
            rec = TransmittalRecipient.objects.get(transmittal=self.tr, user=self.cli)
        r = self.client.post(self.tr.get_absolute_url(), {'action': 'record_receipt', 'recipient': rec.pk})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.fresh(rec).acknowledged_at)

    def test_non_recipients_cannot_see_or_acknowledge(self):
        for u in (self.con, self.sub2):
            self.login(u)
            self.assertEqual(self.client.get(self.tr.get_absolute_url()).status_code, 404)
            self.assertEqual(self.client.post(self.tr.get_absolute_url(), {'action': 'acknowledge'}).status_code, 404)
            self.assertNotContains(self.client.get(self.purl('transmittal_list') + '?status=all'), 'TR-001')
        self.login(self.cli)
        r = self.client.get(self.purl('transmittal_list') + '?status=mine')
        self.assertContains(r, 'TR-001')
        self.assertContains(r, 'Waiting on you')


class UsualRecipientTests(TransmittalBase):
    def test_rules_by_discipline_then_last_transmittal_then_everyone(self):
        with org_context(self.org_a):
            users, companies = services.usual_recipients(self.pa, [self.ga])
            self.assertEqual({u.pk for u in users}, {self.cli.pk, self.con.pk, self.sub1.pk, self.sub1b.pk, self.sub2.pk})
        self.create([self.ga_revs['P02']], users=[self.con])
        with org_context(self.org_a):
            users, companies = services.usual_recipients(self.pa, [self.ga])
            self.assertEqual(users, [self.con])
            DistributionRule.objects.create(project=self.pa, discipline='architect', subcontractor=self.sparks)
            DistributionRule.objects.create(project=self.pa, discipline='Structural engineer', user=self.cli)
            users, companies = services.usual_recipients(self.pa, [self.ga])
            self.assertEqual((users, companies), ([], [self.sparks]))

    def test_issue_button_on_document_page(self):
        with org_context(self.org_a):
            DistributionRule.objects.create(project=self.pa, discipline='', user=self.cli)
        self.login(self.staff)
        r = self.client.get(self.purl('document_detail', self.ga.pk))
        self.assertContains(r, 'Issue rev P02 to usual recipients (Carla Client)')
        r = self.client.post(self.purl('document_detail', self.ga.pk), {'action': 'issue',
                                                                        'revision': self.ga_revs['P02'].pk,
                                                                        'purpose': 'COMMENT'}, follow=True)
        self.assertContains(r, 'TR-001 issued to Carla Client')
        with org_context(self.org_a):
            self.assertEqual(Transmittal.objects.get().purpose, 'COMMENT')

    def test_rules_are_managed_in_settings(self):
        self.login(self.staff)
        self.client.post(self.purl('register_settings'), {'action': 'add_rule', 'discipline': 'Architect',
                                                          'subcontractor': self.joiners.pk})
        r = self.client.post(self.purl('register_settings'), {'action': 'add_rule', 'discipline': 'X'})
        self.assertContains(r, 'Pick a person or a company')
        with org_context(self.org_a):
            rule = DistributionRule.objects.get()
        self.client.post(self.purl('register_settings'), {'action': 'remove_rule', 'rule': rule.pk})
        with org_context(self.org_a):
            self.assertFalse(DistributionRule.objects.exists())
