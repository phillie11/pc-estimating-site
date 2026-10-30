"""Portal tenders: bidders see only their tender, and everything reaches them as a portal notification."""
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from projects.models import Document, DocumentRevision, Project, ProjectInvite, ProjectMember
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Membership, Notification
from tenancy.tests import PW, Base, make_user

from .models import Addendum, AddendumReceipt, TenderQuery


class TenderBase(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.dry_a = make_user('dan@dry-a.test', 'Dan DryA')
        self.dry_b = make_user('bea@dry-b.test', 'Bea DryB')
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Hotel bid', stage=Project.STAGE_TENDERING, owner=self.alice)
            self.trade = Trade.objects.get_or_create(name='Drylining')[0]
            self.sub_a = Subcontractor.objects.create(company_name='Dry A Ltd', email='dan@dry-a.test')
            self.sub_b = Subcontractor.objects.create(company_name='Dry B Ltd', email='bea@dry-b.test')
            for u in (self.dry_a, self.dry_b):
                Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
            self.shared_rfi = RFI.objects.create(project=self.bid, title='Client-side RFI', question='?', is_shared=True)
            self.issued = self._doc('Tender drawings', tender=True)
            self.private = self._doc('Client contract', tender=False, shared=True)

    def _doc(self, title, tender, shared=False):
        d = Document.objects.create(project=self.bid, title=title, tender_issue=tender,
                                    visibility=Document.VIS_SHARED if shared else Document.VIS_INTERNAL)
        DocumentRevision.objects.create(document=d, file=SimpleUploadedFile(f'{title}.pdf', b'%PDF-1.4'),
                                        original_name=f'{title}.pdf', size=8)
        return d

    def invite_both(self):
        self.login(self.alice)
        r = self.client.post(self.url('package_list', self.org_a, self.bid.pk), {
            'action': 'invite', 'trade': self.trade.pk, 'subcontractors': [self.sub_a.pk, self.sub_b.pk],
            'scope': 'Partitions to levels 1-3', 'quote_due': (self.today + timedelta(days=7)).isoformat()})
        self.assertEqual(r.status_code, 302)
        self.pkg_a = Package.all_objects.get(subcontractor=self.sub_a)
        self.pkg_b = Package.all_objects.get(subcontractor=self.sub_b)

    def tender(self, org=None):
        return self.url('tender', org or self.org_a, self.bid.pk)

    def quote(self, user, pkg, value='12000'):
        self.login(user)
        return self.client.post(self.tender(), {'action': 'quote', 'package': pkg.pk, 'quote_value': value,
                                                'quote_file': SimpleUploadedFile('q.pdf', b'%PDF-1.4')})


class InvitationTests(TenderBase):
    def test_existing_portal_users_become_bidders_and_are_notified(self):
        self.invite_both()
        m = ProjectMember.all_objects.get(project=self.bid, user=self.dry_a)
        self.assertEqual((m.role, m.subcontractor), (ProjectMember.ROLE_BIDDER, self.sub_a))
        n = Notification.all_objects.get(user=self.dry_a)
        self.assertIn('Invitation to tender', n.title)
        self.assertEqual(n.url, self.tender())
        self.assertTrue(any(self.tender() in m.body for m in mail.outbox if 'dan@dry-a.test' in m.to))
        self.assertFalse(any(m.attachments for m in mail.outbox))

    def test_new_contact_gets_portal_signup_link_and_lands_on_tender(self):
        with org_context(self.org_a):
            new = Subcontractor.objects.create(company_name='Fresh Partitions', email='new@fresh.test')
        self.login(self.alice)
        self.client.post(self.url('package_list', self.org_a, self.bid.pk), {
            'action': 'invite', 'trade': self.trade.pk, 'subcontractors': [new.pk], 'scope': 'x',
            'quote_due': (self.today + timedelta(days=7)).isoformat()})
        inv = ProjectInvite.all_objects.get(email='new@fresh.test')
        self.assertEqual(inv.role, ProjectMember.ROLE_BIDDER)
        self.assertIn(f'/invite/project/{inv.token}/', mail.outbox[-1].body)
        self.client.logout()
        r = self.client.post(f'/invite/project/{inv.token}/', {'full_name': 'Fay New', 'password': PW, 'password2': PW,
                                                               'password1': PW})
        r = self.client.get(self.url('project_detail', self.org_a, self.bid.pk))
        self.assertRedirects(r, self.tender())

    def test_inviting_twice_does_not_duplicate(self):
        self.invite_both()
        self.invite_both()
        self.assertEqual(Package.all_objects.filter(project=self.bid).count(), 2)


class BidderIsolationTests(TenderBase):
    def setUp(self):
        super().setUp()
        self.invite_both()
        self.login(self.dry_a)

    def test_bidder_only_reaches_the_tender(self):
        self.assertRedirects(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)), self.tender())
        for name in ('rfi_list', 'document_list', 'programme', 'task_list', 'issue_list', 'project_team', 'variation_list'):
            self.assertEqual(self.client.get(self.url(name, self.org_a, self.bid.pk)).status_code, 403, name)
        with org_context(self.org_a):
            current = self.private.current
        self.assertEqual(self.client.get(self.url('document_download', self.org_a, self.bid.pk, self.private.pk,
                                                  current.pk)).status_code, 403)
        r = self.client.get(self.url('rfi_register', self.org_a))
        self.assertNotContains(r, 'Client-side RFI')

    def test_tender_page_shows_scope_and_only_issued_documents(self):
        r = self.client.get(self.tender())
        self.assertContains(r, 'Partitions to levels 1-3')
        self.assertContains(r, 'Tender drawings')
        self.assertNotContains(r, 'Client contract')
        self.assertEqual(self.client.get(self.url('tender_document', self.org_a, self.bid.pk, self.issued.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.url('tender_document', self.org_a, self.bid.pk, self.private.pk)).status_code, 404)
        self.pkg_a.refresh_from_db()
        self.assertIsNotNone(self.pkg_a.viewed_at)

    def test_submit_quote_and_contractor_is_told(self):
        r = self.quote(self.dry_a, self.pkg_a)
        self.assertEqual(r.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual((self.pkg_a.status, self.pkg_a.quote_value), (Package.QUOTED, Decimal('12000')))
        self.assertTrue(Notification.all_objects.filter(user=self.alice, title__startswith='Quote received').exists())

    def test_cannot_quote_or_read_another_bidders_package(self):
        self.assertEqual(self.quote(self.dry_a, self.pkg_b).status_code, 403)
        self.quote(self.dry_b, self.pkg_b, '9000')
        self.login(self.dry_a)
        self.assertEqual(self.client.get(self.url('quote_file', self.org_a, self.bid.pk, self.pkg_b.pk)).status_code, 404)
        self.assertNotContains(self.client.get(self.tender()), '9,000')

    def test_client_on_project_cannot_open_tender(self):
        client = make_user('cli@c.test')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=client, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.bid, user=client, role=ProjectMember.ROLE_CLIENT)
        self.login(client)
        self.assertEqual(self.client.get(self.tender()).status_code, 403)

    def test_other_company_cannot_open_tender(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.tender(self.org_b)).status_code, 404)


