"""
The target time to give out client-reported defects (pending owner decision): a company setting,
default 2 working days after the defect was reported, which is the item's due date on the PM's
Needs me (red once it has passed). Weekends and the contract region's bank holidays don't count.
"""
from datetime import date, timedelta

from django.utils import timezone

from projects.models import Issue
from tenancy.context import TenantContextMissing, org_context

from .. import rules
from ..models import CloseoutSettings
from .base import CloseoutTestBase
from .test_closeout import kinds


class GiveOutTargetTests(CloseoutTestBase):
    def reported(self, days_ago, **kw):
        i = self.issue(title=kw.pop('title', 'From client'), issue_type='DEFECT', created_by=self.carla, **kw)
        with org_context(self.org_a):
            Issue.objects.filter(pk=i.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return i

    def test_due_two_working_days_after_it_was_reported(self):
        self.reported(0)
        item, = kinds(self, self.pat, 'defect_assign')
        self.assertEqual(item.due, rules.give_out_by(timezone.localdate(), 2))
        self.assertEqual(item.tone, 'warn')

    def test_overdue_goes_red(self):
        self.reported(10)
        item, = kinds(self, self.pat, 'defect_assign')
        self.assertLess(item.due, timezone.localdate())
        self.assertEqual((item.tone, item.status_label), ('bad', 'Not given out yet'))

    def test_the_company_sets_the_target(self):
        self.login(self.alice)
        r = self.client.post(self.url('closeout_settings'), {'defect_give_out_days': '5'})
        self.assertRedirects(r, self.url('closeout_settings'))
        self.reported(0)
        item, = kinds(self, self.pat, 'defect_assign')
        self.assertEqual(item.due, rules.give_out_by(timezone.localdate(), 5))
        r = self.client.post(self.url('closeout_settings'), {'defect_give_out_days': '21'})
        self.assertContains(r, 'Enter 20 working days or fewer.')

    def test_settings_are_for_owners_and_admins(self):
        for user, code in ((self.alice, 200), (self.adam, 200), (self.pat, 403), (self.carla, 403), (self.bob, 404)):
            self.assertEqual(self.status_for(user, self.url('closeout_settings')), code, user)

    def test_given_out_drops_off(self):
        i = self.reported(0)
        with org_context(self.org_a):
            Issue.objects.filter(pk=i.pk).update(assignee=self.sid)
        self.assertEqual(kinds(self, self.pat, 'defect_assign'), [])

    def test_rule_table(self):
        # Reported, working days -> give out by. England and Wales; Christmas 2026 is a Friday,
        # Boxing Day Saturday (Monday 28 Dec the substitute)
        cases = [
            (date(2026, 11, 2), 2, date(2026, 11, 4)),    # Monday -> Wednesday
            (date(2026, 11, 5), 2, date(2026, 11, 9)),    # Thursday -> next Monday
            (date(2026, 11, 7), 2, date(2026, 11, 10)),   # reported on a Saturday -> Tuesday
            (date(2026, 12, 23), 2, date(2026, 12, 29)),  # over Christmas and the Boxing Day substitute
            (date(2026, 11, 2), 0, date(2026, 11, 2)),    # 0: the same day
            (date(2026, 11, 2), None, date(2026, 11, 4)),  # no setting: the default 2
        ]
        for reported, n, expected in cases:
            self.assertEqual(rules.give_out_by(reported, n), expected, (reported, n))
        self.assertIsNone(rules.give_out_by(None, 2))
        self.assertEqual(rules.DEFECT_GIVE_OUT_DAYS, 2)

    def test_scotland_counts_its_own_bank_holidays(self):
        # 2 Jan 2026 is a Scottish bank holiday (Friday), not an English one
        self.assertEqual(rules.give_out_by(date(2026, 1, 1), 1, 'scotland'), date(2026, 1, 5))
        self.assertEqual(rules.give_out_by(date(2026, 1, 1), 1), date(2026, 1, 2))

    def test_no_organisation_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(CloseoutSettings.objects.all())
