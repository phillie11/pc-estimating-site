"""
The fixed test table for procurement dates (procurement/dates.py). Each row is one case the
product owner can check by hand against a 2026 calendar. Advisory: check your contract.

2026 bank holidays used here (England and Wales unless said): Thu 1 Jan; Fri 2 Jan (Scotland
only); Good Friday 3 Apr; Easter Monday 6 Apr (not Scotland); Spring 25 May; Christmas Fri
25 Dec; Boxing Day substitute Mon 28 Dec; Fri 1 Jan 2027.
"""
from datetime import date

from django.test import SimpleTestCase

from holidays.calendar import ENGLAND_AND_WALES as EW
from holidays.calendar import SCOTLAND as SC

from .. import dates

D = date

# (why, required on site, lead weeks, approval weeks, region, order by, submittals due)
ORDER_BY_TABLE = [
    ('Order by lands on the Early May bank holiday (Mon 4 May): back to Fri 1 May; submittals 2 weeks before',
     D(2026, 6, 15), 6, 2, EW, D(2026, 5, 1), D(2026, 4, 17)),
    ('Plain case: Mon minus 6 weeks is a Mon; approval only moves submittals due',
     D(2026, 6, 1), 6, 2, EW, D(2026, 4, 20), D(2026, 4, 2)),
    # submittals due: 20 Apr - 2 weeks = Easter Monday 6 Apr -> Sun -> Sat -> Good Friday -> Thu 2 Apr
    ('Required on a Saturday: lands on a Saturday, moves back to Friday',
     D(2026, 6, 13), 4, 0, EW, D(2026, 5, 15), D(2026, 5, 15)),
    ('Lands on the Spring bank holiday: back to the Friday',
     D(2026, 6, 1), 1, 0, EW, D(2026, 5, 22), D(2026, 5, 22)),
    ('No lead time or approval period: the required date itself',
     D(2026, 7, 15), 0, 0, EW, D(2026, 7, 15), D(2026, 7, 15)),
    ('Blank lead time and approval count as 0 weeks',
     D(2026, 7, 15), None, None, EW, D(2026, 7, 15), D(2026, 7, 15)),
    ('No required-on-site date: no dates at all',
     None, 6, 2, EW, None, None),
    ('England: Fri 2 Jan is a working day',
     D(2026, 1, 16), 2, 0, EW, D(2026, 1, 2), D(2026, 1, 2)),
    ('Scotland: 2 Jan and 1 Jan are holidays, back to Wed 31 Dec',
     D(2026, 1, 16), 2, 0, SC, D(2025, 12, 31), D(2025, 12, 31)),
    ('Over Christmas: Mon 28 Dec (substitute) -> weekend -> Christmas Day -> Thu 24 Dec',
     D(2027, 1, 11), 2, 0, EW, D(2026, 12, 24), D(2026, 12, 24)),
    ('A year of lead time',
     D(2027, 3, 1), 52, 0, EW, D(2026, 3, 2), D(2026, 3, 2)),
    ('Approval comes before the order: it moves submittals due, not order by',
     D(2026, 9, 14), 4, 3, EW, D(2026, 8, 17), D(2026, 7, 27)),
    ('No lead time, approval only: order by is the required date',
     D(2026, 4, 20), 0, 2, EW, D(2026, 4, 20), D(2026, 4, 2)),
]

# (why, start, working days, region, answer)
WORKING_DAYS_TABLE = [
    ('Ten working days from a Monday: the Monday two weeks on', D(2026, 11, 2), 10, EW, D(2026, 11, 16)),
    ('Over Christmas and New Year', D(2026, 12, 18), 10, EW, D(2027, 1, 6)),
    ('From a Saturday, one working day is Monday', D(2026, 10, 31), 1, EW, D(2026, 11, 2)),
    ('Zero days is the start itself', D(2026, 10, 31), 0, EW, D(2026, 10, 31)),
    ('Easter, England: skips Good Friday and Easter Monday', D(2026, 4, 2), 1, EW, D(2026, 4, 7)),
    ('Easter, Scotland: Easter Monday is a working day', D(2026, 4, 2), 1, SC, D(2026, 4, 6)),
]

TODAY = D(2026, 10, 31)
# (why, order by, ordered or delivered, RAG, project manager asked)
RAG_TABLE = [
    ('Order-by date yesterday, not ordered', D(2026, 10, 30), False, dates.RED, True),
    ('Order-by date today', D(2026, 10, 31), False, dates.AMBER, True),
    ('7 days away', D(2026, 11, 7), False, dates.AMBER, True),
    ('8 days away', D(2026, 11, 8), False, dates.AMBER, False),
    ('14 days away: still amber', D(2026, 11, 14), False, dates.AMBER, False),
    ('15 days away: green', D(2026, 11, 15), False, dates.GREEN, False),
    ('Past but ordered', D(2026, 9, 1), True, dates.DONE, False),
    ('No order-by date', None, False, dates.NONE, False),
    ('No date but ordered', None, True, dates.DONE, False),
]


class OrderByTests(SimpleTestCase):
    def test_order_by_and_submittals_due_table(self):
        for why, ros, lead, approval, region, ob, due in ORDER_BY_TABLE:
            with self.subTest(why):
                self.assertEqual(dates.order_by(ros, lead, approval, region), ob)
                self.assertEqual(dates.submittals_due(ros, lead, approval, region), due)

    def test_answers_are_always_working_days_and_never_after_the_plain_subtraction(self):
        from datetime import timedelta
        for why, ros, lead, approval, region, ob, due in ORDER_BY_TABLE:
            if ros is None:
                continue
            with self.subTest(why):
                self.assertTrue(dates.is_working_day(ob, region))
                self.assertLessEqual(ob, ros - timedelta(weeks=lead or 0))
                self.assertLessEqual(due, ob - timedelta(weeks=approval or 0))
                self.assertLessEqual(due, ob)


class WorkingDayTests(SimpleTestCase):
    def test_working_days_table(self):
        for why, start, n, region, answer in WORKING_DAYS_TABLE:
            with self.subTest(why):
                self.assertEqual(dates.add_working_days(start, n, region), answer)


class RagTests(SimpleTestCase):
    def test_rag_table(self):
        for why, ob, done, rag, asked in RAG_TABLE:
            with self.subTest(why):
                self.assertEqual(dates.rag(ob, done, TODAY), rag)
                self.assertEqual(dates.needs_ordering_soon(ob, done, TODAY), asked)

    def test_every_rag_has_words_and_a_tone(self):
        for code, _ in dates.RAG_CHOICES:
            self.assertTrue(dates.RAG_LABELS[code])
            self.assertIn(dates.RAG_TONES[code], ('good', 'warn', 'bad', 'muted'))
