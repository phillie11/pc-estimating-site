"""Structured regions: reading free text, the directory filter and form, and the data migration."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from tenancy.context import org_context
from tenancy.tests import Base

from .models import Subcontractor
from .regions import covers, guess_regions, labels


class GuessRegionsTableTests(SimpleTestCase):
    """Fixed table: free text as subcontractors and site addresses really write it."""
    TABLE = [
        ('', []),
        ('North West, Yorkshire', ['NW', 'YH']),
        ('National', ['UK']),
        ('Nationwide', ['UK']),
        ('UK wide', ['UK']),
        ('Midlands', ['EM', 'WM']),
        ('West Midlands only', ['WM']),
        ('north-west and the north east', ['NE', 'NW']),
        ('48 Call Lane, Leeds LS1', ['YH']),
        ('Kingsway, London WC2', ['LDN']),
        ('Spinningfields, Manchester', ['NW']),
        ('Argyle Arcade, Glasgow', ['SCO']),
        ('Battlefield Rd, Shrewsbury', ['WM']),
        ('Bathroom pods', []),          # "bath" only as a whole word
        ('Manchester', ['NW']),
        ('South West & Wales', ['SW', 'WAL']),
        ('M25', ['EE', 'LDN', 'SE']),
        ('Belfast', ['NI']),
        ('anything goes', []),
    ]

    def test_table(self):
        for text, expected in self.TABLE:
            with self.subTest(text=text):
                self.assertEqual(guess_regions(text), expected)

    def test_covers(self):
        self.assertEqual(covers(['UK'], ['LDN']), 'yes')
        self.assertEqual(covers(['NW'], ['NW', 'YH']), 'yes')
        self.assertEqual(covers(['NW'], ['LDN']), 'no')
        self.assertEqual(covers([], ['LDN']), 'unknown')
        self.assertEqual(covers(['NW'], []), 'unknown')
        self.assertEqual(labels(['NW', 'UK', 'nonsense']), ['North West', 'Nationwide'])


class DirectoryRegionViewTests(Base):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.north = Subcontractor.objects.create(company_name='Northern Dry', region_codes=['NW'])
            self.anywhere = Subcontractor.objects.create(company_name='Go Anywhere', region_codes=['UK'])
            self.south = Subcontractor.objects.create(company_name='Southern Ceilings', region_codes=['SE'])

    def test_region_filter_includes_nationwide(self):
        self.login(self.alice)
        r = self.client.get(self.url('sub_list', self.org_a) + '?region=NW')
        names = {s.company_name for s in r.context['subs']}
        self.assertEqual(names, {'Northern Dry', 'Go Anywhere'})
        self.assertContains(r, 'North West')

    def test_form_saves_ticked_regions_and_keeps_notes(self):
        self.login(self.alice)
        r = self.client.post(self.url('sub_edit', self.org_a, self.south.pk), {
            'company_name': 'Southern Ceilings', 'status': 'APPROVED', 'region_codes': ['SE', 'LDN'],
            'regions': 'Inside the M25 mostly'})
        self.assertEqual(r.status_code, 302)
        self.south.refresh_from_db()
        self.assertEqual(sorted(self.south.region_codes), ['LDN', 'SE'])
        self.assertEqual(self.south.regions, 'Inside the M25 mostly')

    def test_csv_has_region_labels_and_notes(self):
        self.login(self.alice)
        r = self.client.get(self.url('sub_list', self.org_a) + '?export=csv')
        self.assertIn(b'Nationwide', r.content)
        self.assertIn(b'Region notes', r.content)


class RegionMigrationTests(TransactionTestCase):
    """directory 0006 on seeded data: text read into codes, text untouched; backward empties the codes."""
    BEFORE = [('directory', '0005_regions_and_chasing')]
    AFTER = [('directory', '0006_regions_from_text')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        self.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_and_backward(self):
        old = self.migrate(self.BEFORE)
        Organization = old.get_model('tenancy', 'Organization')
        Sub = old.get_model('directory', 'Subcontractor')
        alpha = Organization.objects.create(name='Alpha', slug='alpha')
        bravo = Organization.objects.create(name='Bravo', slug='bravo')
        for org, name, text in [(alpha, 'A', 'North West, Yorkshire'), (alpha, 'B', 'National'),
                                (bravo, 'C', 'Midlands'), (bravo, 'D', ''), (bravo, 'E', 'wherever')]:
            Sub.objects.create(organization=org, company_name=name, regions=text)

        new = self.migrate(self.AFTER)
        got = {s.company_name: (s.region_codes, s.regions)
               for s in new.get_model('directory', 'Subcontractor').objects.all()}
        self.assertEqual(got, {
            'A': (['NW', 'YH'], 'North West, Yorkshire'), 'B': (['UK'], 'National'),
            'C': (['EM', 'WM'], 'Midlands'), 'D': ([], ''), 'E': ([], 'wherever'),
        })

        back = self.migrate(self.BEFORE)
        got = {s.company_name: (s.region_codes, s.regions)
               for s in back.get_model('directory', 'Subcontractor').objects.all()}
        self.assertEqual(got['A'], ([], 'North West, Yorkshire'))
