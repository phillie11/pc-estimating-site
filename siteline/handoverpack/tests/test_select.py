"""
The supplier-select pricing document (mapping 'supplier-select', calc select-v1): taking the macros out of
an .xlsm, recalculating and reconciling the layout, figures marked as Excel's, and its gap checks.

Workbooks are built in code by handoverpack.sample_select with the layout's formulas, cell for cell, and
the values Excel would have saved worked out separately; with_macros() adds a VBA project, an ActiveX
button and a form control as Excel saves an .xlsm. Every name and figure is invented.
"""
import copy
import io
import zipfile
from decimal import Decimal as D
from functools import lru_cache

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from handoverpack import engine, mapping, reader, sample, sample_select, services
from handoverpack.models import HandoverPack, OpenItem, PricingSettings, PricingWorkbook
from handoverpack.sample_select import line
from projects.models import Project
from tenancy.context import org_context

from .test_views import Base

M = mapping.get('supplier-select')


@lru_cache(maxsize=None)
def demo(**kw):
    return sample_select.build(sample_select.DEMO, **kw)


def run(data, tolerance=D(0)):
    return engine.run(reader.open_book(data, engine.extents(M)), M, tolerance)


def spec(**changes):
    s = copy.deepcopy(sample_select.DEMO)
    for key, value in changes.items():
        s[key] = value
    return s


def trade(s, name, **changes):
    s['trades'][name].update(changes)
    return s


def gaps(result, code, trade=None):
    return [g for g in result['gaps'] if g['code'] == code and (trade is None or g['trade'] == trade)]


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


class StripTests(SimpleTestCase):
    def test_the_stripped_copy_has_no_macros_or_controls_and_keeps_every_value(self):
        plain = demo()
        xlsm = sample_select.with_macros(plain)
        before = parts(xlsm)
        self.assertIn('xl/vbaProject.bin', before)
        self.assertIn(b'macroEnabled', before['[Content_Types].xml'])
        clean, removed = reader.strip_macros(xlsm)
        self.assertEqual(removed, 6)  # VBA project, signature, ActiveX control and binary, form control, VML
        after = parts(clean)
        names = [n.lower() for n in after]
        self.assertFalse([n for n in names if 'vba' in n or n.startswith(('xl/activex/', 'xl/ctrlprops/'))])
        types = after['[Content_Types].xml']
        for word in (b'macroEnabled', b'vbaProject', b'activeX', b'controlproperties'):
            self.assertNotIn(word, types)
        self.assertIn(b'spreadsheetml.sheet.main+xml', types)
        for name, raw in after.items():
            if name.endswith('.rels'):
                self.assertNotRegex(raw, rb'vbaProject|activeX|ctrlProp|relationships/control"', name)
            if name.startswith('xl/worksheets/'):
                self.assertNotIn(b'<controls', raw, name)
        self.assertEqual(reader.clean(clean)[1], 0)  # nothing left to remove
        # Opens in openpyxl, every sheet, every saved value and formula as it was
        for data_only in (True, False):
            a = openpyxl.load_workbook(io.BytesIO(plain), data_only=data_only)
            b = openpyxl.load_workbook(io.BytesIO(clean), data_only=data_only)
            self.assertEqual(a.sheetnames, b.sheetnames)
            for ws in a.worksheets:
                got = {c.coordinate: c.value for row in b[ws.title].iter_rows() for c in row if c.value is not None}
                want = {c.coordinate: c.value for row in ws.iter_rows() for c in row if c.value is not None}
                if data_only:
                    self.assertEqual(got, want, ws.title)
                else:
                    self.assertEqual(set(got), set(want), ws.title)
        self.assertEqual(run(clean)['totals'], run(plain)['totals'])

    def test_a_plain_workbook_named_xlsm_passes_through(self):
        clean, removed = reader.prepare(demo(), '.xlsm')
        self.assertEqual(removed, 1)  # the comment's VML drawing (the comment stays)
        self.assertEqual(reader.clean(clean)[1], 0)

    def test_an_xlsx_with_macros_inside_is_stripped_the_same_way(self):
        """One rule whatever the name: the active content goes, and only the rebuilt copy is read or stored."""
        data = sample_select.with_macros(demo())
        self.assertEqual(reader.prepare(data, '.xlsx'), reader.prepare(data, '.xlsm'))
        reader.open_book(data, engine.extents(M))  # open_book rebuilds whatever it is given

    def test_the_usual_attacks_are_refused(self):
        p = parts(sample_select.with_macros(demo()))
        cases = {
            'dtd': {**p, 'xl/workbook.xml': b'<!DOCTYPE x [<!ENTITY a "b">]>' + p['xl/workbook.xml']},
            'bomb': {**p, 'xl/media/image1.png': b'\0' * (5 * 1024 * 1024)},
        }
        for label, contents in cases.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, data in contents.items():
                    z.writestr(name, data)
            with self.subTest(label), self.assertRaises(reader.WorkbookRefused):
                reader.strip_macros(buf.getvalue())
        with self.assertRaises(reader.WorkbookRefused):
            reader.strip_macros(b'not a zip')

    def test_names(self):
        for name in ('a.xls', 'a.xlsb', 'a.ods', 'a.csv', 'a.xltm'):
            with self.subTest(name), self.assertRaises(reader.WorkbookRefused):
                reader.check_name(name)
        self.assertEqual(reader.check_name('IPD rev 2.XLSM'), '.xlsm')
        self.assertEqual(reader.xlsx_name('IPD rev 2.xlsm'), 'IPD rev 2.xlsx')
        self.assertEqual(reader.xlsx_name('IPD.xlsx'), 'IPD.xlsx')


class SelectEngineTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = run(demo())

    def test_demo_reconciles_to_the_penny_with_no_issues(self):
        t = self.r['totals']
        self.assertTrue(self.r['cached'])
        self.assertEqual(self.r['issues'], [])
        self.assertTrue(t['reconciled'])
        self.assertEqual(t['difference'], D(0))
        self.assertEqual(t['charge'], t['excel_charge'])
        self.assertEqual(self.r['mapping'], 'supplier-select')

    def test_line_arithmetic_is_the_layouts(self):
        trades = {t['name']: t for t in self.r['trades']}
        f1 = trades['Flooring']['lines'][0]
        self.assertEqual(f1['cost'], D('532'))           # 14 x (26 + 12)
        self.assertEqual(f1['tender_rate'], D('41.8'))   # (1 + 10%) x 38
        self.assertEqual(f1['charge'], D('585.2'))
        self.assertEqual(trades['Sprinklers']['charge'], D('12000'))  # markup 0 on the client's PS
        e3 = trades['Electrical']['lines'][2]
        self.assertEqual((e3['rate'], e3['adj'], e3['cls']), (D(0), D('9600'), engine.ALLOWANCE))
        self.assertEqual(trades['Cleaning']['lines'][0]['cls'], engine.ALLOWANCE)  # an own-allowance "supplier"
        self.assertEqual(trades['Sprinklers']['lines'][0]['cls'], engine.PROVISIONAL)
        self.assertEqual(trades['Builderswork']['lines'][4]['cls'], engine.INCLUDED)
        self.assertFalse(trades['Wood Flooring']['include'])
        # No Include? column: a trade with a supplier or a price is included
        self.assertTrue(all(trades[n]['include'] for n in ('Builderswork', 'Electrical', 'Sprinklers')))

    def test_joinery_and_prelims(self):
        j, p = self.r['joinery'], self.r['prelims']
        self.assertEqual(j['supply_cost'], D('18') + D('1180') + D('120') + D('640.5') + D('85') + D('60') + D('920')
                         + D('260') + D('1600'))
        self.assertEqual(j['install_charge'], D('2250'))
        self.assertEqual(p['markup'], D('0.85'))
        site_manager = next(ln for ln in p['lines'] if ln['description'] == 'Site Manager')
        self.assertEqual(site_manager['cost'], D('7200'))  # =I*F*D: (48000 x 1.3 / 52) x 6 weeks x 1
        self.assertEqual(site_manager['charge'], D('13320'))
        self.assertEqual(p['charge'], p['cost'] * D('1.85'))
        nil = next(ln for ln in p['lines'] if ln['description'] == 'Project Manager')
        self.assertEqual(nil['cls'], engine.EXCLUDED)

    def test_figures_from_excel_are_marked(self):
        where = [x['where'] for x in self.r['from_excel']]
        self.assertIn('Electrical!Y:Z', where)            # the FILTER of the selected supplier's columns
        self.assertIn('1!R2:T14', where)                   # a workshop sheet's build-up
        self.assertTrue(any(w.startswith('Prelim Book!K') for w in where))  # a prelim line with ROUND()
        trades = {t['name']: t for t in self.r['trades']}
        self.assertTrue(trades['Electrical']['from_excel'])
        self.assertEqual(trades['Electrical']['lines'][0]['from_excel'], ['rate', 'adj'])

    def test_clarifications_are_numbered_headings(self):
        c = self.r['clarifications']
        self.assertEqual([x['ref'] for x in c], ['1', '2', '3'])
        self.assertEqual(c[1]['trade'], 'Access')
        self.assertIn('out of hours', c[1]['text'])

    def test_without_saved_values_the_selection_is_worked_out(self):
        r = run(demo(cached=False))
        self.assertFalse(r['cached'])
        self.assertIsNone(r['totals']['reconciled'])
        trades = {t['name']: t for t in r['trades']}
        self.assertEqual(trades['Flooring']['cost'], {t['name']: t for t in self.r['trades']}['Flooring']['cost'])

    def test_a_stale_filter_copy_is_noted_and_excels_values_used(self):
        r = run(sample_select.build(sample_select.DEMO, typed={('Tiling', 'Y4'): D('60')}))
        self.assertTrue(any('Tiling' in n and 'differs' in n for n in r['notes']))

    def test_the_pack_builds_from_it(self):
        from handoverpack import pack
        self.assertTrue(pack.SECTIONS)
        excluded_why = [tr.get('why_excluded') for tr in self.r['trades'] if not tr['include']]
        self.assertTrue(excluded_why and all(excluded_why))


