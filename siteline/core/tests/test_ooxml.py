"""
core.ooxml.clean, against the second independent review's findings (items 1 to 14), one regression test or more
each, and a round trip of an ordinary workbook that must come out with everything a quote or pricing workbook uses.
Workbooks are built here with openpyxl and then tampered with as the review's probes did; every name and address
is a placeholder.
"""
import io
import time
import zipfile
from unittest import mock

import openpyxl
from django.test import SimpleTestCase

from core import ooxml

S = 'xl/worksheets/sheet1.xml'
WS_RELS = 'xl/worksheets/_rels/sheet1.xml.rels'
WB_RELS = 'xl/_rels/workbook.xml.rels'
R = b'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MAIN = b'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def base(setup=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws['A1'], ws['B1'] = 'x', '=1+1'
    if setup:
        setup(wb, ws)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def files(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def build(f):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for n, b in f.items():
            z.writestr(n, b)
    return out.getvalue()


def edited(change, data=None):
    f = files(data or base())
    change(f)
    return build(f)


def refused(test, data, kind):
    with test.assertRaises(ooxml.Refused) as e:
        ooxml.clean(data)
    test.assertEqual(e.exception.kind, kind)
    return e.exception


class TextAndNestingTests(SimpleTestCase):
    """Items 1 to 3: text is kept as pieces, walks are iterative, nesting is capped and never a RecursionError."""

    def test_lots_of_small_text_pieces_is_linear(self):
        import random
        rnd = random.Random(1)  # varied, so the part isn't refused for packing too well
        text = b''.join(rnd.choice(b'abcdefghijklmnopqrstuvwxyz').to_bytes(1, 'big') + b'&#10;' for _ in range(100000))
        data = edited(lambda f: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
            b'</workbook>', b'<extLst><ext uri="u"><x>' + text + b'</x></ext></extLst></workbook>')))
        t = time.monotonic()
        ooxml.clean(data)
        self.assertLess(time.monotonic() - t, 5)
        with mock.patch.object(ooxml, 'MAX_TEXT', 1000):
            refused(self, data, 'size')

    def test_deep_nesting_is_refused_never_a_crash(self):
        for depth, kind in ((100, 'layout'), (5000, 'layout'), (60000, 'size')):
            data = edited(lambda f, d=depth: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
                b'</workbook>', b'<extLst><ext uri="u">' + b'<x>' * d + b'</x>' * d + b'</ext></extLst></workbook>')))
            with self.subTest(depth):
                refused(self, data, kind)
        sheet = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'</worksheet>', b'<extLst><ext uri="u">' + b'<x>' * 2000 + b'</x>' * 2000 + b'</ext></extLst></worksheet>')))
        refused(self, sheet, 'layout')

    def test_a_recursion_error_is_refused(self):
        with mock.patch.object(ooxml, '_clean', side_effect=RecursionError):
            refused(self, base(), 'layout')

    def test_iterative_walk_and_one_pass_flags(self):
        root = ooxml.parse(b'<a>' + b'<b>' * 60 + b'<c/>' + b'</b>' * 60 + b'</a>')
        self.assertEqual(sum(1 for _ in root.walk()), 62)
        flags = ooxml._flags(root, lambda n: n.name()[1] == 'c')
        self.assertTrue(flags[id(root)])

    def test_namespace_declarations_are_capped(self):
        body = ''.join(f'<y xmlns:a{i}="urn:a"/>' for i in range(ooxml.MAX_NAMESPACES + 10)).encode()
        with self.assertRaises(ooxml.Refused):
            ooxml.parse(b'<r>' + body + b'</r>')


