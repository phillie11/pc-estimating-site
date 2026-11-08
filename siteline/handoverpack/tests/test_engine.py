"""
The workbook engine on its own: recalculation, reconciliation, classification and the gap checks.

Workbooks are built in code by handoverpack.sample with the template's formulas; the values Excel would
have saved are worked out separately by the builder (and, outside the suite, the same workbook was
calculated by LibreOffice and by the real template: both reconcile to the penny with no issues).
"""
import copy
import io
import zipfile
from decimal import Decimal as D

from django.test import SimpleTestCase

from handoverpack import codec, engine, mapping, reader, sample


def run(data, tolerance=D(0)):
    m = mapping.get('standard')
    return engine.run(reader.open_book(data, engine.extents(m)), m, tolerance)


def small(**trade):
    t = {'include': True, 'bidder': 1, 'suppliers': ['Volt Ltd', 'Other Ltd'], 'lines': [
        sample.line('E1', 'Lighting', 3, 'nr', (D('10.333'), None, ''), (D('11'), None, '')),
        sample.line('E2', 'Small power', 2, 'nr', (D('50'), D('2.5'), 'adj'), (D('45'), None, '')),
    ]}
    t.update(trade)
    return {'header': {'project': 'Test Unit 4', 'client': 'Example Client Ltd', 'estimator': 'A. Estimator'},
            'default_markup': D('0.1'), 'prelim_markup': D('0.05'), 'target_margin': D('0.05'),
            'trades': {'Electrical': t}, 'prelims': {'duration': 10, 'lines': {}}}


def gap(result, code, trade=None):
    return [g for g in result['gaps'] if g['code'] == code and (trade is None or g['trade'] == trade)]


class RecalculationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.demo = run(sample.build(sample.DEMO))

    def test_demo_reconciles_to_the_penny_with_no_issues(self):
        t = self.demo['totals']
        self.assertTrue(self.demo['cached'])
        self.assertEqual(self.demo['issues'], [])
        self.assertEqual(t['difference'], D('0'))
        self.assertTrue(t['reconciled'])
        self.assertEqual(t['cost'], D('205349.8'))
        self.assertEqual(t['charge'], D('228753.406'))
        self.assertEqual(t['charge'], t['excel_charge'])

    def test_line_arithmetic_is_the_workbooks(self):
        r = run(sample.build(small()))
        lines = {ln['ref']: ln for ln in r['trades'][7]['lines']}  # Electrical is the 8th tab
        self.assertEqual(lines['E1']['cost'], D('30.999'))
        self.assertEqual(lines['E1']['charge'], D('34.0989'))
        self.assertEqual(lines['E2']['cost'], D('105.0'))  # qty x (rate + Est Adj)
        self.assertEqual(lines['E2']['tender_rate'], D('57.75'))
        self.assertEqual(r['totals']['cost'], D('135.999'))

    def test_nothing_is_rounded_before_it_is_summed(self):
        spec = small(lines=[sample.line(f'E{i}', f'Item {i}', 1, 'nr', (D('0.005'), None, '')) for i in range(3)])
        r = run(sample.build(spec))
        # Each line's charge is 0.0055: rounded first that would be 3 x 0.01 = 0.03; the sum is 0.0165
        self.assertEqual(r['totals']['charge'], D('0.0165'))
        self.assertTrue(r['totals']['reconciled'])

    def test_include_no_leaves_the_trade_out_of_the_total(self):
        r = run(sample.build(small(include=False)))
        self.assertEqual(r['totals']['charge'], 0)
        self.assertEqual({ln['cls'] for ln in r['trades'][7]['lines']}, {engine.EXCLUDED})
        self.assertTrue(gap(r, 'excluded_with_items', 'Electrical'))

    def test_markup_override_on_the_summary_is_followed(self):
        r = run(sample.build(small(markup=D('0.25'))))
        self.assertEqual(r['trades'][7]['markup'], D('0.25'))
        self.assertEqual(r['issues'], [])

    def test_prelim_weekly_lines_take_the_duration_and_markup(self):
        spec = small()
        spec['prelims']['lines'] = {'Site Manager': {'include': 'Yes', 'qty': 1, 'rate': 1000},
                                    'Skips - 12yd': {'include': 'Yes', 'qty': 4, 'periods': 1, 'rate': 250}}
        r = run(sample.build(spec))
        costed = {ln['description']: ln for ln in r['prelims']['lines'] if ln['cost'] is not None}
        self.assertEqual(costed['Site Manager']['cost'], D('10000'))  # 1 x 1 x 10 weeks x 1000
        self.assertEqual(costed['Site Manager']['charge'], D('10500.00'))
        self.assertEqual(costed['Skips - 12yd']['cost'], D('1000'))
        self.assertEqual(r['issues'], [])


