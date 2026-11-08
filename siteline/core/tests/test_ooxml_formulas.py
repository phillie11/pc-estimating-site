"""
core.ooxml's formula checks, against the third review (N1 to N4 and point 6): an allowlist of Excel's own
worksheet functions (core/excel_functions.py), the fetching ones never, formulas in every place they live, and the
cost of checking them linear. Workbooks from core.tests.test_ooxml's helpers; addresses are placeholders.
"""
import io
import time
from unittest import mock

import openpyxl
from django.test import SimpleTestCase

from core import ooxml

from .test_ooxml import MAIN, R, S, WB_RELS, base, edited, files, refused


def with_formula(formula, cached=None):
    """The base workbook with B1's formula replaced (and, optionally, the value Excel saved for it)."""
    def change(f):
        cell = b'<f>' + formula.replace('&', '&amp;').replace('<', '&lt;').encode() + b'</f>'
        if cached is not None:
            cell += b'<v>' + cached.encode() + b'</v>'
        assert b'<f>1+1</f>' in f[S]
        f[S] = f[S].replace(b'<f>1+1</f>', cell, 1).replace(b'<v></v>', b'')
    return edited(change)


class FunctionAllowlistTests(SimpleTestCase):
    """N2: only Excel's own worksheet functions; the fetching ones never, even if someone lists them."""

    def test_refused_functions_name_function_sheet_and_cell(self):
        cases = {
            '_xlfn.MAP(A1:A2,_xleta.WEBSERVICE)': 'WEBSERVICE', '_xlfn.BYROW(A1:A2,_xleta.IMAGE)': 'IMAGE',
            '_xlfn.STOCKHISTORY("X",TODAY())': 'STOCKHISTORY', '_xlfn._xlws.PY(0,1)': 'PY',
            '_xlfn.IMPORTCSV("http://x.invalid/a.csv")': 'IMPORTCSV', '_xlfn.IMPORTTEXT("a")': 'IMPORTTEXT',
            '_xlfn.TRANSLATE(A1,"en","fr")': 'TRANSLATE', '_xlfn.COPILOT("x",A1)': 'COPILOT',
            'CUBEVALUE("m","[x]")': 'CUBEVALUE', 'SQL.REQUEST("DSN=x",,,"select 1")': 'SQL.REQUEST',
            '_xlfn.WEBSERVICE ("http://x.invalid")': 'WEBSERVICE',
            '_xlfn.FILTERXML("<a/>","//a")': 'FILTERXML', 'RTD("p",,"x")': 'RTD', 'CALL("k","x","J")': 'CALL',
            'REGISTER.ID("a","b")': 'REGISTER.ID', 'EXEC("x")': 'EXEC', "cmd|'/c calc'!A0": 'DDE',
            'HYPERLINK("file:///c:/x")': 'HYPERLINK', 'HYPERLINK("\\\\\\\\host\\\\s"&A1)': 'HYPERLINK',
            'HYPERLINK("javascript:x")': 'HYPERLINK',
            # Round 3: escapes in a structured reference, a bracket left open, names never called, a LAMBDA's
            # parameter called, a no-break space, lower case, a dotted capital I
            "T1[a'[]+_xlfn.WEBSERVICE(\"http://x.invalid/\")+T1[c']]": 'WEBSERVICE',
            '_xlfn.MAP(A1:A2,WEBSERVICE)': 'WEBSERVICE',
            '_xlfn.MAP(A1:A2,_xlfn.WEBSERVICE)': 'WEBSERVICE', '_xlfn.MAP(A1:A2,_xlfn._xleta.WEBSERVICE)': 'WEBSERVICE',
            '_xlfn.LET(_xlpm.x,1,_xlpm.WEBSERVICE("http://x.invalid/"))': 'WEBSERVICE',
            '_xlfn.WEBSERVICE\u00a0("http://x.invalid/")': 'WEBSERVICE', '_xlfn.webservice("x")': 'webservice',
            '_xlfn.WEBSERV\u0130CE("x")': 'WEBSERV', 'GET.CELL(1,A1)': 'GET.CELL', '_xludf.WEBSERVICE(1)': 'WEBSERVICE',
        }
        for formula, name in cases.items():
            with self.subTest(formula):
                e = refused(self, with_formula(formula), 'fetch')
                self.assertIn(name, str(e))
                self.assertIn("Sheet 'Sheet', cell B1", str(e))

    def test_an_unclosed_bracket_is_refused(self):
        refused(self, with_formula('[+_xlfn.WEBSERVICE("http://x.invalid/")'), 'layout')

    def test_never_wins_over_the_list(self):
        with mock.patch.object(ooxml, 'FUNCTIONS', ooxml.FUNCTIONS | {'WEBSERVICE', 'IMAGE'}):
            refused(self, with_formula('_xlfn.WEBSERVICE("http://x.invalid")'), 'fetch')

    def test_nothing_is_remembered_between_uploads_and_little_within_one(self):
        import tracemalloc
        self.assertIsNone(ooxml._context.get())
        tracemalloc.start()
        for i in range(3000):
            ooxml.formula_problem('"\U0001F600' + ('%06d' % i) * 1300 + '"')
        current, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(current, 2 * 1024 * 1024)  # 3,000 distinct 8 KB formulas: none of them kept
        cache = {}
        token = ooxml._context.set((cache, frozenset(), True))
        try:
            for i in range(ooxml.CACHE_ENTRIES + 500):
                ooxml.formula_problem(f'SUM({i})')
            ooxml.formula_problem('SUM(' + '1,' * 600 + '1)')
        finally:
            ooxml._context.reset(token)
        self.assertEqual(len(cache), ooxml.CACHE_ENTRIES)
        self.assertTrue(all(len(k) < ooxml.CACHE_TEXT for k in cache))

    def test_modern_and_everyday_functions_pass(self):
        for formula in ('_xlfn.XLOOKUP(A1,A2:A5,B2:B5)', '_xlfn._xlws.FILTER(A1:A5,B1:B5>0)',
                        '_xlfn.LET(_xlpm.x,A1,_xlpm.x*2)', '_xlfn.LAMBDA(_xlpm.a,_xlpm.a+1)(2)',
                        '_xlfn._xlws.SORT(A1:A5)', '_xlfn.UNIQUE(A1:A5)', '_xlfn.SEQUENCE(5)',
                        '_xlfn.TEXTJOIN(",",TRUE,A1:A5)', '_xlfn.IFS(A1>1,1,TRUE,2)', '_xlfn.SWITCH(A1,1,"a","b")',
                        '_xlfn.MAXIFS(A1:A5,B1:B5,">0")', '_xlfn.MINIFS(A1:A5,B1:B5,">0")', '_xlfn.CONCAT(A1,"x")',
                        '_xlfn.MAP(A1:A2,_xleta.SUM)', 'SUM(_xlfn.ANCHORARRAY(C1))', '_xlfn.SINGLE(A1:A5)',
                        "IFERROR(INDEX('Sheet 2'!$F$7:$F$45,MATCH($B$2,'Sheet 2'!$D$7:$D$45,0)),0)",
                        'SUM(Table1[[#This Row],[Qty]])', 'HYPERLINK("https://example.com/spec","Spec")',
                        'HYPERLINK("#Sheet!A1")', '"a|b"&"WEBSERVICE("', 'IF(ISNUMBER(D4),D4*(F4+G4),"")',
                        'HYPERLINK(A1)', 'HYPERLINK("https://ok.invalid/"&A1)', 'HYPERLINK(A1&"x","t")',
                        "SUM(Table1[[#Headers],[Rate'[GBP']]])", '_xlnm._FilterDatabase'):
            with self.subTest(formula):
                ooxml.clean(with_formula(formula))

    def test_a_function_excel_doesnt_have_is_taken_out_keeping_the_value(self):
        """A macro's function (NumberToPounds, say): the rebuild removed the macro, so it could only show #NAME?."""
        clean, _, notes = ooxml.clean_report(with_formula('NumberToPounds(A1)', cached='42'))
        out = files(clean)[S]
        self.assertNotIn(b'NumberToPounds', out)
        self.assertIn(b'<v>42</v>', out)
        self.assertIn("Sheet 'Sheet', cell B1 (NumberToPounds)", notes[0])
        self.assertEqual(openpyxl.load_workbook(io.BytesIO(clean), data_only=True).active['B1'].value, 42)

    def test_a_shared_formula_taken_out_takes_the_cells_repeating_it(self):
        def shared(f):
            f[S] = f[S].replace(b'<f>1+1</f>', b'<f t="shared" ref="B1:B2" si="0">NumberToPounds(A1)</f><v>x</v>')
            f[S] = f[S].replace(b'</row>', b'</row><row r="2"><c r="B2"><f t="shared" si="0"/><v>y</v></c></row>', 1)
        out = files(ooxml.clean(edited(shared))[0])[S]
        self.assertNotIn(b'<f', out)
        self.assertIn(b'<v>y</v>', out)

    def test_an_add_in_function_in_a_cell_is_taken_out_too(self):
        clean, _, notes = ooxml.clean_report(with_formula('_xludf.SOMEFN(A1)', cached='7'))
        self.assertNotIn(b'_xludf', files(clean)[S])
        self.assertIn('_xludf.SOMEFN', notes[0])

    def test_a_cell_repeating_a_shared_formula_before_its_master_is_refused(self):
        def change(f):
            f[S] = f[S].replace(b'</sheetData>', b'<row r="2"><c r="A2"><f t="shared" si="0"/><v>5</v></c></row>'
                                b'<row r="3"><c r="A3"><f t="shared" ref="A2:A4" si="0">MYUDF(B3)</f><v>6</v></c>'
                                b'</row></sheetData>')
        refused(self, edited(change), 'layout')

    def test_a_lambda_bound_to_a_defined_name_may_be_called(self):
        def change(f):
            f['xl/workbook.xml'] = f['xl/workbook.xml'].replace(
                b'</sheets>', b'</sheets><definedNames><definedName name="AddVat">_xlfn.LAMBDA(_xlpm.x,_xlpm.x*1.2)'
                b'</definedName><definedName name="Gross">AddVat(100)</definedName></definedNames>')
            f[S] = f[S].replace(b'<f>1+1</f>', b'<f>AddVat(A1)</f>')
        clean, _, notes = ooxml.clean_report(edited(change))
        self.assertIn(b'AddVat(A1)', files(clean)[S])
        self.assertEqual(notes, [])
        bad = edited(lambda f: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
            b'</sheets>', b'</sheets><definedNames><definedName name="Fetch">_xlfn.LAMBDA(_xlpm.u,'
            b'_xlfn.WEBSERVICE(_xlpm.u))</definedName></definedNames>')))
        refused(self, bad, 'fetch')

    def test_elsewhere_an_unknown_function_is_refused(self):
        name = edited(lambda f: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
            b'</sheets>', b'</sheets><definedNames><definedName name="X">NumberToPounds(1)</definedName>'
            b'</definedNames>')))
        self.assertIn('The name X', str(refused(self, name, 'fetch')))

    def test_the_list_is_data_and_complete_enough(self):
        from core import excel_functions as fx
        for name in ('SUM', 'IFERROR', 'INDEX', 'MATCH', 'SUMIFS', 'XLOOKUP', 'FILTER', 'LET', 'LAMBDA', 'TEXTAFTER'):
            self.assertIn(name, fx.ALLOWED)
        self.assertFalse(fx.ALLOWED & fx.NEVER)


