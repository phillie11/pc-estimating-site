"""
The money and date rules for applications for payment, held to fixed tables for the product
owner to sign off. Pure functions: SimpleTestCase, no database. Advisory: check your contract.

Each table row is in plain words in the comment beside it. Days are counted as HGCRA s116 counts
them: from the day after, leaving out Christmas Day, Good Friday and the region's bank holidays.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from contracts import presets
from payments import rules

D = date.fromisoformat
EW, SC = 'england-and-wales', 'scotland'
JCT_IC = rules.Terms(7, 5, 14, 5)            # due +7, payment notice +5 after due, final +14 after due, pay less 5 before
NEC4_Y2 = rules.Terms(7, 0, 14, 7)           # due +7, certificate by the due date, final +14, pay less 7 before
SCHEME = rules.Terms(7, 5, 17, 7, scheme=True)  # Part II paras 4, 9, 8 and 10


def with_region(terms, region):
    return rules.Terms(terms.due_date_offset_days, terms.payment_notice_days, terms.final_date_offset_days,
                       terms.pay_less_notice_days, region, terms.scheme)


class CycleTableTests(SimpleTestCase):
    # (case, terms, region, valuation, claim made, due, payment notice by, pay less by, final date)
    TABLE = [
        # JCT IC monthly cycle across Christmas. E&W 2026: 25 Dec (Fri) and 28 Dec (Boxing Day substitute) and
        # 1 Jan 2027 are left out. Due Sat 26 Dec; payment notice Sat 2 Jan; final Mon 11 Jan; pay less Wed 6 Jan.
        ('JCT IC Christmas E&W', JCT_IC, EW, '2026-12-18', None, '2026-12-26', '2027-01-02', '2027-01-06', '2027-01-11'),
        # Scotland also leaves out Mon 4 Jan 2027 (2 January's substitute), so the final date is a day later.
        ('JCT IC Christmas Scotland', JCT_IC, SC, '2026-12-18', None, '2026-12-26', '2027-01-02', '2027-01-07',
         '2027-01-12'),
        # JCT IC across Good Friday 26 Mar 2027 and Easter Monday 29 Mar (E&W only). Due Tue 30 Mar.
        ('JCT IC Easter E&W', JCT_IC, EW, '2027-03-21', None, '2027-03-30', '2027-04-04', '2027-04-08', '2027-04-13'),
        # Scotland has Good Friday but not Easter Monday, so everything is a day earlier.
        ('JCT IC Easter Scotland', JCT_IC, SC, '2027-03-21', None, '2027-03-29', '2027-04-03', '2027-04-07',
         '2027-04-12'),
        # A plain month with nothing to skip.
        ('JCT IC plain', JCT_IC, EW, '2026-11-28', None, '2026-12-05', '2026-12-10', '2026-12-14', '2026-12-19'),
        # NEC4 Option Y(UK)2: payment due 7 days after the assessment date; the Project Manager's certificate
        # (the payment notice) by then; final date 14 days after; pay less 7 days before the final date.
        ('NEC4 Y(UK)2 plain', NEC4_Y2, EW, '2026-10-25', None, '2026-11-01', '2026-11-01', '2026-11-08', '2026-11-15'),
        ('NEC4 Y(UK)2 Christmas', NEC4_Y2, EW, '2026-12-18', None, '2026-12-26', '2026-12-26', '2027-01-04',
         '2027-01-11'),
        # The Scheme: due 7 days after the period, payment notice 5 after that, final date 17 after the due date,
        # pay less 7 before the final date.
        ('Scheme default', SCHEME, EW, '2026-01-31', None, '2026-02-07', '2026-02-12', '2026-02-17', '2026-02-24'),
        # The Scheme, claim made late (10 Feb): the due date moves to the day the claim is made (para 4(b)).
        ('Scheme late claim', SCHEME, EW, '2026-01-31', '2026-02-10', '2026-02-10', '2026-02-15', '2026-02-20',
         '2026-02-27'),
        # The Scheme, claim made in time: no change.
        ('Scheme claim in time', SCHEME, EW, '2026-01-31', '2026-02-02', '2026-02-07', '2026-02-12', '2026-02-17',
         '2026-02-24'),
    ]

    def test_table(self):
        for case, terms, region, valuation, claim, due, notice, pay_less, final in self.TABLE:
            with self.subTest(case):
                c = rules.payment_cycle(D(valuation), with_region(terms, region), claim_date=D(claim) if claim else None)
                self.assertEqual((c.due_date, c.payment_notice_by, c.pay_less_by, c.final_date),
                                 (D(due), D(notice), D(pay_less), D(final)))

    def test_reuses_the_contract_timetable(self):
        # The due, final and pay less dates are exactly contracts.presets.payment_timetable's
        for _, terms, region, valuation, claim, *_ in self.TABLE:
            if claim:
                continue
            t = presets.payment_timetable(D(valuation), terms.due_date_offset_days, terms.final_date_offset_days,
                                          terms.pay_less_notice_days, region=region)
            c = rules.payment_cycle(D(valuation), with_region(terms, region))
            self.assertEqual((c.due_date, c.final_date, c.pay_less_by), (t.due_date, t.final_date, t.pay_less_by))


class TermsTests(SimpleTestCase):
    def contract(self, **kw):
        kw.setdefault('form', 'JCT_IC')
        base = dict(due_date_offset_days=None, payment_notice_days=None, final_date_offset_days=None,
                    pay_less_notice_days=None, bank_holiday_region=EW)
        base.update(kw)
        c = SimpleNamespace(**base)
        c.preset = presets.preset_for(c.form)
        return c

    def test_no_contract_is_the_scheme(self):
        t = rules.terms_for(None)
        self.assertEqual((t.due_date_offset_days, t.payment_notice_days, t.final_date_offset_days,
                          t.pay_less_notice_days, t.scheme), (7, 5, 17, 7, True))

    def test_blank_terms_come_from_the_form(self):
        # (form, due, payment notice, final, pay less, Scheme rules)
        for form, expected in [('JCT_IC', (7, 5, 14, 5, False)), ('JCT_MW', (0, 5, 14, 5, False)),
                               ('JCT_SBC', (7, 5, 14, 5, False)), ('NEC4_ECC', (7, 0, 14, 7, False)),
                               ('BESPOKE', (7, 5, 17, 7, True))]:
            with self.subTest(form):
                t = rules.terms_for(self.contract(form=form))
                self.assertEqual((t.due_date_offset_days, t.payment_notice_days, t.final_date_offset_days,
                                  t.pay_less_notice_days, t.scheme), expected)

    def test_contract_figures_win(self):
        # Leeds' amendment: pay less notice 7 days before the final date
        t = rules.terms_for(self.contract(pay_less_notice_days=7, bank_holiday_region=SC))
        self.assertEqual((t.pay_less_notice_days, t.region), (7, SC))

    def test_presets_have_payment_notice_days(self):
        self.assertEqual({f: presets.preset_for(f).payment_notice_days for f in
                          ('JCT_MW', 'JCT_MWD', 'JCT_IC', 'JCT_ICD', 'JCT_SBC', 'JCT_DB', 'NEC4_ECC', 'BESPOKE')},
                         {'JCT_MW': 5, 'JCT_MWD': 5, 'JCT_IC': 5, 'JCT_ICD': 5, 'JCT_SBC': 5, 'JCT_DB': 5,
                          'NEC4_ECC': 0, 'BESPOKE': 5})


class RetentionTableTests(SimpleTestCase):
    # Contract: 5% retention, half released at practical completion (PC 15 Mar 2027), defects period 6 months
    # (ends 15 Sep 2027). (valuation date, stage)
    STAGES = [
        ('2027-02-28', rules.STAGE_BEFORE_PC),
        ('2027-03-14', rules.STAGE_BEFORE_PC),    # the day before PC
        ('2027-03-15', rules.STAGE_AFTER_PC),     # PC certified on the valuation date
        ('2027-09-14', rules.STAGE_AFTER_PC),     # last day of the defects period
        ('2027-09-15', rules.STAGE_AFTER_DEFECTS),
    ]
    # (gross, %, released at PC %, stage, retention held): rounded half up to 1p
    HELD = [
        ('100000.00', '5', '50', rules.STAGE_BEFORE_PC, '5000.00'),
        ('104000.00', '5', '50', rules.STAGE_AFTER_PC, '2600.00'),     # half released at PC
        ('104000.00', '5', '50', rules.STAGE_AFTER_DEFECTS, '0.00'),   # the rest at the end of defects
        ('104000.00', '5', '100', rules.STAGE_AFTER_PC, '0.00'),       # all released at completion
        ('104000.00', '5', '0', rules.STAGE_AFTER_PC, '5200.00'),      # none released until the end of defects
        ('12345.67', '5', '50', rules.STAGE_BEFORE_PC, '617.28'),      # 617.2835
        ('12345.67', '3', '50', rules.STAGE_AFTER_PC, '185.19'),       # 185.18505: half up
        ('10.10', '5', '50', rules.STAGE_BEFORE_PC, '0.51'),           # 0.505: half up, not to even
        ('100000.00', '0', '50', rules.STAGE_BEFORE_PC, '0.00'),       # no retention
        ('100000.00', None, '50', rules.STAGE_BEFORE_PC, '0.00'),
    ]

    def test_stage_table(self):
        for valuation, stage in self.STAGES:
            with self.subTest(valuation):
                self.assertEqual(rules.retention_stage(D(valuation), D('2027-03-15'), 6), stage)

    def test_no_completion_or_no_defects_period(self):
        self.assertEqual(rules.retention_stage(D('2030-01-01'), None, 6), rules.STAGE_BEFORE_PC)
        self.assertEqual(rules.retention_stage(D('2030-01-01'), D('2027-03-15'), None), rules.STAGE_AFTER_PC)

    def test_defects_end_month_ends(self):
        self.assertEqual(rules.defects_end(D('2027-08-31'), 6), D('2028-02-29'))   # leap year
        self.assertEqual(rules.defects_end(D('2026-08-31'), 6), D('2027-02-28'))
        self.assertEqual(rules.defects_end(D('2026-11-30'), 12), D('2027-11-30'))

    def test_held_table(self):
        for gross, pct, released, stage, held in self.HELD:
            with self.subTest(gross=gross, pct=pct, released=released, stage=stage):
                self.assertEqual(rules.retention_held(Decimal(gross), Decimal(pct) if pct else None, Decimal(released),
                                                      stage), Decimal(held))

    def test_release_at_practical_completion_through_the_valuation(self):
        # Month before PC: gross 100,000, 5% held = 5,000; net 95,000, nothing before: 95,000 due
        before = rules.valuation(Decimal('100000'), retention_percent=Decimal('5'), stage=rules.STAGE_BEFORE_PC)
        self.assertEqual((before.retention, before.net, before.amount_due), (Decimal('5000.00'), Decimal('95000.00'),
                                                                            Decimal('95000.00')))
        # PC certified: gross 104,000, half of 5% held = 2,600; net 101,400; less 95,000 = 6,400 due,
        # which includes 2,400 of retention released
        after = rules.valuation(Decimal('104000'), retention_percent=Decimal('5'), stage=rules.STAGE_AFTER_PC,
                                previous=before.net)
        self.assertEqual((after.retention, after.net, after.amount_due),
                         (Decimal('2600.00'), Decimal('101400.00'), Decimal('6400.00')))
        self.assertEqual(before.retention - after.retention, Decimal('2400.00'))
        # End of the defects period: the last 2,600 released
        end = rules.valuation(Decimal('104000'), retention_percent=Decimal('5'), stage=rules.STAGE_AFTER_DEFECTS,
                              previous=after.net)
        self.assertEqual((end.retention, end.amount_due), (Decimal('0.00'), Decimal('2600.00')))


class ValuationTableTests(SimpleTestCase):
    # (work done, variations, on site, off site, retention %, stage, previous, reverse charge,
    #  gross, retention, net, due, VAT, total)
    TABLE = [
        # Leeds AP-002: 418,750 + 5,900 + 18,500 = 443,150; 5% = 22,157.50; less AP-001's 190,380
        ('418750', '5900', '18500', '0', '5', rules.STAGE_BEFORE_PC, '190380', False,
         '443150.00', '22157.50', '420992.50', '230612.50', '46122.50', '276735.00'),
        # The same under the domestic reverse charge: no VAT
        ('418750', '5900', '18500', '0', '5', rules.STAGE_BEFORE_PC, '190380', True,
         '443150.00', '22157.50', '420992.50', '230612.50', '0.00', '230612.50'),
        # Salford AP-001 (NEC4, X16 retention 3%), the first application
        ('214600', '0', '0', '0', '3', rules.STAGE_BEFORE_PC, '0', False,
         '214600.00', '6438.00', '208162.00', '208162.00', '41632.40', '249794.40'),
        # Materials off site count in the gross
        ('1000', '0', '0', '250.50', '5', rules.STAGE_BEFORE_PC, '0', False,
         '1250.50', '62.53', '1187.97', '1187.97', '237.59', '1425.56'),
        # Valued down below what was paid before: a negative amount due (and VAT credit)
        ('50000', '0', '0', '0', '5', rules.STAGE_BEFORE_PC, '50000', False,
         '50000.00', '2500.00', '47500.00', '-2500.00', '-500.00', '-3000.00'),
    ]

    def test_table(self):
        for (work, var, mos, off, pct, stage, prev, rc, gross, ret, net, due, vat, total) in self.TABLE:
            with self.subTest(work=work, rc=rc):
                v = rules.valuation(Decimal(work), Decimal(var), Decimal(mos), Decimal(off), Decimal(pct),
                                    Decimal('50'), stage, Decimal(prev), reverse_charge=rc)
                self.assertEqual((v.gross, v.retention, v.net, v.amount_due, v.vat, v.total),
                                 tuple(Decimal(x) for x in (gross, ret, net, due, vat, total)))

    def test_vat_rounding(self):
        # (amount, VAT at 20%): half up to 1p
        for amount, vat in [('1.23', '0.25'), ('1.22', '0.24'), ('0.03', '0.01'), ('100.00', '20.00')]:
            self.assertEqual(rules.vat_on(Decimal(amount)), Decimal(vat))
        self.assertEqual(rules.vat_on(Decimal('100.00'), reverse_charge=True), Decimal('0.00'))

    def test_lines_total(self):
        self.assertEqual(rules.lines_total([Decimal('38000'), Decimal('196400.505'), Decimal('0.10')]),
                         Decimal('234400.61'))

    def test_floats_refused(self):
        with self.assertRaises(TypeError):
            rules.money(0.1)

    def test_reverse_charge_note_is_advisory(self):
        self.assertIn('reverse charge', rules.REVERSE_CHARGE_NOTE)
        self.assertIn('Advisory', rules.REVERSE_CHARGE_NOTE)
        self.assertEqual(rules.ADVISORY, 'Advisory: check your contract.')


class NoticeTableTests(SimpleTestCase):
    # Application 10,000 (ex VAT); payment notice deadline Sat 2 Jan 2027, pay less deadline Wed 6 Jan 2027
    PN_BY, PL_BY = D('2027-01-02'), D('2027-01-06')
    # (payment notice: amount, given on; pay less: amount, given on; notified sum, basis)
    TABLE = [
        (None, None, None, None, '10000.00', rules.BASIS_DEFAULT),                      # no notices: application stands
        ('9000', '2027-01-02', None, None, '9000.00', rules.BASIS_PAYMENT_NOTICE),       # notice on its last day
        ('9000', '2027-01-03', None, None, '10000.00', rules.BASIS_DEFAULT),             # notice one day late: s110B
        ('9000', '2027-01-02', '8000', '2027-01-06', '8000.00', rules.BASIS_PAY_LESS),   # pay less on its last day
        ('9000', '2027-01-03', '8000', '2027-01-05', '8000.00', rules.BASIS_PAY_LESS),   # late notice, pay less in time
        ('9000', '2027-01-02', '8000', '2027-01-07', '9000.00', rules.BASIS_PAYMENT_NOTICE),  # pay less one day late
        (None, None, '8000', '2027-01-07', '10000.00', rules.BASIS_DEFAULT),             # pay less one day late
    ]

    def test_table(self):
        for pn, pn_on, pl, pl_on, expected, basis in self.TABLE:
            with self.subTest(pn=pn, pn_on=pn_on, pl=pl, pl_on=pl_on):
                got = rules.notified_sum(Decimal('10000'), Decimal(pn) if pn else None, D(pn_on) if pn_on else None,
                                         self.PN_BY, Decimal(pl) if pl else None, D(pl_on) if pl_on else None,
                                         self.PL_BY)
                self.assertEqual(got, (Decimal(expected), basis))

    def test_pay_less_one_day_late_is_refused(self):
        self.assertEqual(rules.pay_less_refusal(D('2027-01-06'), self.PL_BY), '')
        refusal = rules.pay_less_refusal(D('2027-01-07'), self.PL_BY)
        self.assertIn('Too late', refusal)
        self.assertIn('Wed 6 Jan 2027, 1 day ago', refusal)
        self.assertIn('Advisory: check your contract.', refusal)
        self.assertIn('3 days ago', rules.pay_less_refusal(D('2027-01-09'), self.PL_BY))

    def test_stands_as_notified(self):
        # (today, payment notice given on, stands as the notified sum)
        for today, given, stands in [('2027-01-02', None, False), ('2027-01-03', None, True),
                                     ('2027-01-03', '2027-01-02', False), ('2027-01-04', '2027-01-03', True)]:
            with self.subTest(today=today, given=given):
                self.assertEqual(rules.stands_as_notified(D(today), self.PN_BY, D(given) if given else None), stands)

    def test_overdue(self):
        self.assertFalse(rules.is_payment_overdue(D('2027-01-11'), D('2027-01-11'), Decimal('1')))
        self.assertTrue(rules.is_payment_overdue(D('2027-01-12'), D('2027-01-11'), Decimal('0.01')))
        self.assertFalse(rules.is_payment_overdue(D('2027-01-12'), D('2027-01-11'), Decimal('0')))


class ClockTests(SimpleTestCase):
    CYCLE = rules.PaymentCycle(D('2026-12-18'), D('2026-12-26'), D('2027-01-02'), D('2027-01-06'), D('2027-01-11'))

    def clocks(self, today, **kw):
        kw.setdefault('submitted_on', D('2026-12-19'))
        return {c.key: c for c in rules.payment_clocks(self.CYCLE, D(today), **kw)}

    def test_order_and_labels(self):
        keys = [c.key for c in rules.payment_clocks(self.CYCLE, D('2026-12-20'), submitted_on=D('2026-12-19'))]
        self.assertEqual(keys, ['valuation', 'due', 'payment_notice', 'pay_less', 'final'])

    def test_waiting_for_the_payment_notice(self):
        c = self.clocks('2026-12-30')['payment_notice']
        self.assertEqual((c.tone, c.days_left), ('warn', 3))

    def test_no_payment_notice_stands_as_notified(self):
        c = self.clocks('2027-01-03')['payment_notice']
        self.assertEqual(c.tone, 'bad')
        self.assertIn('s110B', c.message)
        self.assertIn('Advisory', c.message)

    def test_late_payment_notice(self):
        c = self.clocks('2027-01-04', payment_notice_on=D('2027-01-03'))['payment_notice']
        self.assertEqual(c.tone, 'bad')
        self.assertIn('after the deadline', c.message)

    def test_final_date_overdue_and_paid(self):
        self.assertEqual(self.clocks('2027-01-12')['final'].tone, 'bad')
        self.assertIn('overdue', self.clocks('2027-01-12')['final'].message)
        paid = self.clocks('2027-01-12', paid_in_full_on=D('2027-01-08'))['final']
        self.assertEqual((paid.tone, paid.done_on), ('good', D('2027-01-08')))
        late = self.clocks('2027-01-20', paid_in_full_on=D('2027-01-13'))['final']
        self.assertIn('2 days late', late.message)

    def test_pay_less_deadline_passed(self):
        c = self.clocks('2027-01-07')['pay_less']
        self.assertIn('payable in full', c.message)


class SubmissionWindowTests(SimpleTestCase):
    # (today, valuation day, valuation dates an application could be due for)
    TABLE = [
        ('2026-10-31', 28, ['2026-10-28']),                 # next (28 Nov) is more than 7 days away
        ('2026-11-22', 28, ['2026-10-28', '2026-11-28']),   # next is 6 days away
        ('2026-11-28', 28, ['2026-11-28']),                 # today is the valuation date
        ('2026-02-25', 31, ['2026-01-31', '2026-02-28']),   # day 31 snaps to the end of February
        ('2027-01-03', 28, ['2026-12-28']),                 # across the year end
    ]

    def test_table(self):
        for today, day, expected in self.TABLE:
            with self.subTest(today=today):
                self.assertEqual(rules.submission_window(day, D(today)), [D(x) for x in expected])
        self.assertEqual(rules.submission_window(None, D('2026-10-31')), [])
