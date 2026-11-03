"""
core.exports: the one formula-safe CSV helper, and a test per register export that a cell starting with
a formula, even after leading spaces, reaches the spreadsheet as text.
"""
import csv
import io
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

import changes.tests.base as commercial_base
import costs.tests.test_costs as costs_tests
import procurement.tests.base as procurement_base
import subpayments.tests.test_views as subpay_tests
from core import exports
from tenancy.context import org_context

NASTY = '  =HYPERLINK("http://evil.example","x")'
ESCAPED = "'" + NASTY


def rows(response):
    return list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))


class SafeTests(SimpleTestCase):
    def test_table(self):
        for value, expected in [
            ('=1+1', "'=1+1"), ('+1', "'+1"), ('-cmd', "'-cmd"), ('@SUM(A1)', "'@SUM(A1)"),
            ('\t=1', "'\t=1"), ('\r=1', "'\r=1"), ('\n=1', "'\n=1"),
            ('   =1+1', "'   =1+1"), (' \t@x', "' \t@x"),               # after leading spaces too
            ('-120.00', '-120.00'), ('-1.5%', '-1.5%'), ('42', '42'),  # plain numbers are data
            (Decimal('-12.50'), '-12.50'), (7, '7'), (date(2026, 3, 1), '2026-03-01'),
            (None, ''), ('', ''), ('Bar joinery', 'Bar joinery'), ('a=b', 'a=b'),
        ]:
            with self.subTest(value=value):
                self.assertEqual(exports.safe(value), expected)

    def test_levelling_still_exports_the_same_function(self):
        from levelling import exports as old
        self.assertIs(old.safe, exports.safe)

    def test_writer_and_response_escape_every_cell(self):
        r = exports.csv_response('x.csv', ['=head'], [[NASTY, Decimal('-1.00'), None]])
        self.assertEqual(r['Content-Disposition'], 'attachment; filename="x.csv"')
        self.assertEqual(rows(r), [["'=head"], [ESCAPED, '-1.00', '']])


class SimpleRegisterExportTests(commercial_base.CommercialTestBase):
    """Projects, RFIs, variations, subcontractors and the drawing register."""

    def test_project_list(self):
        with org_context(self.org_a):
            self.pa.client_name = NASTY
            self.pa.save()
        self.login(self.alice)
        body = rows(self.client.get(self.url('project_list') + '?view=all&export=csv'))
        self.assertIn(ESCAPED, [c for r in body for c in r])

    def test_rfi_register(self):
        from rfis.models import RFI
        with org_context(self.org_a):
            RFI(project=self.pa, title=NASTY, question='?').save(actor=self.pat)
        self.login(self.pat)
        body = rows(self.client.get(self.url('rfi_list', self.pa.pk) + '?export=csv'))
        self.assertEqual(body[1][1], ESCAPED)

    def test_variations(self):
        from variations.models import Variation
        with org_context(self.org_a):
            Variation(project=self.pa, title=NASTY, description='x', client_reference='@PO').save(actor=self.pat)
        self.login(self.pat)
        body = rows(self.client.get(self.url('variation_list', self.pa.pk) + '?export=csv'))
        self.assertEqual(body[1][1], ESCAPED)
        self.assertEqual(body[1][8], "'@PO")

    def test_subcontractors(self):
        from directory.models import Subcontractor
        with org_context(self.org_a):
            Subcontractor.objects.create(company_name=NASTY, contact_name='-Dee', email='d@x.test')
        self.login(self.alice)
        body = rows(self.client.get(self.url('sub_list') + '?export=csv'))
        row = next(r for r in body if r[0] == ESCAPED)
        self.assertEqual(row[3], "'-Dee")

    def test_drawing_register(self):
        from projects.models import Document
        with org_context(self.org_a):
            Document.objects.create(project=self.pa, title=NASTY, reference='+A-100')
        self.login(self.pat)
        body = rows(self.client.get(self.url('document_list', self.pa.pk) + '?export=csv'))
        row = next(r for r in body[1:] if r[1] == ESCAPED)
        self.assertEqual(row[0], "'+A-100")


class CostReportExportTests(costs_tests.CostTestBase):
    def test_cost_report(self):
        self.budget(c100='100000', c220='180000', c900='620000')
        with self.a():
            costs_tests.CostCode.objects.filter(pk=self.codes['220'].pk).update(name=NASTY)
        self.login(self.pm)
        body = rows(self.client.get(self.curl('cost_report') + '?export=csv'))
        row = next(r for r in body if r[0] == '220')
        self.assertEqual(row[1], ESCAPED)


class ProcurementExportTests(procurement_base.ProcurementTestBase):
    def test_schedule(self):
        self.line(title=NASTY, required_on_site=timezone.localdate())
        self.login(self.pat)
        body = rows(self.client.get(self.url('procurement_schedule', self.pa.pk), {'export': 'csv'}))
        self.assertIn(ESCAPED, [c for r in body for c in r])

    def test_submittals(self):
        self.submittal(title=NASTY)
        self.login(self.pat)
        body = rows(self.client.get(self.url('submittal_list', self.pa.pk), {'export': 'csv'}))
        self.assertIn(ESCAPED, [c for r in body for c in r])


class CisExportTests(subpay_tests.SubPayTestBase):
    def test_cis_summary_after_leading_spaces(self):
        with org_context(self.org_a):
            subpay_tests.Subcontractor.objects.filter(pk=self.subco.pk).update(company_name=NASTY)
        a = self.receive()
        self.pay(a, '1000')
        self.login(self.alice)
        body = rows(self.client.get(self.url('subpay_cis') + '?export=csv', follow=True))
        self.assertIn(ESCAPED, [c for r in body for c in r])