class SelectGapTests(SimpleTestCase):
    def test_items_but_no_supplier_selected(self):
        r = run(sample_select.build(trade(spec(), 'Plumbing', selected='')))
        g = gaps(r, 'no_supplier', 'Plumbing')
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]['severity'], 'bad')
        self.assertEqual(g[0]['amount'], D('3288'))  # the cheapest supplier's total
        self.assertTrue(r['totals']['reconciled'])  # the workbook agrees with itself; it is just missing a price
        plumbing = next(t for t in r['trades'] if t['name'] == 'Plumbing')
        self.assertFalse(plumbing['include'])

    def test_selected_name_not_in_row_2(self):
        r = run(sample_select.build(trade(spec(), 'Plumbing', selected='Somebody Else Ltd')))
        self.assertEqual(len(gaps(r, 'supplier_not_found', 'Plumbing')), 1)

    def test_rates_only_in_an_unselected_slot(self):
        s = spec()
        s['trades']['Mechanical']['lines'].append(line('M4', 'Controls and commissioning', 1, 'Item', (None, None, ''),
                                                       (1450, None, 'Northfield only')))
        r = run(sample_select.build(s))
        g = gaps(r, 'unselected_rates', 'Mechanical')
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]['amount'], D('1450'))
        m4 = next(t for t in r['trades'] if t['name'] == 'Mechanical')['lines'][-1]
        self.assertEqual(m4['cls'], engine.REVIEW)

    def test_provisional_sum_in_an_unselected_slot(self):
        s = spec()
        s['trades']['Electrical']['lines'].append(line('E5', 'Provisional sum for landlord works', 1, 'Item',
                                                       (None, None, ''), (5000, None, 'PS')))
        r = run(sample_select.build(s))
        g = gaps(r, 'ps_unselected', 'Electrical')
        self.assertEqual((len(g), g[0]['severity'], g[0]['amount']), (1, 'bad', D('5000')))
        self.assertEqual(gaps(r, 'unselected_rates', 'Electrical'), [])

    def test_a_provisional_sum_line_left_empty(self):
        s = spec()
        s['trades']['Electrical']['lines'].append(line('E6', 'Provisional sum for the landlord works, to be confirmed', 1, 'Item'))
        self.assertEqual(len(gaps(run(sample_select.build(s)), 'ps_unpriced', 'Electrical')), 1)

    def test_workshop_delivery_and_install_not_reaching_joinery_summary(self):
        s = spec()
        s['workshops'][0]['delivery'] = (D('90'), D('180'))
        s['workshops'][1]['links'] = {'F': 'R11', 'G': 'T11'}  # install left off the summary
        r = run(sample_select.build(s))
        self.assertTrue(r['totals']['reconciled'])  # nothing is mis-added: it is simply not carried
        d = gaps(r, 'joinery_delivery_not_carried')
        self.assertEqual((len(d), d[0]['sheet'], d[0]['cell'], d[0]['amount']), (1, '1', 'T13', D('180')))
        i = gaps(r, 'joinery_install_not_carried')
        self.assertEqual((len(i), i[0]['sheet'], i[0]['amount']), (1, '2', D('500')))
        # Linking the sub total with delivery in it (R16/T16) carries it
        s['workshops'][0]['links'] = {'F': 'R16', 'G': 'T16'}
        self.assertEqual(gaps(run(sample_select.build(s)), 'joinery_delivery_not_carried'), [])

    def test_a_workshop_sheet_nothing_links_to(self):
        s = spec()
        s['workshops'][2]['links'] = {}
        g = gaps(run(sample_select.build(s)), 'joinery_sheet_not_carried')
        self.assertEqual((len(g), g[0]['sheet']), (1, '3'))

    def test_margin_below_the_sheets_own_target(self):
        r = run(sample_select.build(spec(target=D('0.25'))))
        g = gaps(r, 'margin_below_target')
        self.assertEqual(len(g), 1)
        self.assertIn('25.0%', g[0]['title'])
        self.assertEqual(g[0]['cell'], 'G24')
        self.assertEqual(gaps(run(demo()), 'margin_below_target'), [])
        self.assertEqual(gaps(run(sample_select.build(spec(target=D('0.25')))), 'margin_below_target')[0]['amount'],
                         r['totals']['price_at_target'] - r['totals']['charge'])
        # Within the company's tolerance: not flagged
        self.assertEqual(gaps(run(sample_select.build(spec(target=D('0.18'))), D('0.01')), 'margin_below_target'), [])

    def test_zero_markup_on_a_trade_that_is_not_a_provisional_sum(self):
        r = run(sample_select.build(trade(spec(), 'Tiling', markup=D('0'))))
        self.assertTrue(r['totals']['reconciled'])
        self.assertEqual(len(gaps(r, 'zero_markup', 'Tiling')), 1)
        self.assertEqual(gaps(r, 'zero_markup', 'Sprinklers'), [])

    def test_provisional_sums_at_face_value_are_information(self):
        r = run(demo())
        ps = gaps(r, 'ps_face_value', 'Sprinklers')
        self.assertEqual((len(ps), ps[0]['severity'], ps[0]['amount']), (1, 'info', D('12000')))
        joinery = gaps(r, 'ps_face_value', 'Joinery')
        self.assertEqual((len(joinery), joinery[0]['severity']), (1, 'info'))

    def test_client_document_total(self):
        missing = run(demo())
        self.assertEqual(len(gaps(missing, 'client_total_missing')), 1)
        t = missing['totals']['charge']
        ok = run(sample_select.build(spec(client_total=t.quantize(D('0.001')))))
        self.assertEqual([g for g in ok['gaps'] if g['code'].startswith('client_total')], [])
        self.assertEqual(ok['issues'], [])
        out = run(sample_select.build(spec(client_total=D('120000'))))
        g = gaps(out, 'client_total_differs')
        self.assertEqual((len(g), g[0]['severity'], g[0]['amount']), (1, 'bad', t - D('120000')))
        err = run(sample_select.build(spec(client_total='#DIV/0!')))
        self.assertEqual(len(gaps(err, 'client_total_error')), 1)

    def test_error_values_in_header_cells_are_gaps_not_crashes(self):
        s = spec(words_error='#NAME?')
        s['header']['project_error'] = '#VALUE!'
        r = run(sample_select.build(s))
        self.assertTrue(r['totals']['reconciled'])
        self.assertEqual(r['header']['project'], '')
        self.assertEqual(gaps(r, 'header_error')[0]['cell'], 'D2')
        self.assertEqual(len([g for g in gaps(r, 'cell_error') if '#NAME?' in g['title']]), 1)

    def test_an_error_where_the_total_should_be_is_not_reconciled(self):
        r = run(sample_select.build(sample_select.DEMO, stale={('PRICING SUMMARY', 'G22'): '#REF!'}))
        self.assertIs(r['totals']['reconciled'], False)
        self.assertIsNone(r['totals']['difference'])
        self.assertEqual(r['totals']['excel_text'], '#REF!')

    def test_a_line_with_its_formulas_cleared_is_a_gap_and_does_not_reconcile(self):
        r = run(sample_select.build(sample_select.DEMO, break_formulas={'Plumbing': [5]}))
        g = gaps(r, 'formula_missing', 'Plumbing')
        self.assertEqual((len(g), g[0]['cell'], g[0]['severity']), (1, 'AA5', 'bad'))
        self.assertEqual(g[0]['amount'], D('412.6') * D('1.1'))
        self.assertIs(r['totals']['reconciled'], False)
        self.assertEqual(r['totals']['difference'], D('412.6') * D('1.1'))
        self.assertIn('Plumbing!AD5', [f'{i["sheet"]}!{i["cell"]}' for i in r['issues']])

    def test_a_stale_cell_is_a_reconciliation_issue(self):
        r = run(sample_select.build(sample_select.DEMO, stale={('Electrical', 'AD5'): D('1.00')}))
        self.assertIn('Electrical!AD5', [f'{i["sheet"]}!{i["cell"]}' for i in r['issues']])


