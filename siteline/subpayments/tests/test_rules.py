"""
The fixed tables the product owner signs off for paying subcontractors. Advisory: check your
contract. Each table is plain data: change a rule and a row here must change with it.
"""
from datetime import date
from decimal import Decimal as D

from django.test import SimpleTestCase

from holidays.calendar import ENGLAND_AND_WALES as EW
from holidays.calendar import SCOTLAND as SC

from .. import rules

# Subcontract terms used in the cycle tables: due 7 days after the valuation date, our payment
# notice 5 days after the due date, final date 21 days after the due date, pay less notice no
# later than 5 days before the final date (the company defaults).
TERMS = (7, 5, 21, 5)

# (region, valuation date, due date, payment notice by, pay less by, final date)
CYCLE_TABLE = [
    # England and Wales, Christmas 2026: Fri 25 Dec, Sat 26 Dec (Boxing Day, a bank holiday by statute
    # even on a Saturday), Mon 28 Dec (substitute), Fri 1 Jan 2027 left out of the count
    (EW, date(2026, 12, 18), date(2026, 12, 27), date(2027, 1, 3), date(2027, 1, 14), date(2027, 1, 19)),
    (EW, date(2026, 12, 21), date(2026, 12, 31), date(2027, 1, 6), date(2027, 1, 17), date(2027, 1, 22)),
    (EW, date(2026, 12, 25), date(2027, 1, 4), date(2027, 1, 9), date(2027, 1, 20), date(2027, 1, 25)),
    # Scotland: 25 Dec, Mon 28 Dec, 1 Jan, Sat 2 Jan (a bank holiday by statute in Scotland) and
    # Mon 4 Jan (substitute) left out; Sat 26 Dec counts there (Boxing Day is by proclamation only)
    (SC, date(2026, 12, 18), date(2026, 12, 26), date(2027, 1, 3), date(2027, 1, 15), date(2027, 1, 20)),
    (SC, date(2026, 12, 21), date(2026, 12, 30), date(2027, 1, 7), date(2027, 1, 18), date(2027, 1, 23)),
    (SC, date(2026, 12, 25), date(2027, 1, 5), date(2027, 1, 10), date(2027, 1, 21), date(2027, 1, 26)),
    # An ordinary month for comparison
    (EW, date(2026, 10, 25), date(2026, 11, 1), date(2026, 11, 6), date(2026, 11, 17), date(2026, 11, 22)),
]

# (payment notice by, our notice given on, applied, our notice sum, expected notified sum, basis)
NOTIFIED_TABLE = [
    (date(2026, 11, 6), date(2026, 11, 6), '50000', '42000', '42000.00', 'payment_notice'),  # on the last day
    (date(2026, 11, 6), date(2026, 11, 7), '50000', '42000', '50000.00', 'default'),  # a day late: application stands
    (date(2026, 11, 6), None, '50000', None, '50000.00', 'default'),  # no notice at all: application stands
]

# (pay less by, given on, refused?)
PAY_LESS_TABLE = [
    (date(2026, 11, 17), date(2026, 11, 16), False),
    (date(2026, 11, 17), date(2026, 11, 17), False),  # on the deadline: in time
    (date(2026, 11, 17), date(2026, 11, 18), True),   # one day late: refused, no effect
]

# (gross ex VAT, materials, CIS status, materials used, labour, deduction, VAT, net payable)
CIS_TABLE = [
    ('10000.00', '2500.00', rules.CIS_GROSS, '2500.00', '7500.00', '0.00', '2000.00', '12000.00'),
    ('10000.00', '2500.00', rules.CIS_REGISTERED, '2500.00', '7500.00', '1500.00', '2000.00', '10500.00'),
    ('10000.00', '2500.00', rules.CIS_UNVERIFIED, '2500.00', '7500.00', '2250.00', '2000.00', '9750.00'),
    ('10000.00', '0.00', rules.CIS_REGISTERED, '0.00', '10000.00', '2000.00', '2000.00', '10000.00'),
    # materials stated above the payment: capped at it, so no deduction
    ('1000.00', '1500.00', rules.CIS_UNVERIFIED, '1000.00', '0.00', '0.00', '200.00', '1200.00'),
]

# Rounding: HMRC rounds the deduction DOWN to the penny. (labour, rate, deduction, what half up would give)
CIS_ROUNDING_TABLE = [
    ('1234.58', '20', '246.91', '246.92'),   # 246.916
    ('100.05', '30', '30.01', '30.02'),      # 30.015
    ('333.33', '20', '66.66', '66.67'),      # 66.666
    ('0.04', '20', '0.00', '0.01'),          # 0.008
    ('1000.00', '0', '0.00', '0.00'),
]

# (gross, reverse charge, VAT rate, VAT, net payable at 20% CIS, no materials)
REVERSE_CHARGE_TABLE = [
    ('5000.00', False, '20', '1000.00', '5000.00'),
    ('5000.00', True, '20', '0.00', '4000.00'),
    ('5000.00', False, '0', '0.00', '4000.00'),   # not VAT registered: stated rate 0
]