class SheetStreamingTests(SimpleTestCase):
    """Items 4 and 6: every sheet is streamed and filtered; one sheetData, in the spreadsheet namespace."""
    WEB = (b'<webPublishItems count="1"><webPublishItem id="1" divId="d" sourceType="sheet" destinationFile="out.htm"'
           b' autoRepublish="1"/></webPublishItems>')

    def test_web_publish_items_go_from_a_sheet_with_no_relationships(self):
        out = files(ooxml.clean(edited(lambda f: f.__setitem__(S, f[S].replace(b'</worksheet>', self.WEB
                                                                                 + b'</worksheet>'))))[0])
        self.assertNotIn(b'webPublishItem', out[S])

    def test_a_second_or_foreign_sheet_data(self):
        foreign = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'</sheetData>', b'</sheetData><sheetData xmlns="urn:placeholder">' + self.WEB.replace(
                b'<webPublishItems ', b'<webPublishItems xmlns="' + MAIN + b'" ') + b'</sheetData>')))
        refused(self, foreign, 'layout')
        second = edited(lambda f: f.__setitem__(S, f[S].replace(b'</sheetData>', b'</sheetData><sheetData/>')))
        refused(self, second, 'layout')
        foreign_cell = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'<sheetData>', b'<sheetData><e:row xmlns:e="urn:placeholder"/>')))
        refused(self, foreign_cell, 'layout')
        nested = edited(lambda f: f.__setitem__(S, f[S].replace(b'</worksheet>', b'<extLst><sheetData/></extLst>'
                                                                                  b'</worksheet>')))
        refused(self, nested, 'layout')

    def test_markup_compatibility(self):
        mc = b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        process = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'<worksheet ', b'<worksheet ' + mc + b' xmlns:e="urn:placeholder" mc:Ignorable="e" '
            b'mc:ProcessContent="e:sheetData" ')))
        refused(self, process, 'layout')
        foreign = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'<worksheet ', b'<worksheet ' + mc + b' xmlns:e="urn:placeholder" mc:Ignorable="e" ')))
        refused(self, foreign, 'layout')
        office = edited(lambda f: f.__setitem__(S, f[S].replace(
            b'<worksheet ', b'<worksheet ' + mc + b' xmlns:x14ac="http://schemas.microsoft.com/office/'
            b'spreadsheetml/2009/9/ac" mc:Ignorable="x14ac" ')))
        ooxml.clean(office)


class EncodingTests(SimpleTestCase):
    """Item 5: XML that isn't UTF-8 is refused, with or without a byte-order mark; a DTD anywhere is refused."""

    def test_utf16_parts(self):
        sheet = files(base())[S].decode()
        theme = files(base())['xl/theme/theme1.xml'].decode().split('?>', 1)[-1]
        cases = {
            'sheet utf-16 with mark': (S, ('<?xml version="1.0" encoding="UTF-16"?>' + sheet.split('?>', 1)[-1])
                                       .encode('utf-16')),
            'theme utf-16le, no mark': ('xl/theme/theme1.xml', ('<?xml version="1.0" encoding="UTF-16"?>' + theme)
                                        .encode('utf-16-le')),
            'theme utf-16be, no mark': ('xl/theme/theme1.xml', theme.encode('utf-16-be')),
            'declared latin-1': ('xl/theme/theme1.xml', ('<?xml version="1.0" encoding="ISO-8859-1"?>' + theme)
                                 .encode('latin-1')),
        }
        for label, (name, raw) in cases.items():
            with self.subTest(label):
                refused(self, edited(lambda f, n=name, r=raw: f.__setitem__(n, r)), 'encoding')

    def test_a_dtd_in_a_copied_part(self):
        data = edited(lambda f: f.__setitem__('xl/theme/theme1.xml', b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a '
                                              b'"b">]>' + f['xl/theme/theme1.xml'].split(b'?>', 1)[-1]))
        refused(self, data, 'dtd')


