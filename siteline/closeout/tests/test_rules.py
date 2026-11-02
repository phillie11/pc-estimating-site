"""
The close-out date rules' fixed test table (closeout/rules.py). These rows are the rule table in
the release report, for the product owner to sign off. Advisory: check your contract.
"""
from datetime import date

from django.test import SimpleTestCase

from .. import rules

# (practical completion, defects period months, defects period ends)
DEFECTS_END = [
    (date(2026, 1, 15), 6, date(2026, 7, 15)),     # the same day six months on
    (date(2026, 1, 15), 12, date(2027, 1, 15)),    # NEC-style 52 weeks entered as 12 months
    (date(2026, 8, 31), 6, date(2027, 2, 28)),     # no 31 Feb: the last day of February
    (date(2027, 8, 31), 6, date(2028, 2, 29)),     # ... 29th in a leap year
    (date(2026, 3, 31), 3, date(2026, 6, 30)),     # JCT Minor Works: 3 months; no 31 June
    (date(2026, 11, 30), 3, date(2027, 2, 28)),    # across the year end
    (date(2026, 12, 25), 12, date(2027, 12, 25)),  # calendar months: bank holidays don't move it
    (date(2026, 1, 15), None, None),               # no defects period set: no end date
    (date(2026, 1, 15), 0, None),
    (None, 6, None),                               # not complete: no end date
]

# (defects period ends, made good on, today, reminder on Needs me?)
REMINDER = [
    (date(2026, 7, 15), None, date(2026, 6, 2), False),   # 43 days before: not yet
    (date(2026, 7, 15), None, date(2026, 6, 3), True),    # 42 days (six weeks) before: on
    (date(2026, 7, 15), None, date(2026, 7, 15), True),   # the day it ends
    (date(2026, 7, 15), None, date(2026, 9, 1), True),    # after it ends: on, overdue, until made good
    (date(2026, 7, 15), date(2026, 7, 20), date(2026, 9, 1), False),  # made good: off
    (None, None, date(2026, 9, 1), False),                # no end date: nothing to remind
]

# (today, completion date, O&M information asked for by)  England and Wales bank holidays
OM_DUE = [
    (date(2026, 11, 2), date(2027, 1, 29), date(2027, 1, 15)),   # two weeks before completion
    (date(2026, 11, 2), date(2026, 11, 20), date(2026, 11, 16)),  # too soon: 10 working days from today
    (date(2026, 12, 18), None, date(2027, 1, 6)),                 # no completion date; skips Christmas and New Year
    (date(2026, 11, 2), date(2026, 11, 30), date(2026, 11, 16)),  # 16 Nov is exactly 10 working days: allowed
]

# (whole-works PC date, raised on, defect?)
DEFECT_PHASE = [
    (date(2026, 6, 1), date(2026, 5, 31), False),
    (date(2026, 6, 1), date(2026, 6, 1), True),     # on the PC date itself
    (date(2026, 6, 1), date(2026, 9, 1), True),
    (None, date(2026, 9, 1), False),                # no PC yet: still a snag
]


class RuleTableTests(SimpleTestCase):
    def test_defects_period_end(self):
        for pc, months, end in DEFECTS_END:
            with self.subTest(pc=pc, months=months):
                self.assertEqual(rules.defects_end(pc, months), end)

    def test_end_of_defects_reminder(self):
        for end, made_good, today, due in REMINDER:
            with self.subTest(end=end, today=today):
                self.assertEqual(rules.reminder_due(end, made_good, today), due)
        self.assertEqual(rules.REMINDER_DAYS, 42)

    def test_om_due_date(self):
        for today, completion, due in OM_DUE:
            with self.subTest(today=today, completion=completion):
                self.assertEqual(rules.om_due_default(today, completion), due)

    def test_defect_phase(self):
        for pc, raised, defect in DEFECT_PHASE:
            with self.subTest(pc=pc, raised=raised):
                self.assertEqual(rules.is_defect_phase(pc, raised), defect)

    def test_add_months_goes_backwards_too(self):
        self.assertEqual(rules.add_months(date(2026, 2, 10), -6), date(2025, 8, 10))
        self.assertEqual(rules.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
