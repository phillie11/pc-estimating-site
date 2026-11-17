from datetime import date
from types import SimpleNamespace as P

from django.test import SimpleTestCase

from projects.strip import strip, window

TODAY = date(2026, 11, 17)


def job(start, end):
    return P(start_date=start, completion_date=end)


class WindowTests(SimpleTestCase):
    def test_window_is_the_month_before_today_and_the_eight_after(self):
        self.assertEqual(window(TODAY), (date(2026, 10, 1), date(2027, 7, 1)))

    def test_window_crosses_a_year_end_in_either_direction(self):
        self.assertEqual(window(date(2026, 1, 20))[0], date(2025, 12, 1))
        self.assertEqual(window(date(2026, 12, 3))[1], date(2027, 8, 1))


class AxisTests(SimpleTestCase):
    def test_nine_months_that_fill_the_width_exactly(self):
        axis = strip([], TODAY)['axis']
        self.assertEqual([m['label'] for m in axis], ['Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'])
        self.assertAlmostEqual(sum(m['width'] for m in axis), 100, places=1)
        self.assertEqual(axis[0]['left'], 0)

    def test_year_is_shown_on_the_first_month_and_each_january_only(self):
        years = {m['label']: m['year'] for m in strip([], TODAY)['axis'] if m['year']}
        self.assertEqual(years, {'Oct': '’26', 'Jan': '’27'})

    def test_today_sits_inside_the_window_in_proportion(self):
        self.assertAlmostEqual(strip([], TODAY)['today'], 47 / 273 * 100, places=2)  # 47 days into 273


class RowTests(SimpleTestCase):
    def rows(self, *jobs):
        return strip(list(jobs), TODAY)['rows']

    def test_job_inside_the_window_is_placed_and_not_flagged(self):
        r, = self.rows(job(date(2026, 11, 1), date(2026, 12, 31)))
        self.assertTrue(r['visible'])
        self.assertFalse(r['early'] or r['late'])
        self.assertTrue(r['started'])
        self.assertAlmostEqual(r['left'], 31 / 273 * 100, places=2)

    def test_job_that_began_before_and_ends_after_is_clipped_and_flagged_both_ways(self):
        r, = self.rows(job(date(2026, 3, 1), date(2027, 12, 1)))
        self.assertTrue(r['early'] and r['late'])
        self.assertEqual(r['left'], 0)
        self.assertAlmostEqual(r['left'] + r['width'], 100, places=1)

    def test_progress_is_how_far_through_its_own_dates_today_is(self):
        r, = self.rows(job(date(2026, 11, 7), date(2026, 11, 27)))
        self.assertEqual(r['done'], 50.0)

    def test_progress_never_leaves_zero_to_a_hundred(self):
        ahead, finished = self.rows(job(date(2026, 12, 1), date(2027, 1, 1)), job(date(2026, 10, 1), date(2026, 10, 20)))
        self.assertEqual(ahead['done'], 0)
        self.assertFalse(ahead['started'])
        self.assertEqual(finished['done'], 100)

    def test_a_job_wholly_outside_the_window_is_dated_but_not_drawn(self):
        for r in self.rows(job(date(2025, 1, 1), date(2025, 6, 1)), job(date(2027, 8, 1), date(2027, 9, 1))):
            self.assertTrue(r['dated'])
            self.assertFalse(r['visible'])

    def test_missing_or_backwards_dates_are_not_drawn(self):
        for r in self.rows(job(None, None), job(date(2026, 11, 1), None), job(date(2026, 12, 1), date(2026, 11, 1))):
            self.assertFalse(r['dated'])
            self.assertFalse(r['visible'])

    def test_a_one_day_job_still_gets_a_visible_bar(self):
        r, = self.rows(job(date(2026, 11, 20), date(2026, 11, 20)))
        self.assertGreaterEqual(r['width'], 1.2)
