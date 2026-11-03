"""
The look-ahead: the window and inclusion rules as a fixed table, then the page per role (staff see
every line, clients and consultants shared lines, a subcontractor only their own shared lines, a
tendering subcontractor and other companies nothing), filters, CSV and printing.
"""
from datetime import date, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from changes.tests.base import CommercialTestBase
from directory.models import Subcontractor, Trade
from projects import lookahead as la
from projects.models import Activity
from tenancy.context import TenantContextMissing, org_context

TODAY = date(2026, 11, 4)  # a Wednesday


def act(name, start, finish, progress=0, summary=False, milestone=False):
    return SimpleNamespace(name=name, start=start, finish=finish, progress=progress, is_summary=summary,
                           is_milestone=milestone)


class RuleTableTests(SimpleTestCase):
    """The look-ahead rules, for sign-off. Window: Mon 2 Nov 2026, 3 weeks, so to Sun 22 Nov."""

    def test_window(self):
        table = [  # today, weeks asked for -> first day, last day
            (date(2026, 11, 4), 3, date(2026, 11, 2), date(2026, 11, 22)),
            (date(2026, 11, 2), 2, date(2026, 11, 2), date(2026, 11, 15)),
            (date(2026, 11, 8), 4, date(2026, 11, 2), date(2026, 11, 29)),  # a Sunday is still this week
        ]
        for today, weeks, first, last in table:
            self.assertEqual(la.window(today, weeks), (first, last))
        for raw, want in [('2', 2), ('3', 3), ('4', 4), ('5', 3), ('', 3), (None, 3), ('x', 3)]:
            self.assertEqual(la.weeks_param(raw), want)

    def test_which_lines_are_in(self):
        start, end = la.window(TODAY, 3)
        table = [  # line, in the look-ahead?, behind?
            (act('Overlaps the start', date(2026, 10, 26), date(2026, 11, 3)), True, True),
            (act('Inside', date(2026, 11, 9), date(2026, 11, 13)), True, False),
            (act('Overlaps the end', date(2026, 11, 20), date(2026, 12, 4)), True, False),
            (act('Starts after the window', date(2026, 11, 23), date(2026, 11, 27)), False, None),
            (act('Finished before, not done', date(2026, 10, 1), date(2026, 10, 9), 60), True, True),
            (act('Finished before, done', date(2026, 10, 1), date(2026, 10, 9), 100), False, None),
            (act('Inside but 100% done', date(2026, 11, 9), date(2026, 11, 10), 100), False, None),
            (act('Summary heading', date(2026, 11, 2), date(2026, 11, 27), summary=True), False, None),
            (act('Milestone in window', date(2026, 11, 18), date(2026, 11, 18), milestone=True), True, False),
            (act('Spans the window', date(2026, 10, 1), date(2027, 1, 29)), True, False),
        ]
        got = {ln.activity.name: ln for ln in la.lines([row[0] for row in table], start, end, today=TODAY)}
        for line, included, behind in table:
            with self.subTest(line.name):
                self.assertEqual(line.name in got, included)
                if included:
                    self.assertEqual(got[line.name].behind, behind)

    def test_day_cells_are_working_days_only(self):
        start, end = la.window(TODAY, 2)
        ln = la.lines([act('Fri to Tue', date(2026, 11, 6), date(2026, 11, 10))], start, end, today=TODAY)[0]
        self.assertEqual(len(ln.cells), 10)
        self.assertEqual(ln.cells, [False, False, False, False, True, True, True, False, False, False])
        self.assertEqual([d for d, days in la.weeks_of(start, end)], [date(2026, 11, 2), date(2026, 11, 9)])

    def test_behind_lines_come_first(self):
        start, end = la.window(TODAY, 3)
        rows = la.lines([act('B', date(2026, 11, 9), date(2026, 11, 10)),
                         act('A', date(2026, 10, 1), date(2026, 10, 2))], start, end, today=TODAY)
        self.assertEqual([r.activity.name for r in rows], ['A', 'B'])


