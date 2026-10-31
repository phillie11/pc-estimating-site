"""
Review reproductions for branch claude/r2-levelling (quote levelling).

Copy into siteline/levelling/tests/test_review_repro.py on the branch and run:
    cd siteline && MEDIA_ROOT=$(mktemp -d) DEBUG=1 python manage.py test levelling.tests.test_review_repro --noinput
On PostgreSQL (DB_ENGINE=postgres ...) the PgOnly class also runs.

Every test here FAILS (or errors) on the branch as reviewed and should pass once the matching fix lands.
Each docstring names the finding.
"""
import io
import time
import zipfile
from decimal import Decimal as D
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase

from directory.models import Package
from projects.models import Project
from tenancy.context import org_context

from levelling import importer, services
from levelling.models import Adjustment, LevellingSheet, QuoteCell

from .test_import import HEAD, csv_file, xlsx
from .test_views import LevellingBase


# ---------------------------------------------------------------------------------------------
# Crafted .xlsx builder (hand-written parts, so the XML can be anything)
# ---------------------------------------------------------------------------------------------
_CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships'
       '+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" '
       'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override '
       'PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
       'spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/'
       'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
_RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
         'officeDocument" Target="xl/workbook.xml"/></Relationships>')
_WB = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" '
       'sheetId="1" r:id="rId1"/></sheets></workbook>')
_WBRELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
           '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
           'worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId9" Type="http://schemas.openxmlformats.'
           'org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
_HDR = ('<row r="1"><c r="A1" t="inlineStr"><is><t>Ref</t></is></c><c r="B1" t="inlineStr"><is><t>Description'
        '</t></is></c><c r="C1" t="inlineStr"><is><t>Qty</t></is></c><c r="D1" t="inlineStr"><is><t>Unit</t>'
        '</is></c></row>')


def crafted(rows_xml, shared='<si><t>a</t></si>', name='quote.xlsx'):
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + _HDR + rows_xml + '</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _CT)
        z.writestr('_rels/.rels', _RELS)
        z.writestr('xl/workbook.xml', _WB)
        z.writestr('xl/_rels/workbook.xml.rels', _WBRELS)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
        z.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   + shared + '</sst>')
    return SimpleUploadedFile(name, buf.getvalue())


# A shared-string cell pointing past the end of the table (IndexError inside openpyxl's iter_rows)
BAD_SST_INDEX = ('<row r="2"><c r="A2" t="inlineStr"><is><t>J5</t></is></c><c r="B2" t="s"><v>7</v></c>'
                 '<c r="C2"><v>1</v></c></row>')
# A date cell that isn't a date (ValueError inside openpyxl's iter_rows)
BAD_DATE = ('<row r="2"><c r="A2" t="d"><v>notadate</v></c><c r="B2" t="inlineStr"><is><t>x</t></is></c>'
            '<c r="C2"><v>1</v></c></row>')


# =============================================================================================
# HIGH: deleting a trade (any internal user, incl. projects-only staff) silently wipes levelling
# =============================================================================================
class TradeDeleteWipesLevelling(LevellingBase):
    def test_projects_only_staff_deleting_a_trade_must_not_destroy_levelling(self):
        """F1. directory.views.trades is @internal_required; LevellingSheet.trade is on_delete=CASCADE."""
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON)  # read-only levelling
            before = (LevellingSheet.objects.count(), QuoteCell.objects.count())
        self.login(self.pm)  # AREA_DELIVERY member: cannot even open the levelling
        self.client.post(self.url('trades', self.org_a), {'action': 'delete', 'trade': self.join.pk})
        with org_context(self.org_a):
            after = (LevellingSheet.objects.count(), QuoteCell.objects.count())
        self.assertEqual(after, before, 'Trade delete cascaded through LevellingSheet -> lines -> cells')


# =============================================================================================
# MEDIUM: removing a package from Sub quotes deletes that sub's levelling, on any bid stage
# =============================================================================================
class PackageRemoveWipesLevelling(LevellingBase):
    def test_removing_a_priced_package_on_a_won_bid_keeps_the_levelling(self):
        """F2. QuoteCell/Exclusion/Adjustment.package are CASCADE; carried is SET_NULL with the reason left behind."""
        self.post({'action': 'carry', 'carry-package': self.p_tha.pk, 'carry-reason': 'Best programme'})
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON)
            cells = QuoteCell.objects.filter(package=self.p_tha).count()
        self.assertEqual(cells, 3)
        self.login(self.pm)
        self.client.post(self.url('package_list', self.org_a, self.bid.pk), {'action': 'remove', 'package': self.p_tha.pk})
        with org_context(self.org_a):
            self.assertTrue(Package.objects.filter(pk=self.p_tha.pk).exists(),
                            'A priced package on a decided bid was deleted along with its levelling')
            self.assertEqual(QuoteCell.objects.filter(package_id=self.p_tha.pk).count(), 3)
            self.assertEqual(LevellingSheet.objects.get(pk=self.sheet.pk).carried_id, self.p_tha.pk)


