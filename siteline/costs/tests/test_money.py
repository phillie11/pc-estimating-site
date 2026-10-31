"""
The fixed test table for cost control's money rules (costs/money.py). Each row is in plain
words in the stream report so the product owner can sign it off.
"""
from decimal import Decimal as D

from django.test import SimpleTestCase

from costs.money import budget_from_tender, default_forecast, margin, pennies

# value typed -> stored (half up to 1p; halves go away from zero)
PENNIES = [
    ('1250', D('1250.00')),
    ('1250.5', D('1250.50')),
    ('1.005', D('1.01')),
    ('1.004', D('1.00')),
    ('2.675', D('2.68')),
    ('-1.005', D('-1.01')),
    ('£1,250.505', D('1250.51')),
    (D('0.125'), D('0.13')),
    (7, D('7.00')),
]

# (tender value, margin %) -> budget
BUDGETS = [
    ('1240000', '12', D('1091200.00')),
    ('1650000', '11', D('1468500.00')),
    ('1240000', '12.5', D('1085000.00')),
    ('100000.01', '10', D('90000.01')),   # 90000.009 rounds up
    ('333.33', '33.33', D('222.23')),     # 222.2311 rounds down
    ('500000', '0', D('500000.00')),
]

# (forecast final account, forecast final cost) -> (margin £, margin %)
MARGINS = [
    ('1245900', '1098800', D('147100.00'), D('11.8')),
    ('1650000', '1508500', D('141500.00'), D('8.6')),
    ('100000', '90000', D('10000.00'), D('10.0')),
    ('300000', '320000', D('-20000.00'), D('-6.7')),
    ('3', '2', D('1.00'), D('33.3')),
    ('0', '5000', D('-5000.00'), None),   # no contract sum: no %
]

# (budget, committed, actual) -> standard forecast final cost
FORECASTS = [
    ('180000', '187600', '62000', D('187600.00')),   # over-committed: the order wins
    ('98000', '94200', '0', D('98000.00')),          # under-committed: the budget holds
    ('38000', '0', '41250.50', D('41250.50')),       # spent beyond budget with no order
    ('0', '0', '0', D('0.00')),
]


class MoneyTableTests(SimpleTestCase):
    def test_pennies(self):
        for value, expected in PENNIES:
            with self.subTest(value=value):
                self.assertEqual(pennies(value), expected)

    def test_floats_and_rubbish_are_refused(self):
        with self.assertRaises(TypeError):
            pennies(0.1)
        with self.assertRaises(TypeError):
            pennies(None)
        for bad in ('abc', 'NaN', 'Infinity'):
            with self.assertRaises(ValueError):
                pennies(bad)

    def test_budget_from_tender(self):
        for value, pct, expected in BUDGETS:
            with self.subTest(value=value, pct=pct):
                self.assertEqual(budget_from_tender(value, pct), expected)

    def test_margin(self):
        for fa, cost, amount, pct in MARGINS:
            with self.subTest(fa=fa, cost=cost):
                self.assertEqual(margin(fa, cost), (amount, pct))

    def test_default_forecast(self):
        for b, c, a, expected in FORECASTS:
            with self.subTest(b=b, c=c, a=a):
                self.assertEqual(default_forecast(b, c, a), expected)
