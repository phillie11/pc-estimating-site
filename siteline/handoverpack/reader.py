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

A macro-enabled workbook (.xlsm) is accepted only to be stripped (strip_macros): the same zip checks
run with the VBA and ActiveX parts let through, those parts and everything that points at them are
dropped, the workbook is relabelled with the macro-free content type, and the result must then pass
the full checks as an ordinary .xlsx. Only that stripped copy is read and stored, named .xlsx; the
original is never kept. Nothing in the VBA project is read or run.
"""
import hashlib
import io
import os
import posixpath
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal

from levelling import importer

MAX_BYTES = 15 * 1024 * 1024
MAX_UNZIPPED = 100 * 1024 * 1024
MAX_TEXT = 2000
MAX_SHEETS = 200

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

# What strip_macros drops from an .xlsm (lower-case part names): the VBA project and its signatures, ActiveX
# controls, form control properties and a custom ribbon (whose buttons call macros)
MACRO_PARTS = re.compile(r'^(xl/vbaproject\.bin|xl/vbaprojectsignature[^/]*\.bin|xl/vbadata\.xml'
                         r'|xl/_rels/vbaproject\.bin\.rels|xl/activex/|xl/ctrlprops/|customui/)')
MACRO_REL_TYPES = (b'/vbaproject', b'/vbaprojectsignature', b'/vbaprojectsignatureagile', b'/vbaprojectsignaturev3',
                   b'/ctrlprop', b'/control', b'/activexcontrol', b'/activexcontrolbinary', b'/ui/extensibility',
                   b'/ui/2007/extensibility')
XLSX_MAIN = b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
MACRO_MAIN = re.compile(rb'application/vnd\.ms-excel\.(?:sheet|template)\.macroEnabled\.main\+xml', re.I)
RELATIONSHIP = re.compile(rb'<Relationship\b[^>]*?(?:/>|>\s*</Relationship\s*>)', re.I)
CT_ENTRY = re.compile(rb'<(?:Override|Default)\b[^>]*?(?:/>|>\s*</(?:Override|Default)\s*>)', re.I)
ATTR = re.compile(rb'([\w:]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
# A sheet's <controls> element holds its ActiveX and form controls, with an mc:AlternateContent inside or around it
CONTROLS = re.compile(rb'<((?:\w+:)?controls)\b[^>]*?(?:/>|>.*?</\1\s*>)', re.S)
EMPTY_ALTERNATE = re.compile(rb'<(\w+):AlternateContent\b[^>]*>\s*(?:<\1:Choice\b[^>]*>\s*</\1:Choice>\s*)?'
                             rb'(?:<\1:Fallback\s*>\s*</\1:Fallback>\s*)?</\1:AlternateContent>', re.S)
VML_MACRO = re.compile(rb'<x:FmlaMacro>.*?</x:FmlaMacro>', re.S)
PRINTER_BIN = (b'<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'
               b'printerSettings"/>')


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


def _check_zip(data, allow_macros=False):
    try:
        importer._check_zip(data, max_unzipped=MAX_UNZIPPED, allow_macros=allow_macros)
    except importer.WorkbookRefused as e:
        msg = str(e)
        if 'scope lines' in msg:  # the levelling wording; same check, words for this upload
            msg = 'That workbook is too large to read safely once unpacked. Delete pasted pictures and unused ' \
                  'sheets, save, and try again.'
        elif 'macro' in msg:
            msg = 'That workbook contains macros but isn\'t named .xlsm, so it was refused. Upload the .xlsm as ' \
                  'Excel saved it (its macros are removed), or save it as an Excel Workbook (.xlsx).'
        raise WorkbookRefused(msg)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:  # opened (and its size checked) just above
        if any(i.filename.lower().startswith(('xl/macrosheets/', 'xl/dialogsheets/')) for i in zf.infolist()):
            raise WorkbookRefused('That workbook has Excel 4 macro sheets, so it was refused. Copy the pricing sheets '
                                  'into a new workbook, save it as .xlsx, and upload that.')


def _attrs(tag):
    """An element's attributes, {local name lower-case: value}, from its raw bytes."""
    return {m.group(1).split(b':')[-1].lower(): m.group(2) if m.group(2) is not None else m.group(3)
            for m in ATTR.finditer(tag)}