# Retention 5%, half released at our practical completion (PC 1 Mar 2027), 12 months' defects.
# (valuation date, stage, gross 100,000 -> retention held)
RETENTION_TABLE = [
    (date(2027, 2, 28), rules.STAGE_BEFORE_PC, '5000.00'),      # the day before PC: all held
    (date(2027, 3, 1), rules.STAGE_AFTER_PC, '2500.00'),        # PC: half released
    (date(2028, 2, 29), rules.STAGE_AFTER_PC, '2500.00'),       # before the end of defects
    (date(2028, 3, 1), rules.STAGE_AFTER_DEFECTS, '0.00'),      # end of defects: the rest released
]

# (payment date, tax month start, end, number, tax year)
TAX_MONTH_TABLE = [
    (date(2026, 11, 5), date(2026, 10, 6), date(2026, 11, 5), 7, '2026-27'),
    (date(2026, 11, 6), date(2026, 11, 6), date(2026, 12, 5), 8, '2026-27'),
    (date(2027, 1, 5), date(2026, 12, 6), date(2027, 1, 5), 9, '2026-27'),
    (date(2027, 4, 5), date(2027, 3, 6), date(2027, 4, 5), 12, '2026-27'),
    (date(2027, 4, 6), date(2027, 4, 6), date(2027, 5, 5), 1, '2027-28'),
]


class CycleTests(SimpleTestCase):
    def test_cycle_table(self):
        for region, valuation, due, notice, pay_less, final in CYCLE_TABLE:
            with self.subTest(region=region, valuation=valuation):
                c = rules.payment_cycle(valuation, rules.sub_terms(*TERMS, region))
                self.assertEqual((c.due_date, c.payment_notice_by, c.pay_less_by, c.final_date),
                                 (due, notice, pay_less, final))

    def test_saturday_boxing_day_is_left_out_in_england_only(self):
        ew = rules.payment_cycle(date(2026, 12, 18), rules.sub_terms(*TERMS, EW)).due_date
        sc = rules.payment_cycle(date(2026, 12, 18), rules.sub_terms(*TERMS, SC)).due_date
        self.assertEqual((ew - sc).days, 1)

    def test_monthly_and_listed_valuation_dates(self):
        self.assertEqual(rules.valuation_dates(rules.CYCLE_MONTHLY, date(2026, 11, 3), 25),
                         (date(2026, 10, 25), date(2026, 11, 25)))
        self.assertEqual(rules.valuation_dates(rules.CYCLE_MONTHLY, date(2026, 11, 25), 25),
                         (date(2026, 11, 25), date(2026, 12, 25)))
        self.assertEqual(rules.valuation_dates(rules.CYCLE_MONTHLY, date(2027, 2, 10), 31),
                         (date(2027, 1, 31), date(2027, 2, 28)))
        listed = [date(2026, 11, 20), date(2026, 12, 18)]
        self.assertEqual(rules.valuation_dates(rules.CYCLE_DATES, date(2026, 12, 1), listed=listed),
                         (date(2026, 11, 20), date(2026, 12, 18)))
        self.assertEqual(rules.valuation_dates(rules.CYCLE_DATES, date(2026, 11, 1), listed=listed),
                         (None, date(2026, 11, 20)))

    def test_parse_dates(self):
        self.assertEqual(rules.parse_dates('25/11/2026\n2026-12-18, 25/11/2026'),
                         [date(2026, 11, 25), date(2026, 12, 18)])
        with self.assertRaisesMessage(ValueError, '31/02/2026'):
            rules.parse_dates('31/02/2026')


class NoticeTests(SimpleTestCase):
    def test_notified_sum_when_our_notice_is_late(self):
        for by, given, applied, ours, expected, basis in NOTIFIED_TABLE:
            with self.subTest(given=given):
                self.assertEqual(rules.notified_sum(D(applied), D(ours) if ours else None, given, by),
                                 (D(expected), basis))

    def test_late_warning_shows_only_after_the_deadline_with_no_notice(self):
        by = date(2026, 11, 6)
        self.assertEqual(rules.late_notice_warning(date(2026, 11, 6), by, None), '')
        self.assertIn('stands as the notified sum', rules.late_notice_warning(date(2026, 11, 7), by, None))
        self.assertEqual(rules.late_notice_warning(date(2026, 11, 9), by, date(2026, 11, 5)), '')
        self.assertIn('stands', rules.late_notice_warning(date(2026, 11, 9), by, date(2026, 11, 8)))

    def test_pay_less_one_day_late(self):
        for by, given, refused in PAY_LESS_TABLE:
            with self.subTest(given=given):
                self.assertEqual(bool(rules.pay_less_refusal(given, by)), refused)
        self.assertIn('1 day ago', rules.pay_less_refusal(date(2026, 11, 18), date(2026, 11, 17)))

    def test_pay_less_in_time_overrides_the_notified_sum(self):
        self.assertEqual(rules.notified_sum(D('50000'), D('42000'), date(2026, 11, 5), date(2026, 11, 6),
                                            D('40000'), date(2026, 11, 17), date(2026, 11, 17)),
                         (D('40000.00'), 'pay_less'))
        self.assertEqual(rules.notified_sum(D('50000'), D('42000'), date(2026, 11, 5), date(2026, 11, 6),
                                            D('40000'), date(2026, 11, 18), date(2026, 11, 17)),
                         (D('42000.00'), 'payment_notice'))