class HyperlinkTests(SimpleTestCase):
    """Item 7: external hyperlinks only from sheets, drawings and charts, and only to http, https or mailto."""

    def links(self, targets):
        def change(f):
            f[S] = f[S].replace(b'<pageMargins', b'<hyperlinks xmlns:r="' + R + b'">' + b''.join(
                b'<hyperlink ref="A%d" r:id="rId%d"/>' % (i + 1, i + 1) for i in range(len(targets)))
                + b'</hyperlinks><pageMargins')
            f[WS_RELS] = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                          + b''.join(b'<Relationship Id="rId%d" Type="' % (i + 1) + R + b'/hyperlink" Target="'
                                     + t + b'" TargetMode="External"/>' for i, t in enumerate(targets))
                          + b'</Relationships>')
        return files(ooxml.clean(edited(change))[0])

    def test_schemes(self):
        bad = [b'file:///C:/placeholder/x.txt', b'\\\\placeholder-host\\share\\x', b'javascript:void(0)',
               b'//placeholder-host/x', b'http:no-host', b'vbscript:x']
        good = [b'https://example.com/spec', b'http://example.com/a', b'mailto:qs@example.com']
        out = self.links(bad + good)
        rels = out.get(WS_RELS, b'')
        for t in bad:
            self.assertNotIn(t, rels)
        for t in good:
            self.assertIn(t, rels)
        self.assertEqual(out[S].count(b'<hyperlink '), len(good))
        self.assertTrue(ooxml.safe_url('https://example.com') and not ooxml.safe_url('file:///x'))

    def test_from_the_workbook_and_web_images(self):
        def change(f):
            f[WB_RELS] = f[WB_RELS].replace(b'</Relationships>', b'<Relationship Id="rId99" Type="' + R + b'/hyperlink"'
                                            b' Target="http://placeholder.invalid/x" TargetMode="External"/>'
                                            b'<Relationship Id="rId98" Type="http://schemas.microsoft.com/office/2017/'
                                            b'06/relationships/rdRichValueWebImage" Target="richData/web.xml"/>'
                                            b'</Relationships>')
            f['xl/richData/web.xml'] = b'<webImagesSrd xmlns="urn:x"/>'
            f['[Content_Types].xml'] = f['[Content_Types].xml'].replace(
                b'</Types>', b'<Override PartName="/xl/richData/web.xml" ContentType="application/vnd.ms-excel.'
                b'rdrichvaluewebimage+xml"/></Types>')
        out = files(ooxml.clean(edited(change))[0])
        self.assertNotIn(b'placeholder.invalid', out[WB_RELS])
        self.assertNotIn('xl/richData/web.xml', out)


class FormulaTests(SimpleTestCase):
    """Item 9: formulas that fetch or run something refuse the upload, naming where."""

    def test_cells(self):
        for formula in (b'_xlfn.WEBSERVICE("http://placeholder.invalid/")', b'_xlfn.IMAGE("http://x.invalid/a.png")',
                        b'RTD("placeholder.progid",,"x")', b'CALL("kernel32","x","J")', b'REGISTER("a","b")',
                        b"cmd|'/c calc'!A0", b'HYPERLINK("file:///c:/x")',
                        b'FILTERXML(_xlfn.WEBSERVICE("http://x.invalid"),"//a")'):
            with self.subTest(formula):
                e = refused(self, edited(lambda f, x=formula: f.__setitem__(S, f[S].replace(b'<f>1+1</f>',
                                                                                          b'<f>' + x + b'</f>'))),
                            'fetch')
                self.assertIn("Sheet 'Sheet', cell B1", str(e))
        ooxml.clean(edited(lambda f: f.__setitem__(S, f[S].replace(
            b'<f>1+1</f>', b'<f>HYPERLINK("https://example.com/spec","Spec")</f>'))))

    def test_rules_validations_and_names(self):
        def cf(f):
            f[S] = f[S].replace(b'<pageMargins', b'<conditionalFormatting sqref="A1"><cfRule type="expression" '
                                b'priority="1"><formula>_xlfn.WEBSERVICE("http://x.invalid")</formula></cfRule>'
                                b'</conditionalFormatting><pageMargins')

        def dv(f):
            f[S] = f[S].replace(b'<pageMargins', b'<dataValidations count="1"><dataValidation type="list" sqref="A1">'
                                b'<formula1>RTD("p",,"x")</formula1></dataValidation></dataValidations><pageMargins')

        def name(f):
            f['xl/workbook.xml'] = f['xl/workbook.xml'].replace(
                b'</sheets>', b'</sheets><definedNames><definedName name="Price">_xlfn.WEBSERVICE("http://x.invalid")'
                b'</definedName></definedNames>')
        for change in (cf, dv, name):
            with self.subTest(change.__name__):
                refused(self, edited(change), 'fetch')


