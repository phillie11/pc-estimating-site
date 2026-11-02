"""
The quality rules' fixed test table (quality/rules.py), for sign-off by the product owner. Every row is
one case in plain words; the same table is in the release report. All advisory: check with your
safety adviser.
"""
import datetime

from django.test import SimpleTestCase

from .. import rules

T0 = datetime.datetime(2026, 11, 2, 8, 0, tzinfo=datetime.timezone.utc)
H = datetime.timedelta(hours=1)

# (permit type, hours from start to expiry, problem or None)
VALIDITY = [
    (rules.HOT_WORKS, 8, None),          # a normal shift
    (rules.HOT_WORKS, 12, None),         # hot works: up to 12 hours
    (rules.HOT_WORKS, 13, 'at most'),    # longer than one shift: refused
    (rules.CONFINED, 12, None),          # confined space: up to 12 hours
    (rules.CONFINED, 12.5, 'at most'),
    (rules.HEIGHT, 168, None),           # working at height: up to 7 days
    (rules.HEIGHT, 169, 'at most'),
    (rules.ISOLATION, 168, None),        # isolation: up to 7 days
    (rules.ISOLATION, 169, 'at most'),
    (rules.HOT_WORKS, 0, 'after it starts'),   # expiry must be after the start
    (rules.HOT_WORKS, -1, 'after it starts'),
]

# (stored status, hours from start to now, hours from start to expiry, warning hours, state)
STATES = [
    (rules.REQUESTED, 5, 8, 2, 'requested'),     # not approved: waiting, whatever the clock says
    (rules.APPROVED, -1, 8, 2, 'approved'),      # approved, before its start
    (rules.APPROVED, 0, 8, 2, 'active'),         # active from its start
    (rules.APPROVED, 5.9, 8, 2, 'active'),       # more than 2 hours left
    (rules.APPROVED, 6, 8, 2, 'expiring'),       # 2 hours left: expiring (on Needs me)
    (rules.APPROVED, 7.9, 8, 2, 'expiring'),
    (rules.APPROVED, 8, 8, 2, 'expired'),        # at its expiry: expired (on Needs me until closed)
    (rules.APPROVED, 30, 8, 2, 'expired'),
    (rules.APPROVED, 3, 8, 6, 'expiring'),       # the company's own warning time
    (rules.APPROVED, -1, 1, 2, 'expiring'),      # a short permit can be "expiring" before it starts
    (rules.CLOSED, 30, 8, 2, 'closed'),          # closed, refused and withdrawn never change
    (rules.REFUSED, 0, 8, 2, 'refused'),
    (rules.WITHDRAWN, 0, 8, 2, 'withdrawn'),
]

# (results of the checks, outcome)
OUTCOMES = [
    (['PASS', 'PASS', 'PASS'], 'PASSED'),
    (['PASS', 'NA', 'PASS'], 'PASSED'),     # N/A counts as answered
    (['NA', 'NA'], 'PASSED'),
    (['PASS', 'FAIL', 'PASS'], 'FAILED'),   # any fail fails the inspection
    (['FAIL', 'NA'], 'FAILED'),
    (['PASS', '', 'PASS'], None),           # a check not answered: can't finish
    (['FAIL', ''], None),
    ([], None),                             # no checks: can't finish
]

# (after photos, system or product, problem or None)
SIGN_OFF = [
    (1, 'Intumescent pipe collar', None),
    (3, 'Fire batt', None),
    (0, 'Intumescent pipe collar', 'photo of the finished seal'),
    (1, '', 'system or product'),
    (1, '   ', 'system or product'),
    (0, '', 'system or product'),
]


class PermitRuleTests(SimpleTestCase):
    def test_validity_table(self):
        for kind, hours, problem in VALIDITY:
            with self.subTest(kind=kind, hours=hours):
                got = rules.validity_problem(kind, T0, T0 + datetime.timedelta(hours=hours))
                if problem is None:
                    self.assertIsNone(got)
                else:
                    self.assertIn(problem, got)

    def test_missing_times(self):
        self.assertIn('Enter when', rules.validity_problem(rules.HOT_WORKS, None, T0))

    def test_state_table(self):
        for status, now_h, end_h, warning, state in STATES:
            with self.subTest(status=status, now=now_h, end=end_h, warning=warning):
                now = T0 + datetime.timedelta(hours=now_h)
                self.assertEqual(rules.permit_state(status, T0, T0 + datetime.timedelta(hours=end_h), now, warning),
                                 state)
                self.assertEqual(rules.expiring(status, T0, T0 + datetime.timedelta(hours=end_h), now, warning),
                                 state in ('expiring', 'expired'))

    def test_every_type_has_a_label_and_precautions(self):
        for code, t in rules.PERMIT_TYPES.items():
            self.assertTrue(t['label'] and t['help'] and len(t['precautions']) >= 5, code)
            self.assertIn(code, rules.MAX_HOURS)
        self.assertTrue(any('fire watch' in p for p in rules.precautions_for(rules.HOT_WORKS)))


class InspectionRuleTests(SimpleTestCase):
    def test_outcome_table(self):
        for results, outcome in OUTCOMES:
            with self.subTest(results=results):
                self.assertEqual(rules.inspection_outcome(results), outcome)


class FirestopRuleTests(SimpleTestCase):
    def test_sign_off_table(self):
        for photos, system, problem in SIGN_OFF:
            with self.subTest(photos=photos, system=system):
                got = rules.sign_off_problem(photos, system)
                if problem is None:
                    self.assertIsNone(got)
                else:
                    self.assertIn(problem, got)
