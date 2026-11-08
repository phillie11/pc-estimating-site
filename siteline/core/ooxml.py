"""
Making an uploaded Excel workbook safe to open: a parsed, allowlisted rebuild of the package.

    clean, removed = ooxml.clean(data, max_unzipped=..., max_parts=...)
    clean, removed, notes = ooxml.clean_report(data, ...)     # notes: what was taken out, for the estimator

Every .xlsx and .xlsm that Siteline reads goes through this first (quote levelling's scope and quote
uploads, and the handover pack's pricing workbook), and only the rebuilt copy is ever opened or kept.
Nothing is matched as raw bytes. [Content_Types].xml, every .rels, the workbook, every worksheet and every
part that is changed are parsed with expat (no DTD, no entity declarations, no external entities; character
references resolved) and namespaces are resolved element by element; content types and relationship types
are compared case-insensitively after that. Every XML part must be UTF-8, as Excel writes it.

  - The zip: at most max_parts entries, no duplicate names (ignoring case), every name a plain relative
    path, not too big once unpacked in total or per part, no part that unpacks to over 100 times its
    packed size, shared strings and styles (found by their content type) capped.
  - Parts are kept only when reached from the package root through an allowlisted relationship and their
    content type is one allowed for it: the workbook, worksheets, chart sheets, shared strings, styles,
    theme, drawings, charts, comments, tables, pivot tables, calc chain, metadata, document properties and
    pictures (PNG, JPEG or GIF, checked by their first bytes). External relationships are dropped, except
    hyperlinks from a sheet, drawing or chart to an http, https or mailto address.
  - Dropped, with every reference to them: the VBA project and its signatures, ActiveX controls and form
    control properties, custom ribbons, OLE embeddings, external links (DDE included), data connections,
    query tables, web add-ins, web images, slicers, custom XML, printer settings, legacy VML drawings
    (form controls, ActiveX pictures and comment boxes: the comments themselves stay), pictures in other
    formats. References are removed from the parsed XML: the workbook's externalReferences; a sheet's
    controls, oleObjects, webPublishItems, legacyDrawing; a drawing's form control shapes and macro
    assignments; anything pointing at a dropped relationship (a chart's externalData, say).
  - Refused outright: Excel 4 macro sheets (international ones too) and dialog sheets, found by content type
    or relationship type; Auto_Open and other auto-run names; formulas that fetch or run something
    (WEBSERVICE, IMAGE, RTD, CALL, REGISTER, EXEC, DDE, HYPERLINK to anything but a web or mail address);
    an unknown relationship type; a part whose content type doesn't match what points at it; a second or
    foreign sheetData; mc:ProcessContent, or mc:Ignorable naming a namespace that isn't Office's; nesting
    deeper than Excel writes; a DTD anywhere; XML that isn't UTF-8.

The rebuilt package is written with fresh zip entries and checked again from the start: the second pass must
find nothing to remove or change. Worksheets are streamed (cells written straight through, everything else
parsed); other parsed parts are capped at a few MB, so memory stays near the size of the upload.
"""
import contextvars
import io
import posixpath
import re
import unicodedata
import zipfile
from urllib.parse import unquote, urlsplit
from xml.parsers import expat
# saxutils is only used to escape output; nothing is parsed with it
from xml.sax.saxutils import escape, quoteattr  # nosec B406

from .excel_functions import ALLOWED as FUNCTIONS
from .excel_functions import NEVER

MAX_PARTS = 500
MAX_UNZIPPED = 25 * 1024 * 1024
MAX_WHOLE = 5 * 1024 * 1024          # shared strings and styles: openpyxl parses them whole
MAX_SMALL = 4 * 1024 * 1024          # a part parsed into memory here (workbook, drawing, chart, .rels)
MAX_RATIO = 100
MAX_SHARED_STRINGS = 50000
MAX_ELEMENTS = 100000                # in one parsed part, or in a sheet outside its cells
MAX_DEPTH = 64                       # element nesting outside sheetData; Excel writes under 20
MAX_NAMESPACES = 5000                # namespace declarations in one part
MAX_TEXT = 8 * 1024 * 1024           # characters of text in one parsed part
MAX_FORMULA = 8192                   # Excel's own limit on a formula's length
MAX_RELS = 10000
CHUNK = 256 * 1024
DTD = re.compile(rb'<!\s*(DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)', re.I)
UNIQUE_COUNT = re.compile(rb'uniqueCount\s*=\s*["\'](\d+)', re.I)
DECLARATION = re.compile(rb'^(?:\xef\xbb\xbf)?\s*<\?xml\s[^>]{0,300}?\?>')
ENCODING = re.compile(rb'encoding\s*=\s*["\']([A-Za-z0-9._-]{1,40})["\']')

SML = 'application/vnd.openxmlformats-officedocument.spreadsheetml.'
OD = 'application/vnd.openxmlformats-officedocument.'
XLSX_MAIN = SML + 'sheet.main+xml'
MAINS = {XLSX_MAIN, SML + 'template.main+xml', 'application/vnd.ms-excel.sheet.macroenabled.main+xml',
         'application/vnd.ms-excel.template.macroenabled.main+xml'}
WORKSHEET, CHARTSHEET = SML + 'worksheet+xml', SML + 'chartsheet+xml'
SST, STYLES = SML + 'sharedstrings+xml', SML + 'styles+xml'
DRAWING = OD + 'drawing+xml'
CHART = OD + 'drawingml.chart+xml'
CHARTSHAPES = OD + 'drawingml.chartshapes+xml'
# Pictures kept, by content type and first bytes; others are dropped with what shows them
PICTURES = {'image/png': (b'\x89PNG\r\n\x1a\n',), 'image/jpeg': (b'\xff\xd8\xff',), 'image/gif': (b'GIF87a', b'GIF89a')}
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/x-emf', 'image/x-wmf', 'image/bmp', 'image/tiff',
               'image/x-icon', 'image/svg+xml', 'image/x-pict', 'image/jpg', 'image/pjpeg'}
RICH = {'application/vnd.ms-excel.rdrichvalue+xml', 'application/vnd.ms-excel.rdrichvaluestructure+xml',
        'application/vnd.ms-excel.rdrichvaluetypes+xml', 'application/vnd.ms-excel.richvaluerel+xml',
        'application/vnd.ms-excel.rdarray+xml', 'application/vnd.ms-excel.rdsupportingpropertybag+xml',
        'application/vnd.ms-excel.rdsupportingpropertybagstructure+xml', 'application/vnd.ms-excel.richstyles+xml'}

R = 'http://schemas.openxmlformats.org/officedocument/2006/relationships/'
P = 'http://schemas.openxmlformats.org/package/2006/relationships/'
MS = 'http://schemas.microsoft.com/office/'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
XDR_NS = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
XML_NS = 'http://www.w3.org/XML/1998/namespace'
OFFICE_NS = ('http://schemas.microsoft.com/office/', 'http://schemas.openxmlformats.org/')

