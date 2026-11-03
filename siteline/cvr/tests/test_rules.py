"""
The fixed test tables for the CVR, cashflow and daywork rules (cvr/rules.py). These are what the
product owner signs off: each row is an example in plain numbers.
"""
from datetime import date
from decimal import Decimal as D

from django.test import SimpleTestCase

from cvr import rules


class MonthTests(SimpleTestCase):
    def test_month_helpers(self):
        table = [
            # day,              start,             end,               +1 month,          label
            (date(2026, 2, 14), date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 1), 'February 2026'),
            (date(2028, 2, 29), date(2028, 2, 1), date(2028, 2, 29), date(2028, 3, 1), 'February 2028'),
            (date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31), date(2027, 1, 1), 'December 2026'),
        ]
        for day, start, end, nxt, label in table:
            with self.subTest(day=day):
                self.assertEqual(rules.month_start(day), start)
                self.assertEqual(rules.month_end(day), end)
                self.assertEqual(rules.add_months(day, 1), nxt)
                self.assertEqual(rules.month_label(day), label)
        self.assertEqual(rules.add_months(date(2026, 1, 15), -1), date(2025, 12, 1))
        self.assertEqual(rules.months_between(date(2026, 11, 20), date(2027, 1, 3)),
                         [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)])
        self.assertEqual(rules.months_between(date(2027, 1, 3), date(2026, 11, 20)), [])

    def test_a_month_can_be_submitted_once_it_has_ended(self):
        table = [
            (date(2026, 10, 1), date(2026, 10, 31), False),  # its last day: not yet
            (date(2026, 10, 1), date(2026, 11, 1), True),
            (date(2026, 11, 1), date(2026, 11, 2), False),
        ]
        for month, today, ended in table:
            with self.subTest(month=month, today=today):
                self.assertEqual(rules.has_ended(month, today), ended)

    def test_due_day(self):
        table = [
            (date(2026, 10, 1), 10, date(2026, 11, 10)),
            (date(2026, 1, 1), 31, date(2026, 2, 28)),   # past the month's end: its last day
            (date(2026, 12, 1), 5, date(2027, 1, 5)),
        ]
        for month, day, due in table:
            with self.subTest(month=month, day=day):
                self.assertEqual(rules.due_on(month, day), due)


class CVRTests(SimpleTestCase):
    def test_the_cvr_table(self):
        # value to date, cost to date, final account, final cost, value adj, cost adj, FA adj, FC adj
        #   -> value, cost, margin, margin %, final account, final cost, forecast margin, forecast margin %
        table = [
            (('66000', '19000', '256000', '225000', '0', '0', '0', '0'),
             ('66000.00', '19000.00', '47000.00', D('71.2'), '256000.00', '225000.00', '31000.00', D('12.1'))),
            # an accrual for an invoice not yet received, and the client's QS valuing 4,250 under
            (('418750', '351200.40', '1240000', '1091200', '-4250', '12500', '0', '0'),
             ('414500.00', '363700.40', '50799.60', D('12.3'), '1240000.00', '1091200.00', '148800.00', D('12.0'))),
            # a risk on the final cost and an unagreed change on the final account
            (('100000', '90000', '200000', '180000', '0', '0', '7500', '15000'),
             ('100000.00', '90000.00', '10000.00', D('10.0'), '207500.00', '195000.00', '12500.00', D('6.0'))),
            # losing money: negative margin, % still worked out
            (('50000', '56000', '100000', '104000', '0', '0', '0', '0'),
             ('50000.00', '56000.00', '-6000.00', D('-12.0'), '100000.00', '104000.00', '-4000.00', D('-4.0'))),
            # nothing valued yet: no margin %
            (('0', '1200', '0', '0', '0', '0', '0', '0'),
             ('0.00', '1200.00', '-1200.00', None, '0.00', '0.00', '0.00', None)),
            # half pennies round up
            (('100.005', '0.004', '0', '0', '0', '0', '0', '0'),
             ('100.01', '0.00', '100.01', D('100.0'), '0.00', '0.00', '0.00', None)),
        ]
        for args, expected in table:
            with self.subTest(args=args):
                r = rules.cvr(*args)
                got = (r['value'], r['cost'], r['margin'], r['margin_pct'], r['final_account'], r['final_cost'],
                       r['forecast_margin'], r['forecast_margin_pct'])
                want = tuple(D(x) if isinstance(x, str) else x for x in expected)
                self.assertEqual(got, want)

    def test_floats_are_refused(self):
        with self.assertRaises(TypeError):
            rules.cvr(1.5, 0, 0, 0)

    def test_movement(self):
        table = [('31000', '28500', D('2500.00')), ('28500', '31000', D('-2500.00')), ('100', None, None),
                 ('0', '0', D('0.00'))]
        for this, prev, want in table:
            with self.subTest(this=this, prev=prev):
                self.assertEqual(rules.movement(this, prev), want)

    def test_latest_valuation(self):
        apps = [(date(2026, 9, 28), D('182500'), 1), (date(2026, 10, 28), D('418750'), 2),
                (date(2026, 10, 28), D('420000'), 3)]  # two on one date: the higher number
        table = [(date(2026, 8, 31), D('0.00')), (date(2026, 9, 30), D('182500.00')),
                 (date(2026, 10, 27), D('182500.00')), (date(2026, 10, 31), D('420000.00'))]
        for end, want in table:
            with self.subTest(end=end):
                self.assertEqual(rules.latest_valuation(apps, end), want)

    def test_percent_of(self):
        table = [('31000', '256000', D('12.1')), ('1', '3', D('33.3')), ('2', '3', D('66.7')), ('5', '0', None),
                 ('0.05', '1000', D('0.0')), ('0.5', '1000', D('0.1'))]
        for amount, of, want in table:
            with self.subTest(amount=amount, of=of):
                self.assertEqual(rules.percent_of(amount, of), want)


