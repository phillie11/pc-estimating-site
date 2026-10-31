"""
Cost control: the append-only ledger and its reversals, budgets, commitments, the cost report
reconciling to the ledger, the margin warning on Needs me, who sees what, and tenancy.
"""
from decimal import Decimal as D

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.urls import reverse

import actions
from core.tests.base import RecordTestBase, make_user
from costs import ledger, services
from costs.access import settings_for
from costs.models import (ActualCost, BudgetChange, Commitment, CommitmentAdjustment, CostCode, CostSettings,
                          ForecastOverride, LedgerEntry)
from costs.report import cost_report
from delivery.models import SubVariation
from directory.models import Package, Subcontractor
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.context import TenantContextMissing
from tenancy.models import Membership
from variations.models import Variation


class CostTestBase(RecordTestBase):
    def setUp(self):
        super().setUp()
        self.pm = make_user('pm@a.test', 'Pat PM')
        self.site = make_user('site@a.test', 'Sid Site')
        self.est = make_user('est@a.test', 'Erin Estimator')
        self.subu = make_user('sub@s.test', 'Sam Sub')
        self.bidu = make_user('bid@s.test', 'Bea Bidder')
        self.stranger = make_user('nobody@x.test', 'No Body')
        M = Membership
        M.objects.create(organization=self.org_a, user=self.pm, role=M.ROLE_MEMBER, area=M.AREA_DELIVERY)
        M.objects.create(organization=self.org_a, user=self.site, role=M.ROLE_MEMBER, area=M.AREA_BOTH)
        M.objects.create(organization=self.org_a, user=self.est, role=M.ROLE_MEMBER, area=M.AREA_ESTIMATING)
        M.objects.create(organization=self.org_a, user=self.subu, role=M.ROLE_EXTERNAL)
        M.objects.create(organization=self.org_a, user=self.bidu, role=M.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.pa.value = D('1000000')
            self.pa.manager = self.pm
            self.pa.save()
            ProjectMember.objects.create(project=self.pa, user=self.site, role=ProjectMember.ROLE_SITE)
            self.sub = Subcontractor.objects.create(company_name='Lund Joinery')
            ProjectMember.objects.create(project=self.pa, user=self.subu, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.sub)
            ProjectMember.objects.create(project=self.pa, user=self.bidu, role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.sub)
            settings_for(self.org_a)
            self.codes = {c.code: c for c in CostCode.objects.all()}
            self.package = Package.objects.create(project=self.pa, subcontractor=self.sub, status=Package.APPOINTED,
                                                  quote_value=D('186500'))

    def a(self):
        return org_context(self.org_a)

    def budget(self, project=None, margin='10', **amounts):
        project = project or self.pa
        amounts = amounts or {'100': '100000', '220': '180000', '900': '620000'}
        lines = [(self.codes[k.lstrip('c')], '', D(v)) for k, v in amounts.items()]
        with self.a():
            return services.set_original_budget(project, self.pm, project.value, D(margin), lines)

    def curl(self, name, *args, project=None):
        return reverse(name, args=[self.org_a.slug, (project or self.pa).pk, *args])


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class LedgerTests(CostTestBase):
    def post(self, amount='1000', kind=ledger.ACTUAL, code='220', **kw):
        with self.a():
            return ledger.post(self.pa, self.codes[code], kind, amount, actor=self.pm, **kw)

    def test_post_rounds_half_up_and_records_who_and_why(self):
        e = self.post('1250.505', note='Valuation 1', source=self.package)
        self.assertEqual(e.amount, D('1250.51'))
        self.assertEqual(e.actor_name, 'Pat PM')
        with self.a():
            e = LedgerEntry.objects.get(pk=e.pk)
            self.assertEqual(e.source, self.package)
        self.assertEqual(e.note, 'Valuation 1')

    def test_an_entry_cannot_be_updated_or_deleted(self):
        e = self.post()
        with self.a():
            e.amount = D('1')
            with self.assertRaises(PermissionError):
                e.save()
            with self.assertRaises(PermissionError):
                e.delete()
            qs = LedgerEntry.objects.filter(pk=e.pk)
            with self.assertRaises(PermissionError):
                qs.update(amount=D('2'))
            with self.assertRaises(PermissionError):
                qs.delete()
            with self.assertRaises(PermissionError):
                LedgerEntry.objects.bulk_update([e], ['amount'])
            with self.assertRaises(PermissionError):
                LedgerEntry.all_objects.filter(pk=e.pk).update(note='x')
            self.assertEqual(LedgerEntry.objects.get(pk=e.pk).amount, D('1000.00'))

    def test_source_records_are_append_only_too(self):
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            with self.assertRaises(PermissionError):
                c.save()
            with self.assertRaises(PermissionError):
                Commitment.objects.filter(pk=c.pk).update(original_value=D('1'))
            with self.assertRaises(PermissionError):
                c.delete()

    def test_reversal_adds_the_opposite_entry_and_keeps_both(self):
        e = self.post('1960.00')
        with self.a():
            rev = ledger.reverse(e, self.pm, 'Wrong cost code')
            self.assertEqual(rev.amount, D('-1960.00'))
            self.assertEqual(rev.reverses, e)
            self.assertEqual((rev.project_id, rev.cost_code_id, rev.kind), (e.project_id, e.cost_code_id, e.kind))
            self.assertIn('Wrong cost code', rev.note)
            self.assertTrue(e.is_reversed)
            self.assertEqual(LedgerEntry.objects.count(), 2)
            self.assertEqual(ledger.totals(self.pa)[self.codes['220'].pk][ledger.ACTUAL], D('0.00'))

    def test_reversal_rules(self):
        e = self.post()
        with self.a():
            with self.assertRaises(ledger.LedgerError):
                ledger.reverse(e, self.pm, '  ')
            rev = ledger.reverse(e, self.pm, 'Duplicate')
            with self.assertRaises(ledger.LedgerError):
                ledger.reverse(e, self.pm, 'Again')
            with self.assertRaises(ledger.LedgerError):
                ledger.reverse(rev, self.pm, 'Undo the undo')
            self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_post_refuses_floats_wrong_company_archived_jobs_and_unknown_kinds(self):
        with self.assertRaises(TypeError):
            self.post(0.1)
        with self.assertRaises(ledger.LedgerError):
            self.post(kind='REFUND')
        with org_context(self.org_b):
            settings_for(self.org_b)
            b_code = CostCode.objects.get(code='220')
        with self.a(), self.assertRaises(ledger.LedgerError):
            ledger.post(self.pa, b_code, ledger.ACTUAL, '10')
        with self.a():
            self.pa.archive(self.alice)
            with self.assertRaises(ledger.LedgerError):
                ledger.post(self.pa, self.codes['220'], ledger.ACTUAL, '10')
        self.assertEqual(LedgerEntry.all_objects.count(), 0)

    def test_no_company_fails_closed(self):
        self.post()
        with self.assertRaises(TenantContextMissing):
            LedgerEntry.objects.count()
        with self.assertRaises(TenantContextMissing):
            list(CostCode.objects.all())
        with org_context(self.org_b):
            self.assertEqual(LedgerEntry.objects.count(), 0)


# ---------------------------------------------------------------------------
# Budget, commitments, actuals
# ---------------------------------------------------------------------------

class BudgetAndCommitmentTests(CostTestBase):
    def test_original_budget_once_then_only_transfers_and_revisions(self):
        self.budget()
        with self.a():
            with self.assertRaises(services.CostError):
                services.set_original_budget(self.pa, self.pm, D('1'), D('0'), [(self.codes['100'], '', D('1'))])
            t = services.transfer_budget(self.pa, self.pm, self.codes['900'], self.codes['220'], D('5000'), 'Veneer')
            self.assertEqual(t.total, D('0'))
            services.revise_budget(self.pa, self.pm, self.codes['100'], D('-2500.50'), 'Prelims saved')
            totals = ledger.totals(self.pa)
            self.assertEqual(totals[self.codes['220'].pk][ledger.BUDGET], D('185000.00'))
            self.assertEqual(totals[self.codes['900'].pk][ledger.BUDGET], D('615000.00'))
            self.assertEqual(totals[self.codes['100'].pk][ledger.BUDGET], D('97499.50'))
            # The original is still there, unchanged
            original = BudgetChange.objects.get(kind=BudgetChange.ORIGINAL)
            self.assertEqual(original.total, D('900000.00'))
            self.assertEqual(BudgetChange.objects.count(), 3)
            with self.assertRaises(services.CostError):
                services.transfer_budget(self.pa, self.pm, self.codes['900'], self.codes['220'], D('1'), '')

    def test_commit_package_and_add_an_approved_sub_variation_once(self):
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            self.assertEqual((c.ref, c.supplier, c.value), ('ORD-001', 'Lund Joinery', D('186500.00')))
            with self.assertRaises(services.CostError):
                services.commit_package(self.package, self.codes['220'], self.pm)
            sv = SubVariation.objects.create(project=self.pa, subcontractor=self.sub, package=self.package,
                                             title='Shelves', description='x', value=D('1200'),
                                             status=SubVariation.APPROVED, approved_value=D('1100'))
            pending = SubVariation.objects.create(project=self.pa, subcontractor=self.sub, package=self.package,
                                                  title='Veneer', description='x', value=D('2350'))
            self.assertEqual(list(services.addable_sub_variations(c)), [sv])
            services.add_sub_variation(c, sv, self.pm)
            with self.assertRaises(services.CostError):
                services.add_sub_variation(c, sv, self.pm)
            with self.assertRaises(services.CostError):
                services.add_sub_variation(c, pending, self.pm)
            self.assertEqual(c.value, D('187600.00'))
            self.assertEqual(services.committed_value(c), D('187600.00'))

    def test_package_not_appointed_cannot_be_committed(self):
        with self.a():
            self.package.status = Package.QUOTED
            self.package.save()
            with self.assertRaises(services.CostError):
                services.commit_package(self.package, self.codes['220'], self.pm)

    def test_order_adjustments_cannot_take_it_below_zero(self):
        with self.a():
            o = services.place_order(self.pa, self.pm, self.codes['500'], 'Speedy Hire', '6800')
            services.adjust_commitment(o, self.pm, D('-800'), 'Two weeks less')
            with self.assertRaises(services.CostError):
                services.adjust_commitment(o, self.pm, D('-6001'), 'Too much')
            self.assertEqual(o.value, D('6000.00'))
            self.assertEqual(CommitmentAdjustment.objects.count(), 1)


# ---------------------------------------------------------------------------
# The cost report
# ---------------------------------------------------------------------------

class ReportTests(CostTestBase):
    def scenario(self):
        self.budget(c100='100000', c220='180000', c400='20000', c900='600000')
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            services.place_order(self.pa, self.pm, self.codes['400'], 'Travis Perkins', '11250')
            services.record_actual(self.pa, self.pm, self.codes['220'], 'INVOICE', 'Lund', '62000.005', commitment=c)
            services.record_actual(self.pa, self.pm, self.codes['100'], 'TIMESHEET', 'Site manager', '24000')
            a = services.record_actual(self.pa, self.pm, self.codes['400'], 'INVOICE', 'Travis', '1960')
            ledger.reverse(ledger.entries(self.pa).get(source_id=a.pk, kind=ledger.ACTUAL), self.pm, 'Wrong job')
            v = Variation(project=self.pa, title='Extra', description='x', value=D('6200'), approved_value=D('5900'),
                          status=Variation.STATUS_APPROVED)
            v.save(actor=self.pm)

    def test_totals_reconcile_to_the_ledger(self):
        self.scenario()
        with self.a():
            r = cost_report(self.pa)
            for kind, attr in ((ledger.BUDGET, 'budget'), (ledger.COMMITMENT, 'committed'), (ledger.ACTUAL, 'actual')):
                ledger_total = LedgerEntry.objects.filter(project=self.pa, kind=kind).aggregate(s=Sum('amount'))['s']
                self.assertEqual(getattr(r['total'], attr), ledger_total, kind)
                self.assertEqual(sum(getattr(row, attr) for row in r['rows']), ledger_total, kind)
            self.assertEqual(r['total'].budget, D('900000.00'))
            self.assertEqual(r['total'].committed, D('197750.00'))
            self.assertEqual(r['total'].actual, D('86000.01'))
            joinery = next(row for row in r['rows'] if row.code.code == '220')
            self.assertEqual((joinery.forecast, joinery.variance, joinery.overspent),
                             (D('186500.00'), D('-6500.00'), True))
            self.assertEqual(r['final_cost'], D('906500.00'))
            self.assertEqual(r['final_account'], D('1005900.00'))  # contract sum + approved VO
            self.assertEqual(r['margin'], D('99400.00'))
            self.assertEqual(r['margin_pct'], D('9.9'))
            self.assertTrue(r['below'])

    def test_manual_forecast_is_recorded_and_the_latest_wins(self):
        self.scenario()
        with self.a():
            services.set_forecast(self.pa, self.pm, self.codes['900'], D('500000'), 'Risks retired')
            services.set_forecast(self.pa, self.pm, self.codes['900'], D('550000'), 'One risk back')
            row = next(r for r in cost_report(self.pa)['rows'] if r.code.code == '900')
            self.assertEqual(row.forecast, D('550000.00'))
            self.assertEqual(len(row.history), 2)
            services.set_forecast(self.pa, self.pm, self.codes['900'], None, 'Back to standard')
            row = next(r for r in cost_report(self.pa)['rows'] if r.code.code == '900')
            self.assertEqual(row.forecast, D('600000.00'))
            self.assertIsNone(row.override)
            self.assertEqual(ForecastOverride.objects.count(), 3)

    def test_csv_export(self):
        self.scenario()
        self.login(self.pm)
        r = self.client.get(self.curl('cost_report') + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        text = r.content.decode()
        self.assertTrue(text.startswith('Cost code,Name,Group,Budget,Committed,Actual to date,Forecast final cost'))
        self.assertIn('220,Joinery,Trade packages,180000.00,186500.00,62000.01,186500.00,,-6500.00', text)
        self.assertIn('Forecast margin %,,9.9', text)


class MarginThresholdTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.budget(c100='100000', c220='180000', c900='620000')  # 10% margin on £1m: exactly at the threshold

    def items_for(self, user):
        from django.test import RequestFactory
        req = RequestFactory().get('/')
        req.user = user
        with self.a():
            req.membership = Membership.objects.get(organization=self.org_a, user=user)
            req.org = self.org_a
            return [i for i in actions.for_user(req) if i.kind == 'cost_margin']

    def test_at_the_threshold_is_not_flagged(self):
        with self.a():
            r = cost_report(self.pa)
        self.assertEqual(r['margin_pct'], D('10.0'))
        self.assertFalse(r['below'])
        self.assertEqual(self.items_for(self.pm), [])

    def test_below_threshold_goes_to_the_pm_and_admins_only(self):
        with self.a():
            services.record_actual(self.pa, self.pm, self.codes['220'], 'INVOICE', 'Lund', '181000')
        for user in (self.pm, self.alice):
            items = self.items_for(user)
            self.assertEqual(len(items), 1, user)
            self.assertEqual(items[0].project, self.pa)
            self.assertEqual(items[0].tone, 'bad')
        for user in (self.site, self.est, self.carla, self.subu):
            self.assertEqual(self.items_for(user), [], user)
        self.login(self.pm)
        r = self.client.get(self.curl('cost_report'))
        self.assertContains(r, 'Below margin')
        self.assertContains(r, 'margin-low')

    def test_company_threshold_setting_changes_the_flag(self):
        self.login(self.alice)
        r = self.client.post(reverse('cost_settings', args=[self.org_a.slug]),
                             {'action': 'threshold', 'margin_threshold': '12.5'})
        self.assertEqual(r.status_code, 302)
        with self.a():
            self.assertEqual(CostSettings.objects.get().margin_threshold, D('12.50'))
            self.assertTrue(cost_report(self.pa)['below'])
        self.assertEqual(len(self.items_for(self.pm)), 1)
        # Another company's threshold is its own
        with org_context(self.org_b):
            self.assertEqual(settings_for(self.org_b).margin_threshold, D('10.00'))


# ---------------------------------------------------------------------------
# Views: roles, archived jobs, tenancy
# ---------------------------------------------------------------------------

class RoleViewTests(CostTestBase):
    PAGES = ['cost_report', 'cost_budget', 'cost_commitments', 'cost_actuals', 'cost_ledger', 'cost_budget_setup',
             'cost_order_create', 'cost_actual_create']

    def statuses(self, user):
        self.client.logout()
        if user is not None:
            self.login(user)
        return {name: self.client.get(self.curl(name)).status_code for name in self.PAGES}

    def test_each_role(self):
        expected = {
            'owner': (self.alice, 200), 'pm': (self.pm, 200),
            'site staff not running the job': (self.site, 403),
            'estimator (cannot open live jobs)': (self.est, 404),
            'client': (self.carla, 403), 'subcontractor': (self.subu, 403), 'tendering sub': (self.bidu, 403),
            'non-member': (self.stranger, 404), 'other company': (self.bob, 404),
        }
        for label, (user, code) in expected.items():
            with self.subTest(label):
                self.assertEqual(set(self.statuses(user).values()), {code}, self.statuses(user))

    def test_qs_on_the_team_sees_costs(self):
        with self.a():
            ProjectMember.objects.create(project=self.pa, user=self.est, role=ProjectMember.ROLE_QS)
            m = Membership.objects.get(organization=self.org_a, user=self.est)
            m.area = Membership.AREA_BOTH
            m.save()
        self.assertEqual(set(self.statuses(self.est).values()), {200})

    def test_costs_only_in_the_commercial_nav_for_people_who_see_them(self):
        url = reverse('contract_detail', args=[self.org_a.slug, self.pa.pk])
        self.login(self.pm)
        self.assertContains(self.client.get(url), self.curl('cost_report'))
        self.login(self.site)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.curl('cost_report'))

    def test_bids_have_no_costs(self):
        with self.a():
            bid = Project.objects.create(name='A bid', stage=Project.STAGE_TENDERING)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.curl('cost_report', project=bid)).status_code, 404)

    def test_settings_are_owners_and_admins_only(self):
        url = reverse('cost_settings', args=[self.org_a.slug])
        self.login(self.pm)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.alice)
        r = self.client.get(url)
        self.assertContains(r, 'Drylining &amp; partitions')
        r = self.client.post(url, {'action': 'add_code', 'new-code': '700', 'new-name': 'Surveys',
                                   'new-group': 'FEES', 'new-is_active': 'on'})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(url, {'action': 'add_code', 'new-code': '700', 'new-name': 'Dup', 'new-is_active': 'on'})
        self.assertContains(r, 'Another cost code already uses this code')
        self.login(self.bob)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_code_with_money_is_retired_not_deleted(self):
        self.budget()
        self.login(self.alice)
        code = self.codes['220']
        url = reverse('cost_code_edit', args=[self.org_a.slug, code.pk])
        self.client.post(url, {'action': 'delete'})
        with self.a():
            self.assertTrue(CostCode.objects.filter(pk=code.pk).exists())
            spare = self.codes['265']
        self.client.post(reverse('cost_code_edit', args=[self.org_a.slug, spare.pk]), {'action': 'delete'})
        with self.a():
            self.assertFalse(CostCode.objects.filter(pk=spare.pk).exists())


