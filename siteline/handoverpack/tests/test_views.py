"""
The pages: who may open them (every URL and the export), uploading revisions, the win handing the pack
over, issuing, open items in Needs me, and that nothing reaches a client, subcontractor or the API.

People (changes/tests/base.py): alice owner, adam admin, pat PM on the live job (projects only), eve
estimator (estimating only), stan site staff (projects, not on the job's commercial side), carla client,
connie consultant, sid subcontractor, bea tendering subcontractor, nora external not on the job, bob
another company's owner.
"""
import io
from decimal import Decimal as D
from functools import lru_cache

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from changes.tests.base import CommercialTestBase, make_user
from handoverpack import sample, services
from handoverpack.models import HandoverPack, OpenItem, PackClarification, PricingWorkbook
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import ActivityEvent, Membership


@lru_cache(maxsize=None)
def demo_bytes(rev='C', **kw):
    spec = sample.DEMO if rev == 'C' else sample.demo_revision_b()
    return sample.build(spec, **kw)


@lru_cache(maxsize=None)
def stale_bytes():
    return sample.build(sample.DEMO, stale={('Electrical', 'AA9'): D('1.00'), ('PRICING SUMMARY', 'G50'): D('228000')})


def upload_file(data=None, name='Pricing workbook.xlsx'):
    return SimpleUploadedFile(name, data or demo_bytes(), content_type='application/octet-stream')


