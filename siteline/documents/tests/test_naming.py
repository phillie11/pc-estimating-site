"""ISO 19650 numbering: parsing, assembling, checking and reading file names (pure functions)."""
from django.test import SimpleTestCase

from documents import naming


class ReferenceTests(SimpleTestCase):
    def test_split_and_assemble_round_trip(self):
        parts = naming.split_reference('sln-abc-zz-02-dr-a-1001')
        self.assertEqual(parts, {'project_code': 'SLN', 'originator': 'ABC', 'volume': 'ZZ', 'level': '02',
                                 'doc_type': 'DR', 'role': 'A', 'number': '1001'})
        self.assertEqual(naming.assemble(parts), 'SLN-ABC-ZZ-02-DR-A-1001')

    def test_not_seven_parts_is_not_iso(self):
        for text in ('1203-A-100', 'SLN-ABC-ZZ-02-DR-A', 'SLN-ABC-ZZ-02-DR-A-1001-P01', 'SLN-ABC--02-DR-A-1001',
                     'SLN-ABC-ZZ-02-DR-A-10/01', ''):
            self.assertIsNone(naming.split_reference(text), text)

    def test_conforming_number_has_no_warnings(self):
        parts, warnings = naming.check_reference('SLN-ABC-ZZ-02-DR-A-1001', {'doc_type': {'DR'}, 'role': {'A'}})
        self.assertEqual(warnings, [])
        self.assertEqual(parts['number'], '1001')

    def test_warnings_for_lengths_digits_codes_and_project(self):
        codes = {'doc_type': {'DR', 'SP'}, 'role': {'A'}, 'originator': {'ABC'}}
        _, warnings = naming.check_reference('X-ABCDEFG-ZZZ-2-DRW-Q-10A', codes, project_code='SLN')
        text = ' '.join(warnings)
        for expected in ('Project "X" should be 2 to 6', 'Originator "ABCDEFG" should be 3 to 6',
                         'Volume / system "ZZZ"', 'Level / location "2" should be 2 characters',
                         'Type "DRW"', 'Number "10A" should be digits only', 'Role "Q" isn\'t in this project',
                         'Originator "ABCDEFG" isn\'t in', 'doesn\'t match this project (SLN)'):
            self.assertIn(expected, text)

    def test_empty_code_list_only_checks_format(self):
        _, warnings = naming.check_reference('SLN-ZZZ-ZZ-02-DR-A-1001', {'originator': set()})
        self.assertEqual(warnings, [])

    def test_non_iso_number_gets_one_plain_warning(self):
        parts, warnings = naming.check_reference('1203-A-100')
        self.assertIsNone(parts)
        self.assertEqual(len(warnings), 1)
        self.assertIn('doesn\'t follow ISO 19650', warnings[0])

    def test_blank_number_is_fine(self):
        self.assertEqual(naming.check_reference('  '), (None, []))

    def test_suitability_status(self):
        self.assertEqual(naming.status_for_suitability('S0'), naming.WIP)
        for code in ('S1', 's2', 'S3', 'S4'):
            self.assertEqual(naming.status_for_suitability(code), naming.SHARED)
        for code in ('A1', 'A12', 'B3', 'CR'):
            self.assertEqual(naming.status_for_suitability(code), naming.PUBLISHED)
        for code in ('', 'X9', 'P01'):
            self.assertIsNone(naming.status_for_suitability(code))

    def test_validate_suitability(self):
        self.assertEqual(naming.validate_suitability('S2'), [])
        self.assertTrue(naming.validate_suitability('Q7'))
        self.assertTrue(naming.validate_suitability('S2', {'suitability': {'S0', 'S1'}}))


class FilenameTests(SimpleTestCase):
    def test_iso_with_underscore_revision(self):
        r = naming.parse_filename('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf')
        self.assertEqual((r['reference'], r['revision'], r['suitability'], r['notes']),
                         ('SLN-ABC-ZZ-02-DR-A-1001', 'P03', '', []))

    def test_iso_with_hyphen_revision_suitability_and_title(self):
        r = naming.parse_filename('sln-abc-zz-02-dr-a-1001-S2-C01 Ground floor GA.PDF')
        self.assertEqual((r['reference'], r['revision'], r['suitability'], r['title']),
                         ('SLN-ABC-ZZ-02-DR-A-1001', 'C01', 'S2', 'Ground floor GA'))

    def test_iso_without_revision(self):
        r = naming.parse_filename('SLN-ABC-ZZ-02-DR-A-1001.dwg')
        self.assertEqual((r['reference'], r['revision']), ('SLN-ABC-ZZ-02-DR-A-1001', ''))

    def test_unrecognised_extra_is_noted(self):
        r = naming.parse_filename('SLN-ABC-ZZ-02-DR-A-1001_P03_FINAL.pdf')
        self.assertEqual(r['revision'], 'P03')
        self.assertIn('"FINAL"', r['notes'][0])

    def test_non_iso_name_keeps_number_and_takes_revision(self):
        r = naming.parse_filename('1203-A-100_C02.pdf')
        self.assertEqual((r['reference'], r['parts'], r['revision']), ('1203-A-100', None, 'C02'))
        r = naming.parse_filename('Site photo.jpg')
        self.assertEqual((r['reference'], r['revision'], r['title']), ('Site', '', 'photo'))

    def test_project_pattern(self):
        pattern = '{project}_{originator}_{volume}_{level}_{type}_{role}_{number} ({revision})'
        r = naming.parse_filename('SLN-ABC-ZZ-02-DR-A-1001 (P04).pdf', pattern)
        self.assertEqual((r['reference'], r['revision']), ('SLN-ABC-ZZ-02-DR-A-1001', 'P04'))
        r = naming.parse_filename('nothing like it.pdf', pattern)
        self.assertEqual(r['reference'], '')
        self.assertIn('doesn\'t match', r['notes'][0])

    def test_reference_pattern_for_non_iso_numbers(self):
        r = naming.parse_filename('Rev C03 - 1203-A-100.pdf', 'Rev {revision} - {reference}')
        self.assertEqual((r['reference'], r['revision']), ('1203-A-100', 'C03'))

    def test_bad_patterns_are_refused(self):
        for bad in ('{project}-{nonsense}', '{revision}', '{number}-{number}'):
            with self.assertRaises(naming.PatternError):
                naming.compile_pattern(bad)
        naming.compile_pattern(naming.DEFAULT_PATTERN)
