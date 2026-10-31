"""
Quote levelling through the views: who can open it (each role), another company, no org context,
import (scope, priced copy, from a portal quote, malicious files), plugs, the exclusions register,
carrying a quote and where that shows (Sub quotes, handover), the coverage board's "Not levelled",
exports, read only once the bid is decided, and seed_demo.
"""
import io
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal as D

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from projects.models import Project, ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership
from tenancy.tests import Base, make_user

from levelling import services
from levelling.models import Adjustment, Exclusion, LevellingSheet, QuoteCell, ScopeLine

from .test_import import HEAD, csv_file, xlsx

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA, RATELIMIT_ENABLED=False)
class LevellingBase(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.est = make_user('est@a.test', 'Esme Estimator')
        self.pm = make_user('pm@a.test', 'Pat PM')
        self.adm = make_user('adm@a.test', 'Ada Admin')
        self.client_u = make_user('client@c.test', 'Cara Client')
        self.cons = make_user('cons@c.test', 'Con Sultant')
        self.subu = make_user('sub@s.test', 'Sam Sub')
        self.bidu = make_user('bid@s.test', 'Bea Bidder')
        M = Membership.objects.create
        M(organization=self.org_a, user=self.est, role=Membership.ROLE_MEMBER, area=Membership.AREA_ESTIMATING)
        M(organization=self.org_a, user=self.pm, role=Membership.ROLE_MEMBER, area=Membership.AREA_DELIVERY)
        M(organization=self.org_a, user=self.adm, role=Membership.ROLE_ADMIN)
        for u in (self.client_u, self.cons, self.subu, self.bidu):
            M(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Kingsway bid', stage=Project.STAGE_TENDERING, owner=self.est,
                                              value=D('900000'), quotes_due=timezone.localdate() + timedelta(days=3))
            self.join = Trade.objects.get_or_create(name='Joinery')[0]
            self.dry = Trade.objects.get_or_create(name='Drylining')[0]
            mk = Subcontractor.objects.create
            self.s_oak, self.s_ken, self.s_tha, self.s_lund = (
                mk(company_name='Oakline', email='o@o.test', status='APPROVED'),
                mk(company_name='Kensal', email='k@k.test', status='APPROVED'),
                mk(company_name='Thameside', email='t@t.test', status='APPROVED'),
                mk(company_name='Lund', email='bid@s.test', status='APPROVED'))

            def pkg(sub, value=None, status='QUOTED', trade=None, **kw):
                return Package.objects.create(project=self.bid, subcontractor=sub, trade=trade or self.join,
                                              status=status, scope='Joinery', sent_at=now,
                                              quote_value=D(value) if value else None,
                                              submitted_at=now if value else None, **kw)
            self.p_oak = pkg(self.s_oak, '43406', exclusions='Out-of-hours deliveries. Protection')
            self.p_ken = pkg(self.s_ken, '38330')
            self.p_tha = pkg(self.s_tha, '43142')
            self.p_lund = pkg(self.s_lund, None, 'ENQUIRY')
            self.p_dry = pkg(self.s_oak, '9000', trade=self.dry)
            for u, role, sub in ((self.client_u, ProjectMember.ROLE_CLIENT, None),
                                 (self.cons, ProjectMember.ROLE_CONSULTANT, None),
                                 (self.subu, ProjectMember.ROLE_SUBCONTRACTOR, self.s_oak),
                                 (self.bidu, ProjectMember.ROLE_BIDDER, self.s_lund)):
                ProjectMember.objects.create(project=self.bid, user=u, role=role, subcontractor=sub)
            self.sheet = LevellingSheet.objects.create(project=self.bid, trade=self.join)
            lines = [('J5', 'Storage walls', D('24'), 'm'), ('J6', 'Window boards', D('42'), 'm'),
                     ('J7', 'Ironmongery', None, 'Item')]
            self.l5, self.l6, self.l7 = [services.add_line(self.sheet, *x) for x in lines]
            services.set_cell(self.l5, self.p_oak, rate=D('690'))
            services.set_cell(self.l6, self.p_oak, rate=D('38'))
            services.set_cell(self.l7, self.p_oak, amount=D('1850'))
            services.set_cell(self.l5, self.p_ken, rate=D('655'))
            services.set_cell(self.l7, self.p_ken, included=True)
            services.set_cell(self.l5, self.p_tha, rate=D('640'))
            services.set_cell(self.l6, self.p_tha, rate=D('41'))
            services.set_cell(self.l7, self.p_tha, amount=D('1600'))

    def sheet_url(self, trade=None, org=None):
        return self.url('levelling_sheet', org or self.org_a, self.bid.pk, (trade or self.join).pk)

    def post(self, data, url=None, user=None):
        self.login(user or self.est)
        return self.client.post(url or self.sheet_url(), data)

    def view(self):
        with org_context(self.org_a):
            return services.build(LevellingSheet.objects.get(pk=self.sheet.pk))


class RoleTests(LevellingBase):
    def urls(self):
        return [self.url('levelling_index', self.org_a, self.bid.pk), self.sheet_url(),
                self.url('levelling_rates', self.org_a, self.bid.pk, self.join.pk, self.p_ken.pk),
                self.url('levelling_csv', self.org_a, self.bid.pk, self.join.pk),
                self.url('levelling_pdf', self.org_a, self.bid.pk, self.join.pk)]

    def test_estimating_staff_owner_and_admin_can_open_everything(self):
        for user in (self.alice, self.adm, self.est):
            self.login(user)
            for url in self.urls():
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)

    def test_everyone_else_is_refused(self):
        # Client, consultant, subcontractor, tendering subcontractor: 403. Projects-only staff can't see a bid
        # at all (404, like every bid page); another company: 404
        for user, code in ((self.pm, 404), (self.client_u, 403), (self.cons, 403), (self.subu, 403),
                           (self.bidu, 403), (self.bob, 404)):
            self.login(user)
            for url in self.urls():
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, code)
            r = self.client.post(self.sheet_url(), {'action': 'add_line', 'line-description': 'x'})
            self.assertEqual(r.status_code, code)
        with org_context(self.org_a):
            self.assertEqual(ScopeLine.objects.filter(description='x').count(), 0)

    def test_bidder_portal_never_shows_other_prices_or_levelling(self):
        self.login(self.bidu)
        r = self.client.get(self.url('tender', self.org_a, self.bid.pk))
        self.assertEqual(r.status_code, 200)
        for leak in ('Oakline', 'Kensal', 'Thameside', '43,406', 'evell'):
            self.assertNotContains(r, leak)

    def test_sheet_shows_totals_ranks_and_missing(self):
        self.login(self.est)
        r = self.client.get(self.sheet_url())
        self.assertContains(r, 'Level quotes: Joinery')
        self.assertContains(r, 'Missing')
        self.assertContains(r, 'Avg £1,659.00')   # Kensal's window boards: average of 1,596 and 1,722
        self.assertContains(r, 'High £1,722.00')
        self.assertNotContains(r, '<th class="num">Lund')   # still to quote: not a column, listed under Import
        self.assertContains(r, 'Still to quote: Lund')

    def test_unknown_trade_or_one_not_on_this_bid_is_404(self):
        self.login(self.est)
        with org_context(self.org_a):
            other = Trade.objects.create(name='Zebra crossings')
        self.assertEqual(self.client.get(self.sheet_url(trade=other)).status_code, 404)


