"""
Opening an uploaded pricing workbook safely and reading the cells a mapping names.

The file is untrusted. Before openpyxl sees it the package is rebuilt from its allowlisted parts by
core.ooxml.clean, the same code quote levelling uses: zip checks (sizes, ratios, duplicate names,
paths), [Content_Types].xml and every .rels parsed, macros, controls, OLE objects, external links and
connections dropped with every reference to them, Excel 4 macro sheets and auto-run names refused.
openpyxl parses with defusedxml (installed, see
requirements.txt). The workbook is opened twice, read-only: once for the values Excel last
saved (data_only) and once for the formulas, so the engine can tell a typed figure from a
calculated one and follow simple references. No formula is ever evaluated by openpyxl, macros
are never loaded (keep_vba=False) and external links are dropped (keep_links=False).

Only the sheets the mapping names are read, each only as far as the mapping's last row and
column, so a sheet that claims a million rows costs nothing.

An .xlsx and an .xlsm get the same treatment: whatever active content either holds is removed, and
only the rebuilt copy is read and stored, named .xlsx; the original is never kept. Nothing in a VBA
project is read or run.
"""
import hashlib
import io
import os
from datetime import date, datetime
from decimal import Decimal

from core import ooxml

MAX_BYTES = 15 * 1024 * 1024
MAX_UNZIPPED = 100 * 1024 * 1024
MAX_TEXT = 2000
MAX_SHEETS = 200
MAX_PARTS = 2000

ALLOWED = {'.xlsx', '.xlsm'}
REFUSED = {
    '.xltm': 'Macro-enabled templates are refused. Save the priced workbook as an Excel Workbook (.xlsx) or an '
             'Excel Macro-Enabled Workbook (.xlsm) and upload that.',
    '.xltx': 'That is a template. Save the priced workbook as an Excel Workbook (.xlsx) and upload that.',
    '.xls': 'Old-style .xls workbooks can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.xlsb': 'Binary workbooks (.xlsb) can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.ods': 'OpenDocument spreadsheets can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.csv': 'A CSV file holds one sheet. Upload the whole pricing workbook (.xlsx or .xlsm).',
}

class WorkbookRefused(ValueError):
    """The file can't be read safely, or isn't the pricing workbook. The message is for the estimator."""


def check_name(name):
    ext = os.path.splitext(name or '')[1].lower()
    if ext in REFUSED:
        raise WorkbookRefused(REFUSED[ext])
    if ext not in ALLOWED:
        raise WorkbookRefused('Upload the pricing workbook as an Excel workbook: .xlsx, or .xlsm (its macros are '
                              'removed and only a copy without them is kept).')
    return ext


def xlsx_name(name):
    """The stored file's name: an .xlsm upload is kept as .xlsx, because that is what it now is."""
    stem, ext = os.path.splitext(name or '')
    return f'{stem}.xlsx' if ext.lower() == '.xlsm' else (name or '')


def read_bytes(f):
    if hasattr(f, 'seek'):
        f.seek(0)
    data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise WorkbookRefused(f'The file is over {MAX_BYTES // (1024 * 1024)} MB. A pricing workbook is usually '
                              'under 3 MB: delete pasted pictures and unused sheets, save, and try again.')
    if not data:
        raise WorkbookRefused('That file is empty.')
    return data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def clean(data, verify=True):
    """
    The package rebuilt from its allowlisted parts (core.ooxml.clean), as every upload is before anything opens
    it, .xlsx or .xlsm: (bytes, parts removed). Raises WorkbookRefused.
    """
    try:
        return ooxml.clean(data, max_unzipped=MAX_UNZIPPED, max_parts=MAX_PARTS, verify=verify)
    except ooxml.Refused as e:
        raise WorkbookRefused(str(e))


def strip_macros(data):
    """An .xlsm (or any workbook): the copy without its macros, controls and links. -> (bytes, parts removed)"""
    return clean(data)


def prepare(data, ext):
    """The bytes to read and store: the rebuilt package, whatever it was saved as. -> (bytes, parts removed)"""
    return clean(data)