# =============================================================================================
# MEDIUM: a plug left on a line that is later priced is still added (double count)
# =============================================================================================
class StalePlugDoubleCounts(LevellingBase):
    def test_priced_line_is_not_counted_twice(self):
        """F3. build() flags plug_stale but maths.level still adds every adjustment, so rank/carry/exports are wrong."""
        with org_context(self.org_a):
            # Lund has no tender sum, so his quoted total comes from his lines
            services.set_cell(self.l5, self.p_lund, rate=D('600'))
            services.set_cell(self.l7, self.p_lund, amount=D('1500'))
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_lund.pk, 'basis': 'AVERAGE'})
        with org_context(self.org_a):
            services.set_cell(self.l6, self.p_lund, rate=D('40'))  # their priced copy arrives later
        lund = self.view().column(self.p_lund.pk)
        self.assertEqual(lund.quoted_from, 'lines')
        self.assertEqual(lund.lines_total, D('17580.00'))           # 14400 + 1680 + 1500
        self.assertEqual(lund.levelled, lund.lines_total,
                         f'Levelled {lund.levelled} = lines {lund.lines_total} + stale plug')


# =============================================================================================
# LOW/MEDIUM: crafted workbooks raise uncaught exceptions -> HTTP 500
# =============================================================================================
class CraftedWorkbookErrors(LevellingBase):
    def imp(self, f, kind='scope', package=None):
        data = {'imp-file': f, 'imp-kind': kind}
        if package:
            data['imp-package'] = package.pk
        return self.post(data, url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))

    def test_bad_shared_string_index_is_refused_not_a_500(self):
        """F4a. importer._xlsx_rows only wraps load_workbook; iter_rows raises IndexError."""
        try:
            r = self.imp(crafted(BAD_SST_INDEX))
        except IndexError as e:
            self.fail(f'500: IndexError escaped the import view ({e})')
        self.assertIn('Not imported', str(list(r.wsgi_request._messages)[-1]))

    def test_bad_date_cell_is_refused_not_a_500(self):
        """F4b. ValueError from openpyxl.utils.datetime.from_ISO8601 during iter_rows."""
        try:
            r = self.imp(crafted(BAD_DATE))
        except ValueError as e:
            self.fail(f'500: ValueError escaped the import view ({e})')
        self.assertIn('Not imported', str(list(r.wsgi_request._messages)[-1]))

    def test_tendering_subs_portal_file_cannot_500_the_estimator(self):
        """F4c. _import_quote_file catches (OSError, ValueError) but not IndexError."""
        with org_context(self.org_a):
            self.p_ken.quote_file.save('kensal.xlsx', ContentFile(crafted(BAD_SST_INDEX).read()))
        try:
            r = self.post({'action': 'from_quote', 'package': self.p_ken.pk},
                          url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))
        except IndexError as e:
            self.fail(f'500: IndexError escaped _import_quote_file ({e})')
        self.assertEqual(r.status_code, 302)


# =============================================================================================
# MEDIUM: decompression amplification - a ~60 KB file costs ~10-15 s CPU and ~100 MB in a web worker
# =============================================================================================
class Amplification(SimpleTestCase):
    def test_tiny_workbook_with_huge_shared_strings_is_refused_quickly(self):
        """F5. MAX_UNZIPPED=25 MB total, but sharedStrings.xml is DOM-parsed in full even in read_only mode."""
        n = (24 * 1024 * 1024) // len('<si><t>a</t></si>')
        f = crafted('<row r="2"><c r="A2" t="inlineStr"><is><t>J1</t></is></c><c r="B2" t="s"><v>0</v></c>'
                    '<c r="C2"><v>1</v></c></row>', shared='<si><t>a</t></si>' * n)
        self.assertLess(f.size, 100 * 1024)
        t = time.monotonic()
        try:
            importer.read(f)
        except importer.WorkbookRefused:
            pass
        self.assertLess(time.monotonic() - t, 2.0, f'{f.size // 1024} KB upload took {time.monotonic() - t:.1f}s')