# Relationship type -> the content types its target may have. Lower case.
KEEP = {
    R + 'officedocument': MAINS,
    P + 'metadata/core-properties': {'application/vnd.openxmlformats-package.core-properties+xml'},
    R + 'extended-properties': {OD + 'extended-properties+xml'},
    R + 'custom-properties': {OD + 'custom-properties+xml'},
    R + 'worksheet': {WORKSHEET},
    R + 'chartsheet': {CHARTSHEET},
    R + 'sharedstrings': {SST},
    R + 'styles': {STYLES},
    R + 'theme': {OD + 'theme+xml'},
    R + 'calcchain': {SML + 'calcchain+xml'},
    R + 'sheetmetadata': {SML + 'sheetmetadata+xml'},
    R + 'drawing': {DRAWING},
    R + 'comments': {SML + 'comments+xml'},
    R + 'table': {SML + 'table+xml'},
    R + 'image': IMAGE_TYPES,
    R + 'chart': {CHART},
    R + 'chartusershapes': {CHARTSHAPES},
    MS + '2011/relationships/chartstyle': {'application/vnd.ms-office.chartstyle+xml'},
    MS + '2011/relationships/chartcolorstyle': {'application/vnd.ms-office.chartcolorstyle+xml'},
    R + 'pivottable': {SML + 'pivottable+xml'},
    R + 'pivotcachedefinition': {SML + 'pivotcachedefinition+xml'},
    R + 'pivotcacherecords': {SML + 'pivotcacherecords+xml'},
    MS + '2017/10/relationships/threadedcomment': {'application/vnd.ms-excel.threadedcomments+xml'},
    MS + '2017/10/relationships/person': {'application/vnd.ms-excel.person+xml'},
    MS + '2022/11/relationships/featurepropertybag': {'application/vnd.ms-excel.featurepropertybag+xml'},
    MS + '2017/06/relationships/rdrichvalue': RICH,
    MS + '2017/06/relationships/rdrichvaluestructure': RICH,
    MS + '2017/06/relationships/rdrichvaluetypes': RICH,
    MS + '2017/06/relationships/rdarray': RICH,
    MS + '2017/06/relationships/rdsupportingpropertybag': RICH,
    MS + '2017/06/relationships/rdsupportingpropertybagstructure': RICH,
    MS + '2017/06/relationships/richstyles': RICH,
    MS + '2022/10/relationships/richvaluerel': RICH,
}
HYPERLINK = R + 'hyperlink'
HYPERLINK_SOURCES = {WORKSHEET, DRAWING, CHART}
# Dropped, with what they point at
DROP = {
    MS + '2006/relationships/vbaproject', MS + '2006/relationships/vbaprojectsignature',
    MS + '2014/relationships/vbaprojectsignatureagile', MS + '2020/07/relationships/vbaprojectsignaturev3',
    MS + '2006/relationships/wordvbadata', MS + '2006/relationships/vbadata',
    R + 'control', R + 'ctrlprop', MS + '2006/relationships/activexcontrolbinary',
    MS + '2006/relationships/ui/extensibility', MS + '2007/relationships/ui/extensibility',
    MS + '2006/relationships/ui/usercustomization', MS + '2006/relationships/attachedtoolbars',
    R + 'attachedtoolbars', R + 'oleobject', R + 'package', R + 'externallink', R + 'externallinkpath',
    R + 'connections', R + 'querytable', R + 'customxml', R + 'customxmlprops', R + 'xmlmaps',
    R + 'tablesinglecells', R + 'volatiledependencies', R + 'revisionheaders', R + 'revisionlog',
    R + 'usernames', R + 'diagramdata', R + 'diagramlayout', R + 'diagramquickstyle', R + 'diagramcolors',
    MS + '2007/relationships/diagramdrawing', MS + '2011/relationships/webextension',
    MS + '2011/relationships/webextensiontaskpanes', MS + '2007/relationships/slicer',
    MS + '2007/relationships/slicercache', MS + '2011/relationships/timeline', MS + '2011/relationships/timelinecache',
    P + 'metadata/thumbnail', P + 'digital-signature/origin',
    MS + '2017/10/relationships/namedsheetview', MS + '2019/04/relationships/documenttask',
    MS + '2020/02/relationships/classificationlabels', MS + '2006/relationships/keymapcustomizations',
    R + 'printersettings', R + 'vmldrawing', MS + '2017/06/relationships/rdrichvaluewebimage',
}
# Never stripped: refused
REFUSE = {MS + '2006/relationships/xlmacrosheet', MS + '2006/relationships/xlintlmacrosheet', R + 'dialogsheet',
          R + 'macrosheet', R + 'intlmacrosheet'}
REFUSE_TYPES = {'application/vnd.ms-excel.macrosheet+xml', 'application/vnd.ms-excel.intlmacrosheet+xml',
                SML + 'dialogsheet+xml', 'application/vnd.ms-excel.macrosheet', SML + 'macrosheet+xml'}
AUTO_RUN = re.compile(r'^\s*(_xlnm\.)?auto_', re.I)
# A sheet's top-level elements that only hold active content (and its legacy VML drawings, which go)
SHEET_DROP = {'controls', 'oleObjects', 'webPublishItems', 'legacyDrawing', 'legacyDrawingHF'}
# Formulas that fetch from the web, run code or talk to another program
# A name in a formula: a function, a defined name, a cell, a sheet (matched from a known start: no backtracking)
IDENT = re.compile(r'[A-Za-z_\\][A-Za-z0-9_.\\]*')
SPACES = re.compile('[\\s\u200b-\u200d\u2060\ufeff]*')  # any Unicode space (no-break, zero-width too)
MAX_FORMULA_CHARS = 16 * 1024 * 1024  # formula text checked in one part (repeats are remembered, so cost nothing)
FORMULA_TAGS = {'f', 'formula', 'formula1', 'formula2'}
NUMBER = re.compile(r'^[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$')
TEXT_ESCAPE = str.maketrans({'&': '&amp;', '<': '&lt;', '>': '&gt;'})
ATTR_ESCAPE = str.maketrans({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\n': '&#10;', '\r': '&#13;',
                             '\t': '&#9;'})


class Refused(ValueError):
    """kind: notzip, parts, size, dtd, strings, layout, unreadable, macrosheet, autorun, unknown, fetch, encoding."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


MESSAGES = {
    'notzip': 'That file isn\'t a readable Excel workbook. Save it as .xlsx and try again.',
    'parts': 'That workbook has too many parts to read safely.',
    'size': 'That workbook is too large to read safely once unpacked. Delete pasted pictures and unused sheets, '
            'save, and try again.',
    'dtd': 'That workbook contains a document type declaration, which Excel never writes, so it was refused. Save '
           'it again from Excel as .xlsx.',
    'strings': 'That workbook has too much text to read safely.',
    'layout': 'That workbook isn\'t laid out the way Excel saves one, so it was refused. Save it again from Excel '
              'as .xlsx.',
    'encoding': 'That workbook isn\'t saved the way Excel saves one (its XML isn\'t UTF-8), so it was refused. Save '
                'it again from Excel as .xlsx.',
    'unreadable': 'That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.',
    'macrosheet': 'That workbook has Excel 4 macro sheets or dialog sheets, so it was refused. Copy the sheets you '
                  'need into a new workbook, save it as .xlsx, and upload that.',
    'autorun': 'That workbook sets a macro to run when it opens (Auto_Open), so it was refused. Copy the sheets you '
               'need into a new workbook, save it as .xlsx, and upload that.',
    'unknown': 'That workbook holds something Siteline doesn\'t recognise, so it was refused. Save a copy as an '
               'Excel Workbook (.xlsx) and upload that.',
}


def refuse(kind, message=None):
    raise Refused(kind, message or MESSAGES[kind])


# --- names, scopes, checks ---------------------------------------------------------------------------

def _scope(parent_ns, attrs):
    """The namespaces in scope for an element: its parent's, plus its own xmlns declarations."""
    ns = parent_ns
    for k, v in attrs:
        if k == 'xmlns' or k.startswith('xmlns:'):
            if ns is parent_ns:
                ns = dict(parent_ns)
            ns[k[6:] if k.startswith('xmlns:') else ''] = v
    return ns


