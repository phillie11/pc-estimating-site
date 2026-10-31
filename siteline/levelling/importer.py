"""
Reading scope lines, and a subcontractor's priced copy of them, from Excel (.xlsx) or CSV.

Workbooks are untrusted. They are opened with openpyxl in read-only, data-only mode: no formula
is ever evaluated (a formula cell gives the value Excel last saved for it, or nothing), macros
and external links are never loaded, and macro-enabled or binary formats (.xlsm, .xls, .xlsb...)
are refused before anything is opened. The zip is checked first (size when unpacked, no VBA
project), and only the first MAX_ROWS rows and MAX_COLS columns of the first sheet are read.

Matching to the sheet's lines is by ref, then by description (match_lines).
"""
import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_ROWS = 2000
MAX_COLS = 26             # A to Z
MAX_BYTES = 5 * 1024 * 1024
MAX_UNZIPPED = 25 * 1024 * 1024
MAX_PART = {'xl/sharedstrings.xml': 5 * 1024 * 1024, 'xl/styles.xml': 5 * 1024 * 1024}  # parsed whole by openpyxl
MAX_RATIO = 100           # unpacked : packed, per part
MAX_SHARED_STRINGS = 50000
HEADER_SCAN = 15          # rows searched for the header row
MAX_CELL_TEXT = 500
MAX_MONEY = Decimal('1000000000000')  # anything this big is a typo or an attack; DECIMAL(14, 2) holds < 10^12
MAX_QTY = Decimal('100000000000')     # ScopeLine.qty is DECIMAL(14, 3): < 10^11
DTD = re.compile(rb'<!\s*(DOCTYPE|ENTITY)', re.I)
UNIQUE_COUNT = re.compile(rb'uniqueCount\s*=\s*["\'](\d+)', re.I)

ALLOWED = {'.xlsx', '.csv'}
REFUSED = {
    '.xlsm': 'Macro-enabled workbooks (.xlsm) are refused. Save it as an Excel workbook (.xlsx) and upload that.',
    '.xltm': 'Macro-enabled templates are refused. Save it as an Excel workbook (.xlsx) and upload that.',
    '.xlam': 'Excel add-ins are refused. Save the lines as an Excel workbook (.xlsx).',
    '.xls': 'Old-style .xls workbooks can\'t be read. Save it as an Excel workbook (.xlsx) and upload that.',
    '.xlsb': 'Binary workbooks (.xlsb) can\'t be read. Save it as an Excel workbook (.xlsx) and upload that.',
}

HEADERS = {
    'ref': {'ref', 'reference', 'item', 'item no', 'item ref', 'no', 'number', 'code', 'clause', 'line'},
    'description': {'description', 'desc', 'item description', 'details', 'scope', 'work', 'works'},
    'qty': {'qty', 'quantity', 'quant', 'qnty'},
    'unit': {'unit', 'units', 'uom'},
    'rate': {'rate', 'unit rate', 'rate gbp', 'gbp rate'},
    'total': {'total', 'amount', 'sum', 'price', 'extension', 'line total', 'total gbp', 'gbp', 'value', 'cost'},
}
POSITIONAL = ['ref', 'description', 'qty', 'unit', 'rate', 'total']
INCLUDED_WORDS = {'incl', 'inc', 'included', 'include', 'incl.', 'inc.', 'in', 'included elsewhere'}
TOTAL_ROW = re.compile(r'^(sub[\s-]?)?totals?\b|^carried (to|forward)|^c/f\b|^to collection|^to summary', re.I)


class WorkbookRefused(ValueError):
    """The file can't be read safely, or isn't a scope workbook. The message is for the estimator."""


@dataclass
class Row:
    ref: str = ''
    description: str = ''
    qty: Decimal = None
    unit: str = ''
    rate: Decimal = None
    total: Decimal = None
    included: bool = False

    @property
    def priced(self):
        return self.included or self.rate is not None or self.total is not None


# ---------------------------------------------------------------------------
# Reading the file into raw rows
# ---------------------------------------------------------------------------

def _ext(name):
    return os.path.splitext(name or '')[1].lower()


def check_name(name):
    ext = _ext(name)
    if ext in REFUSED:
        raise WorkbookRefused(REFUSED[ext])
    if ext not in ALLOWED:
        raise WorkbookRefused('Upload an Excel workbook (.xlsx) or a CSV file.')
    return ext


def _read_bytes(f):
    if hasattr(f, 'seek'):
        f.seek(0)
    data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise WorkbookRefused(f'The file is over {MAX_BYTES // (1024 * 1024)} MB. Upload just the scope lines.')
    return data