class LayoutTests(SimpleTestCase):
    def test_both_presets_validate_and_bad_select_mappings_are_refused(self):
        for key in mapping.PRESETS:
            mapping.validate(mapping.get(key))
        for path, value in ((('summary', 'labels'), {}), (('trade', 'selected_name'), 'W'),
                            (('workshops', 'pattern'), '('), (('prelims', 'first_row'), 'one')):
            m = mapping.get('supplier-select')
            m[path[0]][path[1]] = value
            with self.subTest(path), self.assertRaises(mapping.MappingError):
                mapping.validate(m)

    def test_detect_suggests_the_layout(self):
        std = mapping.get('standard')
        for data, key in ((demo(), 'supplier-select'), (sample.build(sample.DEMO), 'standard')):
            book = reader.open_book(data, engine.extents(std))
            cells = [book.value('PRICING SUMMARY', r, c) for r in range(1, 13) for c in range(1, 12)]
            self.assertEqual(mapping.detect(book.sheetnames, cells), key)

    def test_an_error_in_the_standard_layouts_header_is_a_gap_too(self):
        s = copy.deepcopy(sample.DEMO)
        s['header']['project'] = '#VALUE!'
        m = mapping.get('standard')
        r = engine.run(reader.open_book(sample.build(s), engine.extents(m)), m)
        self.assertTrue(r['totals']['reconciled'])
        self.assertEqual(r['header']['project'], '')
        self.assertEqual([g['cell'] for g in gaps(r, 'header_error')], ['D4'])