def _qname(q, ns, attribute=False):
    prefix, _, local = q.rpartition(':')
    if attribute and not prefix:
        return '', local
    return ns.get(prefix, ''), local


def _check_mc(attrs, ns):
    """mc:ProcessContent is refused; mc:Ignorable and mc:MustUnderstand may only name Office namespaces."""
    for k, v in attrs:
        if ':' not in k or k.startswith('xmlns'):
            continue
        uri, local = _qname(k, ns, True)
        if uri != MC_NS:
            continue
        if local == 'ProcessContent':
            refuse('layout')
        if local in ('Ignorable', 'MustUnderstand'):
            for prefix in v.split():
                if not ns.get(prefix, '').startswith(OFFICE_NS):
                    refuse('layout')


def safe_url(url):
    """An address a hyperlink may keep: http or https with a host, or mailto. Nothing local, nothing scripted."""
    url = (url or '').strip()
    if not url or '\\' in url or url.startswith('//') or any(ord(ch) < 32 for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    return (scheme in ('http', 'https') and bool(parts.netloc)) or (scheme == 'mailto' and bool(parts.path))


def _refuse_function(name, where, why):
    refuse('fetch', f'{where} uses {name[:60]}, {why}, so the workbook was refused. Replace the formula with its '
                    'value, save, and upload it again.')


# A run that could be a name, anywhere (Unicode letters too), for the conservative check
ANY_NAME = re.compile(r'[^\W\d][\w.\\]*')
XL_PREFIX = re.compile(r'_xl[a-z]*\.', re.I)
NEVER_FOLDED = frozenset(n.casefold() for n in NEVER)
# Excel 4 macro functions: a defined name may not take one of these names either
XLM_FUNCTIONS = frozenset('''
ALERT APP.ACTIVATE CALL CANCEL.KEY DIRECTORY ECHO ERROR EVALUATE EXEC EXECUTE FCLOSE FILES FOPEN FORMULA FORMULA.ARRAY
FORMULA.FILL FORMULA.FIND FPOS FREAD FREADLN FSIZE FWRITE FWRITELN GET.BAR GET.CELL GET.CHART.ITEM GET.DEF
GET.DOCUMENT GET.FORMULA GET.LINK.INFO GET.NAME GET.NOTE GET.OBJECT GET.PIVOT.FIELD GET.PIVOT.ITEM GET.PIVOT.TABLE
GET.TOOL GET.TOOLBAR GET.WINDOW GET.WORKBOOK GET.WORKSPACE HALT INITIATE INPUT ON.KEY ON.TIME POKE REGISTER
REGISTER.ID REQUEST RESTART RETURN RUN SEND.KEYS SET.NAME SET.VALUE SHELL TERMINATE UNREGISTER
'''.split())
CACHE_TEXT = 1024          # formulas shorter than this are remembered within one clean()
CACHE_ENTRIES = 20000      # and at most this many


def _fold(name):
    """A name as compared: NFKC, case folded, accents and other combining marks dropped (WEBSERV\u0130CE)."""
    name = unicodedata.normalize('NFKD', unicodedata.normalize('NFKC', name).casefold())
    return ''.join(ch for ch in name if not unicodedata.combining(ch))


def _without_strings(text):
    """The formula with its string literals ("...", "" inside) taken out. One pass."""
    out, i, n = [], 0, len(text)
    while i < n:
        k = text.find('"', i)
        if k < 0:
            out.append(text[i:])
            break
        out.append(text[i:k])
        j = k + 1
        while True:
            q = text.find('"', j)
            if q < 0:
                return None  # an unclosed string
            if q + 1 < n and text[q + 1] == '"':
                j = q + 2
                continue
            break
        out.append(' ')
        i = q + 1
    return ''.join(out)


def _skip(text, i):
    """Where the string, quoted name or bracketed reference starting at i ends (just after it), or -1 if it never
    does. Quotes double to escape; inside brackets ' escapes the next character and brackets nest."""
    n, ch = len(text), text[i]
    if ch in '"\'':
        j = i + 1
        while True:
            k = text.find(ch, j)
            if k < 0:
                return -1
            if k + 1 < n and text[k + 1] == ch:
                j = k + 2
                continue
            return k + 1
    depth, j = 0, i
    while j < n:
        c = text[j]
        if c == "'":
            j += 2
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


BARE_BEFORE = ('', '(', ',', '@', '+', '-')


def _never_anywhere(text, plain=frozenset()):
    """
    The conservative check, for the never-allowed functions only: a name (Unicode letters too; compared after NFKC,
    case folding and dropping marks, after stripping any _xl...: prefixes) that is one of them, in function
    position: followed by ( after any space; or carrying an _xl...: prefix; or standing bare as a whole argument
    (between ( or , and , or ), which is how one is passed by name). Strings, quoted sheet names and bracketed
    references are skipped, and a name followed by ! is a sheet, so 'Risk Register'!B2, PY!B2 and T1[Call] pass.
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'[':
            end = _skip(text, i)
            if end < 0:
                return ('layout', '')
            i = end
            continue
        m = ANY_NAME.match(text, i)
        if not m:
            i += 1
            continue
        raw, end = m.group(0), m.end()
        name, prefixed = raw, False
        while True:
            p = XL_PREFIX.match(name)
            if not p:
                break
            name, prefixed = name[p.end():], True
        folded = _fold(name)
        if folded in NEVER_FOLDED or folded.startswith('get.'):
            after = SPACES.match(text, end).end()
            nxt = text[after] if after < n else ''
            k = i - 1
            while k >= 0 and text[k].isspace():  # only the space just before this name: linear overall
                k -= 1
            before = text[k] if k >= 0 else ''
            if before == '=' and k == 0:
                before = ''  # the formula's own =
            if nxt == '!':
                pass  # a sheet called that: PY!B2
            elif nxt == '(' or prefixed:
                return ('fetch', name, 'which fetches from the internet or a database, or runs another program')
            elif before in BARE_BEFORE and nxt in ('', ',', ')') and raw.casefold() not in plain:
                # Standing alone (the whole formula, or a whole argument, which is how a function is passed by
                # name): refused, unless the workbook itself defines that name or a table of it (SUM(Register))
                return ('fetch', name, 'which fetches from the internet or a database, or runs another program')
        i = end
    return None


def _link_ok(url, whole):
    """A HYPERLINK's typed target: a web or mail address (or a place in the workbook); for a target built with &,
    the typed start must not name a local file, a share or a script."""
    if url.startswith('#'):
        return True
    if whole:
        return safe_url(url)
    low = url.strip().lower()
    if not low:
        return True
    if '\\' in low or low.startswith('//'):
        return False
    scheme = low.split(':', 1)[0] if ':' in low else ''
    return scheme in ('', 'http', 'https', 'mailto') and not (scheme == '' and ':' in low)


def _problem(text, names, plain=frozenset()):
    """Why a formula can't be accepted, or None: (kind, name, why). See formula_problem."""
    if len(text) > MAX_FORMULA:
        return ('layout', '')
    never = _never_anywhere(text, plain)
    if never:
        return never
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"' or ch == "'":
            j = i + 1
            while True:
                k = text.find(ch, j)
                if k < 0:
                    return ('layout', '')
                if k + 1 < n and text[k + 1] == ch:  # a doubled quote inside
                    j = k + 2
                    continue
                break
            i = k + 1
            continue
        if ch == '[':  # a structured or external reference: brackets nest, ' escapes the next character
            depth, j = 0, i
            while j < n:
                c = text[j]
                if c == "'":
                    j += 2
                    continue
                if c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                return ('layout', '')  # an unclosed bracket
            i = j + 1
            continue
        if ch == '|':
            return ('fetch', 'a link to another program (DDE)', 'which runs another program')
        m = IDENT.match(text, i)
        if not m:
            i += 1
            continue
        raw, end = m.group(0), m.end()
        name = raw.upper()
        after = SPACES.match(text, end).end()
        called = after < n and text[after] == '('
        prefixed = False
        while True:
            p = XL_PREFIX.match(name)
            if not p or p.group(0).upper() in ('_XLPM.', '_XLUDF.', '_XLNM.'):
                break
            name, prefixed = name[p.end():], True
        if name.startswith(('_XLPM.', '_XLNM.')):
            pass  # a LAMBDA's own parameter (what it may hold is checked where it is bound)
        elif name.startswith('_XLUDF.'):
            return ('unknown', raw, 'an add-in function')
        elif called or prefixed:
            if name in NEVER:
                return ('fetch', name, 'which fetches from the internet or a database, or runs another program')
            if name not in FUNCTIONS and not (called and not prefixed and raw.casefold() in names):
                # (names holds only the workbook's names whose definition is a LAMBDA)
                return ('unknown', raw, 'which isn\'t one of Excel\'s worksheet functions')
            if called and name == 'HYPERLINK':
                k = SPACES.match(text, after + 1).end()
                if k < n and text[k] == '"':
                    e, j = None, k + 1
                    while True:
                        q = text.find('"', j)
                        if q < 0:
                            break
                        if q + 1 < n and text[q + 1] == '"':
                            j = q + 2
                            continue
                        e = q
                        break
                    if e is not None:
                        rest = SPACES.match(text, e + 1).end()
                        whole = rest < n and text[rest] in ',)'
                        if not _link_ok(text[k + 1:e].replace('""', '"'), whole):
                            return ('fetch', 'HYPERLINK', 'to a local file, a share or a script')
        i = end
    return None


_context = contextvars.ContextVar('ooxml_formulas', default=None)


def formula_problem(text, names=frozenset()):
    """
    Why a formula can't be accepted, or None. Every function it calls, or names with an _xl...: prefix, must be one
    of Excel's own worksheet functions (core.excel_functions) or a name the workbook defines (a LAMBDA, say); never
    one that fetches from the internet, runs code or calls another program, and none of those named anywhere in it
    at all (the conservative check); no DDE links (app|topic!item); a HYPERLINK's typed target not a local file, a
    share or a script. One pass over the text: strings, quoted sheet names and brackets are skipped by finding where
    they end, names matched from where they start. Within one clean(), short formulas are remembered.
    """
    ctx = _context.get()
    if ctx is None:
        return _problem(text, names)
    cache, names, checking, plain = ctx
    if not checking:
        return None  # the verify pass: the same text the first pass checked
    if len(text) < CACHE_TEXT and text in cache:
        return cache[text]
    problem = _problem(text, names, plain)
    if len(text) < CACHE_TEXT and len(cache) < CACHE_ENTRIES:
        cache[text] = problem
    return problem


formula_problem.cache_clear = lambda: None  # nothing is remembered between uploads
formula_problem.cache_info = lambda: 'none kept between uploads; within one, at most %d formulas under %d characters' % (
    CACHE_ENTRIES, CACHE_TEXT)


def check_formula(text, where):
    """Refuse a formula that calls anything but Excel's own worksheet functions or the workbook's names."""
    if not text:
        return
    problem = formula_problem(text)
    if problem:
        if problem[0] == 'layout':
            refuse('layout')
        _refuse_function(problem[1], where, problem[2])


# --- the parsed tree ------------------------------------------------------------------------------

class Node:
    """An element as written: its qualified name, attributes in order (xmlns ones included), children."""
    __slots__ = ('q', 'attrs', 'kids', 'ns')

    def __init__(self, q, attrs, ns):
        self.q, self.attrs, self.kids, self.ns = q, attrs, [], ns

    def name(self):
        """(namespace, local name)."""
        return _qname(self.q, self.ns)

    def is_(self, ns, local):
        return self.name() == (ns, local)

    def elements(self):
        return [k for k in self.kids if isinstance(k, Node)]

    def text(self):
        return ''.join(k for k in self.kids if isinstance(k, str))

    def walk(self):
        """Every element, this one first (iterative: depth costs nothing)."""
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed([k for k in n.kids if isinstance(k, Node)]))

    def write(self, out):
        out('<' + self.q + ''.join(f' {k}={quoteattr(v)}' for k, v in self.attrs))
        if not self.kids:
            out('/>')
            return
        out('>')
        for k in self.kids:
            if isinstance(k, Node):
                k.write(out)  # depth is capped at MAX_DEPTH when parsed
            else:
                out(escape(k))
        out(f'</{self.q}>')


