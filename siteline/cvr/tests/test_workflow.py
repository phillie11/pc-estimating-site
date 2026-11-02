"""
The monthly CVR's life: start, adjust, submit (figures frozen), send back, sign off (locked for
good), months in order, corrections only in a later month; adjustments append-only and reversed
like the cost ledger; movement from last month's signed-off figures.
"""
from decimal import Decimal as D

from core.exceptions import InvalidTransition, TransitionForbidden
from core.models import AuditEntry, RecordIndex
from core.workflow import apply_transition
from costs import services as cost_services
from cvr import figures, services
from cvr.models import CVRAdjustment, CVRPeriod
from tenancy.context import org_context

from .base import CVRTestBase


class PeriodLifeTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.rows = self.build_money()

    def start(self, month, actor=None):
        with org_context(self.org_a):
            period, created = services.start_period(self.pa, month, actor or self.pat, self.today)
        return period

    def test_adjustments_count_in_their_month_only(self):
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            services.add_adjustment(p1, 'COST', D('2500'), 'October joinery invoice not yet in', self.pat)
            services.add_adjustment(p1, 'VALUE', D('-4250'), "Client's QS valued the bar back under", self.pat)
            services.add_adjustment(p1, 'FINAL_COST', D('3000'), 'Risk: extra acoustic works', self.pat)
            f = figures.figures_for(p1)
            plain = figures.live_figures([self.pa], self.m1)[self.pa.pk]
        self.assertEqual(f.cost, plain.cost + D('2500.00'))
        self.assertEqual(f.value, plain.value - D('4250.00'))
        self.assertEqual(f.final_cost, plain.final_cost + D('3000.00'))
        self.assertEqual(f.final_account, plain.final_account)
        self.assertEqual(f.margin, D('32750.00'))  # (56,000 - 4,250) - (16,500 + 2,500)

    def test_submit_freezes_sign_off_locks(self):
        p2 = self.start(self.m2)
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            services.add_adjustment(p1, 'COST', D('2500'), 'Accrual', self.pat)
            apply_transition(p1, 'submit', self.pat, today=self.today)
            p1.refresh_from_db()
            frozen = figures.figures_for(p1)
            self.assertEqual(p1.cost_adjustments, D('2500.00'))
            self.assertEqual(frozen.cost_to_date, D('16500.00'))
            # A cost dated last month, posted now, doesn't move the submitted figures...
            cost_services.record_actual(self.pa, self.pat, self.codes['400'], 'INVOICE', 'Late Co', D('800'),
                                        cost_date=self.day(self.m1, 28))
            self.assertEqual(figures.figures_for(CVRPeriod.objects.get(pk=p1.pk)).cost_to_date, D('16500.00'))
            # ...and no adjustment can be added while it waits
            with self.assertRaises(services.CVRError):
                services.add_adjustment(p1, 'COST', D('1'), 'x', self.pat)
            # Months are signed off in order
            with self.assertRaisesMessage(InvalidTransition, p2.reference):
                apply_transition(p1, 'sign_off', self.alice)
            p1.refresh_from_db()
            apply_transition(p2, 'submit', self.pat, today=self.today)
            # The PM can't sign off unless the company allows it
            with self.assertRaises(TransitionForbidden):
                apply_transition(p2, 'sign_off', self.pat)
            apply_transition(p2, 'sign_off', self.alice)
            apply_transition(p1, 'sign_off', self.alice)
            p1 = CVRPeriod.objects.get(pk=p1.pk)
            self.assertEqual(p1.status, CVRPeriod.SIGNED_OFF)
            self.assertEqual(p1.signed_off_by, self.alice)
            # Locked: no save, no adjustment, no transition, no soft delete
            p1.commentary = 'changed'
            with self.assertRaises(PermissionError):
                p1.save(actor=self.alice)
            p1 = CVRPeriod.objects.get(pk=p1.pk)
            with self.assertRaises(services.CVRError):
                services.add_adjustment(p1, 'COST', D('1'), 'x', self.alice)
            with self.assertRaises(InvalidTransition):
                apply_transition(p1, 'send_back', self.alice)
            with self.assertRaises(PermissionError):
                p1.soft_delete(self.alice)
            # The late cost shows as drift on the signed-off month and lands in this month's figures
            self.assertEqual(figures.drift(p1), {'cost': D('800.00'), 'forecast final cost': D('800.00')})
            self.assertEqual(figures.live_figures([self.pa], self.today)[self.pa.pk].cost_to_date, D('18550.00'))
            # Nothing can be started at or before a signed-off month
            with self.assertRaises(services.CVRError):
                services.start_period(self.pa, self.m2, self.pat, self.today)
            self.assertNotIn(self.m1, services.startable_months(self.pa, self.today))
            self.assertIn(self.m0, services.startable_months(self.pa, self.today))
            # Every step is in the audit trail
            actions = list(AuditEntry.objects.filter(record_idx=RecordIndex.with_deleted_for(p1))
                           .values_list('action', flat=True))
        for name in ('created', 'adjusted', 'submit', 'sign_off'):
            self.assertIn(name, actions)

    def test_send_back_unfreezes(self):
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            apply_transition(p1, 'submit', self.pat, today=self.today)
            apply_transition(p1, 'send_back', self.alice, note='Add the scaffold accrual')
            p1.refresh_from_db()
            self.assertIsNone(p1.cost_to_date)
            self.assertFalse(p1.is_frozen)
            services.add_adjustment(p1, 'COST', D('900'), 'Scaffold hire', self.pat)

    def test_this_month_cannot_be_submitted_until_it_ends(self):
        p0 = self.start(self.m0)
        with org_context(self.org_a):
            with self.assertRaisesMessage(InvalidTransition, 'has not ended'):
                apply_transition(p0, 'submit', self.pat, today=self.today)

    def test_pm_signs_off_where_the_company_allows_it(self):
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            s = services.settings_for(self.org_a)
            s.pm_can_sign_off = True
            s.save()
            apply_transition(p1, 'submit', self.pat, today=self.today)
            apply_transition(p1, 'sign_off', self.pat)
            self.assertEqual(CVRPeriod.objects.get(pk=p1.pk).status, CVRPeriod.SIGNED_OFF)
            # Someone on the staff who doesn't run the job still can't
            p0 = services.start_period(self.pa, self.m0, self.pat, self.today)[0]
            from cvr.access import can_sign_off
            self.assertFalse(can_sign_off(self.stan, p0))
            self.assertTrue(can_sign_off(self.adam, p0))

    def test_a_month_cannot_be_started_before_a_signed_off_one(self):
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            apply_transition(p1, 'submit', self.pat, today=self.today)
            apply_transition(p1, 'sign_off', self.adam)
            with self.assertRaisesMessage(services.CVRError, 'later month is already signed off'):
                services.start_period(self.pa, self.m2, self.pat, self.today)
            with self.assertRaises(services.CVRError):
                services.start_period(self.pa, self.m0.replace(year=self.m0.year + 1), self.pat, self.today)

    def test_movement_uses_last_months_signed_off_figures(self):
        p1 = self.start(self.m1)
        with org_context(self.org_a):
            apply_transition(p1, 'submit', self.pat, today=self.today)
            apply_transition(p1, 'sign_off', self.alice)
            p1 = CVRPeriod.objects.get(pk=p1.pk)
            cost_services.record_actual(self.pa, self.pat, self.codes['400'], 'INVOICE', 'Late Co', D('800'),
                                        cost_date=self.day(self.m1, 28))
            p0 = services.start_period(self.pa, self.m0, self.pat, self.today)[0]
            from cvr.views import _previous_figures
            prev = _previous_figures(self.pa, p0.month)
            now = figures.figures_for(p0)
        self.assertTrue(prev.frozen)
        self.assertEqual(prev.cost_to_date, D('16500.00'))  # as signed off, not the 17,300 now
        self.assertEqual(now.movement(prev)['cost'], D('2050.00'))  # 800 late + 1,250 this month


class AdjustmentAppendOnlyTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()
        with org_context(self.org_a):
            self.p1, _ = services.start_period(self.pa, self.m1, self.pat, self.today)
            self.adj = services.add_adjustment(self.p1, 'COST', D('2500'), 'Accrual', self.pat)

    def test_never_edited_or_deleted(self):
        with org_context(self.org_a):
            self.adj.amount = D('1')
            with self.assertRaises(PermissionError):
                self.adj.save()
            with self.assertRaises(PermissionError):
                self.adj.delete()
            with self.assertRaises(PermissionError):
                CVRAdjustment.objects.filter(pk=self.adj.pk).update(amount=D('1'))
            with self.assertRaises(PermissionError):
                CVRAdjustment.objects.filter(pk=self.adj.pk).delete()

    def test_reversed_once(self):
        with org_context(self.org_a):
            adj = CVRAdjustment.objects.get(pk=self.adj.pk)
            rev = services.reverse_adjustment(adj, self.pat, 'Invoice arrived')
            self.assertEqual(rev.amount, D('-2500.00'))
            self.assertEqual(figures.figures_for(self.p1).cost_adjustments, D('0.00'))
            with self.assertRaises(services.CVRError):
                services.reverse_adjustment(CVRAdjustment.objects.get(pk=self.adj.pk), self.pat, 'again')
            with self.assertRaises(services.CVRError):
                services.reverse_adjustment(rev, self.pat, 'reverse the reversal')
            self.assertEqual(services.live_adjustments(self.p1), [])

    def test_bring_forward(self):
        with org_context(self.org_a):
            p0, _ = services.start_period(self.pa, self.m0, self.pat, self.today)
            n = services.bring_forward(p0, self.pat)
            self.assertEqual(n, 1)
            moved = CVRAdjustment.objects.get(period=p0)
            self.assertEqual(moved.amount, D('2500.00'))
            self.assertTrue(moved.reason.startswith('Brought forward from'))
            # The original month keeps its own
            self.assertEqual(CVRAdjustment.objects.filter(period=self.p1).count(), 1)

    def test_validation(self):
        with org_context(self.org_a):
            for kind, amount, reason in (('COST', D('0'), 'x'), ('COST', D('5'), ' '), ('NOPE', D('5'), 'x')):
                with self.subTest(kind=kind, amount=amount, reason=reason):
                    with self.assertRaises(services.CVRError):
                        services.add_adjustment(self.p1, kind, amount, reason, self.pat)
            with self.assertRaises(TypeError):
                services.add_adjustment(self.p1, 'COST', 1.5, 'float', self.pat)
