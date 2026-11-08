"""
Every CVR and cashflow figure reconciles to the module that owns it:

  value, certified, paid      payments (Application, payments.summary.certified_and_paid)
  cost, committed, budget     costs.ledger.totals
  forecast final cost         costs.report.cost_report
  forecast final account      contracts.commercial.commercial_position
  cashflow actuals            payments received ex VAT, and the ledger's actual cost

For this month (which includes today) they must be equal to the penny; for earlier months, only
what is dated by that month's end counts.
"""
from decimal import Decimal as D

from django.db.models import Sum

from contracts.commercial import commercial_position
from costs import ledger
from costs.models import LedgerEntry
from costs.report import cost_report
from cvr import cashflow, figures
from payments.summary import certified_and_paid
from tenancy.context import org_context

from .base import CVRTestBase


class ThisMonthReconcilesTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.rows = self.build_money()

    def test_this_month_equals_each_source(self):
        with org_context(self.org_a):
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            certified, paid = certified_and_paid(self.pa)
            totals = ledger.totals(self.pa)
            report = cost_report(self.pa)
            position = commercial_position(self.pa)
            ap2 = self.rows['ap2']
            ap2.refresh_from_db()
        self.assertEqual(f.value_to_date, ap2.gross_valuation)
        self.assertEqual(f.value_to_date, D('56000.00'))  # 50,000 work + the 6,000 variation
        self.assertEqual(f.retention_held, ap2.retention)
        self.assertEqual((f.certified_to_date, f.paid_to_date), (certified, paid))
        self.assertEqual(f.paid_to_date, D('19000.00'))
        self.assertEqual(f.cost_to_date, sum(t['ACTUAL'] for t in totals.values()))
        self.assertEqual(f.cost_to_date, D('17750.00'))
        self.assertEqual(f.committed_to_date, sum(t['COMMITMENT'] for t in totals.values()))
        self.assertEqual(f.budget_to_date, sum(t['BUDGET'] for t in totals.values()))
        self.assertEqual(f.forecast_cost_base, report['final_cost'])
        self.assertEqual(f.final_account_base, position['forecast'])
        self.assertEqual(f.final_account, D('106000.00'))
        # the derived figures use the same margin rule as the cost report
        self.assertEqual(f.forecast_margin, report['margin'])
        self.assertEqual(f.forecast_margin_pct, report['margin_pct'])
        self.assertEqual(f.margin, D('38250.00'))

    def test_value_is_applied_and_under_certification_is_flagged(self):
        from core.workflow import apply_transition
        from cvr import services
        from cvr.models import CVRPeriod
        ap2 = self.rows['ap2']
        with org_context(self.org_a):
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            self.assertEqual(f.under_certified, D('0.00'))  # ap1 paid in full; ap2 not certified yet
            ap2.refresh_from_db()
            applied = ap2.amount_due
            apply_transition(ap2, 'payment_notice_on_behalf', self.alice, amount=applied - D('1500'),
                             basis='Joinery not complete', given_on=ap2.payment_notice_by)  # in time
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            certified, _ = certified_and_paid(self.pa)
        # The value is what was applied for (the gross valuation), not what was certified
        self.assertEqual(f.value_to_date, D('56000.00'))
        self.assertEqual(f.certified_to_date, certified)
        self.assertEqual(f.under_certified, D('1500.00'))
        # Frozen with the month, and shown on the CVR beside the value
        with org_context(self.org_a):
            period, _ = services.start_period(self.pa, self.today, self.pat, self.today)
            figures.freeze(period, f)
            period.save(update_fields=list(figures.SNAPSHOT_FIELDS))
            period = CVRPeriod.objects.get(pk=period.pk)
            self.assertEqual(figures.frozen_figures(period).under_certified, D('1500.00'))
        self.login(self.pat)
        r = self.client.get(self.url('cvr_period', self.pa.pk, period.pk))
        self.assertContains(r, 'Certified to date')
        self.assertContains(r, 'Under-certified')

    def test_cost_to_date_matches_the_ledger_after_a_reversal(self):
        with org_context(self.org_a):
            entry = LedgerEntry.objects.filter(project=self.pa, kind='ACTUAL').order_by('-pk').first()
            ledger.reverse(entry, self.pat, 'Wrong job')
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            total = LedgerEntry.objects.filter(project=self.pa, kind='ACTUAL').aggregate(s=Sum('amount'))['s']
        self.assertEqual(f.cost_to_date, total)
        self.assertEqual(f.cost_to_date, D('16500.00'))

    def test_many_jobs_at_once_give_the_same_figures(self):
        with org_context(self.org_a):
            from projects.models import Project
            other = Project.objects.create(name='Second job', stage=Project.STAGE_LIVE, value=D('5000'),
                                           manager=self.pat)
            one = figures.live_figures([self.pa], self.today)[self.pa.pk]
            both = figures.live_figures([self.pa, other], self.today)
        self.assertEqual(both[self.pa.pk].derived, one.derived)
        self.assertEqual(both[other.pk].final_account, D('5000.00'))
        self.assertFalse(both[other.pk].has_activity)


class EarlierMonthsTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.rows = self.build_money()

    def test_several_months_at_once_equal_one_at_a_time(self):
        with org_context(self.org_a):
            together = figures.live_figures_months([self.pa], [self.m2, self.m1, self.today])
            for month in (self.m2, self.m1, self.m0):
                alone = figures.live_figures([self.pa], month)[self.pa.pk]
                with self.subTest(month=month):
                    got = together[month][self.pa.pk]
                    for name in figures.SNAPSHOT_FIELDS:
                        self.assertEqual(getattr(got, name), getattr(alone, name), name)

    def test_last_month(self):
        with org_context(self.org_a):
            f = figures.live_figures([self.pa], self.m1)[self.pa.pk]
        self.assertEqual(f.value_to_date, D('56000.00'))   # AP-002 valued on the 25th
        self.assertEqual(f.paid_to_date, D('19000.00'))    # AP-001 paid on the 10th
        self.assertEqual(f.cost_to_date, D('16500.00'))    # not today's timesheet
        self.assertEqual(f.approved_variations, D('6000.00'))
        self.assertEqual(f.budget_to_date, D('0.00'))      # the budget was set this month

    def test_two_months_ago(self):
        with org_context(self.org_a):
            f = figures.live_figures([self.pa], self.m2)[self.pa.pk]
        self.assertEqual(f.value_to_date, D('20000.00'))
        self.assertEqual(f.retention_held, D('1000.00'))
        self.assertEqual(f.paid_to_date, D('0.00'))
        self.assertEqual(f.cost_to_date, D('7000.00'))
        self.assertEqual(f.approved_variations, D('0.00'))  # approved last month
        self.assertEqual(f.final_account, D('100000.00'))

    def test_the_forecast_final_cost_uses_forecasts_made_by_then(self):
        with org_context(self.org_a):
            now = figures.live_figures([self.pa], self.today)[self.pa.pk]
            before = figures.live_figures([self.pa], self.m1)[self.pa.pk]
        # Today: 220 order 48,000 under its 50,000 budget; 300 forecast by hand 24,000; 400 budget 20,000
        self.assertEqual(now.forecast_cost_base, D('94000.00'))
        # Last month there was no budget, order or forecast: just the actuals (4,000+9,500 and 3,000)
        self.assertEqual(before.forecast_cost_base, D('16500.00'))


class CashflowReconcilesTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.rows = self.build_money()

    def test_actual_in_and_out_add_up_to_payments_and_the_ledger(self):
        with org_context(self.org_a):
            data = cashflow.build(self.pa, self.today)
            _, paid = certified_and_paid(self.pa)
            actual = sum(t['ACTUAL'] for t in ledger.totals(self.pa).values())
        self.assertEqual(data['totals']['in_actual'], paid)
        self.assertEqual(data['totals']['out_actual'], actual)
        by_month = {m.month: m for m in data['months']}
        self.assertEqual(by_month[self.m1].in_actual, D('19000.00'))
        self.assertEqual(by_month[self.m2].out_actual, D('7000.00'))
        self.assertEqual(by_month[self.m1].out_actual, D('9500.00'))
        self.assertEqual(by_month[self.m0].out_actual, D('1250.00'))
        # running total is the sum of the nets
        self.assertEqual(data['months'][-1].cumulative, data['totals']['net'])

    def test_the_whole_final_account_comes_in_and_the_whole_final_cost_goes_out(self):
        with org_context(self.org_a):
            data = cashflow.build(self.pa, self.today)
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            report = cost_report(self.pa)
        self.assertEqual((data['final_account'], data['final_cost']), (f.final_account, report['final_cost']))
        # In: received + still owed on AP-002 + the rest less retention + the retention, all ex VAT
        self.assertEqual(data['totals']['in'], f.final_account)
        # Out: the ledger's actuals plus the rest of the forecast final cost
        self.assertEqual(data['totals']['out'], f.final_cost)
        self.assertEqual(data['owed_in'], D('34200.00'))  # AP-002: 53,200 net less 19,000 before
        self.assertTrue(data['uses_programme'] is False)
