"""
The fixed test tables for every safety rule (safety/rules.py), for the product owner to sign off.
All advisory: check your contract and your safety adviser.
"""
import datetime
from datetime import date

from django.test import SimpleTestCase

from safety import rules
from safety.rules import (RIDDOR_DANGEROUS, RIDDOR_DEATH, RIDDOR_DISEASE, RIDDOR_OVER_3, RIDDOR_OVER_7, RIDDOR_PUBLIC,
                          RIDDOR_SPECIFIED)

ACCIDENT = date(2026, 11, 2)  # a Monday


class RiddorDeadlineTable(SimpleTestCase):
    # category, accident date, last day to report (None: no fixed deadline / not reportable), reportable?
    TABLE = [
        (RIDDOR_DEATH, ACCIDENT, date(2026, 11, 12), True),        # 10 days
        (RIDDOR_SPECIFIED, ACCIDENT, date(2026, 11, 12), True),    # 10 days
        (RIDDOR_PUBLIC, ACCIDENT, date(2026, 11, 12), True),       # 10 days
        (RIDDOR_DANGEROUS, ACCIDENT, date(2026, 11, 12), True),    # 10 days
        (RIDDOR_OVER_7, ACCIDENT, date(2026, 11, 17), True),       # 15 days
        (RIDDOR_DISEASE, ACCIDENT, None, True),                    # as soon as diagnosed
        (RIDDOR_OVER_3, ACCIDENT, None, False),                    # record only
        ('', ACCIDENT, None, False),                               # not reportable
        # Weekends and bank holidays count: Christmas makes no difference
        (RIDDOR_OVER_7, date(2026, 12, 20), date(2027, 1, 4), True),
        (RIDDOR_SPECIFIED, date(2026, 12, 24), date(2027, 1, 3), True),
        # Month and leap-year ends
        (RIDDOR_SPECIFIED, date(2028, 2, 25), date(2028, 3, 6), True),
        (RIDDOR_OVER_7, date(2027, 2, 20), date(2027, 3, 7), True),
    ]

    def test_table(self):
        for category, accident, deadline, reportable in self.TABLE:
            with self.subTest(category=category, accident=accident):
                self.assertEqual(rules.riddor_deadline(category, accident), deadline)
                self.assertEqual(rules.is_reportable(category), reportable)

    def test_no_date_no_deadline(self):
        self.assertIsNone(rules.riddor_deadline(RIDDOR_DEATH, None))

    def test_guidance_words(self):
        self.assertIn('without delay', rules.riddor_guidance(RIDDOR_SPECIFIED, ACCIDENT))
        self.assertIn('Thu 12 Nov 2026', rules.riddor_guidance(RIDDOR_SPECIFIED, ACCIDENT))
        self.assertIn('within 15 days', rules.riddor_guidance(RIDDOR_OVER_7, ACCIDENT))
        self.assertIn('Tue 17 Nov 2026', rules.riddor_guidance(RIDDOR_OVER_7, ACCIDENT))
        self.assertIn('diagnosis', rules.riddor_guidance(RIDDOR_DISEASE, ACCIDENT))
        self.assertIn('does not need reporting', rules.riddor_guidance(RIDDOR_OVER_3, ACCIDENT))
        self.assertEqual(rules.riddor_guidance('', ACCIDENT), '')


class RiddorSuggestionTable(SimpleTestCase):
    # anyone hurt, working on the job, went to hospital, days off (not counting the day) -> suggestion
    TABLE = [
        (False, True, False, None, ''),                 # nobody hurt
        (True, True, False, None, ''),                  # days off not known yet
        (True, True, False, 3, ''),                     # 3 days: nothing
        (True, True, False, 4, RIDDOR_OVER_3),          # more than 3: record only
        (True, True, True, 7, RIDDOR_OVER_3),           # exactly 7 is not "more than 7"
        (True, True, False, 8, RIDDOR_OVER_7),          # more than 7: report within 15 days
        (True, False, True, None, RIDDOR_PUBLIC),       # member of the public to hospital
        (True, False, False, 20, ''),                   # member of the public, no hospital: days off don't apply
    ]

    def test_table(self):
        for hurt, worker, hospital, days, expected in self.TABLE:
            with self.subTest(hurt=hurt, worker=worker, hospital=hospital, days=days):
                self.assertEqual(rules.suggest_riddor(hurt, worker, hospital, days), expected)


class NotifiableTable(SimpleTestCase):
    # working days, most people at once, person-days -> notifiable (None: not enough to say)
    TABLE = [
        (31, 21, None, True),       # longer than 30 days AND more than 20 at once
        (30, 21, None, None),       # exactly 30 days isn't longer than 30, but 30 x 21 = 630 could pass 500...
        (31, 20, None, None),       # ...so these can't be decided without the person-days
        (30, 16, None, False),      # at most 480 person-days
        (20, 20, None, False),      # at most 400 person-days and under both limits
        (31, 21, 400, True),        # the first test alone is enough
        (25, 10, 501, True),        # more than 500 person-days
        (25, 10, 500, False),       # exactly 500 isn't more than 500
        (None, None, 501, True),
        (None, None, 300, None),    # the 30-day/20-person test still can't be checked
        (None, None, None, None),
        (60, None, None, None),
    ]

    def test_table(self):
        for days, workers, person_days, expected in self.TABLE:
            with self.subTest(days=days, workers=workers, person_days=person_days):
                self.assertIs(rules.is_notifiable(days, workers, person_days), expected)

    def test_principal_designer_and_contractor_needed_with_more_than_one_contractor(self):
        self.assertEqual([rules.needs_pd_and_pc(n) for n in (None, 0, 1, 2, 12)], [False, False, False, True, True])


class ConstructionPhasePlanTable(SimpleTestCase):
    START = date(2026, 12, 1)
    # today -> chased for (None: too early)
    TABLE = [
        (date(2026, 11, 16), None),               # 15 days before the start
        (date(2026, 11, 17), date(2026, 12, 1)),  # 14 days before: chased, due on the start
        (date(2026, 12, 1), date(2026, 12, 1)),
        (date(2027, 2, 1), date(2026, 12, 1)),    # started and still missing: overdue
    ]

    def test_table(self):
        for today, due in self.TABLE:
            with self.subTest(today=today):
                self.assertEqual(rules.cpp_check_due(self.START, today), due)
        self.assertIsNone(rules.cpp_check_due(None, date(2026, 11, 2)))


class OnSiteAndInvestigationRules(SimpleTestCase):
    def test_on_site_counts_from_midnight(self):
        now = datetime.datetime(2026, 11, 2, 14, 35, 12, 9)
        self.assertEqual(rules.on_site_since(now), datetime.datetime(2026, 11, 2))

    def test_investigation_due_a_week_after_reporting(self):
        self.assertEqual(rules.investigation_due(ACCIDENT), date(2026, 11, 9))