class PictureAndPrinterTests(SimpleTestCase):
    """Item 10: printer settings go; only PNG, JPEG and GIF pictures stay, checked by their bytes, and it says so."""

    def test_printer_settings_and_pictures(self):
        def change(f):
            f['xl/printerSettings/printerSettings1.bin'] = b'\xd0\xcf\x11\xe0' + b'PLACEHOLDER' * 50
            f['xl/media/image1.wmf'] = b'not really an image' * 10
            f['xl/media/image2.png'] = b'GIF89a pretending to be png'
            f['[Content_Types].xml'] = f['[Content_Types].xml'].replace(
                b'</Types>', b'<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.'
                b'spreadsheetml.printerSettings"/><Default Extension="wmf" ContentType="image/x-wmf"/>'
                b'<Default Extension="png" ContentType="image/png"/></Types>')
            f[WS_RELS] = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                          b'<Relationship Id="rId1" Type="' + R + b'/printerSettings" Target="../printerSettings/'
                          b'printerSettings1.bin"/><Relationship Id="rId2" Type="' + R + b'/image" Target="../media/'
                          b'image1.wmf"/><Relationship Id="rId3" Type="' + R + b'/image" Target="../media/image2.png"/>'
                          b'</Relationships>')
            f[S] = f[S].replace(b'</worksheet>', b'<pageSetup orientation="landscape" r:id="rId1" xmlns:r="' + R
                                + b'"/><picture r:id="rId2" xmlns:r="' + R + b'"/></worksheet>')
        clean, removed, notes = ooxml.clean_report(edited(change))
        out = files(clean)
        self.assertFalse([k for k in out if 'printer' in k or 'media' in k])
        self.assertIn(b'orientation="landscape"', out[S])  # the page setup stays, without its printer settings
        self.assertNotIn(b'r:id="rId1"', out[S])
        self.assertNotIn(b'<picture', out[S])
        self.assertEqual(notes, ['2 pictures in a format other than PNG, JPEG or GIF were removed.'])
        openpyxl.load_workbook(io.BytesIO(clean))

    def test_a_real_png_is_kept(self):
        from openpyxl.drawing.image import Image
        from PIL import Image as PImage

        def setup(wb, ws):
            p = io.BytesIO()
            PImage.new('RGB', (10, 10), 'red').save(p, 'PNG')
            p.seek(0)
            ws.add_image(Image(p), 'C3')
        clean, _, notes = ooxml.clean_report(base(setup))
        self.assertEqual(notes, [])
        self.assertEqual(len(openpyxl.load_workbook(io.BytesIO(clean)).active._images), 1)


class TargetAndVerifyTests(SimpleTestCase):
    """Items 11 to 14: targets written normalised; the verify pass must change nothing; caps; dangling refs."""

    def test_targets_are_written_normalised(self):
        data = edited(lambda f: f.__setitem__(WB_RELS, f[WB_RELS].replace(b'Target="theme/theme1.xml"',
                                                                          b'Target="//xl/theme/../theme/theme1.xml"')
                      .replace(b'Target="/xl/theme/theme1.xml"', b'Target="//xl/theme/../theme/theme1.xml"')))
        rels = files(ooxml.clean(data)[0])[WB_RELS]
        self.assertIn(b'Target="/xl/theme/theme1.xml"', rels)
        self.assertNotIn(b'..', rels)

    def test_a_second_pass_that_changes_anything_refuses(self):
        real = ooxml._clean
        calls = []

        def fake(data, a, b, **kw):
            out = real(data, a, b, **kw)
            calls.append(1)
            return out if len(calls) == 1 else (out[0], 0, 1, {})
        with mock.patch.object(ooxml, '_clean', side_effect=fake):
            refused(self, base(), 'unknown')

    def test_parsed_parts_are_capped(self):
        big = edited(lambda f: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
            b'</workbook>', b'<extLst><ext uri="u"><x>' + b'y' * (ooxml.MAX_SMALL + 10) + b'</x></ext></extLst>'
            b'</workbook>')))
        refused(self, big, 'size')

    def test_a_charts_reference_to_a_dropped_embedding(self):
        from openpyxl.chart import BarChart, Reference

        def setup(wb, ws):
            ch = BarChart()
            ch.add_data(Reference(ws, min_col=1, min_row=1, max_row=2))
            ws.add_chart(ch, 'D2')
        data = base(setup)
        f = files(data)
        chart = next(k for k in f if k.startswith('xl/charts/chart'))
        rels = chart.replace('charts/', 'charts/_rels/') + '.rels'
        f[chart] = f[chart].replace(b'</c:chartSpace>', b'<c:externalData xmlns:r="' + R + b'" r:id="rIdE">'
                                    b'<c:autoUpdate val="0"/></c:externalData></c:chartSpace>')
        f[rels] = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   b'Id="rIdE" Type="' + R + b'/package" Target="../embeddings/book.xlsx"/></Relationships>')
        f['xl/embeddings/book.xlsx'] = b'PK placeholder'
        out = files(ooxml.clean(build(f))[0])
        self.assertNotIn(b'externalData', out[chart])
        self.assertFalse([k for k in out if 'embeddings' in k])


