"""
core.ooxml, against the fourth review: a never-allowed function counts only in function position, so ordinary
sheet, table column and defined names pass; a defined name can't shadow a function, and only a workbook's LAMBDA
names may be called. Workbooks built with openpyxl; every name is ordinary or a placeholder.
"""
import io

import openpyxl
from django.test import SimpleTestCase
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table

from core import ooxml

from .test_ooxml import refused


def book(setup):
    wb = openpyxl.Workbook()
    setup(wb)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def sheets(names, formulas):
    def setup(wb):
        ws = wb.active
        ws.title = 'Summary'
        for name in names:
            wb.create_sheet(name)['B2'] = 100
        for i, f in enumerate(formulas, start=1):
            ws.cell(row=i, column=1, value=f)
    return setup


class OrdinaryNamesTests(SimpleTestCase):
    """Item 1: names that merely look like a never-allowed function are references, not calls."""

    def test_sheet_names(self):
        cases = [
            (['Risk Register'], ["='Risk Register'!B2"]), (['Drawing Register'], ["=SUM('Drawing Register'!B2:B9)"]),
            (['Exec Summary'], ["='Exec Summary'!B2"]), (['PY Budget'], ["='PY Budget'!B2*1.03"]),
            (['PY'], ['=PY!B2']), (['Call Off Orders'], ["='Call Off Orders'!B2"]),
            (['Image Schedule'], ["='Image Schedule'!B2"]), (['Translate'], ['=Translate!B2']),
            (['Get Data'], ["='Get Data'!B2"]), (['Files'], ['=Files!B2+SUM(Files!B2:B3)']),
        ]
        for names, formulas in cases:
            with self.subTest(names[0]):
                ooxml.clean(book(sheets(names, formulas)))

    def test_table_column_and_defined_names(self):
        def table(wb):
            ws = wb.active
            ws.append(['Item', 'Call', 'Image'])
            ws.append(['a', 1, 2])
            ws.add_table(Table(displayName='T1', ref='A1:C2'))
            ws['E1'] = '=SUM(T1[Call])+SUM(T1[[#This Row],[Image]])'
        ooxml.clean(book(table))

        def register(wb):
            ws = wb.active
            ws['A1'] = 5
            wb.defined_names['Register'] = DefinedName('Register', attr_text='Sheet!$A$1')
            wb.defined_names['GET.TOTAL'] = DefinedName('GET.TOTAL', attr_text='Sheet!$A$1')
            ws['B1'], ws['B2'] = '=Register*2', '=GET.TOTAL'
        ooxml.clean(book(register))

    def test_function_position_is_still_refused(self):
        for formula in ('=_xlfn.WEBSERVICE("u")', '=_xlfn.MAP(A1:A2,WEBSERVICE)', '=_xlfn.MAP(A1:A2, Webservice )',
                        '=_xlfn.MAP(A1:A2,_xlfn.WEBSERVICE)', '=PY (1)', '=EVALUATE("1+1")', '=FILES("C:\\\\x\\\\*")',
                        '=DIRECTORY()', '=GET.WORKSPACE(1)', "=T1[a'[]+_xlfn.WEBSERVICE(\"u\")+T1[c']]",
                        '=_xlfn.LET(_xlpm.x,1,_xlpm.WEBSERVICE("u"))', '=_xlfn.WEBSERVICE\u00a0("u")'):
            with self.subTest(formula):
                refused(self, book(sheets([], [formula])), 'fetch')


class ShadowingTests(SimpleTestCase):
    """Item 2: a defined name can't take a function's name, and only LAMBDA names may be called."""

    def test_a_lambda_name_shadowing_a_function_is_refused(self):
        for name in ('EVALUATE', 'FILES', 'GET.CELL', 'SUM', 'Exec', 'RUN', 'webservice'):
            def setup(wb, name=name):
                wb.defined_names[name] = DefinedName(name, attr_text='_xlfn.LAMBDA(_xlpm.x,_xlpm.x)')
            with self.subTest(name):
                e = refused(self, book(setup), 'fetch')
                self.assertIn('also the name of a function', str(e))

    def test_a_plain_name_shadowing_a_function_unlocks_nothing(self):
        """EVALUATE or FILES defined as =1: the name stays, but a call to EVALUATE(...) is still refused."""
        for name, call in (('EVALUATE', '=EVALUATE("1+1")'), ('FILES', '=FILES("C:\\\\x\\\\*")'),
                           ('GET.CELL', '=GET.CELL(1,A1)')):
            def setup(wb, name=name, call=call):
                wb.defined_names[name] = DefinedName(name, attr_text='1')
                wb.active['B1'] = call
            with self.subTest(name):
                refused(self, book(setup), 'fetch')
        for name in ('Register', 'Rate', 'N'):
            def plain(wb, name=name):
                wb.defined_names[name] = DefinedName(name, attr_text='Sheet!$A$1')
            ooxml.clean(book(plain))

    def test_the_review_probe(self):
        def shadow(wb):
            ws = wb.active
            wb.defined_names['EVALUATE'] = DefinedName('EVALUATE', attr_text='1')
            wb.defined_names['X'] = DefinedName('X', attr_text='EVALUATE("1+1")')
            wb.defined_names['FILES'] = DefinedName('FILES', attr_text='1')
            ws['B1'], ws['B2'] = '=FILES("C:\\\\placeholder\\\\*")', '=X'
        refused(self, book(shadow), 'fetch')

    def test_only_lambda_names_may_be_called(self):
        def lam(wb):
            wb.defined_names['AddVat'] = DefinedName('AddVat', attr_text='_xlfn.LAMBDA(_xlpm.x,_xlpm.x*1.2)')
            wb.defined_names['Rate'] = DefinedName('Rate', attr_text='0.2')
            wb.active['A1'] = '=AddVat(10)'
        ooxml.clean(book(lam))

        def called(wb):
            wb.defined_names['Markup'] = DefinedName('Markup', attr_text='0.2')
            wb.active['A1'] = '=Markup(10)'  # not a LAMBDA: an unknown function, so the cell's formula is taken out
        clean, _, notes = ooxml.clean_report(book(called))
        self.assertIn("Sheet 'Sheet', cell A1 (Markup)", notes[0])