class FormulaCostTests(SimpleTestCase):
    """N1: one linear pass per formula, every formula and defined name capped, the formula text in a part capped."""

    def test_adversarial_formulas_are_linear(self):
        for text in ('a|' * 4000, '"' * 8191, "'" * 8191, '[' * 8000, 'SUM(' * 2000, 'A' * 8192, '""' * 4096,
                     'x' + 'a' * 8000 + '(', '_xleta.' * 1000, 'A1' + ' ' * 8000 + '(', "[a'" * 2700,
                     '_xlfn.' * 1300, '\u00a0' * 8000, 'A1+' * 2666):
            t = time.monotonic()
            ooxml.formula_problem(text[:8192])
            with self.subTest(text[:12]):
                self.assertLess(time.monotonic() - t, 0.05)

    def test_long_defined_names_and_too_much_formula_text(self):
        long_name = edited(lambda f: f.__setitem__('xl/workbook.xml', f['xl/workbook.xml'].replace(
            b'</sheets>', b'</sheets><definedNames><definedName name="n">' + b''.join(
                b'%08x' % ((i * 2654435761) % 4294967296) for i in range(12500)) + b'</definedName>'
            b'</definedNames>')))
        refused(self, long_name, 'layout')
        cells = b''.join(b'<c r="A%d"><f>SUM(%d,A1)</f></c>' % (i + 2, i) for i in range(500))
        many = edited(lambda f: f.__setitem__(S, f[S].replace(b'</sheetData>', b'<row r="2">' + cells
                                                              + b'</row></sheetData>')))
        ooxml.clean(many)
        with mock.patch.object(ooxml, 'MAX_FORMULA_CHARS', 1000):
            refused(self, many, 'size')

    def test_many_long_formulas_in_good_time(self):
        import random
        rnd = random.Random(5)
        cells = b''.join(b'<c r="A%d"><f>%s</f></c>' % (i + 2, ''.join(
            rnd.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8000)).encode()) for i in range(200))
        data = edited(lambda f: f.__setitem__(S, f[S].replace(b'</sheetData>', b'<row r="2">' + cells
                                                              + b'</row></sheetData>')))
        t = time.monotonic()
        ooxml.clean(data)
        self.assertLess(time.monotonic() - t, 5)