def _parser():
    """expat with no DTD, no entity declarations and no external entities: any of them refuses the part."""
    p = expat.ParserCreate(encoding='UTF-8')
    p.buffer_text = True
    p.buffer_size = 64 * 1024
    p.ordered_attributes = True

    def forbid(*args):
        refuse('dtd')
    p.StartDoctypeDeclHandler = forbid
    p.EntityDeclHandler = forbid
    p.UnparsedEntityDeclHandler = forbid
    p.NotationDeclHandler = forbid
    p.ElementDeclHandler = forbid
    p.AttlistDeclHandler = forbid
    p.ExternalEntityRefHandler = forbid
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    return p


def _pairs(flat):
    return list(zip(flat[0::2], flat[1::2]))


class _Builder:
    """Builds the tree; nesting, elements, namespace declarations and text are capped."""

    def __init__(self, base_ns=None, depth=0):
        self.stack, self.root = [], None
        self.base_ns = base_ns or {'xml': XML_NS}
        self.depth0 = depth
        self.count = self.text = 0
        self.namespaces = set()

    def start(self, name, flat):
        self.count += 1
        if self.count > MAX_ELEMENTS:
            refuse('size')
        if len(self.stack) + self.depth0 >= MAX_DEPTH:
            refuse('layout')
        attrs = _pairs(flat)
        parent = self.stack[-1] if self.stack else None
        ns = _scope(parent.ns if parent else self.base_ns, attrs)
        if parent is None or ns is not parent.ns:  # distinct declarations: Excel repeats xmlns:xm on every x14 rule
            self.namespaces.update((k, v) for k, v in attrs if k == 'xmlns' or k.startswith('xmlns:'))
            if len(self.namespaces) > MAX_NAMESPACES:
                refuse('size')
        _check_mc(attrs, ns)
        node = Node(name, attrs, ns)
        if parent is not None:
            parent.kids.append(node)
        else:
            self.root = node
        self.stack.append(node)

    def end(self, name):
        self.stack.pop()

    def chars(self, data):
        if self.stack:
            self.text += len(data)
            if self.text > MAX_TEXT:
                refuse('size')
            self.stack[-1].kids.append(data)  # pieces, never concatenated: linear in the text