class SpreadTests(SimpleTestCase):
    def test_spread_adds_up_exactly(self):
        table = [
            ('100.00', [1, 1, 1], [D('33.33'), D('33.33'), D('33.34')]),
            ('100.00', [1, 2, 1], [D('25.00'), D('50.00'), D('25.00')]),
            ('10.00', [0, 0, 0], [D('3.33'), D('3.33'), D('3.34')]),        # no weights: equal shares
            ('10.00', [2, 1, 0], [D('6.67'), D('3.33'), D('0.00')]),        # the last month with work takes the pennies
            ('0', [1, 1], [D('0.00'), D('0.00')]),
            ('1000.01', [D('0.5'), D('0.25'), D('0.25')], [D('500.01'), D('250.00'), D('250.00')]),
        ]
        for amount, weights, want in table:
            with self.subTest(amount=amount, weights=weights):
                got = rules.spread(amount, weights)
                self.assertEqual(got, want)
                self.assertEqual(sum(got), D(amount).quantize(D('0.01')))
        self.assertEqual(rules.spread('10', []), [])

    def test_programme_weights(self):
        months = [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]
        today = date(2026, 11, 16)
        table = [
            # one 30-day activity 15 Nov to 14 Dec, not started. Today is the 16th, so its 29 days left carry all
            # 30 days of work: 15 of those days in Nov, 14 in Dec
            ([(date(2026, 11, 15), date(2026, 12, 14), 0, False)], ['15.51724137931034482758620690',
                                                                    '14.48275862068965517241379310', '0']),
            # half done: half the work left over the same days
            ([(date(2026, 11, 15), date(2026, 12, 14), 50, False)], ['7.758620689655172413793103450',
                                                                     '7.241379310344827586206896550', '0']),
            # finished, a milestone, and one already past: no work
            ([(date(2026, 11, 1), date(2026, 11, 30), 100, False), (date(2027, 1, 5), date(2027, 1, 5), 0, True),
              (date(2026, 10, 1), date(2026, 11, 10), 0, False)], ['0', '0', '0']),
        ]
        for acts, want in table:
            with self.subTest(acts=acts):
                got = rules.programme_weights(months, acts, today)
                self.assertEqual([round(g, 6) for g in got], [round(D(w), 6) for w in want])

    def test_even_weights(self):
        months = [date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]
        self.assertEqual(rules.even_weights(months, date(2026, 11, 2), date(2026, 12, 20)), [0, 1, 1, 0])
        self.assertEqual(rules.even_weights(months, date(2026, 11, 2), None), [0, 1, 0, 0])

    def test_less_retention_and_ex_vat_steps(self):
        table = [('10000', '5', D('9500.00')), ('333.33', '3', D('323.33')), ('100', None, D('100.00')),
                 ('100.01', '5', D('95.01'))]  # 95.0095 rounds up
        for amount, pct, want in table:
            with self.subTest(amount=amount, pct=pct):
                self.assertEqual(rules.less_retention(amount, pct), want)
        # cumulative ex VAT at each month's end -> each month's receipts; they add back to the last total
        self.assertEqual(rules.ex_vat_steps([D('62700.00'), D('62700.00'), D('96833.33')]),
                         [D('62700.00'), D('0.00'), D('34133.33')])


class DayworkTests(SimpleTestCase):
    def test_line_amounts(self):
        table = [('8', '42.50', D('340.00')), ('7.5', '28.33', D('212.48')),  # 212.475 rounds up
                 ('3', '12.99', D('38.97')), ('0.25', '15.01', D('3.75'))]   # 3.7525
        for qty, rate, want in table:
            with self.subTest(qty=qty, rate=rate):
                self.assertEqual(rules.line_amount(D(qty), D(rate)), want)

    def test_a_priced_sheet(self):
        lines = [(rules.LABOUR, D('8'), D('42.50')), (rules.LABOUR, D('7.5'), D('28.33')),
                 (rules.MATERIALS, D('3'), D('12.99')), (rules.PLANT, D('4'), D('15.00'))]
        pct = {rules.LABOUR: D('80'), rules.MATERIALS: D('15'), rules.PLANT: D('10')}
        p = rules.daywork_price(lines, pct)
        # labour 340.00 + 212.48 = 552.48, +80% = 441.98 (441.984)
        self.assertEqual(p['kinds'][rules.LABOUR], (D('552.48'), D('80'), D('441.98')))
        # materials 38.97, +15% = 5.85 (5.8455)
        self.assertEqual(p['kinds'][rules.MATERIALS], (D('38.97'), D('15'), D('5.85')))
        self.assertEqual(p['kinds'][rules.PLANT], (D('60.00'), D('10'), D('6.00')))
        self.assertEqual((p['prime'], p['additions'], p['total']), (D('651.45'), D('453.83'), D('1105.28')))

    def test_no_percentages_is_prime_cost(self):
        p = rules.daywork_price([(rules.LABOUR, D('1'), D('30'))], {})
        self.assertEqual((p['prime'], p['additions'], p['total']), (D('30.00'), D('0.00'), D('30.00')))
