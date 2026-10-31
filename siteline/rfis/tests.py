"""
The project RFI register's CSV export: every RFI on the project the person can see (not just the
filter the page is on), the register's columns plus dates, spreadsheet-safe cells, who may export
it, and tenancy.
"""
import csv
import io
from datetime import date, timedelta

from django.utils import timezone

from changes.tests.base import CommercialTestBase
from tenancy.context import org_context

from .models import RFI


class RegisterCsvTests(CommercialTestBase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        with org_context(self.org_a):
            self.open = RFI.objects.create(project=self.pa, title='=1+1', question='?', created_by=self.pat,
                                           ball_in_court_user=self.carla, due_date=date(2026, 11, 20))
            self.internal = RFI.objects.create(project=self.pa, title='Our own query on the bar top', question='?',
                                               is_shared=False, status=RFI.STATUS_CLOSED, closed_at=now)
            self.closed = RFI.objects.create(project=self.pa, title='  =HYPERLINK("http://evil.example","x")',
                                             question='?', status=RFI.STATUS_CLOSED, response='@SUM(A1:A9)',
                                             responded_at=now - timedelta(days=2), closed_at=now)
        with org_context(self.org_b):
            RFI.objects.create(project=self.pb, title='Bravo question', question='?')

    def export(self, user, project=None, org=None, query='?export=csv'):
        self.client.logout()
        if user is not None:
            self.login(user)
        return self.client.get(self.url('rfi_list', (project or self.pa).pk, org=org) + query)

    def rows(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        return list(csv.reader(io.StringIO(response.content.decode())))

    def test_link_on_the_register(self):
        self.login(self.pat)
        r = self.client.get(self.url('rfi_list', self.pa.pk))
        self.assertContains(r, 'Export CSV')
        self.assertContains(r, self.url('rfi_list', self.pa.pk) + '?export=csv')

    def test_every_rfi_whatever_the_filter_with_the_register_columns_and_dates(self):
        # The page defaults to live RFIs; the export still has the closed ones
        rows = self.rows(self.export(self.pat, query='?status=OPEN&export=csv'))
        self.assertEqual(rows[0], ['Ref', 'Subject', 'Category', 'Priority', 'Status', 'Raised by', 'Raised',
                                   'Ball in court', 'Required by', 'Responded', 'Closed', 'Days open', 'Shared',
                                   'Response'])
        self.assertEqual([r[0] for r in rows[1:]], ['RFI-001', 'RFI-002', 'RFI-003'])
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual((first['Status'], first['Raised by'], first['Ball in court'], first['Required by'],
                          first['Raised'], first['Shared']),
                         ('Open', 'Pat PM', 'Carla Client', '2026-11-20', timezone.localdate().isoformat(), 'Yes'))
        third = dict(zip(rows[0], rows[3]))
        self.assertEqual(third['Responded'], timezone.localdate(self.closed.responded_at).isoformat())
        self.assertEqual(third['Closed'], timezone.localdate(self.closed.closed_at).isoformat())
        self.assertEqual(dict(zip(rows[0], rows[2]))['Shared'], 'No, internal')
        self.assertNotIn('Bravo question', str(rows))

    def test_formulas_are_neutralised(self):
        body = self.export(self.pat).content.decode()
        self.assertIn("'=1+1", body)
        self.assertNotIn(',=1+1', body)
        self.assertIn('"\'  =HYPERLINK(""http://evil.example"",""x"")"', body)  # even after leading spaces
        self.assertIn("'@SUM(A1:A9)", body)

    def test_externals_get_only_what_the_register_shows_them(self):
        for user in (self.carla, self.connie, self.sid):
            with self.subTest(user=user.email):
                rows = self.rows(self.export(user))
                self.assertEqual([r[0] for r in rows[1:]], ['RFI-001', 'RFI-003'])  # never the internal one
                self.assertNotIn('Shared', rows[0])
                self.assertNotIn('Our own query', str(rows))

    def test_who_can_export(self):
        expected = {'alice': 200, 'adam': 200, 'pat': 200, 'carla': 200, 'connie': 200, 'sid': 200,
                    'eve': 404,   # estimating staff can't open live jobs
                    'bea': 403,   # a tendering sub sees only their tender
                    'nora': 404,  # not on the job
                    'bob': 404}   # another company
        for name, code in expected.items():
            with self.subTest(name):
                r = self.export(getattr(self, name))
                self.assertEqual(r.status_code, code)
                if code != 200:
                    self.assertNotIn(b'RFI-001', r.content)
        self.assertEqual(self.export(None).status_code, 302)  # to sign in

    def test_another_company_gets_a_404_even_through_its_own_workspace(self):
        self.assertEqual(self.export(self.bob).status_code, 404)
        self.assertEqual(self.export(self.bob, org=self.org_b).status_code, 404)  # Alpha's job under Bravo's slug
        rows = self.rows(self.export(self.bob, project=self.pb, org=self.org_b))
        self.assertEqual([r[1] for r in rows[1:]], ['Bravo question'])
