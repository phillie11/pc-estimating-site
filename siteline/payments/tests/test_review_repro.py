"""Reviewer reproductions from the independent review of claude/r2-payments; each asserts the fixed behaviour."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.access import visible_index
from core.workflow import apply_transition
from payments import rules
from payments.pdf import application_pdf
from tenancy.context import org_context

from ..models import Application, ApplicationLine, Payment
from .test_views import PaymentsTestBase

T = timezone.localdate()


class Repro(PaymentsTestBase):
    def test_r1_out_of_order_submit_double_claims(self):
        """AP-001 submitted; AP-002 (earlier date) and AP-003 (later) drafts; AP-003 submitted first, then AP-002."""
        self.make(valuation=T - timedelta(days=90), work='100000', submit=True)          # AP-001
        ap2 = self.make(valuation=T - timedelta(days=60), work='150000')                 # draft
        ap3 = self.make(valuation=T - timedelta(days=30), work='200000')                 # draft
        self.login(self.pat)
        self.client.post(self.detail(ap3), {'action': 'submit'})
        r = self.client.post(self.detail(ap2), {'action': 'submit'})
        ap2, ap3 = self.reload(ap2), self.reload(ap3)
        with org_context(self.org_a):
            applied = sum(a.amount_due for a in Application.objects.filter(status='SUBMITTED'))
        # Cumulative net at AP-003 is 200,000 x 95% = 190,000; total applied must not exceed it
        self.assertEqual(ap2.status, 'DRAFT', 'an application earlier than a submitted one must not be submittable')
        self.assertLessEqual(applied, Decimal('190000.00'))

    def test_r2_withdrawing_a_part_paid_application_drops_the_money(self):
        a = self.make(submit=True)
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '50000',
                                          'received_on': T.isoformat()})
        from contracts.commercial import commercial_position
        with org_context(self.org_a):
            before = commercial_position(self.pa)['paid']
        self.client.post(self.detail(a), {'action': 'withdraw'})
        with org_context(self.org_a):
            after = commercial_position(self.pa)['paid']
            status = self.reload(a).status
        # Paid to date is now shown excluding VAT (product owner decision after the review): 50,000 / 1.2
        self.assertEqual(before, Decimal('41666.67'))
        self.assertTrue(status != 'WITHDRAWN' and after == before,
                        f'withdrawn with money received: status={status}, paid to date {before} -> {after}')

    def test_r3_pay_less_below_paid_never_becomes_paid(self):
        a = self.make(submit=True)       # 95,000 + VAT
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '60000', 'received_on': T.isoformat()})
        self.login(self.carla)
        self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '50000', 'basis': 'x'})   # 60,000 inc VAT
        with org_context(self.org_a):
            a = self.reload(a)
            self.assertEqual(a.outstanding, Decimal('0.00'))
            self.assertEqual(a.status, 'PAID', f'notified total covered but status is {a.status}')

    def test_r4_withdrawn_application_still_in_clients_index(self):
        a = self.make(submit=True)
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'withdraw'})
        self.assertEqual(self.status_for(self.carla, self.detail(a)), 404)
        with org_context(self.org_a):
            req = self.request_for(self.carla)
            n = visible_index(req).filter(record_type='payments.application').count()
        self.assertEqual(n, 0, 'withdrawn application still SHARED in the record index the client reads')

    def test_r5_overpayment_is_silently_accepted(self):
        a = self.make(submit=True)  # 114,000 inc VAT
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '1140000', 'received_on': T.isoformat()})
        with org_context(self.org_a):
            self.assertFalse(Payment.objects.filter(application=a, amount=Decimal('1140000')).exists(),
                             'ten times the notified total recorded with no warning')

    def test_r6_variation_approved_after_valuation_date_counts(self):
        from variations.models import Variation
        a = self.make(valuation=T - timedelta(days=20))
        with org_context(self.org_a):
            v = Variation(project=self.pa, title='Later VO', value=Decimal('10000'), approved_value=Decimal('10000'),
                          status=Variation.STATUS_APPROVED)
            v.save(actor=self.pat)
            a = self.reload(a)
            a.recalculate(self.contract)
        self.assertEqual(a.variations_value, Decimal('0.00'),
                         'a variation approved after the valuation date is in the valuation at 100%')

    def test_r7_pdf_injection(self):
        nasty = '<img src="/etc/passwd" width="10" height="10"/><a href="javascript:alert(1)">x</a></para><para>'
        a = self.make(submit=True, notes=nasty)
        with org_context(self.org_a):
            ApplicationLine.objects.create(application=a, section=nasty, value=Decimal('1'))
            a = Application.objects.select_related('project', 'organization').get(pk=a.pk)
            data = application_pdf(a, self.contract, {})
        self.assertTrue(data.startswith(b'%PDF'))
        self.assertNotIn(b'/URI', data)
        self.assertNotIn(b'javascript', data.replace(b' ', b''))  # streams may be compressed; text check below
        import io
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
        self.assertIn('<img src="/etc/passwd"', text)


class StatutoryRepro(PaymentsTestBase):
    def test_r8_boxing_day_on_a_saturday_is_a_statutory_bank_holiday(self):
        """Banking and Financial Dealings Act 1971 Sch 1 para 1 (E&W): '26th December, if it be not a Sunday'."""
        from holidays.calendar import is_excluded
        self.assertTrue(is_excluded(date(2026, 12, 26)), 'Sat 26 Dec 2026 counted as a day (s116 excludes it)')

    def test_r8b_effect_on_jct_cycle(self):
        c = rules.payment_cycle(date(2026, 12, 18), rules.Terms(7, 5, 14, 5))
        self.assertEqual((c.due_date, c.payment_notice_by, c.pay_less_by, c.final_date),
                         (date(2026, 12, 27), date(2027, 1, 3), date(2027, 1, 7), date(2027, 1, 12)))