class QueryAndAddendumTests(TenderBase):
    def setUp(self):
        super().setUp()
        self.invite_both()

    def test_private_answer_then_publish_without_naming_asker(self):
        self.login(self.dry_a)
        self.client.post(self.tender(), {'action': 'ask', 'question': 'Are bulkheads included?'})
        q = TenderQuery.all_objects.get()
        self.assertTrue(Notification.all_objects.filter(user=self.alice, title__contains=q.reference).exists())
        self.login(self.dry_b)
        self.assertNotContains(self.client.get(self.tender()), 'bulkheads')
        self.login(self.alice)
        self.client.post(self.tender(), {'action': 'answer', 'query': q.pk, 'question': 'Are bulkheads included?',
                                         'answer': 'Yes, see drawing 201.', 'published': 'on'})
        self.assertTrue(Notification.all_objects.filter(user=self.dry_b, title__contains='answered for all').exists())
        self.login(self.dry_b)
        r = self.client.get(self.tender())
        self.assertContains(r, 'see drawing 201')
        self.assertContains(r, 'Issued to all tenderers')
        self.assertNotContains(r, 'Dry A Ltd')

    def test_addendum_notifies_and_tracks_acknowledgement(self):
        self.login(self.alice)
        r = self.client.post(self.tender(), {'action': 'addendum', 'title': 'Revised ceiling layout',
                                             'description': 'Level 2 ceilings now MF.',
                                             'files': [SimpleUploadedFile('rcp.pdf', b'%PDF-1.4')]})
        self.assertEqual(r.status_code, 302)
        a = Addendum.all_objects.get()
        with org_context(self.org_a):
            self.assertEqual(a.files.count(), 1)
            file_pk = a.files.get().pk
        for u in (self.dry_a, self.dry_b):
            self.assertTrue(Notification.all_objects.filter(user=u, title__startswith='Addendum 01').exists())
        self.login(self.dry_a)
        self.client.post(self.tender(), {'action': 'ack', 'addendum': a.pk})
        self.assertTrue(AddendumReceipt.all_objects.filter(addendum=a, subcontractor=self.sub_a).exists())
        self.assertEqual(self.client.get(self.url('addendum_file', self.org_a, self.bid.pk, file_pk)).status_code, 200)
        self.login(self.alice)
        r = self.client.get(self.tender())
        self.assertContains(r, 'Not yet acknowledged')
        self.assertContains(r, 'Dry B Ltd')

    def test_issuing_documents_notifies_bidders(self):
        self.login(self.alice)
        self.client.post(self.tender(), {'action': 'issue_docs', 'docs': [self.issued.pk, self.private.pk]})
        self.private.refresh_from_db()
        self.assertTrue(self.private.tender_issue)
        self.assertTrue(Notification.all_objects.filter(user=self.dry_a, title__contains='Client contract').exists())


