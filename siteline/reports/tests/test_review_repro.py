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