def _value(v):
    """openpyxl values as Decimal, text, ISO date or None. Floats go through their shortest repr, never kept as float."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return None
        return Decimal(repr(v))
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    if not isinstance(v, str) and hasattr(v, 'text'):  # an array formula (openpyxl ArrayFormula): its text
        v = getattr(v, 'text', '') or ''
    text = str(v).replace('\x00', '')
    return text[:MAX_TEXT]


class Grid:
    """One sheet's cells, {(row, col): value}, for the rectangle the mapping needs."""

    def __init__(self, cells):
        self.cells = cells

    def get(self, row, col):
        return self.cells.get((row, col))


class Book:
    """
    values[sheet] and formulas[sheet]: Grids. `formulas` holds the formula text ('=SUM(...)') where a cell
    has one and the typed value otherwise. `sheetnames` is every sheet in the file, read or not. `extents`
    is how far each sheet was read; `calc` the workbook's calculation settings (calcPr).
    """

    def __init__(self, values, formulas, sheetnames, extents=None, calc=None):
        self.values, self.formulas, self.sheetnames = values, formulas, sheetnames
        self.extents, self.calc = extents or {}, calc or {}

    def within(self, sheet, row, col):
        """Whether the cell was read (a cell beyond the extent has a value Siteline doesn't know)."""
        if sheet not in self.values:
            return False
        ext = self.extents.get(sheet)
        return ext is None or (row <= ext[0] and col <= ext[1])

    def calc_problem(self):
        """Why Excel's saved values can't be trusted to be calculated, or ''."""
        mode = str(self.calc.get('calcMode') or '').strip().lower()
        full = str(self.calc.get('fullCalcOnLoad') or '').strip().lower() in ('1', 'true')
        if mode == 'manual':
            return ('The workbook is set to manual calculation, so the values Excel saved may be out of date and the '
                    'total can\'t be checked. In Excel set calculation to automatic (Formulas > Calculation Options), '
                    'recalculate, save, and upload it again.')
        if full:
            return ('The workbook asks to be recalculated when it is next opened, so the values saved in it may be out '
                    'of date and the total can\'t be checked. Open it in Excel with calculation set to automatic, '
                    'recalculate, save, and upload it again.')
        return ''

    def has(self, sheet):
        return sheet in self.values

    def value(self, sheet, row, col):
        g = self.values.get(sheet)
        return g.get(row, col) if g else None

    def formula(self, sheet, row, col):
        g = self.formulas.get(sheet)
        return g.get(row, col) if g else None

    def is_formula(self, sheet, row, col):
        f = self.formula(sheet, row, col)
        return isinstance(f, str) and f.startswith('=')


def _load(data, data_only):
    import openpyxl
    try:
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=data_only, keep_vba=False,
                                      keep_links=False)
    except Exception:  # openpyxl raises many kinds on a damaged file; all mean the same to the estimator
        raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')


def _grid(ws, max_row, max_col):
    cells = {}
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True), start=1):
        for c, v in enumerate(row, start=1):
            if v is not None:
                cells[(r, c)] = _value(v)
    return Grid(cells)


def open_book(data, extents):
    """
    data: the file's bytes. extents: {sheet name: (max_row, max_col)} to read; the key '*' reads every other sheet
    to that extent (for a layout whose trade tabs are found from its summary). Sheets the file doesn't have are
    left out of the Book (the engine reports them). Raises WorkbookRefused.
    """
    data, _ = clean(data, verify=False)  # whatever the caller passed, openpyxl only sees the rebuilt package
    out, read = {}, {}
    rest = extents.get('*')
    for data_only in (True, False):
        wb = _load(data, data_only)
        try:
            names = list(wb.sheetnames)
            grids = {}
            for sheet in names[:MAX_SHEETS]:
                ext = extents.get(sheet) or rest
                if ext is not None:
                    grids[sheet] = _grid(wb[sheet], *ext)
                    read[sheet] = ext
            out[data_only] = (grids, names)
        except WorkbookRefused:
            raise
        except Exception:  # a crafted sheet (bad shared-string index, a "date" that isn't) fails inside openpyxl
            raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')
        finally:
            wb.close()
    try:
        calc = ooxml.calc_properties(data)
    except ooxml.Refused as e:
        raise WorkbookRefused(str(e))
    return Book(out[True][0], out[False][0], out[True][1], read, calc)