def xlsm_file(data=None, name='Pricing document rev 2.xlsm'):
    return SimpleUploadedFile(name, data or sample_select.with_macros(demo()), content_type='application/octet-stream')


class SelectUploadTests(Base):
    def win(self):
        self.login(self.alice)
        return self.client.post(self.url('project_handover', self.bid.pk), {
            'manager': self.pat.pk, 'value': '121548.31', 'start_date': '2026-12-01',
            'completion_date': '2027-03-12', 'handover_notes': 'Over to you'})

    def use_layout(self, key='supplier-select'):
        with org_context(self.org_a):
            PricingSettings.objects.update_or_create(organization=self.org_a, defaults={'preset': key})

    def test_owner_picks_the_layout_in_settings(self):
        self.login(self.alice)
        r = self.client.get(self.url('hpack_settings'))
        self.assertContains(r, 'supplier-select')
        self.client.post(self.url('hpack_settings'), {'preset': 'supplier-select', 'margin_tolerance': '0',
                                                      'item_due_days': '14', 'assign_to_manager': 'on',
                                                      'unchecked_issue': 'BLOCK'})
        with org_context(self.org_a):
            self.assertEqual(PricingSettings.objects.get(organization=self.org_a).preset, 'supplier-select')

    def test_xlsm_upload_stores_only_the_stripped_xlsx(self):
        self.use_layout()
        self.login(self.eve)
        r = self.client.post(self.url('hpack_upload', self.bid.pk), {'file': xlsm_file()})
        with org_context(self.org_a):
            wb = PricingWorkbook.objects.get(project=self.bid)
        self.assertRedirects(r, wb.get_absolute_url())
        self.assertTrue(wb.reconciled)
        self.assertEqual(wb.mapping_key, 'supplier-select')
        self.assertEqual(wb.original_name, 'Pricing document rev 2.xlsx')
        self.assertTrue(wb.file.name.endswith('.xlsx'))
        with wb.file.open('rb') as fh:
            stored = fh.read()
        self.assertEqual(reader.sha256(stored), wb.sha256)
        self.assertEqual(len(stored), wb.size)
        names = [n.lower() for n in parts(stored)]
        self.assertFalse([n for n in names if 'vba' in n or 'activex' in n or 'ctrlprop' in n])
        self.assertNotIn(b'macroEnabled', parts(stored)['[Content_Types].xml'])
        book = openpyxl.load_workbook(io.BytesIO(stored), data_only=True)
        self.assertIn('PRICING SUMMARY', book.sheetnames)
        self.assertEqual(services.pennies(D(repr(book['PRICING SUMMARY']['G22'].value))), wb.excel_charge)
        self.assertTrue(any('.xlsm' in n for n in wb.result['notes']))
        # The download is the .xlsx
        r = self.client.get(self.url('hpack_file', self.bid.pk, wb.pk))
        self.assertEqual(r.status_code, 200)
        self.assertIn('.xlsx', r['Content-Disposition'])
        # The pages show what came from Excel
        page = self.client.get(wb.get_absolute_url())
        self.assertContains(page, 'hp-from-excel')
        self.assertContains(self.client.get(self.url('hpack_pack', self.bid.pk)), 'Unit 4 Abbey Court')
        export = self.client.get(self.url('hpack_pack', self.bid.pk) + '?export=xlsx')
        self.assertEqual(export.status_code, 200)
        self.assertIn('Price summary', openpyxl.load_workbook(io.BytesIO(export.content)).sheetnames)
        self.assertEqual(self.client.get(self.url('hpack_items', self.bid.pk)).status_code, 200)

    def test_issue_needs_it_to_reconcile(self):
        self.use_layout()
        broken = sample_select.with_macros(sample_select.build(sample_select.DEMO, break_formulas={'Plumbing': [5]}))
        with org_context(self.org_a):
            wb = services.upload(self.bid, self.eve, xlsm_file(broken))
            self.assertIs(wb.reconciled, False)
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_SUBMITTED)
        self.win()
        self.login(self.pat)
        self.client.post(self.url('hpack_issue', self.bid.pk))
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.bid)
            self.assertFalse(pack.is_issued)
            self.assertTrue(OpenItem.objects.filter(pack=pack, code='formula_missing', status=OpenItem.OPEN).exists())
        self.client.post(self.url('hpack_upload', self.bid.pk), {'file': xlsm_file()})
        self.client.post(self.url('hpack_issue', self.bid.pk))
        with org_context(self.org_a):
            pack.refresh_from_db()
            self.assertTrue(pack.is_issued)

    def test_the_other_layout_is_suggested_and_the_setting_decides(self):
        self.login(self.eve)  # the company is on the standard layout
        r = self.client.post(self.url('hpack_upload', self.bid.pk), {'file': xlsm_file()}, follow=True)
        with org_context(self.org_a):
            wb = PricingWorkbook.objects.filter(project=self.bid).first()
        if wb is not None:
            self.assertEqual(wb.mapping_key, 'standard')
            self.assertEqual(wb.result['suggested_layout']['key'], 'supplier-select')
        self.assertContains(r, 'Pricing document (supplier-select layout)')
        # And the other way round
        self.use_layout()
        r = self.client.post(self.url('hpack_upload', self.bid.pk),
                             {'file': SimpleUploadedFile('std.xlsx', sample.build(sample.DEMO))}, follow=True)
        self.assertContains(r, 'Standard pricing workbook')