class FormulaPlacesTests(SimpleTestCase):
    """N3: formulas in attributes, tables and pivot caches are checked too."""

    def test_cfvo_formula_whatever_its_type(self):
        for kind in (b'formula', b'Formula', b'num', b'percent', b'percentile'):
            data = edited(lambda f, kind=kind: f.__setitem__(S, f[S].replace(b'<pageMargins', (
                b'<conditionalFormatting sqref="A1"><cfRule type="dataBar" priority="1"><dataBar><cfvo type="' + kind
                + b'" val="_xlfn.WEBSERVICE(&quot;http://x.invalid&quot;)"/><cfvo type="max"/><color rgb="FF638EC6"/>'
                b'</dataBar></cfRule></conditionalFormatting><pageMargins'))))
            with self.subTest(kind):
                refused(self, data, 'fetch')
        plain = edited(lambda f: f.__setitem__(S, f[S].replace(b'<pageMargins', (
            b'<conditionalFormatting sqref="A1"><cfRule type="dataBar" priority="1"><dataBar><cfvo type="num" '
            b'val="-1.5E2"/><cfvo type="percent" val="90"/><color rgb="FF638EC6"/></dataBar></cfRule>'
            b'</conditionalFormatting><pageMargins'))))
        ooxml.clean(plain)

    def test_table_formulas(self):
        from openpyxl.worksheet.table import Table

        def setup(wb, ws):
            ws.append(['a', 'b'])
            ws.add_table(Table(displayName='T', ref='A2:B3'))
        for tag in (b'calculatedColumnFormula', b'totalsRowFormula'):
            def change(f, tag=tag):
                k = next(k for k in f if 'tables/' in k)
                f[k] = f[k].replace(b'<tableColumn id="2" name="b" />', b'<tableColumn id="2" name="b"><' + tag
                                    + b'>_xlfn.WEBSERVICE("http://x.invalid")</' + tag + b'></tableColumn>')
                assert tag in f[k]
            with self.subTest(tag):
                self.assertIn('Table table1.xml', str(refused(self, edited(change, base(setup)), 'fetch')))

    def test_pivot_cache_field_formula(self):
        self.pivot(b'<cacheFields count="1"><cacheField name="x" numFmtId="0" formula="_xlfn.WEBSERVICE(&quot;'
                   b'http://x.invalid&quot;)"/></cacheFields>')

    def test_pivot_calculated_items_and_members(self):
        for body in (b'<cacheFields count="1"><cacheField name="x" numFmtId="0"/></cacheFields><calculatedItems '
                     b'count="1"><calculatedItem formula="_xlfn.WEBSERVICE(&quot;http://x.invalid/&quot;)"><pivotArea/>'
                     b'</calculatedItem></calculatedItems>',
                     b'<cacheFields count="1"><cacheField name="x" numFmtId="0"/></cacheFields><calculatedMembers '
                     b'count="1"><calculatedMember name="m" mdx="_xlfn.WEBSERVICE(&quot;http://x.invalid/&quot;)"/>'
                     b'</calculatedMembers>'):
            with self.subTest(body[60:100]):
                self.pivot(body)

    def pivot(self, body):
        def change(f):
            f['xl/pivotCache/pivotCacheDefinition1.xml'] = (
                b'<pivotCacheDefinition xmlns="' + MAIN + b'" refreshOnLoad="1"><cacheSource type="worksheet">'
                b'<worksheetSource ref="A1:B2" sheet="Sheet"/></cacheSource>' + body + b'</pivotCacheDefinition>')
            f['[Content_Types].xml'] = f['[Content_Types].xml'].replace(
                b'</Types>', b'<Override PartName="/xl/pivotCache/pivotCacheDefinition1.xml" ContentType="application/'
                b'vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml"/></Types>')
            f[WB_RELS] = f[WB_RELS].replace(b'</Relationships>', b'<Relationship Id="rIdP" Type="' + R + b'/'
                                            b'pivotCacheDefinition" Target="pivotCache/pivotCacheDefinition1.xml"/>'
                                            b'</Relationships>')
        refused(self, edited(change), 'fetch')