def _check_zip(data):
    """Before openpyxl sees it: a real .xlsx zip, no VBA project, not a zip bomb."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise WorkbookRefused('That file isn\'t a readable Excel workbook. Save it as .xlsx and try again.')
    with zf:
        infos = zf.infolist()
        if len(infos) > 500:
            raise WorkbookRefused('That workbook has too many parts to read safely.')
        names = [i.filename.lower() for i in infos]
        if any(n.endswith('vbaproject.bin') or n.startswith('xl/activex') for n in names):
            raise WorkbookRefused('That workbook contains macros, so it was refused. Save it as an Excel workbook '
                                  '(.xlsx) without macros and upload that.')
        too_big = WorkbookRefused('That workbook is too large once unpacked. Copy the scope lines into a new '
                                  'workbook and upload that.')
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise too_big
        for i in infos:
            name = i.filename.lower()
            if i.file_size > MAX_PART.get(name, MAX_UNZIPPED):
                raise too_big
            # A part that unpacks to over 100 times its packed size is built to amplify, not to hold a bill
            if i.file_size > MAX_RATIO * max(i.compress_size, 1) and i.file_size > 64 * 1024:
                raise too_big
        for i in infos:
            name = i.filename.lower()
            if not (name.endswith('.xml') or name.endswith('.rels')):
                continue
            data = _read_part(zf, i)
            if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
                data = data.decode('utf-16', errors='replace').encode('utf-8')
            if DTD.search(data):
                raise WorkbookRefused('That workbook contains a document type declaration, which Excel never writes, '
                                      'so it was refused. Save it again from Excel as .xlsx.')
            if name == 'xl/sharedstrings.xml':
                m = UNIQUE_COUNT.search(data[:4096])
                if m and int(m.group(1)) > MAX_SHARED_STRINGS:
                    raise WorkbookRefused('That workbook has too much text to read safely. Copy the scope lines into '
                                          'a new workbook and upload that.')
                if data.count(b'<si') > MAX_SHARED_STRINGS:
                    raise WorkbookRefused('That workbook has too much text to read safely. Copy the scope lines into '
                                          'a new workbook and upload that.')
        if '[content_types].xml' in names:
            types = zf.read(next(i for i in infos if i.filename.lower() == '[content_types].xml'))
            if b'macroEnabled' in types or b'vbaProject' in types:
                raise WorkbookRefused('That workbook is macro-enabled, so it was refused. Save it as an Excel '
                                      'workbook (.xlsx) and upload that.')


def _read_part(zf, info):
    """One part, never more than its declared (already checked) size, whatever the stream really holds."""
    try:
        with zf.open(info) as fh:
            return fh.read(info.file_size + 1)[:info.file_size]
    except Exception:  # bad CRC, sizes that lie, unsupported compression: all a damaged or crafted file
        raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')


def _clean(text):
    """No NUL characters: PostgreSQL text can't hold them."""
    return text.replace('\x00', '')


def _cell(v):
    """openpyxl values as text or Decimal (floats go through their shortest repr, never kept as float)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return _clean(str(v))[:MAX_CELL_TEXT]


def _xlsx_rows(data):
    _check_zip(data)
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_vba=False,
                                    keep_links=False)
    except Exception:  # openpyxl raises many kinds on a damaged file; all mean the same to the estimator
        raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx and try again.')
    try:
        if not wb.worksheets:
            raise WorkbookRefused('That workbook has no sheets.')
        ws = wb.worksheets[0]
        rows = []
        # max_row and max_col bound the read whatever size the sheet claims to be
        for values in ws.iter_rows(min_row=1, max_row=MAX_ROWS + 1, max_col=MAX_COLS, values_only=True):
            rows.append([_cell(v) for v in values])
    except WorkbookRefused:
        raise
    except Exception:  # a crafted sheet (bad shared-string index, a "date" that isn't) fails inside openpyxl
        raise WorkbookRefused('That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.')
    finally:
        wb.close()
    return rows


def _csv_rows(data):
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise WorkbookRefused('That CSV file couldn\'t be read. Save it as CSV (UTF-8) and try again.')
    rows = []
    try:
        for i, row in enumerate(csv.reader(io.StringIO(text))):
            if i > MAX_ROWS:
                break
            row = [_clean(c) for c in row[:MAX_COLS]]
            rows.append([(c[:MAX_CELL_TEXT] if c.strip() else None) for c in row])
    except csv.Error:
        raise WorkbookRefused('That CSV file couldn\'t be read. Save it as CSV (UTF-8) and try again.')
    return rows


def raw_rows(f):
    """The first sheet's cells, as lists of str / Decimal / None. Raises WorkbookRefused."""
    ext = check_name(getattr(f, 'name', ''))
    data = _read_bytes(f)
    rows = _xlsx_rows(data) if ext == '.xlsx' else _csv_rows(data)
    if len(rows) > MAX_ROWS and any(v not in (None, '') for v in rows[MAX_ROWS]):
        raise WorkbookRefused(f'That sheet has more than {MAX_ROWS:,} rows. Upload one trade package at a time.')
    return rows[:MAX_ROWS]


# ---------------------------------------------------------------------------
# Turning raw rows into scope lines
# ---------------------------------------------------------------------------

def _norm_header(v):
    s = str(v or '').lower().replace('£', ' gbp ')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return ' '.join(s.split())


