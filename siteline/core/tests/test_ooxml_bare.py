"""
core.ooxml, against the final pass: a never-allowed function's name standing alone (the whole formula, or a whole
argument after ( , @ + or -) is refused, so a plain defined name can't hand one to LET or MAP; but a workbook's own
plain defined names and tables that share such a name may be used as values. Workbooks built with openpyxl.
"""
import io

import openpyxl
from django.test import SimpleTestCase
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table

from core import ooxml

from .test_ooxml import refused

U = '"http://placeholder.invalid/"'


def book(setup):
    wb = openpyxl.Workbook()
    setup(wb)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


class BareNameTests(SimpleTestCase):
    def test_function_and_bare_positions(self):
        for formula in ('{1,2}+WEBSERVICE(%s)' % U, '-WEBSERVICE(%s)' % U, '1&WEBSERVICE(%s)' % U,
                        '@WEBSERVICE(%s)' % U, 'A1:WEBSERVICE(%s)' % U, 'A1#+WEBSERVICE(%s)' % U,
                        '_xlfn.MAP(A1:A2,@WEBSERVICE)', '_xlfn.MAP(A1:A2,+WEBSERVICE)', '_xlfn.MAP(A1:A2,-WEBSERVICE)',
                        '_xlfn.MAP(A1:A2, WEBSERVICE )', 'WEBSERVICE', '=WEBSERVICE',
                        '_xlfn.LET(_xlpm.f,(WEBSERVICE),_xlpm.f(%s))' % U, 'Sheet1!WEBSERVICE(%s)' % U,
                        'EVALUATE​("1+1")'):
            with self.subTest(formula):
                self.assertEqual(ooxml.formula_problem(formula)[0], 'fetch')
        self.assertIsNone(ooxml.formula_problem('WEBSERVICE!A1'))  # a sheet called that

    def test_a_plain_name_cant_hand_a_function_to_let_or_map(self):
        def bare(wb):
            wb.defined_names['F'] = DefinedName('F', attr_text='WEBSERVICE')
            wb.active['A1'] = '=_xlfn.LET(_xlpm.f,F,_xlpm.f(%s))' % U
            wb.active['A2'] = '=_xlfn.MAP(B1:B2,F)'
        refused(self, book(bare), 'fetch')

        def eta(wb):
            wb.defined_names['F'] = DefinedName('F', attr_text='_xleta.WEBSERVICE')
            wb.active['A1'] = '=_xlfn.MAP(B1:B2,F)'
        refused(self, book(eta), 'fetch')

    def test_the_workbooks_own_names_and_tables_may_be_values(self):
        def table(wb):
            ws = wb.active
            ws.append(['Item', 'Cost'])
            ws.append(['a', 1])
            ws.add_table(Table(displayName='Register', ref='A1:B2'))
            ws['D1'] = '=ROWS(Register)'
        ooxml.clean(book(table))

        def plain(wb):
            ws = wb.active
            ws['A1'] = 5
            wb.defined_names['Register'] = DefinedName('Register', attr_text='Sheet!$A$1:$A$3')
            wb.defined_names['Image'] = DefinedName('Image', attr_text='Sheet!$A$1')
            ws['B1'], ws['B2'], ws['B3'] = '=SUM(Register)', '=IF(A2>0,Image,0)', '=Register'
        ooxml.clean(book(plain))

    def test_but_a_name_never_unlocks_a_call(self):
        def called(wb):
            wb.defined_names['Image'] = DefinedName('Image', attr_text='Sheet!$A$1')
            wb.active['B1'] = '=_xlfn.IMAGE(%s)' % U
        refused(self, book(called), 'fetch')

    def test_lambda_names_and_chains(self):
        def chain(wb):
            wb.defined_names['A_'] = DefinedName('A_', attr_text='_xlfn.LAMBDA(_xlpm.x,B_(_xlpm.x))')
            wb.defined_names['B_'] = DefinedName('B_', attr_text='_xlfn.LAMBDA(_xlpm.x,_xlfn.WEBSERVICE(_xlpm.x))')
            wb.active['A1'] = '=A_(%s)' % U
        refused(self, book(chain), 'fetch')
        for name in ('evaluate', 'Ｅｖａｌｕａｔｅ', 'Rtd', '_xlfn.WEBSERVICE'):
            def named(wb, name=name):
                wb.defined_names[name] = DefinedName(name, attr_text='_xlfn.LAMBDA(_xlpm.x,_xlpm.x)')
                wb.active['A1'] = '=%s(%s)' % (name, U)
            with self.subTest(name):
                refused(self, book(named), 'fetch')
