"""
Review of the company reports (claude/r3b-api): each test reproduces one defect found in review, and
failed before its fix.
"""
from datetime import timedelta
from decimal import Decimal as D

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from api.tests.base import ApiTestBase
from cvr import cashflow
from projects.models import Activity, Project
from reports import data
from tenancy.context import org_context


class MarginMonthInYearOneTests(ApiTestBase):
    """?month=0001-01 on the portfolio margin report was a server error (the month before it doesn't exist)."""

    def test_earliest_month_is_a_page_not_a_crash(self):
        self.login(self.alice)
        for month in ('0001-01', '0001-02', '1899-12'):
            with self.subTest(month):
                self.assertEqual(self.client.get(self.url('report_margin'), {'month': month}).status_code, 200)
                resp = self.client.get(self.url('report_margin'), {'month': month, 'export': 'csv'})
                self.assertEqual(resp.status_code, 200)
                self.assertIn('portfolio-margin-1900-01.csv', resp['Content-Disposition'])


class CashPositionQueryCostTests(ApiTestBase):
    """The cash position ran every live job's cashflow one at a time: about nine queries a job, so 50 jobs
    was over 450 queries for one page (the house limit is 50, whatever the company's size)."""

    def add_live_jobs(self, n, start):
        with org_context(self.org_a):
            for i in range(n):
                p = Project.objects.create(name=f'Live job {start + i}', stage=Project.STAGE_LIVE,
                                           value=D('100000'), completion_date=self.today + timedelta(days=200))
                Activity.objects.create(project=p, name='Fit-out', start=self.today, finish=self.today + timedelta(days=90))

    def count(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self.url('report_cash'))
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def test_the_same_queries_for_three_jobs_or_thirty(self):
        self.login(self.alice)
        self.add_live_jobs(2, 0)
        few = self.count()
        self.add_live_jobs(28, 2)
        many = self.count()
        self.assertLessEqual(many, few, f'{few} queries grew to {many}')
        self.assertLessEqual(many, 50)

    def test_build_many_is_build_for_every_job(self):
        self.add_live_jobs(3, 0)
        with org_context(self.org_a):
            jobs = list(data.live_jobs())
            many = cashflow.build_many(jobs, self.today)
            for p in jobs:
                one = cashflow.build(Project.objects.get(pk=p.pk), self.today)
                with self.subTest(p.name):
                    self.assertEqual([(m.month, m.in_actual, m.in_forecast, m.out_actual, m.out_forecast, m.cumulative)
                                      for m in many[p.pk]['months']],
                                     [(m.month, m.in_actual, m.in_forecast, m.out_actual, m.out_forecast, m.cumulative)
                                      for m in one['months']])
                    self.assertEqual(many[p.pk]['totals'], one['totals'])
                    self.assertEqual(many[p.pk]['notes'], one['notes'])
