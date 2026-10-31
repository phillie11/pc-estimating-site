"""
The fixes from the independent review (payments/tests/test_review_repro.py holds the reviewer's own
reproductions): submit order, withdraw, settling after a notice, overpayments, reversal, variations by
date, the consultant contract administrator, and certified and paid excluding VAT.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contracts.commercial import commercial_position
from core.models import AuditEntry
from core.workflow import apply_transition
from projects.models import ProjectMember
from tenancy.context import org_context
from variations.models import Variation

from ..models import Application, Payment
from .test_views import PaymentsTestBase

T = timezone.localdate()
ZERO = Decimal('0.00')


class SubmitOrderTests(PaymentsTestBase):
    def test_earlier_draft_cannot_be_submitted_after_a_later_one(self):
        early = self.make(valuation=T - timedelta(days=60))
        self.make(valuation=T - timedelta(days=30), submit=True)
        self.login(self.pat)
        r = self.client.get(self.detail(early))
        self.assertContains(r, "This draft can't be submitted")
        self.assertNotContains(r, 'Submit to client</button>')
        r = self.client.post(self.detail(early), {'action': 'submit'}, follow=True)
        self.assertContains(r, 'claim the same money twice')
        self.assertEqual(self.reload(early).status, 'DRAFT')

    def test_in_order_still_works(self):
        self.make(valuation=T - timedelta(days=60), submit=True)
        later = self.make(valuation=T - timedelta(days=30), submit=True)
        self.assertEqual(later.status, 'SUBMITTED')


class WithdrawTests(PaymentsTestBase):
    def test_refused_once_money_is_received(self):
        a = self.make(submit=True)
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '1000', 'received_on': T.isoformat()})
        r = self.client.get(self.detail(a))
        self.assertContains(r, 'Money has been received against it')
        self.assertNotContains(r, 'value="withdraw"')
        self.client.post(self.detail(a), {'action': 'withdraw'})
        self.assertEqual(self.reload(a).status, 'SUBMITTED')

    def test_refused_once_it_stands_as_notified(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.payment_notice_by = T - timedelta(days=1)
            a.save(actor=self.pat)
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'withdraw'}, follow=True)
        self.assertContains(r, 'stands as the notified sum and cannot be withdrawn')
        self.assertEqual(self.reload(a).status, 'SUBMITTED')

    def test_withdrawn_goes_back_to_internal(self):
        a = self.make(submit=True)
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'withdraw'})
        a = self.reload(a)
        self.assertEqual((a.status, a.visibility), ('WITHDRAWN', 'INTERNAL'))
        self.assertEqual(self.status_for(self.carla, self.detail(a)), 404)


class SettleTests(PaymentsTestBase):
    def test_payment_notice_brings_the_sum_down_to_what_was_paid(self):
        a = self.make(submit=True)
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '60000', 'received_on': T.isoformat()})
        self.login(self.carla)
        self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '50000', 'basis': 'x'})
        with org_context(self.org_a):
            a = self.reload(a)
            self.assertEqual((a.status, a.outstanding, a.paid_in_full_on), ('PAID', ZERO, T))
            self.assertTrue(AuditEntry.objects.filter(action='settled').exists())

    def test_no_payments_no_settling(self):
        a = self.make(submit=True)
        self.login(self.carla)
        self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '0', 'basis': 'Nothing due'})
        self.assertEqual(self.reload(a).status, 'NOTIFIED')


class OverpaymentTests(PaymentsTestBase):
    def test_more_than_outstanding_is_refused(self):
        a = self.make(submit=True)   # 114,000 inc VAT
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'payment', 'amount': '114000.01', 'received_on': T.isoformat()})
        self.assertContains(r, 'Overpayments are not supported')
        self.assertContains(r, '£114,000.00 outstanding')
        r = self.client.post(self.detail(a), {'action': 'payment', 'amount': '114000.00', 'received_on': T.isoformat()})
        self.assertEqual(self.reload(a).status, 'PAID')


class ReversalTests(PaymentsTestBase):
    def pay(self, a, amount):
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': amount, 'received_on': T.isoformat()})
        with org_context(self.org_a):
            return Payment.objects.filter(application=a).order_by('-pk').first()

    def test_admin_reverses_with_a_reason_and_it_reopens(self):
        a = self.make(submit=True)
        p = self.pay(a, '114000')
        self.assertEqual(self.reload(a).status, 'PAID')
        self.login(self.adam)
        r = self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk, 'reason': ''})
        self.assertContains(r, 'This field is required')
        self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk, 'reason': 'Wrong job'})
        with org_context(self.org_a):
            a, p = self.reload(a), Payment.objects.get(pk=p.pk)
            self.assertEqual((a.status, a.paid_total, a.outstanding, a.paid_in_full_on),
                             ('SUBMITTED', ZERO, Decimal('114000.00'), None))
            self.assertEqual((p.reversal_reason, p.reversed_by), ('Wrong job', self.adam))
            self.assertTrue(AuditEntry.objects.filter(action='reopen_submitted', note__contains='Wrong job').exists())
            with self.assertRaises(PermissionError):
                p.delete()

    def test_part_payment_reversal_is_audited(self):
        a = self.make(submit=True)
        p = self.pay(a, '1000')
        self.login(self.alice)
        self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk, 'reason': 'Bounced'})
        with org_context(self.org_a):
            self.assertTrue(AuditEntry.objects.filter(action='payment_reversed', note__contains='Bounced').exists())
            self.assertEqual(self.reload(a).paid_total, ZERO)

    def test_only_owners_and_admins(self):
        a = self.make(submit=True)
        p = self.pay(a, '1000')
        for user in (self.pat, self.carla):
            self.login(user)
            self.assertEqual(self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk,
                                                               'reason': 'x'}).status_code, 403)
        with org_context(self.org_a):
            self.assertIsNone(Payment.objects.get(pk=p.pk).reversed_at)


class VariationsByDateTests(PaymentsTestBase):
    def vo(self, value, decided):
        with org_context(self.org_a):
            v = Variation(project=self.pa, title='VO', description='x', value=Decimal(value))
            v.save(actor=self.pat)
            Variation.objects.filter(pk=v.pk).update(status='APPROVED', approved_value=Decimal(value), decided_at=decided)

    def test_only_those_approved_by_the_valuation_date(self):
        valuation = T - timedelta(days=10)
        self.vo('1000', timezone.now() - timedelta(days=20))   # before: in
        self.vo('2000', timezone.now() - timedelta(days=10))   # on the valuation date: in
        self.vo('4000', timezone.now() - timedelta(days=5))    # after: next time
        a = self.make(valuation=valuation)
        self.assertEqual(a.variations_value, Decimal('3000.00'))


class ConsultantAdministratorTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.connie_member = ProjectMember.objects.get(project=self.pa, user=self.connie)
            self.contract.administrator = self.connie_member
            self.contract.save()

    def test_named_consultant_gives_notices(self):
        a = self.make(submit=True)
        self.assertEqual(self.reload(a).ball_in_court_user, self.connie)
        self.assertEqual(self.status_for(self.connie, self.detail(a)), 200)
        self.login(self.connie)
        r = self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '90000', 'basis': 'Certified'})
        self.assertEqual(r.status_code, 302)
        a = self.reload(a)
        self.assertEqual((a.status, a.payment_notice_given_by), ('NOTIFIED', self.connie))
        self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '80000', 'basis': 'Defects'})
        self.assertEqual(self.reload(a).status, 'PAY_LESS')

    def test_client_still_can_and_nobody_else(self):
        a = self.make(submit=True)
        self.assertEqual(self.status_for(self.carla, self.detail(a)), 200)
        for user in (self.sid, self.bea):
            self.assertEqual(self.status_for(user, self.detail(a)), 403)
        with org_context(self.org_a):
            other = ProjectMember.objects.create(project=self.pa, user=self.nora, role=ProjectMember.ROLE_CONSULTANT)
        self.assertEqual(self.status_for(self.nora, self.detail(a)), 403)   # a consultant, not the administrator
        self.assertTrue(other.pk)

    def test_consultant_needs_me_and_portal_tab(self):
        a = self.make(submit=True)
        from .. import actions
        with org_context(self.org_a):
            items = actions.items(self.request_for(self.connie))
        self.assertEqual([i.ref for i in items], [a.ref])
        self.login(self.connie)
        self.assertContains(self.client.get(self.url('project_decisions', self.pa.pk)), 'Applications for payment')

    def test_consultant_administrator_on_another_job_gets_nothing_here(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            self.contract.administrator = None
            self.contract.save()
        self.assertEqual(self.status_for(self.connie, self.detail(a)), 403)


class ExVatTotalsTests(PaymentsTestBase):
    def test_certified_and_paid_both_exclude_vat(self):
        a = self.make(submit=True)   # 95,000 + 19,000 VAT
        with org_context(self.org_a):
            apply_transition(a, 'payment_notice', self.carla, amount=Decimal('90000'), basis='x')
            Payment.objects.create(application=a, amount=Decimal('54000.00'), received_on=T, recorded_by=self.pat)
            pos = commercial_position(self.pa)
        self.assertEqual((pos['certified'], pos['paid']), (Decimal('90000.00'), Decimal('45000.00')))
        self.login(self.pat)
        r = self.client.get(self.url('application_list', self.pa.pk))
        self.assertContains(r, 'Paid to date (ex VAT)')
        self.assertContains(r, 'VAT £9,000')

    def test_withdrawn_and_draft_count_nothing(self):
        self.make()
        with org_context(self.org_a):
            self.assertEqual(commercial_position(self.pa)['certified'], ZERO)
            self.assertFalse(Application.objects.filter(status='PAID').exists())
