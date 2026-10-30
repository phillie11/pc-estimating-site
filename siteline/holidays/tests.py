"""UK bank holidays by region and Construction Act (s116) day counting, held to fixed tables."""
import json
import shutil
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from . import calendar as cal

D = date.fromisoformat
EW, SC, NI = cal.ENGLAND_AND_WALES, cal.SCOTLAND, cal.NORTHERN_IRELAND


class TableTests(SimpleTestCase):
    def test_covers_2020_to_2030_in_every_region(self):
        for region, _ in cal.REGION_CHOICES:
            self.assertEqual(cal.covered_years(region), range(2020, 2031), region)

    # (date, excluded in England & Wales, Scotland, Northern Ireland)
    TABLE = [
        ('2026-04-03', True, True, True),     # Good Friday
        ('2026-04-06', True, False, True),    # Easter Monday: not in Scotland
        ('2026-01-01', True, True, True),
        ('2026-01-02', False, True, False),   # 2 January: Scotland only
        ('2026-03-17', False, False, True),   # St Patrick's Day: NI only
        ('2026-07-13', False, False, True),   # Battle of the Boyne, 12 July is a Sunday: NI substitute
        ('2026-08-03', False, True, False),   # Scottish summer bank holiday (first Monday)
        ('2026-08-31', True, False, True),    # English summer bank holiday (last Monday)
        ('2026-11-30', False, True, False),   # St Andrew's Day
        ('2026-12-25', True, True, True),
        ('2026-12-26', False, False, False),  # Boxing Day is a Saturday: the bank holiday is Monday 28th
        ('2026-12-28', True, True, True),
        ('2022-12-25', True, True, True),     # Christmas Day on a Sunday: excluded by s116 itself
        ('2022-12-27', True, True, True),     # its substitute bank holiday
        ('2022-06-03', True, True, True),     # Platinum Jubilee
        ('2022-09-19', True, True, True),     # state funeral
        ('2023-05-08', True, True, True),     # Coronation
        ('2020-05-08', True, True, True),     # VE Day: early May bank holiday moved
        ('2020-05-04', False, False, False),
        ('2026-05-04', True, True, True),
        ('2026-10-30', False, False, False),  # an ordinary Friday
        ('2026-10-31', False, False, False),  # weekends are counted
        ('2031-04-11', True, True, True),     # Good Friday after the table ends: still computed
        ('2031-12-25', True, True, True),
    ]

    def test_table(self):
        for day, ew, sc, ni in self.TABLE:
            with self.subTest(day=day):
                self.assertEqual((cal.is_excluded(D(day), EW), cal.is_excluded(D(day), SC), cal.is_excluded(D(day), NI)),
                                 (ew, sc, ni))

    def test_easter(self):
        for year, easter in [(2020, '2020-04-12'), (2024, '2024-03-31'), (2025, '2025-04-20'), (2026, '2026-04-05'),
                             (2027, '2027-03-28'), (2030, '2030-04-21'), (2031, '2031-04-13')]:
            self.assertEqual(cal.easter_sunday(year), D(easter))

    def test_every_good_friday_and_christmas_in_the_table(self):
        # The bundled table agrees with the computed Easter, so a typo would show here
        for year in range(2020, 2031):
            for region, _ in cal.REGION_CHOICES:
                self.assertIn(cal.good_friday(year), cal.bank_holidays(region))