class ViewFlowTests(CostTestBase):
    def test_budget_setup_must_add_up(self):
        self.login(self.pm)
        url = self.curl('cost_budget_setup')
        c100, c220 = self.codes['100'].pk, self.codes['220'].pk
        r = self.client.post(url, {'basis_value': '1,000,000', 'margin_pct': '10', f'amt_{c100}': '100000',
                                   f'amt_{c220}': '700000'})
        self.assertContains(r, 'Add £100,000.00, or change the margin')
        r = self.client.post(url, {'basis_value': '1,000,000', 'margin_pct': '20', f'amt_{c100}': '100000',
                                   f'amt_{c220}': '700000', f'desc_{c220}': 'Bar joinery'})
        self.assertRedirects(r, self.curl('cost_report'))
        with self.a():
            self.assertEqual(ledger.totals(self.pa)[c220][ledger.BUDGET], D('700000.00'))
        # A second set-up is turned away to the budget page
        self.assertRedirects(self.client.get(url), self.curl('cost_budget'))

    def test_transfer_and_revision_through_the_page(self):
        self.budget()
        self.login(self.pm)
        url = self.curl('cost_budget_change')
        r = self.client.post(url, {'kind': 'transfer', 'from_code': self.codes['900'].pk,
                                   'to_code': self.codes['220'].pk, 'amount': '5000', 'reason': 'Veneer'})
        self.assertRedirects(r, self.curl('cost_budget'))
        r = self.client.post(url, {'kind': 'revision', 'cost_code': self.codes['100'].pk, 'amount': '-250.255',
                                   'reason': 'Saved'})
        self.assertRedirects(r, self.curl('cost_budget'))
        with self.a():
            self.assertEqual(ledger.totals(self.pa)[self.codes['100'].pk][ledger.BUDGET], D('99749.74'))
        page = self.client.get(self.curl('cost_budget'))
        self.assertContains(page, 'BC-001')
        self.assertContains(page, 'Veneer')

    def test_one_click_commit_and_sub_variation(self):
        self.login(self.pm)
        r = self.client.get(self.curl('cost_commitments'))
        self.assertContains(r, 'Commit this order')
        r = self.client.post(self.curl('cost_commit_package'), {'package': self.package.pk,
                                                                 'cost_code': self.codes['220'].pk})
        self.assertRedirects(r, self.curl('cost_commitments'))
        with self.a():
            c = Commitment.objects.get()
            sv = SubVariation.objects.create(project=self.pa, subcontractor=self.sub, package=self.package,
                                             title='Shelves', description='x', value=D('1200'),
                                             status=SubVariation.APPROVED, approved_value=D('1100'))
        self.assertContains(self.client.get(self.curl('cost_commitments')), 'Add to order')
        self.client.post(c.get_absolute_url(), {'action': 'add_sv', 'sub_variation': sv.pk})
        self.client.post(c.get_absolute_url(), {'action': 'add_sv', 'sub_variation': sv.pk})  # twice: 404
        with self.a():
            self.assertEqual(c.value, D('187600.00'))
        self.assertNotContains(self.client.get(self.curl('cost_commitments')), 'Add to order')

    def test_actual_with_attachment_then_reverse(self):
        self.login(self.pm)
        pdf = SimpleUploadedFile('inv.pdf', b'%PDF-1.4 demo', content_type='application/pdf')
        r = self.client.post(self.curl('cost_actual_create'), {
            'kind': 'INVOICE', 'cost_code': self.codes['400'].pk, 'supplier': 'Travis Perkins',
            'reference': 'TP-1', 'cost_date': '2026-10-20', 'amount': '4380.125', 'file': pdf})
        self.assertRedirects(r, self.curl('cost_actuals'))
        with self.a():
            a = ActualCost.objects.get()
            self.assertEqual(a.amount, D('4380.13'))
            entry = LedgerEntry.objects.get(kind=ledger.ACTUAL)
        bad = SimpleUploadedFile('evil.exe', b'MZ', content_type='application/octet-stream')
        r = self.client.post(self.curl('cost_actual_create'), {
            'kind': 'INVOICE', 'cost_code': self.codes['400'].pk, 'supplier': 'X', 'cost_date': '2026-10-20',
            'amount': '1', 'file': bad})
        self.assertContains(r, '.exe files are not allowed')
        # Budget entries can't be reversed from the page; actual costs can
        r = self.client.post(self.curl('cost_ledger_reverse', entry.pk), {'reason': ''})
        with self.a():
            self.assertEqual(LedgerEntry.objects.count(), 1)
        self.client.post(self.curl('cost_ledger_reverse', entry.pk), {'reason': 'Wrong job'})
        with self.a():
            self.assertEqual(LedgerEntry.objects.count(), 2)
            self.assertEqual(ActualCost.objects.count(), 1)  # the invoice record stays
        self.assertContains(self.client.get(self.curl('cost_actuals')), 'Reversed')
        self.assertContains(self.client.get(self.curl('cost_ledger')), 'Reversal')
        # The attachment is served through projects/files.py to people who see costs only
        self.assertEqual(self.client.get(self.curl('cost_actual_file', a.pk)).status_code, 200)
        self.login(self.site)
        self.assertEqual(self.client.get(self.curl('cost_actual_file', a.pk)).status_code, 403)

    def test_archived_job_is_read_only(self):
        self.budget()
        with self.a():
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.curl('cost_report')).status_code, 200)
        r = self.client.post(self.curl('cost_commit_package'), {'package': self.package.pk,
                                                                 'cost_code': self.codes['220'].pk})
        self.assertEqual(r.status_code, 403)
        with self.a():
            self.assertFalse(Commitment.objects.exists())

    def test_empty_states_and_filters(self):
        self.login(self.pm)
        self.assertContains(self.client.get(self.curl('cost_report')), 'No budget yet')
        self.assertContains(self.client.get(self.curl('cost_ledger')), 'The ledger is empty')
        self.assertContains(self.client.get(self.curl('cost_actuals')), 'No actual costs yet')
        self.budget()
        r = self.client.get(self.curl('cost_report') + '?show=over')
        self.assertContains(r, 'No cost codes match')
        r = self.client.get(self.curl('cost_ledger') + '?kind=ACTUAL')
        self.assertContains(r, 'No entries match')

    def test_another_company_cannot_reach_rows_by_id(self):
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
        with org_context(self.org_b):
            settings_for(self.org_b)
        self.login(self.bob)
        self.assertEqual(self.client.get(reverse('cost_commitment_detail',
                                                 args=[self.org_b.slug, self.pb.pk, c.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('cost_commitment_detail',
                                                 args=[self.org_a.slug, self.pa.pk, c.pk])).status_code, 404)
