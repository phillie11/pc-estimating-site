"""
The diary's date rules as fixed tables (diary/rules.py). Each row is one line the product owner
can read and sign off. 2026: Fri 30 Oct, Sat 31 Oct, Sun 1 Nov, Mon 2 Nov. Bank holidays:
Mon 25 May 2026 (spring), Fri 25 Dec / Mon 28 Dec 2026 (Christmas, Boxing Day substitute).
"""
from datetime import date, datetime

from django.test import SimpleTestCase

from diary import rules

D = date


class LockWindowTable(SimpleTestCase):
    # (entry date, written on, today, unlocked until, locked?, why)
    TABLE = [
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 28), None, False, 'on its own day'),
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 29), None, False, 'the day after'),
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 30), None, True, 'two days later: locked'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 11, 2), None, True, 'Friday entry on Monday: locked (calendar days)'),
        (D(2026, 10, 26), D(2026, 10, 30), D(2026, 10, 30), None, False, 'written late: open on the day it was written'),
        (D(2026, 10, 26), D(2026, 10, 30), D(2026, 10, 31), None, True, 'written late: locked the day after writing'),
        (D(2026, 10, 20), D(2026, 10, 20), D(2026, 10, 30), D(2026, 10, 31), False, 'unlocked until tomorrow'),
        (D(2026, 10, 20), D(2026, 10, 20), D(2026, 11, 1), D(2026, 10, 31), True, 'unlock has run out'),
    ]

    def test_table(self):
        for entry, written, today, unlocked, locked, why in self.TABLE:
            with self.subTest(why):
                self.assertEqual(rules.is_locked(entry, written, today, unlocked), locked)

    def test_unlock_lasts_to_end_of_next_day(self):
        self.assertEqual(rules.unlock_until(D(2026, 10, 30)), D(2026, 10, 31))


class WorkingDayTable(SimpleTestCase):
    # (day, Saturday counts?, working day?, why)
    TABLE = [
        (D(2026, 10, 30), False, True, 'Friday'),
        (D(2026, 10, 31), False, False, 'Saturday, not counted'),
        (D(2026, 10, 31), True, True, 'Saturday, counted by the company'),
        (D(2026, 11, 1), True, False, 'Sunday never counts'),
        (D(2026, 5, 25), False, False, 'spring bank holiday'),
        (D(2026, 12, 25), False, False, 'Christmas Day'),
        (D(2026, 12, 28), False, False, 'Boxing Day (substitute day)'),
    ]

    def test_table(self):
        for day, saturday, working, why in self.TABLE:
            with self.subTest(why):
                self.assertEqual(rules.is_working_day(day, saturday), working)


class DueTable(SimpleTestCase):
    # (working day, Saturday counts?, entry due at, why)
    TABLE = [
        (D(2026, 10, 28), False, datetime(2026, 10, 29, 10), 'Wednesday: Thursday 10:00'),
        (D(2026, 10, 30), False, datetime(2026, 11, 2, 10), 'Friday: Monday 10:00'),
        (D(2026, 10, 30), True, datetime(2026, 10, 31, 10), 'Friday, Saturday counts: Saturday 10:00'),
        (D(2026, 10, 31), True, datetime(2026, 11, 2, 10), 'Saturday: Monday 10:00'),
        (D(2026, 5, 22), False, datetime(2026, 5, 26, 10), 'Friday before a bank holiday Monday: Tuesday 10:00'),
    ]

    def test_table(self):
        for day, saturday, due, why in self.TABLE:
            with self.subTest(why):
                self.assertEqual(rules.due_at(day, saturday), due)


class ExpectedDaysTable(SimpleTestCase):
    # (now, Saturday counts?, the most recent days already due (first 2), why)
    TABLE = [
        (datetime(2026, 10, 29, 9, 59), False, [D(2026, 10, 27), D(2026, 10, 26)], 'Thu 09:59: Wednesday not due yet'),
        (datetime(2026, 10, 29, 10, 0), False, [D(2026, 10, 28), D(2026, 10, 27)], 'Thu 10:00: Wednesday due'),
        (datetime(2026, 11, 1, 12, 0), False, [D(2026, 10, 29), D(2026, 10, 28)], 'Sunday: Friday not due till Monday'),
        (datetime(2026, 11, 2, 9, 0), False, [D(2026, 10, 29), D(2026, 10, 28)], 'Mon 09:00: Friday not due yet'),
        (datetime(2026, 11, 2, 10, 0), False, [D(2026, 10, 30), D(2026, 10, 29)], 'Mon 10:00: Friday due'),
        (datetime(2026, 11, 1, 12, 0), True, [D(2026, 10, 30), D(2026, 10, 29)], 'Sunday, Saturday counts: Friday due'),
        (datetime(2026, 11, 2, 10, 0), True, [D(2026, 10, 31), D(2026, 10, 30)], 'Mon 10:00, Saturday counts: Saturday due'),
    ]

    def test_table(self):
        for now, saturday, first_two, why in self.TABLE:
            with self.subTest(why):
                self.assertEqual(rules.expected_days(now, saturday)[:2], first_two)

    def test_lookback_and_start(self):
        days = rules.expected_days(datetime(2026, 11, 2, 10, 0))
        self.assertEqual(len(days), rules.LOOKBACK_DAYS)
        self.assertEqual(rules.expected_days(datetime(2026, 11, 2, 10, 0), start=D(2026, 10, 30)), [D(2026, 10, 30)])