class LookaheadViewTests(CommercialTestBase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        with org_context(self.org_a):
            self.trade, _ = Trade.objects.get_or_create(name='Joinery')
            self.other_sub = Subcontractor.objects.create(company_name='Pennine Drylining')
            mk = lambda name, **kw: Activity.objects.create(project=self.pa, name=name, start=today,  # noqa: E731
                                                             finish=today + timedelta(days=3), **kw)
            self.mine = mk('Bar back joinery', subcontractor=self.subco, trade=self.trade)
            self.mine_hidden = mk('Joinery snag float', subcontractor=self.subco, is_shared=False)
            self.theirs = mk('Partitions level 1', subcontractor=self.other_sub)
            self.internal = mk('Contingency (internal)', is_shared=False)
            self.done = mk('Already done', progress=100)
        with org_context(self.org_b):
            Activity.objects.create(project=self.pb, name='Bravo line', start=today, finish=today)

    def page(self, user, query=''):
        self.client.logout()
        self.login(user)
        return self.client.get(self.url('programme_lookahead', self.pa.pk) + query)

    def test_staff_see_every_open_line_with_filters_and_csv(self):
        r = self.page(self.pat)
        for name in ('Bar back joinery', 'Joinery snag float', 'Partitions level 1', 'Contingency (internal)'):
            self.assertContains(r, name)
        self.assertNotContains(r, 'Already done')
        self.assertNotContains(r, 'Bravo line')
        self.assertContains(r, 'data-print')
        r = self.page(self.pat, f'?sub={self.other_sub.pk}')
        self.assertContains(r, 'Partitions level 1')
        self.assertNotContains(r, 'Bar back joinery')
        r = self.page(self.pat, f'?trade={self.trade.pk}&weeks=2')
        self.assertContains(r, 'Bar back joinery')
        self.assertNotContains(r, 'Partitions level 1')
        self.assertContains(r, '2-week look-ahead')
        with org_context(self.org_a):
            Activity.objects.filter(pk=self.theirs.pk).update(name='=HYPERLINK("http://x")')
        r = self.client.get(self.url('programme_lookahead', self.pa.pk) + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        body = r.content.decode()
        self.assertTrue(body.startswith('Activity,WBS,Who,Subcontractor,Trade,Start,Finish,% done,Status'))
        self.assertIn('Bar back joinery', body)
        self.assertIn("'=HYPERLINK", body)  # no formulas in the spreadsheet

    def test_client_and_consultant_see_shared_lines(self):
        for u in (self.carla, self.connie):
            r = self.page(u)
            self.assertContains(r, 'Bar back joinery')
            self.assertContains(r, 'Partitions level 1')
            self.assertNotContains(r, 'Contingency (internal)')
            self.assertNotContains(r, 'Joinery snag float')
            self.assertNotContains(r, '?export=csv')

    def test_subcontractor_sees_only_their_own_shared_lines(self):
        r = self.page(self.sid)
        self.assertContains(r, 'Your work in the next 3 weeks')
        self.assertContains(r, 'Bar back joinery')
        self.assertNotContains(r, 'Partitions level 1')
        self.assertNotContains(r, 'Joinery snag float')
        self.assertNotContains(r, 'Contingency (internal)')
        # Asking for someone else's lines changes nothing
        r = self.page(self.sid, f'?sub={self.other_sub.pk}')
        self.assertContains(r, 'Bar back joinery')
        self.assertNotContains(r, 'Partitions level 1')
        # CSV is staff only
        r = self.client.get(self.url('programme_lookahead', self.pa.pk) + '?export=csv')
        self.assertNotEqual(r.get('Content-Type'), 'text/csv')

    def test_who_can_open_it(self):
        page = self.url('programme_lookahead', self.pa.pk)
        for name, want in [('alice', 200), ('adam', 200), ('pat', 200), ('carla', 200), ('connie', 200), ('sid', 200),
                           ('bea', 403), ('eve', 404), ('nora', 404), ('bob', 404)]:
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), page), want)

    def test_empty_states(self):
        with org_context(self.org_a):
            Activity.objects.filter(project=self.pa).delete()
        self.assertContains(self.page(self.pat), 'Import from MS Project')
        self.assertContains(self.page(self.sid), 'Nothing planned for you yet')
        self.assertContains(self.page(self.carla), 'No programme shared yet')

    def test_programme_page_has_the_sub_navigation(self):
        self.login(self.pat)
        r = self.client.get(self.url('programme', self.pa.pk))
        self.assertContains(r, 'Look-ahead')
        self.assertContains(r, 'Import from MS Project')
        self.client.logout()
        self.login(self.sid)
        r = self.client.get(self.url('programme', self.pa.pk))
        self.assertContains(r, 'Look-ahead')
        self.assertNotContains(r, 'Import from MS Project')

    def test_no_organisation_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(Activity.objects.filter(subcontractor=self.subco))
