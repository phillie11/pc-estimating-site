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
    """An entry can be changed on its own day and the next working day; then it locks."""
    # (entry date, written on, today, unlocked until, Saturday counts?, locked?, why)
    TABLE = [
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 28), None, False, False, 'Wednesday entry on Wednesday: open'),
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 29), None, False, False, 'Wednesday entry on Thursday: open'),
        (D(2026, 10, 28), D(2026, 10, 28), D(2026, 10, 30), None, False, True, 'Wednesday entry on Friday: locked'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 10, 31), None, False, False, 'Friday entry on Saturday: open'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 11, 2), None, False, False, 'Friday entry on Monday: open'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 11, 3), None, False, True, 'Friday entry on Tuesday: locked'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 10, 31), None, True, False,
         'Saturday counts: Friday entry on Saturday: open'),
        (D(2026, 10, 30), D(2026, 10, 30), D(2026, 11, 1), None, True, True,
         'Saturday counts: Friday entry on Sunday: locked'),
        (D(2026, 5, 22), D(2026, 5, 22), D(2026, 5, 26), None, False, False,
         'Friday before a bank holiday Monday, on Tuesday: open'),
        (D(2026, 5, 22), D(2026, 5, 22), D(2026, 5, 27), None, False, True,
         'Friday before a bank holiday Monday, on Wednesday: locked'),
        (D(2026, 10, 26), D(2026, 10, 27), D(2026, 10, 28), None, False, True,
         'Monday entry written Tuesday (in time), on Wednesday: locked'),
        (D(2026, 10, 26), D(2026, 10, 30), D(2026, 10, 30), None, False, False,
         'Monday entry written late on Friday: open on Friday'),
        (D(2026, 10, 26), D(2026, 10, 30), D(2026, 11, 2), None, False, False,
         'Monday entry written late on Friday: open on Monday'),
        (D(2026, 10, 26), D(2026, 10, 30), D(2026, 11, 3), None, False, True,
         'Monday entry written late on Friday: locked on Tuesday'),
        (D(2026, 10, 20), D(2026, 10, 20), D(2026, 11, 2), D(2026, 11, 2), False, False,
         'unlocked on Friday: open to the end of Monday'),
        (D(2026, 10, 20), D(2026, 10, 20), D(2026, 11, 3), D(2026, 11, 2), False, True,
         'unlocked on Friday: locked again on Tuesday'),
    ]

    def test_table(self):
        for entry, written, today, unlocked, saturday, locked, why in self.TABLE:
            with self.subTest(why):
                self.assertEqual(rules.is_locked(entry, written, today, unlocked, saturday), locked)

    # (unlocked on, Saturday counts?, open until the end of, why)
    UNLOCK = [
        (D(2026, 10, 28), False, D(2026, 10, 29), 'Wednesday: to the end of Thursday'),
        (D(2026, 10, 30), False, D(2026, 11, 2), 'Friday: to the end of Monday'),
        (D(2026, 10, 30), True, D(2026, 10, 31), 'Friday, Saturday counts: to the end of Saturday'),
        (D(2026, 5, 22), False, D(2026, 5, 26), 'Friday before a bank holiday Monday: to the end of Tuesday'),
    ]

    def test_unlock_lasts_to_the_end_of_the_next_working_day(self):
        for day, saturday, until, why in self.UNLOCK:
            with self.subTest(why):
                self.assertEqual(rules.unlock_until(day, saturday), until)

    # (entry date, written on, written late?, why)
    LATE = [
        (D(2026, 10, 26), D(2026, 10, 27), False, 'Monday entry written Tuesday'),
        (D(2026, 10, 26), D(2026, 10, 28), True, 'Monday entry written Wednesday'),
        (D(2026, 10, 30), D(2026, 11, 2), False, 'Friday entry written Monday'),
        (D(2026, 10, 30), D(2026, 11, 3), True, 'Friday entry written Tuesday'),
    ]

    def test_written_late(self):
        for entry, written, late, why in self.LATE:
            with self.subTest(why):
                self.assertEqual(rules.is_written_late(entry, written), late)


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
