"""
The engine findings of the independent review of the supplier-select layout (F9 to F13, and the design limit on
the Total formula), one regression test or more each. Workbooks from handoverpack.sample_select and sample,
invented names and figures. `typed` puts a formula or value in a cell; `stale` then gives a cell the value Excel
would show, so a formula can be changed while its saved value stays.
"""
import io
import zipfile
from decimal import Decimal as D
from functools import lru_cache
from types import SimpleNamespace

from django.test import SimpleTestCase

from handoverpack import engine, mapping, reader, sample, sample_select, services
from projects.models import Project

M = mapping.get('supplier-select')
S = sample_select.SUMMARY


def run(data, m=M):
    return engine.run(reader.open_book(data, engine.extents(m)), m)


@lru_cache(maxsize=None)
def base():
    return run(sample_select.build(sample_select.DEMO))


def build(**kw):
    return sample_select.build(sample_select.DEMO, **kw)


def gaps(r, code):
    return [g for g in r['gaps'] if g['code'] == code]


def where(r):
    return [f'{i["sheet"]}!{i["cell"]}' for i in r['issues']]


def with_calc(data, attrs):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for i in zin.infolist():
            raw = zin.read(i)
            if i.filename == 'xl/workbook.xml':
                raw = raw.replace(b'<calcPr calcId="191029"/>', b'<calcPr calcId="191029" ' + attrs + b'/>')
            z.writestr(i, raw)
    return out.getvalue()


class StaleFilterTests(SimpleTestCase):
    """F9: a selected supplier's rate changed without recalculating leaves Excel's FILTER copy stale."""

    def test_a_stale_copy_is_a_reconciliation_issue_and_not_reconciled(self):
        r = run(build(typed={('Tiling', 'F4'): D('999')}))  # the selected supplier's rate, not recalculated
        self.assertIs(r['totals']['reconciled'], False)
        self.assertEqual(r['totals']['difference'], D(0))  # the totals still agree: it is the cells that don't
        self.assertIn('Tiling!Y4', where(r))
        issue = next(i for i in r['issues'] if i['cell'] == 'Y4')
        self.assertEqual((issue['computed'], issue['excel']), (D('999'), D('64.2')))

    def test_issuing_is_blocked_with_a_reason_that_fits(self):
        wb = SimpleNamespace(reconciled=False, difference=D('0.00'), result={'totals': {}})
        pack = SimpleNamespace(is_issued=False)
        project = SimpleNamespace(stage=Project.STAGE_WON, organization=None)
        self.assertIn('cell by cell', services.why_not_issue(pack, wb, project))


class CalculationModeTests(SimpleTestCase):
    """F9: a workbook on manual calculation, or flagged to recalculate when opened, is not checked."""

    def test_manual_and_full_calc_on_load(self):
        for attrs in (b'calcMode="manual"', b'fullCalcOnLoad="1"'):
            with self.subTest(attrs):
                r = run(with_calc(build(), attrs))
                self.assertIsNone(r['totals']['reconciled'])
                self.assertIn('recalculate', r['totals']['unchecked_reason'])
                self.assertTrue(r['notes'][0].startswith('The workbook'))
        self.assertTrue(base()['totals']['reconciled'])  # Excel's own calcPr: checked

    def test_the_standard_layout_too(self):
        m = mapping.get('standard')
        r = run(with_calc(sample.build(sample.DEMO), b'calcMode="manual"'), m)
        self.assertIsNone(r['totals']['reconciled'])

    def test_it_can_never_be_issued_whatever_the_setting(self):
        wb = SimpleNamespace(reconciled=None, difference=None, result={'totals': {'unchecked_reason': 'Set it to '
                                                                                  'automatic.'}})
        pack = SimpleNamespace(is_issued=False)
        project = SimpleNamespace(stage=Project.STAGE_WON, organization=None)
        self.assertEqual(services.why_not_issue(pack, wb, project), 'Set it to automatic.')
        rows = dict(services.item_rows(None, {'totals': {**base()['totals'], 'reconciled': None,
                                                         'unchecked_reason': 'Set it to automatic.'},
                                              'issues': [], 'gaps': []}))
        self.assertEqual(rows['rec:calc']['severity'], 'bad')