# =============================================================================================
# LOW: defusedxml is not a declared dependency (openpyxl only uses it when importable)
# =============================================================================================
class Dependencies(SimpleTestCase):
    def test_defusedxml_is_pinned_next_to_openpyxl(self):
        """F6. In venv-int defusedxml is only present as a transitive dep of pip-audit (py-serializable)."""
        req = (Path(settings.BASE_DIR) / 'requirements.txt').read_text()
        self.assertRegex(req, r'(?m)^defusedxml==\d')


# =============================================================================================
# LOW: carried reason only checked at the moment of carrying
# =============================================================================================
class CarriedReasonDrift(LevellingBase):
    def test_carried_sub_that_stops_being_lowest_needs_a_reason(self):
        """F7. Carry the lowest with no reason, then adjust: carried, not lowest, and no reason on file/export."""
        self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': ''})
        self.post({'action': 'adjust', 'adj-package': self.p_ken.pk, 'adj-kind': 'QUALIFICATION',
                   'adj-amount': '9000', 'adj-reason': 'Excludes site labour'})
        v = self.view()
        with org_context(self.org_a):
            sheet = LevellingSheet.objects.get(pk=self.sheet.pk)
        # Agreed fix (coordinator, F7): the carry is kept, the drift is flagged on the sheet and the tender
        # page, and the bid can't move to Submitted until a reason is given.
        self.assertEqual(sheet.carried_id, self.p_ken.pk)
        self.assertFalse(v.carried.result.is_lowest or sheet.carried_reason)
        self.assertTrue(v.reason_needed)
        self.login(self.est)
        self.assertContains(self.client.get(self.sheet_url()), 'no longer the lowest levelled price')
        self.assertContains(self.client.get(self.url('tender', self.org_a, self.bid.pk)),
                            'no longer the lowest levelled price')
        r = self.client.post(self.url('project_stage', self.org_a, self.bid.pk), {'stage': Project.STAGE_SUBMITTED})
        self.assertIn('/levelling/', r['Location'])
        with org_context(self.org_a):
            self.assertEqual(Project.objects.get(pk=self.bid.pk).stage, Project.STAGE_TENDERING)
        self.post({'action': 'carry', 'carry-package': self.p_ken.pk, 'carry-reason': 'Only one with capacity'})
        self.assertFalse(self.view().reason_needed)
        self.login(self.est)
        r = self.client.post(self.url('project_stage', self.org_a, self.bid.pk), {'stage': Project.STAGE_SUBMITTED})
        self.assertNotIn('/levelling/', r['Location'])


# =============================================================================================
# LOW: zero (or negative) lowest levelled total renders "None%" on the page and "(0%)" in the PDF
# =============================================================================================
class ZeroLowestDisplay(LevellingBase):
    def test_no_none_percent_on_page(self):
        """F8. _summary_row.html prints {{ var_lowest_pct }}% unguarded; exports.pdf_bytes prints `or 0`."""
        with org_context(self.org_a):
            Adjustment.objects.create(sheet=self.sheet, package=self.p_ken, kind=Adjustment.CORRECTION,
                                      amount=D('-38330'), reason='zero it')
        self.login(self.est)
        r = self.client.get(self.sheet_url())
        self.assertNotContains(r, 'None%')


# =============================================================================================
# LOW (PostgreSQL / MySQL only): values the importer accepts overflow the columns -> 500
# =============================================================================================
@skipUnless(connection.vendor in ('postgresql', 'mysql'), 'SQLite does not enforce numeric precision or NULs')
class PgOnly(LevellingBase):
    def imp(self, f):
        return self.post({'imp-file': f, 'imp-kind': 'scope'},
                         url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))

    def test_qty_beyond_the_column_is_refused(self):
        """F9a. parse_number allows < 1e12; ScopeLine.qty is DECIMAL(14,3) (< 1e11)."""
        r = self.imp(csv_file('Ref,Description,Qty,Unit\nX1,Huge,500000000000,m\n'))
        self.assertEqual(r.status_code, 302)

    def test_nul_byte_in_csv_is_refused(self):
        """F9b. NUL survives _csv_rows; PostgreSQL text can't hold it."""
        r = self.imp(csv_file('Ref,Description,Qty,Unit\nX1,a\x00b,1,m\n'))
        self.assertEqual(r.status_code, 302)

    def test_plug_from_a_huge_line_value_is_refused(self):
        """F9c. line value qty x rate is unbounded; the plug Adjustment.amount is DECIMAL(14,2)."""
        with org_context(self.org_a):
            big = services.add_line(self.sheet, 'X9', 'Big', D('1000'), 'm')
            services.set_cell(big, self.p_oak, rate=D('50000000000'))  # 5e13 for the line
        r = self.post({'action': 'plug', 'line': big.pk, 'package': self.p_ken.pk, 'basis': 'HIGHEST'})
        self.assertEqual(r.status_code, 302)