class OrdinaryWorkbookTests(SimpleTestCase):
    """What a quote or pricing workbook uses survives the rebuild."""

    def test_round_trip(self):
        from openpyxl.chart import BarChart, Reference
        from openpyxl.comments import Comment
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Font, PatternFill
        from openpyxl.workbook.defined_name import DefinedName
        from openpyxl.worksheet.datavalidation import DataValidation
        from openpyxl.worksheet.table import Table

        def setup(wb, ws):
            ws.title = 'Quote'
            ws.append(['Item', 'Qty', 'Rate', 'Total'])
            for i in range(1, 6):
                ws.append([f'Item {i}', i, 12.5 * i, f'=B{i + 1}*C{i + 1}'])
            ws['A1'].comment = Comment('Header note', 'QS')
            ws['A9'], ws['A9'].hyperlink = 'Spec', 'https://example.com/spec'
            ws['A10'], ws['A10'].hyperlink = 'Inside', "#'Quote'!A1"
            ws.merge_cells('H1:J2')
            ws['A1'].font, ws['B2'].fill = Font(bold=True), PatternFill('solid', fgColor='FFFF00')
            ws.add_table(Table(displayName='Lines', ref='A1:D6'))
            ws.conditional_formatting.add('C2:C6', CellIsRule(operator='greaterThan', formula=['100'],
                                                              fill=PatternFill('solid', fgColor='FFC7CE')))
            dv = DataValidation(type='list', formula1='"Yes,No"')
            ws.add_data_validation(dv)
            dv.add('E2:E6')
            ch = BarChart()
            ch.add_data(Reference(ws, min_col=4, min_row=1, max_row=6), titles_from_data=True)
            ws.add_chart(ch, 'L5')
            wb.defined_names['GrandTotal'] = DefinedName('GrandTotal', attr_text='Quote!$D$6')
            ws.freeze_panes = 'A2'
        data = base(setup)
        clean, _, notes = ooxml.clean_report(data)
        a, c = openpyxl.load_workbook(io.BytesIO(data)), openpyxl.load_workbook(io.BytesIO(clean))
        ws = c['Quote']
        self.assertEqual([[x.value for x in r] for r in a['Quote'].iter_rows()],
                         [[x.value for x in r] for r in ws.iter_rows()])
        self.assertEqual(ws['A1'].comment.text, 'Header note')
        self.assertEqual(ws['A9'].hyperlink.target, 'https://example.com/spec')
        self.assertEqual(ws['A10'].hyperlink.target, "#'Quote'!A1")  # openpyxl writes a link inside as a target
        self.assertEqual((list(ws.tables), len(ws.conditional_formatting), len(ws.data_validations.dataValidation),
                          len(ws._charts), ws.freeze_panes, ws['A1'].font.b, list(c.defined_names)),
                         (['Lines'], 1, 1, 1, 'A2', True, ['GrandTotal']))
        self.assertEqual(notes, [])