class DecisionTests(TenderBase):
    def setUp(self):
        super().setUp()
        self.invite_both()
        self.quote(self.dry_a, self.pkg_a, '10000')
        self.quote(self.dry_b, self.pkg_b, '11000')
        self.login(self.alice)

    def test_appoint_opens_portal_and_closes_out_the_rest(self):
        self.client.post(self.url('package_list', self.org_a, self.bid.pk), {'action': 'appoint', 'package': self.pkg_a.pk})
        self.pkg_b.refresh_from_db()
        self.assertEqual(self.pkg_b.status, Package.UNSUCCESSFUL)
        self.assertEqual(ProjectMember.all_objects.get(project=self.bid, user=self.dry_a).role,
                         ProjectMember.ROLE_SUBCONTRACTOR)
        self.assertFalse(ProjectMember.all_objects.filter(project=self.bid, user=self.dry_b).exists())
        self.assertTrue(Notification.all_objects.filter(user=self.dry_b, title__startswith='Tender outcome').exists())
        self.login(self.dry_a)
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)).status_code, 200)
        self.login(self.dry_b)
        self.assertEqual(self.client.get(self.tender()).status_code, 404)

    def test_comparison_marks_lowest(self):
        r = self.client.get(self.url('package_list', self.org_a, self.bid.pk))
        self.assertContains(r, 'Lowest')
        self.assertContains(r, '+10.0%')

    def test_decline_revokes_access(self):
        with org_context(self.org_a):
            other = Trade.objects.get_or_create(name='Ceilings')[0]
        self.client.post(self.url('package_list', self.org_a, self.bid.pk), {
            'action': 'invite', 'trade': other.pk, 'subcontractors': [self.sub_a.pk], 'scope': 'x',
            'quote_due': (self.today + timedelta(days=7)).isoformat()})
        ceilings = Package.all_objects.get(trade=other)
        self.login(self.dry_a)
        self.client.post(self.tender(), {'action': 'decline', 'package': ceilings.pk, 'reason': 'No capacity'})
        self.assertTrue(ProjectMember.all_objects.filter(project=self.bid, user=self.dry_a).exists())  # still pricing drylining
        self.client.post(self.tender(), {'action': 'decline', 'package': self.pkg_a.pk})
        self.assertFalse(ProjectMember.all_objects.filter(project=self.bid, user=self.dry_a).exists())

    def test_losing_the_main_tender_closes_every_package(self):
        r = self.client.post(self.url('project_stage', self.org_a, self.bid.pk), {'stage': Project.STAGE_LOST})
        # Losing goes through the loss review first (tenders.gate), which then closes the packages
        self.assertRedirects(r, self.url('bid_lost', self.org_a, self.bid.pk))
        self.assertTrue(Package.all_objects.filter(project=self.bid, status__in=Package.OPEN_STATUSES).exists())
        self.client.post(r.url, {'loss_reason': 'PRICE', 'our_price': '100000'})
        self.assertFalse(Package.all_objects.filter(project=self.bid, status__in=Package.OPEN_STATUSES).exists())
        self.assertFalse(ProjectMember.all_objects.filter(project=self.bid, role=ProjectMember.ROLE_BIDDER).exists())


class NotificationTests(TenderBase):
    def test_open_marks_read_and_only_own(self):
        with org_context(self.org_a):
            n = Notification.objects.create(user=self.alice, title='Hi', url=self.tender())
            evil = Notification.objects.create(user=self.alice, title='Bad', url='//evil.test/x')
        self.login(self.dry_a)
        self.assertEqual(self.client.get(self.url('notification_open', self.org_a, n.pk)).status_code, 404)
        self.login(self.alice)
        self.assertRedirects(self.client.get(self.url('notification_open', self.org_a, n.pk)), self.tender())
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)
        r = self.client.get(self.url('notification_open', self.org_a, evil.pk))
        self.assertRedirects(r, self.url('notifications', self.org_a))

    def test_bell_count_is_per_company(self):
        with org_context(self.org_a):
            Notification.objects.create(user=self.alice, title='Unread one', url='/')
        self.login(self.alice)
        r = self.client.get(self.url('estimating_home', self.org_a))
        self.assertEqual(r.context['unread_notifications'], 1)
