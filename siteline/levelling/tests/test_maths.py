"""
The levelling maths: a fixed test table for sign-off (also in the stream report).
Every figure here is worked by hand.
"""
from decimal import Decimal as D

from django.test import SimpleTestCase

from levelling import maths


class LineValueTable(SimpleTestCase):
    # (qty, rate, sum, included) -> line value
    TABLE = [
        ((D('8'), D('520'), None, False), D('4160.00')),          # rate x qty
        ((D('2.5'), D('10.333'), None, False), D('25.83')),        # 25.8325 -> 25.83
        ((D('3'), D('0.005'), None, False), D('0.02')),             # 0.015 -> 0.02 (half up, not banker's)
        ((None, D('14500'), None, False), D('14500.00')),          # lump sum line: the rate is the sum
        ((D('8'), D('520'), D('4100'), False), D('4100.00')),      # their sum wins over rate x qty
        ((D('8'), None, None, True), D('0.00')),                    # included elsewhere
        ((D('8'), None, None, False), None),                        # missing
        ((None, None, None, False), None),                          # missing
    ]

    def test_table(self):
        for args, expected in self.TABLE:
            with self.subTest(args=args):
                self.assertEqual(maths.line_value(*args), expected)

    def test_float_refused(self):
        with self.assertRaises(TypeError):
            maths.pennies(0.1)


class PlugTable(SimpleTestCase):
    # (others' values, basis) -> plug
    TABLE = [
        (([D('1596'), D('1722')], maths.AVERAGE), D('1659.00')),
        (([D('1596'), D('1722')], maths.HIGHEST), D('1722.00')),
        (([D('10'), D('10'), D('11')], maths.AVERAGE), D('10.33')),   # 10.333...
        (([D('10'), D('10'), D('12.5')], maths.AVERAGE), D('10.83')),  # 10.8333...
        (([D('0.01'), D('0.02')], maths.AVERAGE), D('0.02')),          # 0.015 half up
        (([D('100'), None], maths.AVERAGE), D('100.00')),              # blanks ignored
        (([], maths.AVERAGE), None),                                   # nobody else priced it
        (([None], maths.HIGHEST), None),
    ]

    def test_table(self):
        for (others, basis), expected in self.TABLE:
            with self.subTest(others=others, basis=basis):
                self.assertEqual(maths.plug_value(others, basis), expected)

    def test_unknown_basis(self):
        with self.assertRaises(ValueError):
            maths.plug_value([D('1')], 'LOWEST')


class LevelTable(SimpleTestCase):
    def row(self, s, key):
        r = s.get(key)
        return (r.levelled, r.rank, r.tied, r.var_lowest, r.var_lowest_pct, r.var_average, r.var_average_pct)

    def test_adjustments_and_variances(self):
        # Quoted + adjustments; adjustments can be negative
        s = maths.level([
            ('A', D('38330'), [D('650'), D('1659')]),      # exclusion priced in, plug
            ('B', D('43406'), []),
            ('C', D('43142'), [D('-120.50')]),             # arithmetic correction in our favour
        ])
        self.assertEqual(s.lowest, D('40639.00'))
        self.assertEqual(s.average, D('42355.50'))       # (40639 + 43406 + 43021.50) / 3
        self.assertEqual(self.row(s, 'A'), (D('40639.00'), 1, False, D('0.00'), D('0.0'), D('-1716.50'), D('-4.1')))
        self.assertEqual(self.row(s, 'B'), (D('43406.00'), 3, False, D('2767.00'), D('6.8'), D('1050.50'), D('2.5')))
        self.assertEqual(self.row(s, 'C'), (D('43021.50'), 2, False, D('2382.50'), D('5.9'), D('666.00'), D('1.6')))
        self.assertEqual(s.get('C').adjustments, D('-120.50'))

    def test_ties_share_a_rank_and_skip_the_next(self):
        s = maths.level([('A', D('100'), []), ('B', D('100'), []), ('C', D('120'), []), ('D', D('90'), [D('15')])])
        self.assertEqual([(s.get(k).rank, s.get(k).tied) for k in 'ABCD'],
                         [(1, True), (1, True), (4, False), (3, False)])
        self.assertEqual(s.average, D('106.25'))  # (100 + 100 + 120 + 105) / 4
        self.assertTrue(s.get('A').is_lowest and s.get('B').is_lowest)

    def test_three_way_tie_after_adjustment(self):
        s = maths.level([('A', D('100.004'), []), ('B', D('99'), [D('1')]), ('C', D('101'), [D('-1')])])
        self.assertEqual([s.get(k).rank for k in 'ABC'], [1, 1, 1])  # 100.004 rounds to 100.00

    def test_no_quote_is_not_ranked(self):
        s = maths.level([('A', None, [D('500')]), ('B', D('2000'), [])])
        a = s.get('A')
        self.assertEqual((a.levelled, a.rank, a.var_lowest, a.adjustments), (None, None, None, D('500.00')))
        self.assertEqual((s.lowest, s.average, s.get('B').rank), (D('2000.00'), D('2000.00'), 1))

    def test_nothing_quoted(self):
        s = maths.level([('A', None, [])])
        self.assertIsNone(s.lowest)
        self.assertIsNone(s.get('A').rank)

    def test_zero_lowest_has_no_percentage(self):
        s = maths.level([('A', D('0'), []), ('B', D('50'), [])])
        self.assertIsNone(s.get('B').var_lowest_pct)
        self.assertEqual(s.get('B').var_lowest, D('50.00'))

    def test_negative_lowest_has_no_percentage(self):
        s = maths.level([('A', D('100'), [D('-150')]), ('B', D('50'), [])])
        self.assertEqual(s.lowest, D('-50.00'))
        self.assertIsNone(s.get('B').var_lowest_pct)
        self.assertIsNone(s.get('A').var_average_pct)   # average 0.00

    def test_plug_counts_only_while_the_line_is_missing(self):
        # Line missing: plug of 1,659 counts. Line later priced at 1,680: the plug is dropped, not added on top.
        self.assertTrue(maths.plug_counts(None))
        self.assertFalse(maths.plug_counts(D('1680.00')))
        self.assertFalse(maths.plug_counts(D('0.00')))   # "included" is priced too
        lines = [D('14400'), D('1680'), D('1500')]
        plugs = [D('1659')] if maths.plug_counts(D('1680')) else []
        s = maths.level([('Lund', sum(lines), plugs)])
        self.assertEqual(s.get('Lund').levelled, D('17580.00'))