def check_encoding(head):
    """Excel writes UTF-8. A byte-order mark for anything else, a NUL in the first bytes (UTF-16 or UTF-32 with no
    mark) or a declaration naming another encoding is refused."""
    if head[:2] in (b'\xff\xfe', b'\xfe\xff') or head[:4] in (b'\x00\x00\xfe\xff', b'\xff\xfe\x00\x00') \
            or b'\x00' in head[:64]:
        refuse('encoding')
    m = DECLARATION.match(head)
    if m:
        enc = ENCODING.search(m.group(0))
        if enc and enc.group(1).lower().replace(b'_', b'-') not in (b'utf-8', b'utf8'):
            refuse('encoding')


def parse(data):
    """Bytes -> the root Node. Raises Refused (a DTD, nesting, size, not UTF-8, not well-formed XML)."""
    if len(data) > MAX_SMALL:
        refuse('size')
    check_encoding(data[:512])
    b = _Builder()
    p = _parser()
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = b.start, b.end, b.chars
    try:
        p.Parse(data, True)
    except expat.ExpatError:
        refuse('unreadable')
    except RecursionError:
        refuse('layout')
    if b.root is None:
        refuse('unreadable')
    return b.root


def serialise(root):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n']
    root.write(out.append)
    return ''.join(out).encode('utf-8')


def _flags(root, test):
    """{id(node): node or a descendant passes test}, in one bottom-up pass."""
    flags, stack = {}, [(root, False)]
    while stack:
        node, done = stack.pop()
        if done:
            flags[id(node)] = test(node) or any(flags[id(k)] for k in node.kids if isinstance(k, Node))
        else:
            stack.append((node, True))
            stack.extend((k, False) for k in node.kids if isinstance(k, Node))
    return flags


# --- paths ------------------------------------------------------------------------------------------

def norm(name):
    """A zip entry name or relationship target, as a part name: relative, no '..' out of the package."""
    if not name or '\x00' in name or '\\' in name or re.match(r'^[A-Za-z]:', name):
        return None
    p = posixpath.normpath(name.lstrip('/'))
    if p in ('.', '..') or p.startswith('../') or p.startswith('/'):
        return None
    return p


def rels_for(part):
    """The .rels part of a part ('' is the package itself)."""
    d, f = posixpath.split(part)
    return posixpath.join(d, '_rels', f + '.rels')


def resolve(source, target):
    target = unquote(target or '').strip()
    if target.startswith('/'):
        return norm(target)
    return norm(posixpath.join(posixpath.dirname(source), target))


# --- the package --------------------------------------------------------------------------------------

class _Package:
    def __init__(self, data, max_unzipped, max_parts):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zf.infolist()
        except Exception:  # BadZipFile, struct errors, bad sizes: all mean it isn't a workbook
            refuse('notzip')
        if len(infos) > max_parts:
            refuse('parts')
        self.parts = {}  # lower-case part name -> ZipInfo
        for i in infos:
            if i.is_dir():
                continue
            p = None if i.filename.startswith('/') else norm(i.filename)
            if p is None:
                refuse('layout')
            key = p.lower()
            if key in self.parts:
                refuse('layout')  # two entries with one name: which one a reader takes is a guess
            self.parts[key] = i
        if sum(i.file_size for i in self.parts.values()) > max_unzipped:
            refuse('size')
        for i in self.parts.values():
            if i.file_size > max_unzipped or (i.file_size > MAX_RATIO * max(i.compress_size, 1)
                                              and i.file_size > 64 * 1024):
                refuse('size')
        self.types()

    def name(self, key):
        return norm(self.parts[key].filename)

    def read(self, key, cap=MAX_SMALL):
        info = self.parts[key]
        if info.file_size > cap:
            refuse('size')
        try:
            with self.zf.open(info) as fh:
                return fh.read(info.file_size + 1)[:info.file_size]
        except Exception:  # bad CRC, sizes that lie, unsupported compression: a damaged or crafted file
            refuse('unreadable')

    def head(self, key, n=64):
        try:
            with self.zf.open(self.parts[key]) as fh:
                return fh.read(n)
        except Exception:
            refuse('unreadable')

    def chunks(self, key):
        info = self.parts[key]
        try:
            with self.zf.open(info) as fh:
                left = info.file_size
                while left > 0:
                    block = fh.read(min(CHUNK, left))
                    if not block:
                        break
                    left -= len(block)
                    yield block
        except Refused:
            raise
        except Exception:
            refuse('unreadable')

    def types(self):
        if '[content_types].xml' not in self.parts:
            refuse('layout')
        root = parse(self.read('[content_types].xml', 2 * 1024 * 1024))
        self.defaults, self.overrides, self.written = {}, {}, {}
        for e in root.elements():
            a = dict(e.attrs)
            written = (a.get('ContentType') or '').strip()
            ctype = written.lower()
            local = e.name()[1]
            if local == 'Default':
                self.defaults[(a.get('Extension') or '').strip().lower()] = ctype
                self.written[ctype] = written
                if ctype in (SST, STYLES):  # every part with an extension parsed whole: never what Excel writes
                    refuse('layout')
            elif local == 'Override':
                p = norm(unquote(a.get('PartName') or ''))
                if p is not None:
                    self.overrides[p.lower()] = ctype
                    self.written[ctype] = written
        for key in self.parts:
            ctype = self.ctype(key)
            if ctype in REFUSE_TYPES:
                refuse('macrosheet')
            if ctype in (SST, STYLES) and self.parts[key].file_size > MAX_WHOLE:
                refuse('size')
            if ctype == SST:  # whatever the part is called, and whether or not anything points at it
                head = self.read(key, MAX_WHOLE)
                check_encoding(head[:512])
                m = UNIQUE_COUNT.search(head[:4096])
                if (m and int(m.group(1)) > MAX_SHARED_STRINGS) or head.count(b'<si') > MAX_SHARED_STRINGS:
                    refuse('strings')

    def ctype(self, key):
        if key in self.overrides:
            return self.overrides[key]
        return self.defaults.get(posixpath.splitext(key)[1].lstrip('.'), '')

    def rels(self, source):
        """[(id, type lower, type as written, target, external)] of a part ('' for the package)."""
        key = rels_for(source).lower()
        if key not in self.parts:
            return []
        root = parse(self.read(key))
        out = []
        for e in root.elements():
            a = dict(e.attrs)
            rtype = (a.get('Type') or '').strip()
            out.append((a.get('Id') or '', rtype.lower(), rtype, a.get('Target') or '',
                        (a.get('TargetMode') or '').strip().lower() == 'external'))
            if len(out) > MAX_RELS:
                refuse('parts')
        return out


def _picture_ok(pkg, key):
    ctype = pkg.ctype(key)
    return ctype in PICTURES and any(pkg.head(key, 16).startswith(m) for m in PICTURES[ctype])