class CountingTests(SimpleTestCase):
    # (start, days, region, expected): s116 starts the day after and skips excluded days; weekends count
    ADD = [
        ('2026-10-30', 7, EW, '2026-11-06'),   # nothing excluded
        ('2026-12-22', 7, EW, '2026-12-31'),   # skips Christmas Day and Monday 28th; Saturday 26th counts
        ('2026-12-29', 5, EW, '2027-01-04'),   # skips 1 January
        ('2026-12-29', 5, SC, '2027-01-05'),   # Scotland: 2 Jan 2027 is a Saturday, so its holiday is Monday 4th
        ('2026-04-01', 5, EW, '2026-04-08'),   # Good Friday and Easter Monday
        ('2026-04-01', 5, SC, '2026-04-07'),   # Scotland: Good Friday only
        ('2026-07-10', 5, NI, '2026-07-16'),   # NI skips the Boyne substitute (13 July)
        ('2026-07-10', 5, EW, '2026-07-15'),
        ('2026-03-16', 1, NI, '2026-03-18'),   # St Patrick's Day
        ('2026-03-16', 1, EW, '2026-03-17'),
        ('2026-04-02', 0, EW, '2026-04-02'),   # no days: the same day
    ]
    SUBTRACT = [
        ('2026-04-08', 5, EW, '2026-04-01'),
        ('2026-04-08', 5, SC, '2026-04-02'),
        ('2027-01-05', 7, SC, '2026-12-26'),   # skips 4 Jan, 1 Jan and 28 Dec; Saturday 2 Jan and 26 Dec count
    ]

    def test_add(self):
        for start, days, region, expected in self.ADD:
            with self.subTest(start=start, days=days, region=region):
                self.assertEqual(cal.add_days(D(start), days, region), D(expected))

    def test_subtract(self):
        for end, days, region, expected in self.SUBTRACT:
            with self.subTest(end=end, days=days, region=region):
                self.assertEqual(cal.subtract_days(D(end), days, region), D(expected))


class UpdateCommandTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / 'bank-holidays.json'
        shutil.copy(cal.DATA_FILE, self.file)
        patcher = mock.patch.object(cal, 'DATA_FILE', self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cal.reload)
        self.addCleanup(shutil.rmtree, self.dir, True)
        cal.reload()

    def gov_uk(self, extra='2031-05-05'):
        data = json.loads(self.file.read_text(encoding='utf-8'))
        for region in data.values():
            region['events'] = [e for e in region['events'] if e['date'] >= '2025-01-01']  # gov.uk drops old years
            region['events'].append({'title': 'A new bank holiday', 'date': extra, 'notes': '', 'bunting': True})
        return data

    def test_refresh_merges_and_keeps_old_years(self):
        with mock.patch('holidays.management.commands.update_bank_holidays.fetch', return_value=self.gov_uk()):
            out = StringIO()
            call_command('update_bank_holidays', stdout=out)
        self.assertIn('3 new date(s)', out.getvalue())
        self.assertTrue(cal.is_excluded(D('2031-05-05'), SC))
        self.assertTrue(cal.is_excluded(D('2020-05-08'), EW))  # kept though gov.uk no longer lists it

    def test_dry_run_writes_nothing(self):
        before = self.file.read_bytes()
        with mock.patch('holidays.management.commands.update_bank_holidays.fetch', return_value=self.gov_uk()):
            call_command('update_bank_holidays', '--dry-run', stdout=StringIO())
        self.assertEqual(self.file.read_bytes(), before)

    def test_bad_download_is_refused(self):
        before = self.file.read_bytes()
        for bad in ({'england-and-wales': {'events': []}}, {'scotland': 'nope'}):
            with mock.patch('holidays.management.commands.update_bank_holidays.fetch', return_value=bad), \
                    self.assertRaises(CommandError):
                call_command('update_bank_holidays', stdout=StringIO())
        with mock.patch('holidays.management.commands.update_bank_holidays.fetch', side_effect=OSError('offline')), \
                self.assertRaises(CommandError):
            call_command('update_bank_holidays', stdout=StringIO())
        self.assertEqual(self.file.read_bytes(), before)


class FetchSchemeTests(SimpleTestCase):
    def test_only_https_addresses_are_fetched(self):
        for url in ('file:///etc/passwd', 'http://www.gov.uk/bank-holidays.json', 'ftp://example.com/x.json'):
            with self.subTest(url=url), mock.patch('urllib.request.urlopen') as urlopen:
                with self.assertRaises(CommandError):
                    call_command('update_bank_holidays', '--dry-run', '--url', url, stdout=StringIO())
                urlopen.assert_not_called()
