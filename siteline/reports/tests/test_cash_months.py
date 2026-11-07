"""
The company cash position reads only the three months it shows (cvr.cashflow.build_many with months):
the figures must be exactly those of each job's full cashflow, which is how the report worked them out
before. Checked on a company built by tenancy.scale (the data behind seed_scale), with extra receipts
on top that the scale data doesn't have: part payments spread over months, a reversed one, one dated
ahead and an unpaid application already part paid, and for several dates either side of today.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from cvr import cashflow, rules
from reports import data
from tenancy import scale
from tenancy.context import org_context

ZERO = Decimal('0.00')


def cash_position_from_full_cashflow(projects, today):
    """The report as it was worked out before: every job's whole cashflow, then the three months picked out."""
    first = rules.month_start(today)
    months = [rules.add_months(first, i) for i in range(data.CASH_MONTHS)]
    built = cashflow.build_many(projects, today)
    rows, totals = [], [{'month': m, 'in': ZERO, 'out': ZERO, 'net': ZERO} for m in months]
    for p in projects:
        by_month = {m.month: m for m in built[p.pk]['months']}
        cells = []
        for i, m in enumerate(months):
            row = by_month.get(m)
            money_in, money_out = (row.money_in, row.money_out) if row else (ZERO, ZERO)
            cells.append({'month': m, 'in': money_in, 'out': money_out, 'net': money_in - money_out})
            totals[i]['in'] += money_in
            totals[i]['out'] += money_out
            totals[i]['net'] += money_in - money_out
        rows.append({'project': p, 'cells': cells, 'net': sum((c['net'] for c in cells), ZERO),
                     'has_money': any(c['in'] or c['out'] for c in cells)})
    grand = {'in': sum((t['in'] for t in totals), ZERO), 'out': sum((t['out'] for t in totals), ZERO)}
    grand['net'] = grand['in'] - grand['out']
    return {'months': months, 'rows': rows, 'totals': totals, 'grand': grand}


class CashPositionReadsOnlyItsMonthsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.built = scale.build('Cash Fitout', jobs=16, staff=6, subs=40, email_domain='cash.test', rfis_per_job=2,
                                diary_per_job=2, docs_per_job=2, apps_per_job=12, subapps_per_job=8,
                                actuals_per_job=60, quality_per_job=(2, 2, 2), incidents_per_job=2, signins_per_job=2)
        cls.today = timezone.localdate()
        from payments.models import Application, Payment
        with org_context(cls.built.org):
            live = list(data.live_jobs())
            for i, p in enumerate(live[:4]):
                apps = list(Application.objects.filter(project=p, status__in=Application.COUNTED)
                            .order_by('valuation_date'))
                if len(apps) < 3:
                    continue
                paid, unpaid = apps[-3], apps[-1]
                pay = lambda app, amount, on, **kw: Payment.objects.create(  # noqa: E731
                    application=app, amount=Decimal(amount), received_on=on, recorded_by=p.manager, **kw)
                # Part payments on an older application over three months, the last of them reversed
                pay(paid, '1234.57', cls.today - timedelta(days=70))
                pay(paid, '999.99', cls.today - timedelta(days=35))
                pay(paid, '500.01', cls.today, reversed_at=timezone.now(), reversed_by=p.manager)
                # The latest, still unpaid: part paid two months ago and again this month, and one dated ahead
                pay(unpaid, '2500.00', cls.today - timedelta(days=60))
                pay(unpaid, '333.33', cls.today)
                if i % 2:
                    pay(unpaid, '100.00', cls.today + timedelta(days=40))

    def flat(self, rep):
        return {'months': rep['months'], 'totals': rep['totals'], 'grand': rep['grand'],
                'rows': [(r['project'].pk, r['cells'], r['net'], r['has_money']) for r in rep['rows']]}

    def test_the_same_figures_as_each_jobs_full_cashflow(self):
        with org_context(self.built.org):
            jobs = list(data.live_jobs())
            self.assertGreaterEqual(len(jobs), 8)
            for days in (0, -40, 35, -200, 400):
                today = self.today + timedelta(days=days)
                for when in (today, rules.month_start(today), rules.month_end(today)):
                    with self.subTest(today=when):
                        now = data.cash_position(when)
                        self.assertTrue(any(r['has_money'] for r in now['rows']))
                        self.assertEqual(self.flat(now), self.flat(cash_position_from_full_cashflow(jobs, when)))

    def test_months_are_the_full_cashflows_months(self):
        """Every figure of the Month, not only in and out, for this month and the next two."""
        with org_context(self.built.org):
            jobs = list(data.live_jobs())
            first = rules.month_start(self.today)
            months = [rules.add_months(first, i) for i in range(3)]
            some = cashflow.build_many(jobs, self.today, months=months)
            full = cashflow.build_many(jobs, self.today)

            def figures(m):
                return (m.month, m.in_actual, m.in_forecast, m.out_actual, m.out_forecast)
            for p in jobs:
                by_month = {m.month: m for m in full[p.pk]['months']}
                with self.subTest(p.name):
                    self.assertEqual([figures(m) for m in some[p.pk]['months']],
                                     [figures(by_month[m]) if m in by_month else (m, ZERO, ZERO, ZERO, ZERO)
                                      for m in months])

    def test_reads_fewer_rows_and_the_same_queries_whatever_the_size(self):
        """Fixed queries: the report's page budget is in e2e/test_query_counts.py; here, not one per job."""
        with org_context(self.built.org):
            jobs = list(data.live_jobs())
            with CaptureQueriesContext(connection) as few:
                data.cash_position(self.today, projects=jobs[:2])
            with CaptureQueriesContext(connection) as all_jobs:
                data.cash_position(self.today, projects=jobs)
        self.assertLessEqual(len(all_jobs), len(few))

    def test_months_must_start_by_this_month(self):
        with org_context(self.built.org):
            with self.assertRaises(ValueError):
                cashflow.build_many(list(data.live_jobs())[:1], self.today,
                                    months=[rules.add_months(self.today, 1)])