def _walk(pkg, notes):
    """The parts to keep, the relationships to keep, and the ids dropped from each. Raises Refused."""
    keep = {'': ''}              # lower-case part -> its normalised name ('' is the package)
    kept_rels = {}               # part -> [(id, type as written, target written, external)]
    dropped = {}                 # part -> {relationship ids dropped}
    queue = ['']
    while queue:
        source = queue.pop()
        source_type = pkg.ctype(source) if source else ''
        rows, gone = [], set()
        for rid, rtype, written, target, external in pkg.rels(source):
            if rtype in REFUSE:
                refuse('macrosheet')
            if external:
                link = target.strip()
                inside = link.startswith('#') and '\\' not in link and not any(ord(ch) < 32 for ch in link)
                if rtype == HYPERLINK and source_type in HYPERLINK_SOURCES and (inside or safe_url(link)):
                    rows.append((rid, written, target.strip(), True))
                else:
                    gone.add(rid)
                continue
            if rtype in DROP or rtype == HYPERLINK:
                gone.add(rid)
                continue
            if rtype not in KEEP:
                refuse('unknown')
            path = resolve(source, target)
            key = path.lower() if path else None
            if key is None or key not in pkg.parts:
                gone.add(rid)  # points at nothing: dropped, as Excel would
                continue
            ctype = pkg.ctype(key)
            if ctype not in KEEP[rtype]:
                refuse('unknown')  # a sheet relationship to a VBA project, say
            if ctype in IMAGE_TYPES and not _picture_ok(pkg, key):
                gone.add(rid)
                notes['pictures'] = notes.get('pictures', 0) + 1
                continue
            if source == '' and rtype == R + 'officedocument' and any(
                    r[1].lower() == R + 'officedocument' for r in rows):
                refuse('layout')
            rows.append((rid, written, '/' + path, False))  # written normalised, never as it came
            if key not in keep:
                keep[key] = path
                queue.append(key)
            if len(keep) > MAX_RELS:
                refuse('parts')
        kept_rels[source] = rows
        dropped[source] = gone
    if not any(r[1].lower() == R + 'officedocument' for r in kept_rels.get('', [])):
        refuse('layout')
    return keep, kept_rels, dropped


def _rel_refs(node, gone):
    """True when this element has an r:id, r:embed, r:link... pointing at a dropped relationship."""
    if not gone:
        return False
    for k, v in node.attrs:
        prefix, _, _ = k.rpartition(':')
        if prefix and prefix != 'xmlns' and node.ns.get(prefix) == R_NS and v in gone:
            return True
    return False


def _strip_ref_attrs(node, gone):
    """Remove only the attributes pointing at dropped relationships (a sheet's pageSetup keeps its settings)."""
    keep = [(k, v) for k, v in node.attrs
            if not (k.rpartition(':')[0] and node.ns.get(k.rpartition(':')[0]) == R_NS and v in gone)]
    removed = len(node.attrs) - len(keep)
    node.attrs = keep
    return removed


def _prune(node, drop, gone=frozenset()):
    """Remove child elements drop(child) says to, then mc:AlternateContent left with nothing. -> count."""
    removed = 0
    kids = []
    for k in node.kids:
        if isinstance(k, Node):
            if k.name()[1] in ('pageSetup', 'printOptions') and gone:
                removed += _strip_ref_attrs(k, gone)
            elif drop(k):
                removed += 1
                continue
            removed += _prune(k, drop, gone)
            if k.is_(MC_NS, 'AlternateContent') and not any(c.elements() for c in k.elements()):
                removed += 1
                continue
        kids.append(k)
    node.kids = kids
    return removed


def _formulas(root, where):
    for n in root.walk():
        local = n.name()[1]
        if local in FORMULA_TAGS:
            check_formula(n.text(), where)
        elif local == 'cfvo':
            val = (dict(n.attrs).get('val') or '').strip()
            if val and not NUMBER.match(val):
                check_formula(val, where)


def _workbook(root, gone):
    for n in root.walk():
        if n.is_(MAIN_NS, 'definedName'):
            name = dict(n.attrs).get('name') or ''
            if AUTO_RUN.match(name):
                refuse('autorun')
            bare = name.upper()
            lam = n.text().strip().lstrip('=').lstrip().casefold().startswith('_xlfn.lambda(')
            # Only a LAMBDA name can be called (see _defined_names), so only one could shadow a function; a plain
            # name such as Register, Rate or N refers to cells or a value and stays
            if lam and (bare in FUNCTIONS or bare in NEVER or bare in XLM_FUNCTIONS):
                refuse('fetch', f'The workbook defines a name {name[:60]}, which is also the name of a function, so '
                                'the workbook was refused. Rename it, save, and upload it again.')
            check_formula(n.text(), f'The name {name[:60]}')
    return _prune(root, lambda n: n.is_(MAIN_NS, 'externalReferences') or n.is_(MAIN_NS, 'webPublishObjects')
                  or _rel_refs(n, gone), gone)


def _drawing(root, gone):
    """Form control shapes (an a14:compatExt pointing at a VML control) and anything using a dropped part go."""
    anchors = {'twoCellAnchor', 'oneCellAnchor', 'absoluteAnchor'}
    flags = _flags(root, lambda n: n.name()[1] == 'compatExt' or _rel_refs(n, gone))

    def drop(n):
        ns, local = n.name()
        if (local in anchors and ns == XDR_NS) or (ns == MC_NS and local == 'AlternateContent'):
            return flags[id(n)]
        return ns != MC_NS and _rel_refs(n, gone)
    removed = _prune(root, drop)
    for n in root.walk():  # a shape's macro assignment
        attrs = [(k, '' if k.rpartition(':')[2] == 'macro' and v else v) for k, v in n.attrs]
        if attrs != n.attrs:
            n.attrs = attrs
            removed += 1
    return removed


class _Writer:
    """Buffers text and writes it to a zip entry in blocks."""

    def __init__(self, fh):
        self.fh, self.buf = fh, []

    def __call__(self, s):
        self.buf.append(s)
        if len(self.buf) > 8192:
            self.flush()

    def flush(self):
        if self.buf:
            self.fh.write(''.join(self.buf).encode('utf-8'))
            self.buf = []