class Base(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.stan = make_user('stan@a.test', 'Stan Site')
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        self.quinn = make_user('quinn@a.test', 'Quinn QS')
        Membership.objects.create(organization=self.org_a, user=self.quinn, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Alpha bid', stage=Project.STAGE_TENDERING, owner=self.eve,
                                              value=D('200000'))
            ProjectMember.objects.create(project=self.pa, user=self.stan, role=ProjectMember.ROLE_SITE)
            ProjectMember.objects.create(project=self.pa, user=self.quinn, role=ProjectMember.ROLE_QS)
            ProjectMember.objects.create(project=self.bid, user=self.carla, role=ProjectMember.ROLE_CLIENT)
            ProjectMember.objects.create(project=self.bid, user=self.bea, role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.subco)

    def seed(self, project, user, data=None):
        with org_context(self.org_a):
            return services.upload(project, user, upload_file(data))

    def pages(self, project, wb):
        pk = project.pk
        return [self.url('hpack_workbook', pk), self.url('hpack_revision', pk, wb.pk), self.url('hpack_file', pk, wb.pk),
                self.url('hpack_pack', pk), self.url('hpack_pack', pk) + '?export=xlsx', self.url('hpack_items', pk),
                self.url('hpack_clarifications', pk), self.url('hpack_site_rules', pk)]

    def posts(self, project):
        pk = project.pk
        return [self.url('hpack_upload', pk), self.url('hpack_issue', pk), self.url('hpack_items', pk),
                self.url('hpack_clarifications', pk), self.url('hpack_site_rules', pk)]


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.bid_wb = self.seed(self.bid, self.eve)
            self.job_wb = self.seed(self.pa, self.pat)

    def test_bid_pages(self):
        expected = {self.alice: 200, self.adam: 200, self.eve: 200, self.pat: 404, self.stan: 404, self.carla: 403,
                    self.bea: 403, self.sid: 403, self.connie: 403, self.nora: 403, self.bob: 404}
        for url in self.pages(self.bid, self.bid_wb):
            for user, code in expected.items():
                with self.subTest(url=url, user=user.email):
                    self.assertEqual(self.status_for(user, url), code)

    def test_job_pages_use_the_commercial_check(self):
        expected = {self.alice: 200, self.adam: 200, self.pat: 200, self.quinn: 200, self.stan: 403, self.eve: 404,
                    self.carla: 403, self.connie: 403, self.sid: 403, self.bea: 403, self.nora: 403, self.bob: 404}
        for url in self.pages(self.pa, self.job_wb):
            for user, code in expected.items():
                with self.subTest(url=url, user=user.email):
                    self.assertEqual(self.status_for(user, url), code)

    @override_settings(RATELIMIT_ENABLED=False)
    def test_writes_are_refused_to_everyone_without_access(self):
        before = (PricingWorkbook.all_objects.count(), PackClarification.all_objects.count(),
                  HandoverPack.all_objects.filter(issued_at__isnull=False).count())
        for url in self.posts(self.pa):
            for user in (self.stan, self.eve, self.carla, self.sid, self.bea, self.nora, self.bob):
                with self.subTest(url=url, user=user.email):
                    code = self.status_for(user, url, 'post', {'file': upload_file(), 'new-text': 'x',
                                                                 'site_rules': 'x', 'item': '1'})
                    self.assertIn(code, (403, 404))
        self.assertEqual(before, (PricingWorkbook.all_objects.count(), PackClarification.all_objects.count(),
                                  HandoverPack.all_objects.filter(issued_at__isnull=False).count()))

    def test_settings_are_for_owners_and_admins(self):
        url = self.url('hpack_settings')
        for user, code in ((self.alice, 200), (self.adam, 200), (self.pat, 403), (self.eve, 403), (self.carla, 403)):
            with self.subTest(user.email):
                self.assertEqual(self.status_for(user, url), code)

    def test_nothing_in_portals_api_feed_or_externals_needs_me(self):
        import actions
        with org_context(self.org_a):
            OpenItem.objects.filter(project=self.pa).update(owner=self.carla)  # even if someone tried
        for user in (self.carla, self.sid, self.connie):
            with org_context(self.org_a):
                found = [i for i in actions.for_user(self.request_for(user)) if i.kind == 'handover_item']
            self.assertEqual(found, [])
        self.login(self.carla)
        for url in (self.url('project_detail', self.pa.pk), self.url('needs_me'), self.url('activity')):
            r = self.client.get(url)
            self.assertNotContains(r, 'hpack', status_code=r.status_code)
            self.assertNotContains(r, 'andover pack', status_code=r.status_code)
        with org_context(self.org_a):
            self.assertFalse(ActivityEvent.objects.filter(verb__contains='pricing workbook', internal_only=False)
                             .exists())
        from api.resources import resources
        models = {getattr(r, 'model', None) for r in resources()}
        for m in (PricingWorkbook, HandoverPack, OpenItem, PackClarification):
            self.assertNotIn(m, models)


class UploadTests(Base):
    def test_upload_revisions_and_see_what_changed(self):
        self.login(self.eve)
        r = self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file(demo_bytes('B'))})
        with org_context(self.org_a):
            first = PricingWorkbook.objects.get(project=self.bid)
        self.assertRedirects(r, first.get_absolute_url())
        self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file()})
        with org_context(self.org_a):
            wbs = list(PricingWorkbook.objects.filter(project=self.bid).order_by('revision'))
            pack = HandoverPack.objects.get(project=self.bid)
        self.assertEqual([w.revision for w in wbs], [1, 2])
        self.assertEqual(pack.workbook, wbs[1])
        self.assertTrue(wbs[1].reconciled)
        self.assertEqual(wbs[1].charge, D('228753.41'))
        deltas = {d['name']: d for d in services.deltas(wbs[1].result, wbs[0].result)}
        self.assertTrue(deltas['Mechanical']['changed'])
        self.assertEqual(deltas['Mechanical']['cost_delta'], D('450'))  # 18 fan coils, 395 -> 420
        self.assertFalse(deltas['Doors']['changed'])
        page = self.client.get(self.url('hpack_workbook', self.bid.pk))
        self.assertContains(page, self.url('hpack_revision', self.bid.pk, wbs[0].pk))
        self.assertContains(page, 'hp-delta')
        # The file goes out through the files app, with the download logged
        from projects.models import FileAccess
        r = self.client.get(self.url('hpack_file', self.bid.pk, wbs[0].pk))
        self.assertEqual(r.status_code, 200)
        with org_context(self.org_a):
            self.assertTrue(FileAccess.objects.filter(project=self.bid, user=self.eve).exists())

    def test_refused_files_say_why(self):
        self.login(self.eve)
        for name, data in (('book.xlsb', demo_bytes()), ('book.xls', b'x'), ('book.ods', b'x'), ('book.csv', b'a,b'),
                           ('book.xlsx', b'not a zip')):
            with self.subTest(name):
                r = self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file(data, name)},
                                     follow=True)
                self.assertContains(r, 'Not uploaded')
        with org_context(self.org_a):
            self.assertFalse(PricingWorkbook.objects.exists())

    def test_clarifications_come_from_the_workbook_and_edits_survive_a_reupload(self):
        wb = self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.bid)
            self.assertEqual(pack.clarifications.count(), 4)
            c = pack.clarifications.get(ref='CL1')
        self.login(self.eve)
        self.client.post(self.url('hpack_clarification', self.bid.pk, c.pk),
                         {'ref': 'CL1', 'text': 'Normal hours, agreed with the client', 'status': 'Agreed'})
        self.client.post(self.url('hpack_clarifications', self.bid.pk), {'new-text': 'Our own note', 'new-status': 'Open'})
        self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            self.assertEqual(pack.clarifications.count(), 5)
            self.assertEqual(pack.clarifications.get(ref='CL1').text, 'Normal hours, agreed with the client')
        self.assertTrue(wb.pk)

    def test_lost_bid_is_read_only(self):
        self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_LOST)
        self.login(self.eve)
        self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file()})
        with org_context(self.org_a):
            self.assertEqual(PricingWorkbook.objects.filter(project=self.bid).count(), 1)