class IsolationTests(LevellingBase):
    def test_other_company_cannot_reach_it_through_its_own_workspace(self):
        self.login(self.bob)
        r = self.client.get(self.url('levelling_sheet', self.org_b, self.bid.pk, self.join.pk))
        self.assertEqual(r.status_code, 404)
        r = self.client.post(self.url('levelling_import', self.org_b, self.bid.pk, self.join.pk),
                             {'imp-kind': 'scope', 'imp-file': csv_file('J1,Desk,1,nr\n')})
        self.assertEqual(r.status_code, 404)

    def test_no_org_context_fails_closed(self):
        for model in (LevellingSheet, ScopeLine, QuoteCell, Adjustment, Exclusion):
            with self.subTest(model=model.__name__), self.assertRaises(TenantContextMissing):
                list(model.objects.all())

    def test_other_company_sees_none_of_the_rows(self):
        with org_context(self.org_b):
            self.assertEqual(LevellingSheet.objects.count(), 0)
            self.assertEqual(QuoteCell.objects.count(), 0)
            self.assertEqual(services.levelled_counts([self.bid.pk]), {})


class SheetActionTests(LevellingBase):
    def test_plug_with_average_is_recorded_as_an_adjustment(self):
        r = self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'AVERAGE'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            a = Adjustment.objects.get(package=self.p_ken)
        self.assertEqual((a.kind, a.amount, a.basis, a.line_id), (Adjustment.PLUG, D('1659.00'), 'AVERAGE', self.l6.pk))
        v = self.view()
        ken = v.column(self.p_ken.pk)
        self.assertEqual((ken.missing, ken.levelled, ken.result.rank), (0, D('39989.00'), 1))
        # Plugging the same line again is refused; so is highest-of-others on it now
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'HIGHEST'})
        with org_context(self.org_a):
            self.assertEqual(Adjustment.objects.filter(package=self.p_ken).count(), 1)

    def test_plug_with_highest(self):
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'HIGHEST'})
        with org_context(self.org_a):
            self.assertEqual(Adjustment.objects.get(package=self.p_ken).amount, D('1722.00'))

    def test_adjustments_need_a_reason_and_a_nonzero_amount(self):
        r = self.post({'action': 'adjust', 'adj-package': self.p_tha.pk, 'adj-kind': 'PROVISIONAL', 'adj-amount': '0',
                       'adj-reason': ''})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Enter an amount other than 0.')
        r = self.post({'action': 'adjust', 'adj-package': self.p_tha.pk, 'adj-kind': 'CORRECTION',
                       'adj-amount': '-120.50', 'adj-reason': 'Line J5 extended wrongly'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.view().column(self.p_tha.pk).levelled, D('43021.50'))
        r = self.post({'action': 'adjust', 'adj-package': self.p_lund.pk, 'adj-kind': 'PROVISIONAL',
                       'adj-amount': '10', 'adj-reason': 'x'})
        self.assertEqual(r.status_code, 200)  # Lund hasn't quoted: not a column

    def test_exclusion_priced_by_us_is_an_adjustment_and_unpriced_is_a_risk(self):
        r = self.post({'action': 'copy_exclusions', 'package': self.p_oak.pk})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            texts = list(Exclusion.objects.filter(package=self.p_oak).values_list('text', flat=True))
        self.assertEqual(texts, ['Out-of-hours deliveries', 'Protection'])
        self.assertEqual(len(self.view().column(self.p_oak.pk).unpriced_exclusions), 2)
        self.login(self.est)
        self.assertContains(self.client.get(self.sheet_url()), '2 not priced')
        with org_context(self.org_a):
            e1, e2 = Exclusion.objects.filter(package=self.p_oak)
        self.post({'action': 'exclusion_status', 'exclusion': e1.pk, 'status': 'PRICED', 'amount': '650'})
        self.post({'action': 'exclusion_status', 'exclusion': e2.pk, 'status': 'ACCEPTED', 'amount': '99'})
        v = self.view()
        oak = v.column(self.p_oak.pk)
        self.assertEqual((oak.result.adjustments, len(oak.unpriced_exclusions)), (D('650.00'), 0))
        with org_context(self.org_a):
            self.assertIsNone(Exclusion.objects.get(pk=e2.pk).amount)
            self.assertEqual(Adjustment.objects.get(exclusion=e1).kind, Adjustment.EXCLUSION)
        # Priced needs an amount; back to unpriced removes the adjustment
        self.post({'action': 'exclusion_status', 'exclusion': e2.pk, 'status': 'PRICED', 'amount': ''})
        self.post({'action': 'exclusion_status', 'exclusion': e1.pk, 'status': 'OPEN', 'amount': ''})
        with org_context(self.org_a):
            self.assertFalse(Adjustment.objects.filter(package=self.p_oak).exists())
            self.assertEqual(Exclusion.objects.get(pk=e2.pk).status, 'ACCEPTED')

    def test_add_and_remove_lines(self):
        r = self.post({'action': 'add_line', 'line-ref': 'J8', 'line-description': 'Coat hooks', 'line-qty': '12',
                       'line-unit': 'nr'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            line = ScopeLine.objects.get(ref='J8')
        self.assertEqual(self.view().column(self.p_oak.pk).missing, 1)
        self.post({'action': 'delete_line', 'line': line.pk})
        with org_context(self.org_a):
            self.assertFalse(ScopeLine.objects.filter(ref='J8').exists())

    def test_rates_page(self):
        url = self.url('levelling_rates', self.org_a, self.bid.pk, self.join.pk, self.p_ken.pk)
        r = self.post({f'rate_{self.l5.pk}': 'abc'}, url=url)
        self.assertContains(r, 'Enter a number')
        r = self.post({f'rate_{self.l5.pk}': '£650', f'sum_{self.l6.pk}': '1,500', f'incl_{self.l7.pk}': '1'}, url=url)
        self.assertEqual(r.status_code, 302)
        ken = self.view().column(self.p_ken.pk)
        self.assertEqual((ken.lines_total, ken.missing), (D('17100.00'), 0))  # 24 x 650 + 1,500 + incl

    def test_arithmetic_correction_from_their_lines(self):
        with org_context(self.org_a):
            Package.objects.filter(pk=self.p_tha.pk).update(quote_value=D('19000'))
        # Thameside's lines: 24 x 640 + 42 x 41 + 1,600 = 18,682
        self.post({'action': 'correction', 'package': self.p_tha.pk})
        with org_context(self.org_a):
            a = Adjustment.objects.get(package=self.p_tha)
        self.assertEqual((a.kind, a.amount), (Adjustment.CORRECTION, D('-318.00')))


class ImportViewTests(LevellingBase):
    def imp(self, f, kind='scope', package=None):
        data = {'imp-kind': kind, 'imp-file': f}
        if package:
            data['imp-package'] = package.pk
        return self.post(data, url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))

    def test_scope_import_adds_and_updates_never_removes(self):
        r = self.imp(xlsx([HEAD, ['J5', 'Storage walls, oak', 26, 'm'], ['J9', 'Lockers', 10, 'nr']]))
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            lines = {x.ref: x for x in ScopeLine.objects.filter(sheet=self.sheet)}
        self.assertEqual(set(lines), {'J5', 'J6', 'J7', 'J9'})
        self.assertEqual((lines['J5'].qty, lines['J5'].description), (D('26.000'), 'Storage walls, oak'))

    def test_priced_copy_fills_one_column_matched_by_ref_then_description(self):
        f = csv_file('Ref,Description,Qty,Unit,Rate,Total\n'
                     'J5,Storage walls,24,m,700,16800\n'
                     ',WINDOW BOARDS,42,m,40,1600\n'      # no ref: matched by description; 42 x 40 != 1,600
                     'J7,Ironmongery,,Item,,incl\n'
                     'X1,Extra shelving,1,nr,,250\n')
        r = self.imp(f, 'priced', self.p_lund)
        self.assertEqual(r.status_code, 302)
        msg = [str(m) for m in r.wsgi_request._messages][-1]
        self.assertIn('Lund: 3 lines priced', msg)
        self.assertIn("qty × rate isn't their sum", msg)
        self.assertIn('match nothing in our scope (X1)', msg)
        lund = self.view().column(self.p_lund.pk)   # now a column, from his lines
        self.assertEqual((lund.lines_total, lund.quoted_from, lund.missing), (D('18400.00'), 'lines', 0))

    def test_priced_copy_needs_a_subcontractor_and_scope_lines(self):
        r = self.imp(csv_file('J5,Storage,24,m,700,\n'), 'priced')
        self.assertIn('Pick whose priced copy', str(list(r.wsgi_request._messages)[-1]))

    def test_malicious_files_are_refused_through_the_view(self):
        for f, words in ((xlsx([HEAD], name='bill.xlsm'), 'Macro-enabled'),
                         (xlsx([HEAD], name='bill.exe'), '.exe files are not allowed'),
                         (xlsx([HEAD] + [[f'L{i}', 'x', 1, 'nr'] for i in range(2005)]), 'more than 2,000 rows')):
            with self.subTest(name=f.name):
                r = self.imp(f)
                self.assertIn(words, str(list(r.wsgi_request._messages)[-1]))
        with org_context(self.org_a):
            self.assertEqual(ScopeLine.objects.filter(sheet=self.sheet).count(), 3)

    def test_fill_from_the_workbook_attached_to_a_portal_quote(self):
        wb = xlsx([HEAD, ['J6', 'Window boards', 42, 'm', 39, None]])
        with org_context(self.org_a):
            self.p_ken.quote_file.save('kensal.xlsx', ContentFile(wb.read()))
        r = self.post({'action': 'from_quote', 'package': self.p_ken.pk},
                      url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))
        self.assertEqual(r.status_code, 302)
        ken = self.view().column(self.p_ken.pk)
        self.assertEqual(ken.missing, 0)
        self.login(self.est)
        self.assertContains(self.client.get(self.sheet_url()), 'Fill Kensal from their workbook')


class CarryTests(LevellingBase):
    def test_lowest_needs_no_reason_others_do(self):
        # Levelled: Kensal 38,330 (missing J6), Thameside 43,142, Oakline 43,406
        r = self.post({'action': 'carry', 'carry-package': self.p_tha.pk, 'carry-reason': ''})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Say why you are carrying them')
        r = self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': ''})
        self.assertEqual(r.status_code, 302)
        r = self.post({'action': 'carry', 'carry-package': self.p_tha.pk,
                       'carry-reason': 'Kensal excludes window boards; Thameside proven on Leeds'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = LevellingSheet.objects.get(pk=self.sheet.pk)
        self.assertEqual((s.carried_id, s.carried_by_id), (self.p_tha.pk, self.est.pk))
        # Carrying changes nobody's package: no one is appointed or told
        with org_context(self.org_a):
            self.assertEqual(set(Package.objects.filter(trade=self.join).values_list('status', flat=True)),
                             {'QUOTED', 'ENQUIRY'})

    def test_cannot_carry_someone_without_a_quote_or_when_another_is_appointed(self):
        r = self.post({'action': 'carry', 'carry-package': self.p_lund.pk, 'carry-reason': 'x'})
        self.assertEqual(r.status_code, 200)
        with org_context(self.org_a):
            Package.objects.filter(pk=self.p_oak.pk).update(status=Package.APPOINTED)
        r = self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': 'x'})
        self.assertContains(r, 'Oakline is already appointed')

    def test_carried_quote_feeds_sub_quotes_handover_and_the_index(self):
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'AVERAGE'})
        self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': ''})
        with org_context(self.org_a):
            self.assertEqual(services.carried_for(Project.objects.get(pk=self.bid.pk)), {self.p_ken.pk: D('39989.00')})
        self.login(self.est)
        r = self.client.get(self.url('package_list', self.org_a, self.bid.pk))
        self.assertContains(r, 'Carried in tender', count=1)
        self.assertContains(r, 'Level quotes')
        r = self.client.get(self.url('project_handover', self.org_a, self.bid.pk))
        self.assertContains(r, 'Carried in tender', count=1)
        self.assertContains(r, '£39,989 levelled')
        r = self.client.get(self.url('levelling_index', self.org_a, self.bid.pk))
        self.assertContains(r, 'Carried: Kensal')
        self.assertContains(r, '£39,989')
        # Uncarry
        self.post({'action': 'uncarry'})
        with org_context(self.org_a):
            self.assertIsNone(LevellingSheet.objects.get(pk=self.sheet.pk).carried_id)

    def test_projects_team_sees_the_carried_badge_but_not_the_levelling(self):
        self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': ''})
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON)
        self.login(self.pm)
        r = self.client.get(self.url('package_list', self.org_a, self.bid.pk))
        self.assertContains(r, 'Carried in tender')
        self.assertNotContains(r, 'Level quotes')

    def test_decided_bid_is_read_only(self):
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_LOST)
        r = self.post({'action': 'add_line', 'line-description': 'After the event'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertFalse(ScopeLine.objects.filter(description='After the event').exists())
        self.login(self.est)
        r = self.client.get(self.sheet_url())
        self.assertContains(r, 'read only')
        self.assertNotContains(r, 'Add line')


class CoverageTests(LevellingBase):
    def test_board_shows_not_levelled_until_two_quotes_are_priced_line_by_line(self):
        self.login(self.est)
        r = self.client.get(self.url('tender_coverage', self.org_a))
        # Joinery: 3 priced line by line (levelled); Drylining: 1 quote, no lines (not levelled)
        self.assertContains(r, 'Not levelled', count=1)
        self.assertContains(r, '>Levelled<', count=1)
        with org_context(self.org_a):
            QuoteCell.objects.filter(package__in=[self.p_ken, self.p_tha]).delete()
        r = self.client.get(self.url('tender_coverage', self.org_a))
        self.assertContains(r, 'Not levelled', count=2)
        r = self.client.get(self.url('tender', self.org_a, self.bid.pk))
        self.assertContains(r, 'Not levelled', count=2)

    def test_declined_quotes_do_not_count(self):
        with org_context(self.org_a):
            self.assertEqual(services.levelled_counts([self.bid.pk]), {(self.bid.pk, self.join.pk): 3})
            Package.objects.filter(pk__in=[self.p_ken.pk, self.p_tha.pk]).update(status=Package.DECLINED)
            self.assertEqual(services.levelled_counts([self.bid.pk]), {(self.bid.pk, self.join.pk): 1})


class ExportTests(LevellingBase):
    def test_csv_is_formula_safe(self):
        with org_context(self.org_a):
            ScopeLine.objects.filter(pk=self.l5.pk).update(description='=HYPERLINK("http://evil.example")')
            Adjustment.objects.create(sheet=self.sheet, package=self.p_tha, kind=Adjustment.CORRECTION,
                                      amount=D('-120.50'), reason='+cmd|calc')
        self.login(self.est)
        r = self.client.get(self.url('levelling_csv', self.org_a, self.bid.pk, self.join.pk))
        body = r.content.decode('utf-8-sig')
        self.assertEqual(r['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('"\'=HYPERLINK(""http://evil.example"")"', body)
        self.assertIn("'+cmd|calc", body)
        self.assertIn(',-120.50,', body)  # plain numbers stay numbers
        self.assertIn('MISSING', body)
        self.assertIn('Levelled total', body)

    def test_pdf_escapes_typed_text(self):
        with org_context(self.org_a):
            Adjustment.objects.create(sheet=self.sheet, package=self.p_tha, kind=Adjustment.PROVISIONAL,
                                      amount=D('500'), reason='<img src="/etc/passwd"/> & <b>bold</b>')
        self.login(self.est)
        r = self.client.get(self.url('levelling_pdf', self.org_a, self.bid.pk, self.join.pk))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))


class SeedDemoTests(TestCase):
    def test_kingsway_joinery_is_levelled(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
        from tenancy.models import Organization
        org = Organization.objects.get(slug='northbank-interiors')
        with org_context(org):
            sheet = LevellingSheet.objects.get(project__name='Office CAT B, Kingsway', trade__name='Joinery')
            v = services.build(sheet)
            got = {c.package.subcontractor.company_name: (c.levelled, c.result.rank, c.missing,
                                                          len(c.unpriced_exclusions)) for c in v.columns}
            self.assertEqual(got, {'Kensal Interiors': (D('40639.00'), 1, 0, 0),
                                   'Thameside Joinery': (D('43142.00'), 2, 0, 1),
                                   'Oakline Joinery': (D('43406.00'), 3, 0, 0)})
            kinds = sorted(a.kind for a in sheet.adjustments.all())
            self.assertEqual(kinds, [Adjustment.EXCLUSION, Adjustment.PLUG])
            # Lund (sub@demo.test) is still to quote, so the portal demo keeps its quote form
            self.assertEqual(Package.objects.get(project=sheet.project, subcontractor__company_name='Lund Joinery Ltd').status,
                             Package.ENQUIRY)