class _SheetFilter:
    """
    A worksheet, streamed: the cells (exactly one sheetData, in the spreadsheet namespace) are written straight
    through, their formulas checked as they pass; every other top-level element is parsed into a tree, checked,
    its active content removed, and written. Names are resolved with each element's own namespace scope.
    """

    def __init__(self, out, gone, where):
        self.out, self.gone, self.where = out, gone, where
        self.scopes, self.removed, self.sheet_data, self.count = [], 0, 0, 0
        self.passing, self.builder, self.cell, self.formula = False, None, '', None
        self.fchars = 0
        self.f_tag, self.f_attrs, self.dropped_shared, self.neutralised = '', [], set(), []
        self.shared_masters = set()

    def start(self, name, flat):
        if self.passing and self.builder is None and 'xmlns' not in ' '.join(flat[0::2]):
            # A cell, the common case: no new namespaces, so the parent's scope; written straight out
            ns = self.scopes[-1]
            self.scopes.append(ns)
            prefix, _, local = name.rpartition(':')
            if ns.get(prefix, '') != MAIN_NS:
                refuse('layout')  # only cells in a sheet's cells
            if ':' in ' '.join(flat[0::2]):
                _check_mc(_pairs(flat), ns)
            if local == 'c':
                self.cell = next((flat[i + 1] for i in range(0, len(flat), 2) if flat[i] == 'r'), '')
            tag = '<' + name + ''.join(f' {flat[i]}="{flat[i + 1].translate(ATTR_ESCAPE)}"'
                                       for i in range(0, len(flat), 2)) + '>'
            if local == 'f':  # held back until its text is checked
                self.formula, self.flen, self.f_tag, self.f_attrs = [], 0, tag, _pairs(flat)
                return
            self.out(tag)
            return
        depth = len(self.scopes)
        attrs = _pairs(flat)
        if self.builder is not None:
            self.builder.start(name, flat)
            self.scopes.append(None)
            return
        parent = self.scopes[-1] if self.scopes else {'xml': XML_NS}
        ns = _scope(parent, attrs) if attrs else parent
        self.scopes.append(ns)
        uri, local = _qname(name, ns)
        if depth == 0:
            _check_mc(attrs, ns)
            self.out('<' + name + ''.join(f' {k}={quoteattr(v)}' for k, v in attrs) + '>')
            return
        if self.passing:
            if uri != MAIN_NS:
                refuse('layout')  # only cells in a sheet's cells
            if ns is not parent:
                _check_mc(attrs, ns)
            if local == 'c':
                self.cell = dict(attrs).get('r', '')
            tag = '<' + name + ''.join(f' {k}={quoteattr(v)}' for k, v in attrs) + '>'
            if local == 'f':
                self.formula, self.flen, self.f_tag, self.f_attrs = [], 0, tag, attrs
                return
            self.out(tag)
            return
        if depth == 1 and (uri, local) == (MAIN_NS, 'sheetData'):
            self.sheet_data += 1
            if self.sheet_data > 1:
                refuse('layout')
            _check_mc(attrs, ns)
            self.passing = True
            self.out('<' + name + ''.join(f' {k}={quoteattr(v)}' for k, v in attrs) + '>')
            return
        if local == 'sheetData':
            refuse('layout')  # a sheetData in another namespace, or anywhere but the top of the sheet
        self.scopes[-1] = None
        self.builder = _Builder(parent, depth=1)
        self.builder.count = self.count
        self.builder.start(name, flat)

    def end(self, name):
        ns = self.scopes.pop()
        depth = len(self.scopes)
        if self.builder is not None:
            self.builder.end(name)
            if depth == 1:
                self.count = self.builder.count
                node, self.builder = self.builder.root, None
                self.finish(node)
            return
        if self.passing and self.formula is not None and _qname(name, ns)[1] == 'f':
            self.end_formula(name)
            return
        if depth == 1 and self.passing:
            self.passing = False
        self.out(f'</{name}>')

    def end_formula(self, name):
        """
        A cell's formula: written if it only calls Excel's own functions. One that calls a function Excel doesn't
        have (a macro's, say, which the rebuild has removed: it could only ever show #NAME?) is taken out, and the
        value Excel saved stays in the cell; the result notes it. Anything that fetches or runs something is refused.
        """
        text, self.formula = ''.join(self.formula), None
        self.fchars += len(text)
        if self.fchars > MAX_FORMULA_CHARS:
            refuse('size')
        attrs = dict(self.f_attrs)
        where = f'{self.where}, cell {self.cell or "?"}'
        problem = formula_problem(text) if text else None
        si = attrs.get('si')
        if attrs.get('t') == 'shared' and si is not None:
            if text:
                self.shared_masters.add(si)
            elif si not in self.shared_masters:
                refuse('layout')  # a cell repeating a shared formula before the cell that holds it: never Excel's
        if problem is None and attrs.get('t') == 'shared' and si in self.dropped_shared:
            problem = ('unknown', '', 'the shared formula it repeats was taken out')
        if problem is None:
            self.out(self.f_tag + text.translate(TEXT_ESCAPE) + f'</{name}>')
            return
        if problem[0] != 'unknown':
            if problem[0] == 'layout':
                refuse('layout')
            _refuse_function(problem[1], where, problem[2])
        if attrs.get('t') == 'shared' and si is not None:
            self.dropped_shared.add(si)
        self.removed += 1
        if problem[1]:
            self.neutralised.append(f'{where} ({problem[1][:40]})')

    def finish(self, node):
        uri, local = node.name()
        if local in ('pageSetup', 'printOptions'):
            self.removed += _strip_ref_attrs(node, self.gone)
            node.write(self.out)
            return
        if local in SHEET_DROP or _rel_refs(node, self.gone):
            self.removed += 1
            return
        if any(n.name()[1] == 'sheetData' for n in node.walk()):
            refuse('layout')  # cells anywhere but the sheet's one sheetData
        _formulas(node, self.where)
        self.removed += _prune(node, lambda n: _rel_refs(n, self.gone) or n.name()[1] in SHEET_DROP, self.gone)
        if node.is_(MC_NS, 'AlternateContent') and not any(c.elements() for c in node.elements()):
            self.removed += 1
            return
        node.write(self.out)

    def chars(self, data):
        if self.builder is not None:
            self.builder.chars(data)
            return
        if self.formula is not None:
            self.formula.append(data)
            self.flen += len(data)
            if self.flen > MAX_FORMULA:
                refuse('layout')
            return
        self.out(data.translate(TEXT_ESCAPE))


def _entry(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _copy_xml(pkg, key, fh):
    """An XML part copied as it is: UTF-8, and no DTD in it (streamed, so a big part costs no memory)."""
    tail = b''
    for i, block in enumerate(pkg.chunks(key)):
        if i == 0:
            check_encoding(block[:512])
        if DTD.search(tail + block):
            refuse('dtd')
        tail = block[-64:]
        fh.write(block)


FORMULA_PARTS = {SML + 'table+xml': 'Table', SML + 'pivotcachedefinition+xml': 'Pivot cache'}
FORMULA_ELEMENTS = {'calculatedColumnFormula', 'totalsRowFormula'}
PIVOT_FORMULAS = {'cacheField': ('formula',), 'calculatedItem': ('formula',), 'calculatedMember': ('mdx', 'formula')}


class _FormulaScan:
    """Checks a table's or pivot cache's formulas (element text, cacheField formula="..."), capped like any part."""

    def __init__(self, where):
        self.where, self.depth, self.text, self.chars = where, 0, None, 0

    def start(self, name, flat):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            refuse('layout')
        local = name.rpartition(':')[2]
        if local in FORMULA_ELEMENTS:
            self.text = []
        attrs = dict(_pairs(flat))
        for attr in PIVOT_FORMULAS.get(local, ()):
            if attrs.get(attr):
                self.count(attrs[attr])
                check_formula(attrs[attr], self.where)

    def end(self, name):
        self.depth -= 1
        if self.text is not None and name.rpartition(':')[2] in FORMULA_ELEMENTS:
            text, self.text = ''.join(self.text), None
            check_formula(text, self.where)

    def chars_(self, data):
        if self.text is not None:
            self.text.append(data)
            self.count(data)

    def count(self, data):
        self.chars += len(data)
        if self.chars > MAX_FORMULA_CHARS:
            refuse('size')


def _scan_copy(pkg, key, fh, where):
    """Copy the part as it is, parsing it as it goes: UTF-8, no DTD, its formulas checked."""
    scan = _FormulaScan(where)
    p = _parser()
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = scan.start, scan.end, scan.chars_
    try:
        for i, block in enumerate(pkg.chunks(key)):
            if i == 0:
                check_encoding(block[:512])
            p.Parse(block, False)
            fh.write(block)
        p.Parse(b'', True)
    except expat.ExpatError:
        refuse('unreadable')


def _rels_xml(rows):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
           'openxmlformats.org/package/2006/relationships">']
    for rid, rtype, target, external in rows:
        out.append(f'<Relationship Id={quoteattr(rid)} Type={quoteattr(rtype)} Target={quoteattr(target)}'
                   + (' TargetMode="External"' if external else '') + '/>')
    out.append('</Relationships>')
    return ''.join(out).encode('utf-8')