class ReconciliationTests(SimpleTestCase):
    def test_a_stale_saved_value_is_named_by_sheet_and_cell(self):
        r = run(sample.build(small(), stale={('Electrical', 'AA8'): D('31.50')}))
        self.assertEqual([(i['sheet'], i['cell']) for i in r['issues']], [('Electrical', 'AA8')])
        self.assertEqual(r['issues'][0]['difference'], D('0.501'))

    def test_under_a_penny_is_not_an_issue(self):
        r = run(sample.build(small(), stale={('Electrical', 'AA8'): D('31.008')}))
        self.assertEqual(r['issues'], [])

    def test_a_typed_over_total_breaks_reconciliation(self):
        r = run(sample.build(small(), typed={('PRICING SUMMARY', 'G50'): D('200')}))
        self.assertFalse(r['totals']['reconciled'])
        self.assertEqual(r['totals']['difference'], D('149.5989') - D('200'))
        self.assertIn(('PRICING SUMMARY', 'G50'), [(i['sheet'], i['cell']) for i in r['issues']])

    def test_without_saved_values_the_recalculation_is_used_and_says_so(self):
        r = run(sample.build(small(), cached=False))
        self.assertFalse(r['cached'])
        self.assertIsNone(r['totals']['difference'])
        self.assertIsNone(r['totals']['reconciled'])
        self.assertEqual(r['issues'], [])
        self.assertEqual(r['totals']['cost'], D('135.999'))
        self.assertTrue(any('saved without calculated values' in n for n in r['notes']))


class ClassificationAndGapTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = run(sample.build(sample.DEMO))
        cls.trades = {t['name']: t for t in cls.r['trades']}

    def cls_of(self, trade, ref):
        return next(ln['cls'] for ln in self.trades[trade]['lines'] if ln['ref'] == ref)

    def test_classes(self):
        self.assertEqual(self.cls_of('Electrical', 'E1'), engine.PRICED)
        self.assertEqual(self.cls_of('Electrical', 'E4'), engine.INCLUDED)    # 0 with "Included in E2"
        self.assertEqual(self.cls_of('Electrical', 'E6'), engine.BY_OTHERS)   # 0 with "By client IT contractor"
        self.assertEqual(self.cls_of('Signage', 'S1'), engine.EXCLUDED)
        self.assertEqual(self.cls_of('Mechanical', 'M3'), engine.REVIEW)
        self.assertEqual(self.cls_of('Prov Sums', 'PS1'), engine.PROVISIONAL)
        self.assertEqual(self.cls_of('Decoration', 'D1'), engine.ALLOWANCE)   # Est Adj on a zero rate

    def test_gaps(self):
        r = self.r
        self.assertTrue(gap(r, 'excluded_with_items', 'Signage'))
        self.assertTrue(gap(r, 'no_bidder', 'Cleaning'))
        self.assertTrue(gap(r, 'review_lines', 'Mechanical'))
        self.assertTrue(gap(r, 'unselected_rates', 'Partitions Ceilings'))  # PC5 priced only by bidder 3
        self.assertTrue(gap(r, 'prelim_no_rate'))
        self.assertTrue(gap(r, 'joinery_incomplete'))
        self.assertTrue(gap(r, 'zero_markup', 'Prov Sums'))
        short = gap(r, 'margin_below_target')[0]
        self.assertEqual(short['amount'].quantize(D('0.01')), D('4598.64'))  # price at target less the charge

    def test_margin_tolerance_is_a_company_setting(self):
        r = run(sample.build(sample.DEMO), tolerance=D('0.02'))
        self.assertFalse(gap(r, 'margin_below_target'))

    def test_allowance_without_a_rate_is_left_out_and_flagged(self):
        spec = small(lines=[sample.line('E1', 'Own allowance', 10, 'nr', (None, D('5'), ''))])
        r = run(sample.build(spec))
        self.assertEqual(r['trades'][7]['lines'][0]['cls'], engine.ALLOWANCE)
        self.assertEqual(r['totals']['cost'], 0)
        self.assertEqual(gap(r, 'allowance_not_costed')[0]['amount'], D('50'))
        self.assertEqual(r['issues'], [])

    def test_zero_markup_on_an_included_trade(self):
        spec = small()
        spec['default_markup'] = D('0')
        self.assertTrue(gap(run(sample.build(spec)), 'zero_markup', 'Electrical'))