class NamespaceAndMarkupTests(SimpleTestCase):
    """N4 and point 6."""

    def test_thousands_of_x14_rules_each_declaring_xm(self):
        items = b''.join(b'<x14:conditionalFormatting xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main">'
                         b'<x14:cfRule type="dataBar" id="{00000000-0000-0000-0000-%012d}"><x14:dataBar><x14:cfvo '
                         b'type="autoMin"/><x14:cfvo type="autoMax"/></x14:dataBar></x14:cfRule><xm:sqref>A%d'
                         b'</xm:sqref></x14:conditionalFormatting>' % (i, i + 1) for i in range(6000))
        ext = (b'<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" xmlns:x14="http://schemas.microsoft.com/'
               b'office/spreadsheetml/2009/9/main"><x14:conditionalFormattings>' + items
               + b'</x14:conditionalFormattings></ext></extLst>')
        ooxml.clean(edited(lambda f: f.__setitem__(S, f[S].replace(b'</worksheet>', ext + b'</worksheet>'))))

    def test_markup_compatibility_on_a_cell(self):
        def change(f):
            assert b'<c r="A1"' in f[S]
            f[S] = f[S].replace(b'<worksheet ', b'<worksheet xmlns:mc="http://schemas.openxmlformats.org/markup-'
                                b'compatibility/2006" xmlns:e="urn:placeholder" ').replace(
                b'<c r="A1"', b'<c mc:Ignorable="e" r="A1"')
        refused(self, edited(change), 'layout')