def _rel_target(rels_name, target):
    """Where a relationship points, as a lower-case part name. xl/worksheets/_rels/sheet1.xml.rels + ../a.xml."""
    if target.startswith('/'):
        path = target.lstrip('/')
    else:
        path = posixpath.join(posixpath.dirname(posixpath.dirname(rels_name)), target)
    return posixpath.normpath(path).lower()


def strip_macros(data):
    """
    An .xlsm's bytes -> (.xlsx bytes, number of parts removed). Runs the zip checks with the macro parts allowed,
    drops those parts and every relationship, content type and <controls> element that points at them, switches
    the workbook to the macro-free content type, and checks the result again as an ordinary .xlsx. Raises
    WorkbookRefused. Nothing is opened as a spreadsheet and nothing in the VBA project is read.
    """
    _check_zip(data, allow_macros=True)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        infos = zin.infolist()
        dropped = {i.filename.lower() for i in infos if MACRO_PARTS.match(i.filename.lower())}
        bins_left = any(i.filename.lower().endswith('.bin') and i.filename.lower() not in dropped for i in infos)

        def keep_rel(rels_name):
            def sub(m):
                a = _attrs(m.group(0))
                if a.get(b'targetmode', b'').lower() == b'external':
                    return m.group(0)
                path = _rel_target(rels_name, a.get(b'target', b'').decode('utf-8', 'replace'))
                if path in dropped or MACRO_PARTS.match(path) or a.get(b'type', b'').lower().endswith(MACRO_REL_TYPES):
                    return b''
                return m.group(0)
            return sub

        def keep_type(m):
            a = _attrs(m.group(0))
            part = a.get(b'partname', b'').decode('utf-8', 'replace').lstrip('/').lower()
            ctype = a.get(b'contenttype', b'').lower()
            if (part and (part in dropped or MACRO_PARTS.match(part))) or b'vbaproject' in ctype \
                    or b'activex' in ctype or b'controlproperties' in ctype:
                return b''
            return MACRO_MAIN.sub(XLSX_MAIN, m.group(0))

        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                name = info.filename.lower()
                if name in dropped or info.is_dir():
                    continue
                raw = importer._read_part(zin, info)
                if name.endswith('.rels'):
                    raw = RELATIONSHIP.sub(keep_rel(name), raw)
                elif name == '[content_types].xml':
                    raw = CT_ENTRY.sub(keep_type, raw)
                    if bins_left and not re.search(rb'<Default\b[^>]*Extension\s*=\s*["\']bin["\']', raw, re.I):
                        raw = raw.replace(b'</Types>', PRINTER_BIN + b'</Types>')
                elif name.startswith('xl/worksheets/') and name.endswith('.xml'):
                    raw = EMPTY_ALTERNATE.sub(b'', CONTROLS.sub(b'', raw))
                elif name.endswith('.vml'):
                    raw = VML_MACRO.sub(b'', raw)
                zout.writestr(info, raw)
    clean = out.getvalue()
    try:
        _check_zip(clean)
    except WorkbookRefused:
        raise WorkbookRefused('Siteline couldn\'t take the macros out of that workbook safely. In Excel use File > '
                              'Save a copy > Excel Workbook (.xlsx) and upload that.')
    return clean, len(dropped)


def prepare(data, ext):
    """The bytes to read and store: an .xlsm stripped of its macros, anything else as it came. -> (bytes, removed)"""
    if ext == '.xlsm':
        return strip_macros(data)
    return data, 0


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
    data: the file's bytes. extents: {sheet name: (max_row, max_col)} to read; the key '*' reads every other sheet
    to that extent (for a layout whose trade tabs are found from its summary). Sheets the file doesn't have are
    left out of the Book (the engine reports them). Raises WorkbookRefused.
    """
    _check_zip(data)
    out = {}
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
            out[data_only] = (grids, names)
        except WorkbookRefused:
            raise
        except Exception:  # a crafted sheet (bad shared-string index, a "date" that isn't) fails inside openpyxl
            raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')
        finally:
            wb.close()
    return Book(out[True][0], out[False][0], out[True][1])