class MappingAndCodecTests(SimpleTestCase):
    def test_preset_is_valid_and_bad_mappings_are_refused(self):
        mapping.validate(mapping.STANDARD)
        bad = copy.deepcopy(mapping.STANDARD)
        bad['trade']['bidder'] = 'not a cell'
        with self.assertRaises(mapping.MappingError):
            mapping.validate(bad)
        bad = copy.deepcopy(mapping.STANDARD)
        del bad['prelims']['duration']
        with self.assertRaises(mapping.MappingError):
            mapping.validate(bad)

    def test_a_moved_layout_is_read_through_the_mapping(self):
        """Move the target margin to another cell: only the mapping changes."""
        spec = small()
        data = sample.build(spec, typed={('PRICING SUMMARY', 'L6'): D('0.3')})
        m = mapping.get('standard', custom={**copy.deepcopy(mapping.STANDARD), 'key': 'moved',
                                            'summary': {**mapping.STANDARD['summary'], 'target_margin': 'L6'}})
        r = engine.run(reader.open_book(data, engine.extents(m)), m)
        self.assertEqual(r['settings']['target_margin'], D('0.3'))

    def test_codec_keeps_decimals_and_text(self):
        value = {'a': D('1.005'), 'b': ['#d:1', 'x', None, 3]}
        self.assertEqual(codec.decode(codec.encode(value)), value)


class ReaderSafetyTests(SimpleTestCase):
    def test_names(self):
        for name in ('a.xls', 'a.xlsb', 'a.csv', 'a.ods', 'a.pdf', 'a.xltm'):
            with self.subTest(name):
                with self.assertRaises(reader.WorkbookRefused):
                    reader.check_name(name)
        self.assertEqual(reader.check_name('Rev C.XLSX'), '.xlsx')
        self.assertEqual(reader.check_name('Rev C.xlsm'), '.xlsm')  # accepted, to be stripped of its macros

    def _zip(self, parts):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in parts.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_macros_dtd_bombs_and_junk_are_refused(self):
        good = sample.build(small())
        with zipfile.ZipFile(io.BytesIO(good)) as z:
            parts = {i.filename: z.read(i) for i in z.infolist()}
        cases = {
            'dtd': {**parts, 'xl/workbook.xml': b'<!DOCTYPE x [<!ENTITY a "b">]>' + parts['xl/workbook.xml']},
            'bomb': {**parts, 'xl/media/image1.png': b'\0' * (5 * 1024 * 1024)},
        }
        m = mapping.get('standard')
        for label, p in cases.items():
            with self.subTest(label):
                with self.assertRaises(reader.WorkbookRefused):
                    reader.open_book(self._zip(p), engine.extents(m))
        with self.assertRaises(reader.WorkbookRefused):
            reader.open_book(b'not a zip at all', engine.extents(m))
        # A VBA project in an .xlsx is left behind in the rebuild, never opened (handoverpack/tests/test_package.py)
        clean, removed = reader.prepare(self._zip({**parts, 'xl/vbaProject.bin': b'x' * 100}), '.xlsx')
        self.assertEqual(removed, 1)
        self.assertNotIn(b'vbaProject', clean)

    def test_size_cap(self):
        with self.assertRaises(reader.WorkbookRefused):
            reader.read_bytes(io.BytesIO(b'x' * (reader.MAX_BYTES + 1)))

    def test_not_the_pricing_workbook(self):
        from openpyxl import Workbook
        buf = io.BytesIO()
        Workbook().save(buf)
        m = mapping.get('standard')
        with self.assertRaises(ValueError):
            engine.run(reader.open_book(buf.getvalue(), engine.extents(m)), m)