class ArithmeticTests(SimpleTestCase):
    """F10: huge exponents never escape as decimal.Overflow; F13: references beyond what was read aren't guessed."""

    def test_huge_numbers_fall_back_to_excels_value(self):
        sm = next(ln for ln in base()['prelims']['lines'] if ln['description'] == 'Site Manager')
        for formula in ('=1E+999999999', '=1E+900000*1E+900000', '=1/1E-999999999', '=I11*1E+20'):
            with self.subTest(formula):
                r = run(build(typed={('Prelim Book', 'K11'): formula}, stale={('Prelim Book', 'K11'): sm['cost']}))
                line = next(ln for ln in r['prelims']['lines'] if ln['row'] == 11)
                self.assertEqual((line['cost'], line['from_excel']), (sm['cost'], ['cost']))
                self.assertTrue(r['totals']['reconciled'])

    def test_a_reference_beyond_the_read_extent(self):
        sm = next(ln for ln in base()['prelims']['lines'] if ln['description'] == 'Site Manager')
        r = run(build(typed={('Prelim Book', 'K11'): '=I11*F11*Z500'}, stale={('Prelim Book', 'K11'): sm['cost']}))
        self.assertIn('Prelim Book!K11', [x['where'] for x in r['from_excel']])
        self.assertTrue(r['totals']['reconciled'])
        self.assertFalse(reader.open_book(build(), engine.extents(M)).within('Prelim Book', 500, 26))


class SummaryRowTests(SimpleTestCase):
    """F11: labels match as whole words; the total is the one that sums the trade block; trades below it flagged."""
    TOTAL = 22  # the demo's Total row

    def test_a_label_that_only_starts_with_total(self):
        r = run(build(typed={(S, 'D17'): 'Totally separate note', (S, 'G17'): D('0')}))
        self.assertEqual(r['totals']['charge_cell'], f'G{self.TOTAL}')
        self.assertTrue(r['totals']['reconciled'])

    def test_the_total_that_sums_the_block_is_chosen(self):
        r = run(build(typed={(S, 'D17'): 'Total', (S, 'G17'): D('0')}))
        self.assertEqual(r['totals']['charge_cell'], f'G{self.TOTAL}')
        self.assertTrue(r['totals']['reconciled'])

    def test_a_trade_row_below_the_total(self):
        r = run(build(typed={(S, 'D30'): "='Wood Flooring'!B2", (S, 'G30'): D('500')}))
        g = gaps(r, 'trade_below_total')
        self.assertEqual((len(g), g[0]['severity'], g[0]['amount']), (1, 'bad', D('500')))
        self.assertTrue(r['totals']['reconciled'])  # not in Excel's total, not in the pack's


class TotalFormulaTests(SimpleTestCase):
    """Design limit: the Total must be a SUM over the summary rows; anything else is a reconciliation issue."""

    def test_supplier_select(self):
        charge = base()['totals']['excel_charge']
        r = run(build(typed={(S, f'G{SummaryRowTests.TOTAL}'): '=G7+G8+G9'},
                      stale={(S, f'G{SummaryRowTests.TOTAL}'): charge}))
        self.assertIs(r['totals']['reconciled'], False)
        self.assertIn(f'{S}!G{SummaryRowTests.TOTAL}', where(r))
        short = run(build(typed={(S, f'G{SummaryRowTests.TOTAL}'): '=SUM(G7:G15)'},
                          stale={(S, f'G{SummaryRowTests.TOTAL}'): charge}))
        self.assertIs(short['totals']['reconciled'], False)

    def test_standard(self):
        m = mapping.get('standard')
        good = run(sample.build(sample.DEMO), m)
        self.assertTrue(good['totals']['reconciled'])
        r = run(sample.build(sample.DEMO, typed={('PRICING SUMMARY', 'G50'): '=G11+G12'},
                             stale={('PRICING SUMMARY', 'G50'): good['totals']['excel_charge']}), m)
        self.assertIs(r['totals']['reconciled'], False)
        self.assertIn('PRICING SUMMARY!G50', where(r))


class MissingTabTests(SimpleTestCase):
    """F12: a summary row whose tab is missing contributes Excel's figures, and they are listed as such."""

    def test_missing_tab_figures_are_from_excel(self):
        r = run(build(typed={(S, 'D16'): "='Gone Trade'!B2", (S, 'E16'): D('100'), (S, 'G16'): D('110')}))
        trade = next(t for t in r['trades'] if t['sheet'] == 'Gone Trade')
        self.assertTrue(trade['missing'])
        self.assertEqual((trade['cost'], trade['charge'], trade['from_excel']), (D('100'), D('110'), True))
        self.assertIn(f'{S}!E16:G16', [x['where'] for x in r['from_excel']])
        self.assertEqual(len(gaps(r, 'missing_sheet')), 1)