def _sheet_names(pkg, keep, kept_rels):
    """{lower-case sheet part: the sheet's name}, for messages."""
    names = {}
    for key in keep:
        if key and pkg.ctype(key) in MAINS:
            ids = {rid: target for rid, _, target, external in kept_rels.get(key, []) if not external}
            for n in parse(pkg.read(key)).walk():
                if n.is_(MAIN_NS, 'sheet'):
                    a = dict(n.attrs)
                    rid = next((v for k, v in n.attrs if k.endswith(':id') and n.ns.get(k.rpartition(':')[0]) == R_NS),
                               None)
                    if rid in ids:
                        names[ids[rid].lstrip('/').lower()] = a.get('name') or ''
    return names


XML_TYPES_PARSED_WHEN_POINTING = {CHART, CHARTSHAPES}


def _defined_names(pkg, keep):
    """
    (LAMBDA names, plain names), case folded. A formula may call a name holding a LAMBDA, and nothing else the
    workbook defines (a name called EVALUATE defined as =1 must not unlock EVALUATE(...)). Plain names, and tables'
    names, may stand as a value where a never-allowed function's name would otherwise be refused (SUM(Register)).
    """
    lambdas, plain = set(), set()
    for key in keep:
        ctype = pkg.ctype(key) if key else ''
        if ctype in MAINS:
            for n in parse(pkg.read(key)).walk():
                if n.is_(MAIN_NS, 'definedName'):
                    name = (dict(n.attrs).get('name') or '').casefold()
                    body = n.text().strip().lstrip('=').lstrip().casefold()
                    (lambdas if body.startswith('_xlfn.lambda(') else plain).add(name)
        elif ctype == SML + 'table+xml':
            root = parse(pkg.read(key))
            for attr in ('displayName', 'name'):
                if dict(root.attrs).get(attr):
                    plain.add(dict(root.attrs)[attr].casefold())
    return frozenset(lambdas), frozenset(plain - lambdas)


def _clean(data, max_unzipped, max_parts, check_formulas=True):
    """-> (bytes, parts removed, changes made, notes)."""
    pkg = _Package(data, max_unzipped, max_parts)
    notes = {}
    keep, kept_rels, dropped = _walk(pkg, notes)
    lambdas, plain = _defined_names(pkg, keep)
    token = _context.set(({}, lambdas, check_formulas, plain))
    try:
        return _clean_parts(pkg, keep, kept_rels, dropped, notes)
    finally:
        _context.reset(token)


def _clean_parts(pkg, keep, kept_rels, dropped, notes):
    names = _sheet_names(pkg, keep, kept_rels)
    removed = sum(1 for k in pkg.parts if k != '[content_types].xml' and not k.endswith('.rels') and k not in keep)
    changes = sum(len(g) for g in dropped.values())
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.'
                 'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/'
                 'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/'
                 'xml"/>']
        for key, name in keep.items():
            if key:
                ctype = pkg.ctype(key)
                written = XLSX_MAIN if ctype in MAINS else pkg.written.get(ctype, ctype)
                types.append(f'<Override PartName={quoteattr("/" + name)} ContentType={quoteattr(written)}/>')
        types.append('</Types>')
        z.writestr(_entry('[Content_Types].xml'), ''.join(types).encode('utf-8'))
        for key, name in keep.items():
            rows = kept_rels.get(key) or []
            if key == '' or rows:
                z.writestr(_entry(rels_for(name)), _rels_xml(rows))
            if not key:
                continue
            ctype = pkg.ctype(key)
            gone = dropped.get(key) or set()
            with z.open(_entry(name), 'w') as fh:
                w = _Writer(fh)
                if ctype in MAINS:
                    root = parse(pkg.read(key))
                    changes += _workbook(root, gone)
                    w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
                    root.write(w)
                elif ctype == DRAWING or (gone and ctype in XML_TYPES_PARSED_WHEN_POINTING):
                    root = parse(pkg.read(key))
                    changes += _drawing(root, gone) if ctype == DRAWING else _prune(
                        root, lambda n: _rel_refs(n, gone))
                    w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
                    root.write(w)
                elif ctype in (WORKSHEET, CHARTSHEET):
                    changes += _stream_sheet(pkg, key, w, gone, names.get(key) or name, notes)
                elif ctype in FORMULA_PARTS:
                    w.flush()
                    _scan_copy(pkg, key, fh, f'{FORMULA_PARTS[ctype]} {name.rsplit("/", 1)[-1]}')
                elif ctype.endswith('xml'):
                    w.flush()
                    _copy_xml(pkg, key, fh)
                else:
                    for block in pkg.chunks(key):
                        fh.write(block)
                w.flush()
    return out.getvalue(), removed, changes, notes


def _stream_sheet(pkg, key, w, gone, where, notes):
    w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
    f = _SheetFilter(w, gone, f"Sheet '{where}'")
    p = _parser()
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = f.start, f.end, f.chars
    try:
        for i, block in enumerate(pkg.chunks(key)):
            if i == 0:
                check_encoding(block[:512])
            p.Parse(block, False)
        p.Parse(b'', True)
    except expat.ExpatError:
        refuse('unreadable')
    if not f.sheet_data and pkg.ctype(key) == WORKSHEET:
        refuse('layout')
    notes.setdefault('formulas', []).extend(f.neutralised)
    return f.removed


def clean_report(data, max_unzipped=MAX_UNZIPPED, max_parts=MAX_PARTS):
    """data -> (the rebuilt package's bytes, parts removed, notes for the estimator). Raises Refused."""
    try:
        result, removed, changes, notes = _clean(data, max_unzipped, max_parts)
        # The rebuilt package, from the start: there must be nothing left to remove or change
        # (its formulas are the very text the first pass checked, so they aren't checked twice)
        _, again, more, _ = _clean(result, max_unzipped, max_parts, check_formulas=False)
    except RecursionError:
        refuse('layout')
    if again or more:
        refuse('unknown')
    messages = []
    taken = notes.get('formulas') or []
    if taken:
        messages.append(f'{len(taken)} formula{"s" if len(taken) != 1 else ""} calling a function Excel doesn\'t have '
                        '(a macro\'s, which was removed) taken out, keeping the value Excel saved: '
                        + '; '.join(taken[:5]) + ('…' if len(taken) > 5 else '') + '.')
    if notes.get('pictures'):
        n = notes['pictures']
        messages.append(f'{n} picture{"s" if n != 1 else ""} in a format other than PNG, JPEG or GIF '
                        f'{"were" if n != 1 else "was"} removed.')
    return result, removed, messages


def clean(data, max_unzipped=MAX_UNZIPPED, max_parts=MAX_PARTS, verify=True):
    """data -> (the rebuilt package's bytes, number of parts removed). Raises Refused."""
    if not verify:
        try:
            result, removed, _, _ = _clean(data, max_unzipped, max_parts)
        except RecursionError:
            refuse('layout')
        return result, removed
    result, removed, _ = clean_report(data, max_unzipped, max_parts)
    return result, removed


def calc_properties(data):
    """The workbook's calcPr attributes ({'calcMode': 'manual', ...}) from a package clean() has rebuilt."""
    pkg = _Package(data, max_unzipped=MAX_UNZIPPED * 100, max_parts=MAX_RELS)
    for rid, rtype, written, target, external in pkg.rels(''):
        if rtype == R + 'officedocument' and not external:
            key = (resolve('', target) or '').lower()
            if key in pkg.parts:
                for n in parse(pkg.read(key)).elements():
                    if n.is_(MAIN_NS, 'calcPr'):
                        return dict(n.attrs)
    return {}
