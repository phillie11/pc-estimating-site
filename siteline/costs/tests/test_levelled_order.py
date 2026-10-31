"""
Committing an appointed package uses the levelled total from quote levelling (the quote plus
the estimator's adjustments for gaps in it), keeps the quote for reference, and falls back to
the quoted value, saying so, when the package has no levelled total.
"""
from decimal import Decimal as D

from costs import ledger, services
from costs.models import Commitment
from directory.models import Package, Trade
from levelling.models import Adjustment, LevellingSheet

from .test_costs import CostTestBase


class LevelledOrderTests(CostTestBase):
    def setUp(self):
        super().setUp()
        with self.a():
            self.trade = Trade.objects.get_or_create(name='Joinery')[0]
            self.package.trade = self.trade
            self.package.save()

    def level(self, *amounts):
        """Adjustments on the package's trade sheet: 186,500 quoted plus these."""
        with self.a():
            sheet = LevellingSheet.objects.get_or_create(project=self.pa, trade=self.trade)[0]
            for amount in amounts:
                Adjustment.objects.create(sheet=sheet, package=self.package, kind=Adjustment.PROVISIONAL,
                                          amount=D(amount), reason='Ironmongery allowance')
            return sheet

    def test_levelled_total_is_the_order_value_and_the_quote_is_kept(self):
        self.level('3500.00', '-250.01')  # 186,500 + 3,500 - 250.01 = 189,749.99
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            self.assertEqual((c.original_value, c.quoted_value, c.value_basis),
                             (D('189749.99'), D('186500.00'), Commitment.BASIS_LEVELLED))
            # The ledger commits the levelled figure, not the quote
            self.assertEqual(ledger.totals(self.pa)[self.codes['220'].pk][ledger.COMMITMENT], D('189749.99'))

    def test_no_levelled_total_falls_back_to_the_quote(self):
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            self.assertEqual((c.original_value, c.quoted_value, c.value_basis),
                             (D('186500.00'), D('186500.00'), Commitment.BASIS_QUOTE))

    def test_a_sheet_for_another_trade_does_not_count(self):
        with self.a():
            other = Trade.objects.create(name='Drylining')
            sheet = LevellingSheet.objects.create(project=self.pa, trade=other)
            dry = Package.objects.create(project=self.pa, subcontractor=self.sub, trade=other,
                                         status=Package.QUOTED, quote_value=D('1000'))
            Adjustment.objects.create(sheet=sheet, package=dry, kind=Adjustment.PROVISIONAL, amount=D('99'),
                                      reason='x')
            self.assertEqual(services.order_value_for(self.package), (D('186500.00'), Commitment.BASIS_QUOTE))

    def test_a_levelled_total_of_zero_or_less_is_refused(self):
        self.level('-186500')
        with self.a():
            with self.assertRaises(services.CostError):
                services.commit_package(self.package, self.codes['220'], self.pm)
            self.assertFalse(Commitment.objects.exists())

    def test_supplier_orders_have_no_basis(self):
        with self.a():
            o = services.place_order(self.pa, self.pm, self.codes['500'], 'Speedy Hire', '6800')
            self.assertEqual((o.value_basis, o.quoted_value), ('', None))

    def test_commit_screen_and_order_show_both_figures(self):
        self.level('3500')
        self.login(self.pm)
        r = self.client.get(self.curl('cost_commitments'))
        self.assertContains(r, '£190,000.00')
        self.assertContains(r, 'Levelled total · quoted £186,500.00')
        r = self.client.post(self.curl('cost_commit_package'), {'package': self.package.pk,
                                                                 'cost_code': self.codes['220'].pk}, follow=True)
        self.assertContains(r, 'at £190,000.00 (the levelled total from quote levelling)')
        with self.a():
            c = Commitment.objects.get()
        r = self.client.get(c.get_absolute_url())
        self.assertContains(r, 'Levelled total from quote levelling')
        self.assertContains(r, 'Original quote')
        self.assertContains(r, '£186,500.00')

    def test_commit_screen_says_when_it_falls_back(self):
        self.login(self.pm)
        r = self.client.get(self.curl('cost_commitments'))
        self.assertContains(r, 'Quoted value: no levelled total')
        r = self.client.post(self.curl('cost_commit_package'), {'package': self.package.pk,
                                                                 'cost_code': self.codes['220'].pk}, follow=True)
        self.assertContains(r, 'at £186,500.00 (the quoted value, as there is no levelled total)')
        with self.a():
            c = Commitment.objects.get()
        self.assertContains(self.client.get(c.get_absolute_url()), 'Quoted value: there was no levelled total')

    def test_another_company_cannot_commit_or_see_it(self):
        self.level('3500')
        self.login(self.bob)
        self.assertEqual(self.client.get(self.curl('cost_commitments')).status_code, 404)
        r = self.client.post(self.curl('cost_commit_package'), {'package': self.package.pk,
                                                                 'cost_code': self.codes['220'].pk})
        self.assertEqual(r.status_code, 404)
        with self.a():
            self.assertFalse(Commitment.objects.exists())

    def test_externals_never_see_the_commit_screen(self):
        self.level('3500')
        for who in (self.subu, self.bidu, self.est):
            self.login(who)
            self.assertIn(self.client.get(self.curl('cost_commitments')).status_code, (403, 404), who)