class HandoverTests(Base):
    def win(self):
        self.login(self.alice)
        return self.client.post(self.url('project_handover', self.bid.pk), {
            'manager': self.pat.pk, 'value': '228753.41', 'start_date': '2026-12-01', 'completion_date': '2027-03-12',
            'handover_notes': 'Over to you'})

    def test_winning_hands_the_pack_to_the_pm(self):
        self.seed(self.bid, self.eve)
        r = self.win()
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.bid.refresh_from_db()
            self.assertEqual(self.bid.stage, Project.STAGE_WON)
            pack = HandoverPack.objects.get(project=self.bid)
            self.assertIsNotNone(pack.released_at)
            items = list(OpenItem.objects.filter(pack=pack, status=OpenItem.OPEN))
            self.assertTrue(items)
            self.assertTrue(all(i.owner_id == self.pat.pk and i.due_date for i in items))
        # The PM sees it under Commercial, and their open items in Needs me (staff only)
        self.login(self.pat)
        page = self.client.get(self.url('hpack_pack', self.bid.pk))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'INTERNAL ONLY')
        self.assertContains(page, self.url('hpack_issue', self.bid.pk))
        import actions
        with org_context(self.org_a):
            mine = [i for i in actions.for_user(self.request_for(self.pat)) if i.kind == 'handover_item']
        self.assertEqual(len(mine), 1)  # one line per job, linking to its open items
        self.assertEqual(mine[0].title, f'{len(items)} handover pack open items')
        self.assertTrue(mine[0].url.startswith(self.url('hpack_items', self.bid.pk)))
        self.assertTrue(all('£' not in i.title and i.amount is None for i in mine))
        # The estimator who can't open live jobs no longer can
        self.assertEqual(self.status_for(self.eve, self.url('hpack_pack', self.bid.pk)), 404)

    def test_handover_page_mentions_the_pack(self):
        self.seed(self.bid, self.eve)
        self.login(self.eve)
        r = self.client.get(self.url('project_handover', self.bid.pk))
        self.assertContains(r, self.url('hpack_pack', self.bid.pk))

    def test_issue_needs_the_total_to_reconcile(self):
        self.seed(self.bid, self.eve, stale_bytes())
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_SUBMITTED)
        self.win()
        self.login(self.pat)
        page = self.client.get(self.url('hpack_pack', self.bid.pk))
        self.assertContains(page, 'Does not reconcile')
        self.client.post(self.url('hpack_issue', self.bid.pk))
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.bid)
            self.assertFalse(pack.is_issued)
            self.assertTrue(OpenItem.objects.filter(pack=pack, key='rec:total', status=OpenItem.OPEN).exists())
            self.assertTrue(OpenItem.objects.filter(pack=pack, key='rec:Electrical!AA9').exists())
        # Correct it: a reconciled revision clears those items and the pack can be issued
        self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file()})
        self.client.post(self.url('hpack_issue', self.bid.pk))
        with org_context(self.org_a):
            pack.refresh_from_db()
            self.assertTrue(pack.is_issued)
            self.assertEqual(OpenItem.objects.get(pack=pack, key='rec:total').status, OpenItem.DONE)
        # Issued: the workbook is fixed
        self.client.post(self.url('hpack_upload', self.bid.pk), {'file': upload_file(demo_bytes('B'))})
        with org_context(self.org_a):
            self.assertEqual(PricingWorkbook.objects.filter(project=self.bid).count(), 2)

    def test_open_item_owner_and_date(self):
        self.seed(self.pa, self.pat)
        with org_context(self.org_a):
            item = OpenItem.objects.filter(project=self.pa).first()
        self.login(self.pat)
        p = f'i{item.pk}'
        r = self.client.post(self.url('hpack_items', self.pa.pk), {
            'item': item.pk, f'{p}-owner': self.quinn.pk, f'{p}-due_date': '2026-12-01', f'{p}-status': 'OPEN'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            item.refresh_from_db()
        self.assertEqual(item.owner, self.quinn)
        # Only people who can open the pack may own an item
        self.client.post(self.url('hpack_items', self.pa.pk), {
            'item': item.pk, f'{p}-owner': self.stan.pk, f'{p}-status': 'OPEN'})
        with org_context(self.org_a):
            item.refresh_from_db()
        self.assertEqual(item.owner, self.quinn)


class ExportTests(Base):
    def test_export_is_a_new_internal_workbook_with_formula_totals(self):
        spec = dict(sample.DEMO)
        spec['clarifications'] = sample.DEMO['clarifications'] + [
            ('CL9', 'General', '=HYPERLINK("http://evil.example","x")', '@SUM(1)', 'Open', '', '')]
        self.seed(self.pa, self.pat, sample.build(spec))
        self.login(self.pat)
        r = self.client.get(self.url('hpack_pack', self.pa.pk) + '?export=xlsx')
        self.assertEqual(r.status_code, 200)
        self.assertIn('INTERNAL', r['Content-Disposition'])
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        self.assertEqual(wb.sheetnames, ['Cover', 'Project summary', 'Price summary', 'Scope allowed', 'Not allowed',
                                         'Clarifications', 'Drawings', 'Suppliers', 'Site rules', 'Buy-out targets',
                                         'Open items'])
        self.assertNotIn('PRICING SUMMARY', wb.sheetnames)  # never a copy of the upload
        for ws in wb.worksheets:
            self.assertEqual(ws.oddHeader.center.text, 'INTERNAL ONLY')
            self.assertEqual(ws.page_setup.orientation, 'landscape')
        prices = wb['Price summary']
        last = prices.max_row
        self.assertTrue(str(prices.cell(last, 5).value).startswith('=SUM('))
        cells = [c.value for row in wb['Clarifications'].iter_rows() for c in row if isinstance(c.value, str)]
        self.assertIn("'=HYPERLINK(\"http://evil.example\",\"x\")", cells)
        self.assertIn("'@SUM(1)", cells)
        self.assertFalse(any(v.startswith('=') for v in cells))
        with org_context(self.org_a):
            self.assertTrue(ActivityEvent.objects.filter(verb='exported the handover pack', internal_only=True).exists())
