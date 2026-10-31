"""
Reproductions for the review of origin/claude/r2-subpay. Each test asserts the CORRECT behaviour,
so each one FAILS on the branch as reviewed.

Run (from a worktree of the branch):
    cp subpay_review_repro.py <worktree>/siteline/subpayments/tests/test_review_repro.py
    cd <worktree>/siteline && MEDIA_ROOT=$(mktemp -d) DEBUG=1 python manage.py test \
        subpayments.tests.test_review_repro --noinput
"""
import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from costs import ledger
from costs import services as cost_services
from costs.models import ActualCost
from subpayments.models import SubApplication, SubcontractorTax
from subpayments.tests.test_views import SubPayTestBase
from tenancy.context import org_context

D = Decimal


class R1BackdatedApplicationTests(SubPayTestBase):
    """HIGH: a sub picks an old valuation date; the cycle runs from it, not from receipt, so the
    application 'stands as notified' the moment it arrives and staff can neither pay less nor withdraw."""

    def test_backdated_portal_application_is_not_instantly_a_default_notified_sum(self):
        self.login(self.sid)
        old = timezone.localdate() - timedelta(days=90)
        # Adapted after the fix: a date three cycles back is refused outright (no application) ...
        r = self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk),
                             {'valuation_date': old.isoformat(), 'gross_to_date': '100000', 'materials': '0',
                              'form_key': 'r1'})
        self.assertEqual(r.status_code, 200)
        with org_context(self.org_a):
            self.assertFalse(SubApplication.objects.filter(subcontract=self.sc).exists())
        # ... and the oldest date still allowed (the previous cycle) runs from the day it is received
        with org_context(self.org_a):
            self.sc.refresh_from_db()
            old = self.sc.allowed_dates(timezone.localdate())[0]
        r = self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk),
                             {'valuation_date': old.isoformat(), 'gross_to_date': '100000', 'materials': '0',
                              'form_key': 'r1b'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            app = SubApplication.objects.get(subcontract=self.sc)
        # Staff try to respond the same day it arrived
        self.login(self.pat)
        self.client.post(self.detail(app), {'action': 'pay_less', 'amount': '0', 'basis': 'Not valued'})
        self.client.post(self.detail(app), {'action': 'withdraw'})
        app = self.reload(app)
        # Expected: received today, so our payment notice / pay less clocks start from today
        self.assertFalse(app.stands_as_notified, 'received today but already stands as notified (s110B)')
        self.assertIn(app.status, (SubApplication.PAY_LESS, SubApplication.WITHDRAWN),
                      f'staff could not pay less or withdraw; notified sum is {app.notified_sum}')


class R2VerificationNumberTests(SubPayTestBase):
    """MEDIUM: the HMRC verification number is shown to the job's PM (not an owner/admin)."""

    def test_pm_does_not_see_verification_number_on_subcontract_page(self):
        self.login(self.pat)
        r = self.client.get(self.url('subpay_subcontract', self.pa.pk, self.sc.pk))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'V1234567890')


class R3MaterialsTests(SubPayTestBase):
    """MEDIUM: adjust_materials has no upper bound, so staff can set materials above the sum due and
    every later payment has a nil CIS deduction."""

    def test_adjusted_materials_capped_at_sum_due(self):
        a = self.receive('20000', '4000')
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'materials', 'amount': '999999', 'reason': 'typo'})
        a = self.reload(a)
        self.assertLessEqual(a.materials, a.payable, f'materials {a.materials} > payable {a.payable} (status {r.status_code})')

    def test_part_payment_does_not_take_all_materials_first(self):
        """LOW/design: the first part payment takes all the materials, so the CIS is deferred to later
        parts (and possibly a later tax month). Pro rata would give 4000 x 4000/19000 = 842.11."""
        a = self.receive('20000', '4000')
        self.pay(a, '4000')
        with org_context(self.org_a):
            p = a.payments.get()
        self.assertGreater(p.cis_deduction, D('0'), f'materials {p.materials}, labour {p.labour}: CIS nil')


class R4DoubleCountTests(SubPayTestBase):
    """MEDIUM (costs interaction): a hand-entered ActualCost against the subcontract order plus the
    sub payment both post ACTUAL to the same code; nothing refuses either."""

    def test_actual_against_sub_order_after_sub_payment_is_refused(self):
        a = self.receive('20000', '0')
        self.pay(a, '10000')
        with org_context(self.org_a):
            refused = False
            try:
                cost_services.record_actual(self.pa, self.pat, self.code, ActualCost.INVOICE, 'Sid Joinery',
                                            D('10000'), reference='INV-1', commitment=self.order)
            except Exception:
                refused = True
            actual = ledger.totals(self.pa)[self.code.pk][ledger.ACTUAL]
        self.assertTrue(refused, f'invoice against the sub order accepted; ledger ACTUAL now {actual} for {D("10000")} paid')


class R5CisCsvTests(SubPayTestBase):
    """LOW/MEDIUM: the CIS summary CSV prints the sub's CURRENT verification number, not the one the
    payment was made under (frozen on SubPayment)."""

    def test_csv_uses_verification_number_frozen_on_the_payment(self):
        a = self.receive('20000', '0')
        self.pay(a, '1000')
        with org_context(self.org_a):
            t = SubcontractorTax.objects.get(subcontractor=self.subco)
            t.verification_number = 'V9999999999'
            t.save()
        self.login(self.alice)
        r = self.client.get(self.url('subpay_cis') + '?export=csv')
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        body = '\n'.join(','.join(x) for x in rows)
        self.assertIn('V1234567890', body, body)