class CISTests(SimpleTestCase):
    def test_cis_table(self):
        for gross, mats, status, used, labour, cis, vat, net in CIS_TABLE:
            with self.subTest(status=status, gross=gross, mats=mats):
                a = rules.advice(D(gross), D(mats), status)
                self.assertEqual((a.materials, a.labour, a.cis, a.vat, a.net),
                                 (D(used), D(labour), D(cis), D(vat), D(net)))

    def test_rates(self):
        self.assertEqual([rules.cis_rate(s) for s in (rules.CIS_GROSS, rules.CIS_REGISTERED, rules.CIS_UNVERIFIED,
                                                       'unknown')], [D('0'), D('20'), D('30'), D('30')])

    def test_rounding_down_to_the_penny(self):
        from payments.rules import money
        for labour, rate, down, half_up in CIS_ROUNDING_TABLE:
            with self.subTest(labour=labour, rate=rate):
                self.assertEqual(rules.cis_deduction(D(labour), D('0'), D(rate)), D(down))
                self.assertEqual(money(D(labour) * D(rate) / 100), D(half_up))

    def test_reverse_charge(self):
        for gross, rc, rate, vat, net in REVERSE_CHARGE_TABLE:
            with self.subTest(rc=rc, rate=rate):
                a = rules.advice(D(gross), D('0'), rules.CIS_REGISTERED, D(rate), rc)
                self.assertEqual((a.vat, a.net, a.reverse_charge), (D(vat), D(net), rc))

    def test_floats_are_refused(self):
        with self.assertRaises(TypeError):
            rules.advice(1000.0)


class ValuationTests(SimpleTestCase):
    def test_retention_before_and_after_practical_completion(self):
        pc = date(2027, 3, 1)
        for valuation, stage, held in RETENTION_TABLE:
            with self.subTest(valuation=valuation):
                self.assertEqual(rules.retention_stage(valuation, pc, 12), stage)
                v = rules.sub_valuation(D('95000'), D('5000'), D('5'), D('50'), stage)
                self.assertEqual(v.retention, D(held))

    def test_valuation_chain(self):
        v = rules.sub_valuation(D('120000'), D('3500'), D('5'), D('50'), rules.STAGE_BEFORE_PC, D('80000'))
        self.assertEqual((v.gross, v.retention, v.net, v.amount_due),
                         (D('123500.00'), D('6175.00'), D('117325.00'), D('37325.00')))

    def test_discount(self):
        self.assertEqual(rules.discount_on(D('37325'), D('2.5')), D('933.13'))  # 933.125 half up
        self.assertEqual(rules.payable_after_discount(D('37325'), D('2.5')), D('36391.87'))
        self.assertEqual(rules.discount_on(D('1000'), D('0')), D('0.00'))

    def test_retention_ledger(self):
        rows = [('SA-001', date(2027, 1, 25), rules.STAGE_BEFORE_PC, D('3000')),
                ('SA-002', date(2027, 2, 25), rules.STAGE_BEFORE_PC, D('5000')),
                ('SA-003', date(2027, 3, 1), rules.STAGE_AFTER_PC, D('2500')),
                ('SA-004', date(2028, 3, 1), rules.STAGE_AFTER_DEFECTS, D('0'))]
        ledger, held, pc, defects = rules.retention_ledger(rows)
        self.assertEqual((held, pc, defects), (D('0.00'), D('2500.00'), D('2500.00')))
        self.assertEqual([r.movement for r in ledger], [D('3000.00'), D('2000.00'), D('-2500.00'), D('-2500.00')])
        self.assertEqual([r.released_at for r in ledger], ['', '', rules.STAGE_AFTER_PC, rules.STAGE_AFTER_DEFECTS])


class TaxMonthTests(SimpleTestCase):
    def test_tax_month_boundaries(self):
        for day, start, end, number, year in TAX_MONTH_TABLE:
            with self.subTest(day=day):
                m = rules.tax_month(day)
                self.assertEqual((m.start, m.end, m.number, m.tax_year), (start, end, number, year))

    def test_keys_and_return_due(self):
        m = rules.tax_month_from_key('2026-10')
        self.assertEqual((m.start, m.end, m.return_due), (date(2026, 10, 6), date(2026, 11, 5), date(2026, 11, 19)))
        self.assertIsNone(rules.tax_month_from_key('nonsense'))