def find_columns(rows):
    """(index of the header row or -1, {field: column index}). Without a header: A-F as ref, description, qty, unit, rate, total."""
    for i, row in enumerate(rows[:HEADER_SCAN]):
        found = {}
        for j, v in enumerate(row):
            h = _norm_header(v)
            for name, words in HEADERS.items():
                if name not in found and h in words:
                    found[name] = j
                    break
        if 'description' in found:
            return i, found
    return -1, {name: j for j, name in enumerate(POSITIONAL)}


def _text(v, limit):
    if v is None:
        return ''
    if isinstance(v, Decimal):
        v = format(v.normalize(), 'f') if v == v.to_integral_value() else str(v)
    return ' '.join(str(v).split())[:limit]


def parse_number(v):
    """A quantity or money cell as Decimal, else None. '£1,250.00' and '1 250' read; 'n/a' doesn't."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        d = v
    else:
        s = str(v).strip().replace('£', '').replace(',', '').replace(' ', '')
        neg = s.startswith('(') and s.endswith(')')
        s = s.strip('()')
        if not s or not re.fullmatch(r'-?\d+(\.\d+)?', s):
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        if neg:
            d = -d
    if not d.is_finite() or abs(d) >= MAX_MONEY:
        return None
    return d


def parse_money(v):
    """A rate or sum, to the penny half up, or None when it isn't one or won't fit the money columns."""
    d = parse_number(v)
    if d is None:
        return None
    d = d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return d if abs(d) < MAX_MONEY else None


def parse_qty(v):
    """A quantity, to 3 decimal places half up, or None when it isn't one or won't fit ScopeLine.qty."""
    d = parse_number(v)
    if d is None:
        return None
    d = d.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    return d if abs(d) < MAX_QTY else None


def is_included(v):
    return isinstance(v, str) and v.strip().lower() in INCLUDED_WORDS


def parse(rows):
    """
    Scope rows from raw rows: (lines, skipped) where skipped counts headings and total rows.
    A row needs a description or a ref. A row with a description but no ref, qty, unit or price
    is a heading; a row whose description starts "Total", "Sub-total", "Carried to..." with no
    ref is a total row. Both are skipped.
    """
    head, cols = find_columns(rows)
    get = lambda row, name: row[cols[name]] if name in cols and cols[name] < len(row) else None  # noqa: E731
    lines, skipped = [], 0
    for row in rows[head + 1:]:
        if not any(v not in (None, '') for v in row):
            continue
        r = Row(ref=_text(get(row, 'ref'), 40), description=_text(get(row, 'description'), 500),
                unit=_text(get(row, 'unit'), 20), qty=parse_qty(get(row, 'qty')))
        rate_v, total_v = get(row, 'rate'), get(row, 'total')
        r.rate, r.total = parse_money(rate_v), parse_money(total_v)
        r.included = r.rate is None and r.total is None and (is_included(rate_v) or is_included(total_v))
        if not r.description and not r.ref:
            continue
        if not r.ref and (TOTAL_ROW.match(r.description) or
                          (r.qty is None and not r.unit and not r.priced)):
            skipped += 1
            continue
        if not r.description:
            r.description = r.ref
        lines.append(r)
    return lines, skipped


def read(f):
    """Scope rows from an uploaded file: (rows, skipped). Raises WorkbookRefused."""
    lines, skipped = parse(raw_rows(f))
    if not lines:
        raise WorkbookRefused('No scope lines found. Put ref, description, qty and unit in the first columns '
                              '(with a header row naming them), one line per row.')
    return lines, skipped


# ---------------------------------------------------------------------------
# Matching to the sheet's lines
# ---------------------------------------------------------------------------

def norm_ref(s):
    return re.sub(r'[\s.]+', '', str(s or '').lower())


def norm_desc(s):
    s = re.sub(r'[^a-z0-9 ]+', ' ', str(s or '').lower())
    return ' '.join(s.split())


def match_lines(existing, incoming):
    """
    existing: [(line id, ref, description)]; incoming: [Row]. Returns {incoming index: line id}.
    By ref first (where the ref is unique on both sides), then by description, each line once.
    """
    def unique(keys):
        seen = {}
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
        return {k for k, n in seen.items() if n == 1 and k}

    ex_refs = unique(norm_ref(r) for _, r, _ in existing)
    in_refs = unique(norm_ref(r.ref) for r in incoming)
    by_ref = {norm_ref(ref): pk for pk, ref, _ in existing if norm_ref(ref) in ex_refs}
    ex_desc = unique(norm_desc(d) for _, _, d in existing)
    by_desc = {norm_desc(d): pk for pk, _, d in existing if norm_desc(d) in ex_desc}
    out, used = {}, set()
    for i, r in enumerate(incoming):
        k = norm_ref(r.ref)
        if k in in_refs and k in by_ref and by_ref[k] not in used:
            out[i] = by_ref[k]
            used.add(by_ref[k])
    for i, r in enumerate(incoming):
        if i in out:
            continue
        pk = by_desc.get(norm_desc(r.description))
        if pk is not None and pk not in used:
            out[i] = pk
            used.add(pk)
    return out
