"""
Opening an uploaded pricing workbook safely and reading the cells a mapping names.

The file is untrusted. Before openpyxl sees it the zip is checked by the same code as quote
levelling's importer (levelling.importer._check_zip): a real .xlsx, no VBA project or ActiveX,
no document type declaration (so no XML entities), not a zip bomb (total and per-part unpacked
size, and the unpacked-to-packed ratio). openpyxl parses with defusedxml (installed, see
requirements.txt). The workbook is opened twice, read-only: once for the values Excel last
saved (data_only) and once for the formulas, so the engine can tell a typed figure from a
calculated one and follow simple references. No formula is ever evaluated by openpyxl, macros
are never loaded (keep_vba=False) and external links are dropped (keep_links=False).

Only the sheets the mapping names are read, each only as far as the mapping's last row and
column, so a sheet that claims a million rows costs nothing.
"""
import hashlib
import io
import os
from datetime import date, datetime
from decimal import Decimal

from levelling import importer

MAX_BYTES = 15 * 1024 * 1024
MAX_UNZIPPED = 100 * 1024 * 1024
MAX_TEXT = 2000

ALLOWED = {'.xlsx'}
REFUSED = {
    '.xlsm': 'Macro-enabled workbooks (.xlsm) are refused. In Excel use File > Save a copy > Excel Workbook (.xlsx) '
             'and upload that: the prices are all in the cells, the macros are not needed.',
    '.xltm': 'Macro-enabled templates are refused. Save the priced workbook as an Excel Workbook (.xlsx).',
    '.xltx': 'That is a template. Save the priced workbook as an Excel Workbook (.xlsx) and upload that.',
    '.xls': 'Old-style .xls workbooks can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.xlsb': 'Binary workbooks (.xlsb) can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.ods': 'OpenDocument spreadsheets can\'t be read. Save it as an Excel Workbook (.xlsx) and upload that.',
    '.csv': 'A CSV file holds one sheet. Upload the whole pricing workbook (.xlsx).',
}


class WorkbookRefused(ValueError):
    """The file can't be read safely, or isn't the pricing workbook. The message is for the estimator."""


def check_name(name):
    ext = os.path.splitext(name or '')[1].lower()
    if ext in REFUSED:
        raise WorkbookRefused(REFUSED[ext])
    if ext not in ALLOWED:
        raise WorkbookRefused('Upload the pricing workbook as an Excel Workbook (.xlsx).')
    return ext


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


def _check_zip(data):
    try:
        importer._check_zip(data, max_unzipped=MAX_UNZIPPED)
    except importer.WorkbookRefused as e:
        msg = str(e)
        if 'scope lines' in msg:  # the levelling wording; same check, words for this upload
            msg = 'That workbook is too large to read safely once unpacked. Delete pasted pictures and unused ' \
                  'sheets, save, and try again.'
        raise WorkbookRefused(msg)


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
    has one and the typed value otherwise. `sheetnames` is every sheet in the file, read or not.
    """

    def __init__(self, values, formulas, sheetnames):
        self.values, self.formulas, self.sheetnames = values, formulas, sheetnames

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
    data: the file's bytes. extents: {sheet name: (max_row, max_col)} to read. Sheets the file doesn't have are
    left out of the Book (the engine reports them). Raises WorkbookRefused.
    """
    _check_zip(data)
    out = {}
    for data_only in (True, False):
        wb = _load(data, data_only)
        try:
            names = list(wb.sheetnames)
            grids = {}
            for sheet, (max_row, max_col) in extents.items():
                if sheet in names:
                    grids[sheet] = _grid(wb[sheet], max_row, max_col)
            out[data_only] = (grids, names)
        except WorkbookRefused:
            raise
        except Exception:  # a crafted sheet (bad shared-string index, a "date" that isn't) fails inside openpyxl
            raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')
        finally:
            wb.close()
    return Book(out[True][0], out[False][0], out[True][1])
