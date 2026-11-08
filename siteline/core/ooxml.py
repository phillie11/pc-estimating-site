"""
Making an uploaded Excel workbook safe to open: a parsed, allowlisted rebuild of the package.

    clean, removed = ooxml.clean(data, max_unzipped=..., max_parts=...)

Every .xlsx and .xlsm that Siteline reads goes through this first (quote levelling's scope and quote
uploads, and the handover pack's pricing workbook), and only the rebuilt copy is ever opened or kept.
Nothing is matched as raw bytes: [Content_Types].xml, every .rels and every part that is changed are
parsed (defusedxml: no DTD, no entities; character references and namespaces resolved), and content
types and relationship types are compared case-insensitively after that.

  - The zip: at most max_parts entries, no duplicate names (ignoring case), every name a plain relative
    path, not too big once unpacked in total or per part, no part that unpacks to over 100 times its
    packed size, shared strings and styles (found by their content type) capped.
  - Parts are kept only when reached from the package root through an allowlisted relationship and
    their content type is one allowed for it: the workbook, worksheets, chart sheets, shared strings,
    styles, theme, drawings, charts, images, comments, tables, pivot tables, calc chain, metadata and
    document properties. External relationships are dropped, except hyperlinks.
  - Dropped, with every reference to them: the VBA project and its signatures, ActiveX controls and form
    control properties, custom ribbons, OLE embeddings, external links (DDE included), data connections,
    query tables, web add-ins, slicers, custom XML. References are removed by parsing: the workbook's
    externalReferences; a sheet's controls, oleObjects and webPublishItems; a drawing's form control
    shapes and macro assignments; the legacy VML shapes of form and ActiveX controls (macro links
    included). A macro-enabled workbook is relabelled as a plain one.
  - Refused outright: Excel 4 macro sheets (international ones too) and dialog sheets, found by content
    type or relationship type; Auto_Open, Auto_Close and other auto-run defined names; any relationship
    type that isn't known; a part whose content type doesn't match what points at it.

The rebuilt package is written with fresh zip entries and checked again from the start; it must come
through with nothing to remove. Parts are streamed, so memory stays near the size of the upload.
"""
import codecs
import io
import posixpath
import re
import zipfile
from urllib.parse import unquote
from xml.sax.saxutils import escape, quoteattr

from defusedxml.expatreader import DefusedExpatParser
from xml.sax import SAXException
from xml.sax.handler import ContentHandler

MAX_PARTS = 500
MAX_UNZIPPED = 25 * 1024 * 1024
MAX_WHOLE = 5 * 1024 * 1024          # shared strings and styles: openpyxl parses them whole
MAX_SMALL = 20 * 1024 * 1024         # a part parsed into memory here (workbook, drawing, VML, .rels)
MAX_RATIO = 100
MAX_SHARED_STRINGS = 50000
MAX_ELEMENTS = 200000                # in one parsed part, or in a sheet's parts outside its cells
MAX_RELS = 10000
CHUNK = 256 * 1024
DTD = re.compile(rb'<!\s*(DOCTYPE|ENTITY)', re.I)
UNIQUE_COUNT = re.compile(rb'uniqueCount\s*=\s*["\'](\d+)', re.I)

SML = 'application/vnd.openxmlformats-officedocument.spreadsheetml.'
OD = 'application/vnd.openxmlformats-officedocument.'
XLSX_MAIN = SML + 'sheet.main+xml'
MAINS = {XLSX_MAIN, SML + 'template.main+xml', 'application/vnd.ms-excel.sheet.macroenabled.main+xml',
         'application/vnd.ms-excel.template.macroenabled.main+xml'}
WORKSHEET, CHARTSHEET = SML + 'worksheet+xml', SML + 'chartsheet+xml'
SST, STYLES = SML + 'sharedstrings+xml', SML + 'styles+xml'
VML = OD + 'vmldrawing'
DRAWING = OD + 'drawing+xml'
IMAGES = {'image/png', 'image/jpeg', 'image/gif', 'image/x-emf', 'image/x-wmf', 'image/bmp', 'image/tiff',
          'image/x-icon'}
RICH = {'application/vnd.ms-excel.rdrichvalue+xml', 'application/vnd.ms-excel.rdrichvaluestructure+xml',
        'application/vnd.ms-excel.rdrichvaluetypes+xml', 'application/vnd.ms-excel.richvaluerel+xml',
        'application/vnd.ms-excel.rdrichvaluewebimage+xml', 'application/vnd.ms-excel.rdarray+xml',
        'application/vnd.ms-excel.rdsupportingpropertybag+xml',
        'application/vnd.ms-excel.rdsupportingpropertybagstructure+xml', 'application/vnd.ms-excel.richstyles+xml'}

R = 'http://schemas.openxmlformats.org/officedocument/2006/relationships/'
P = 'http://schemas.openxmlformats.org/package/2006/relationships/'
MS = 'http://schemas.microsoft.com/office/'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
XDR_NS = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'

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
    R + 'vmldrawing': {VML},
    R + 'comments': {SML + 'comments+xml'},
    R + 'table': {SML + 'table+xml'},
    R + 'printersettings': {SML + 'printersettings'},
    R + 'image': IMAGES,
    R + 'chart': {OD + 'drawingml.chart+xml'},
    R + 'chartusershapes': {OD + 'drawingml.chartshapes+xml'},
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
    MS + '2017/06/relationships/rdrichvaluewebimage': RICH,
    MS + '2017/06/relationships/rdarray': RICH,
    MS + '2017/06/relationships/rdsupportingpropertybag': RICH,
    MS + '2017/06/relationships/rdsupportingpropertybagstructure': RICH,
    MS + '2017/06/relationships/richstyles': RICH,
    MS + '2022/10/relationships/richvaluerel': RICH,
}
HYPERLINK = R + 'hyperlink'
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
    MS + '2011/relationships/webextensiontaskpanes', P + 'metadata/thumbnail', P + 'digital-signature/origin',
    MS + '2017/10/relationships/namedsheetview', MS + '2019/04/relationships/documenttask',
    MS + '2020/02/relationships/classificationlabels', MS + '2006/relationships/keymapcustomizations',
}
# Never stripped: refused
REFUSE = {MS + '2006/relationships/xlmacrosheet', MS + '2006/relationships/xlintlmacrosheet', R + 'dialogsheet',
          R + 'macrosheet', R + 'intlmacrosheet'}
REFUSE_TYPES = {'application/vnd.ms-excel.macrosheet+xml', 'application/vnd.ms-excel.intlmacrosheet+xml',
                SML + 'dialogsheet+xml', 'application/vnd.ms-excel.macrosheet', SML + 'macrosheet+xml'}
AUTO_RUN = re.compile(r'^\s*(_xlnm\.)?auto_', re.I)
# A sheet's top-level elements that only hold active content
SHEET_DROP = {'controls', 'oleObjects', 'webPublishItems'}
VML_KEEP = {'Note'}  # comment boxes; every other shape with ClientData is a form or ActiveX control


class Refused(ValueError):
    """kind: notzip, parts, size, dtd, strings, layout, unreadable, macrosheet, autorun, unknown."""

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
    'unreadable': 'That workbook couldn\'t be read. Save it as .xlsx from Excel and try again.',
    'macrosheet': 'That workbook has Excel 4 macro sheets or dialog sheets, so it was refused. Copy the sheets you '
                  'need into a new workbook, save it as .xlsx, and upload that.',
    'autorun': 'That workbook sets a macro to run when it opens (Auto_Open), so it was refused. Copy the sheets you '
               'need into a new workbook, save it as .xlsx, and upload that.',
    'unknown': 'That workbook holds something Siteline doesn\'t recognise, so it was refused. Save a copy as an '
               'Excel Workbook (.xlsx) and upload that.',
}


def refuse(kind):
    raise Refused(kind, MESSAGES[kind])


# --- the parsed tree ------------------------------------------------------------------------------

class Node:
    """An element as written: its qualified name, attributes in order (xmlns ones included), children."""
    __slots__ = ('q', 'attrs', 'kids', 'ns')

    def __init__(self, q, attrs, ns):
        self.q, self.attrs, self.kids, self.ns = q, attrs, [], ns

    def name(self):
        """(namespace, local name)."""
        prefix, _, local = self.q.rpartition(':')
        return self.ns.get(prefix, ''), local

    def is_(self, ns, local):
        return self.name() == (ns, local)

    def elements(self):
        return [k for k in self.kids if isinstance(k, Node)]

    def walk(self):
        yield self
        for k in self.elements():
            yield from k.walk()

    def write(self, out):
        out.append('<' + self.q)
        for k, v in self.attrs:
            out.append(f' {k}={quoteattr(v)}')
        if not self.kids:
            out.append('/>')
            return
        out.append('>')
        for k in self.kids:
            if isinstance(k, Node):
                k.write(out)
            else:
                out.append(escape(k))
        out.append(f'</{self.q}>')


def _scope(parent_ns, attrs):
    ns = parent_ns
    for k, v in attrs:
        if k == 'xmlns' or k.startswith('xmlns:'):
            if ns is parent_ns:
                ns = dict(parent_ns)
            ns[k[6:] if k.startswith('xmlns:') else ''] = v
    return ns


class _Builder(ContentHandler):
    def __init__(self, limit=MAX_ELEMENTS):
        super().__init__()
        self.stack, self.root, self.count, self.limit = [], None, 0, limit

    def startElement(self, name, attrs):
        self.count += 1
        if self.count > self.limit:
            refuse('size')
        items = list(attrs.items())
        parent = self.stack[-1] if self.stack else None
        node = Node(name, items, _scope(parent.ns if parent else {'xml': 'http://www.w3.org/XML/1998/namespace'},
                                        items))
        if parent is not None:
            parent.kids.append(node)
        else:
            self.root = node
        self.stack.append(node)

    def endElement(self, name):
        self.stack.pop()

    def characters(self, content):
        if self.stack:
            kids = self.stack[-1].kids
            if kids and isinstance(kids[-1], str):
                kids[-1] += content
            else:
                kids.append(content)

    ignorableWhitespace = characters


def _parser(handler):
    p = DefusedExpatParser(forbid_dtd=True, forbid_entities=True, forbid_external=True)
    p.setContentHandler(handler)
    return p


def parse(data, limit=MAX_ELEMENTS):
    """Bytes -> the root Node. Raises Refused (a DTD, or not well-formed XML)."""
    b = _Builder(limit)
    try:
        p = _parser(b)
        p.feed(data)
        p.close()
    except Refused:
        raise
    except SAXException as e:
        refuse('dtd' if 'DTD' in type(e).__name__ or 'Entities' in type(e).__name__ else 'unreadable')
    except ValueError:  # defusedxml's DTDForbidden and friends are ValueErrors
        refuse('dtd')
    if b.root is None:
        refuse('unreadable')
    return b.root


def serialise(root):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n']
    root.write(out)
    return ''.join(out).encode('utf-8')


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
            p = None if i.filename.startswith("/") else norm(i.filename)
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

    def read(self, key, cap=MAX_SMALL):
        info = self.parts[key]
        if info.file_size > cap:
            refuse('size')
        try:
            with self.zf.open(info) as fh:
                data = fh.read(info.file_size + 1)[:info.file_size]
        except Exception:  # bad CRC, sizes that lie, unsupported compression: a damaged or crafted file
            refuse('unreadable')
        return data

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
        root = parse(self.read('[content_types].xml', 2 * 1024 * 1024), MAX_RELS)
        self.defaults, self.overrides, self.written = {}, {}, {}
        for e in root.elements():
            a = dict(e.attrs)
            written = (a.get('ContentType') or '').strip()
            ctype = written.lower()
            _, local = e.name()
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
                if head[:2] in (b'\xff\xfe', b'\xfe\xff'):
                    head = head.decode('utf-16', errors='replace').encode('utf-8')
                m = UNIQUE_COUNT.search(head[:4096])
                if (m and int(m.group(1)) > MAX_SHARED_STRINGS) or head.count(b'<si') > MAX_SHARED_STRINGS:
                    refuse('strings')

    def ctype(self, key):
        if key in self.overrides:
            return self.overrides[key]
        return self.defaults.get(posixpath.splitext(key)[1].lstrip('.'), '')

    def rels(self, source):
        """[(id, type lower, target, external)] of a part ('' for the package)."""
        key = rels_for(source).lower()
        if key not in self.parts:
            return []
        root = parse(self.read(key, 4 * 1024 * 1024), MAX_RELS)
        out = []
        for e in root.elements():
            a = dict(e.attrs)
            rtype = (a.get('Type') or '').strip()
            out.append((a.get('Id') or '', rtype.lower(), rtype, a.get('Target') or '',
                        (a.get('TargetMode') or '').strip().lower() == 'external'))
        return out


def _walk(pkg):
    """The parts to keep, their relationships to keep, and the ids dropped from each. Raises Refused."""
    keep = {'': None}            # lower-case part -> its original name ('' is the package)
    kept_rels = {}               # part -> [(id, type, target, external)]
    dropped = {}                 # part -> {relationship ids dropped}
    queue = ['']
    while queue:
        source = queue.pop()
        rows, gone = [], set()
        for rid, rtype, written, target, external in pkg.rels(source):
            if rtype in REFUSE:
                refuse('macrosheet')
            if external:
                if rtype == HYPERLINK:
                    rows.append((rid, written, target, True))
                else:
                    gone.add(rid)
                continue
            path = resolve(source, target)
            key = path.lower() if path else None
            if rtype in DROP or rtype == HYPERLINK:
                gone.add(rid)
                continue
            if rtype not in KEEP:
                refuse('unknown')
            if key is None or key not in pkg.parts:
                gone.add(rid)  # points at nothing: dropped, as Excel would
                continue
            ctype = pkg.ctype(key)
            if ctype not in KEEP[rtype]:
                refuse('unknown')  # a sheet relationship to a VBA project, say
            if source == '' and rtype == R + 'officedocument' and any(
                    r[1].lower() == R + 'officedocument' for r in rows):
                refuse('layout')
            rows.append((rid, written, target, False))
            if key not in keep:
                keep[key] = pkg.parts[key].filename
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
    for k, v in node.attrs:
        prefix, _, _ = k.rpartition(':')
        if prefix and prefix != 'xmlns' and node.ns.get(prefix) == R_NS and v in gone:
            return True
    return False


def _prune(node, drop):
    """Remove child elements drop(child) says to, recursively; then mc:AlternateContent left with nothing."""
    removed = 0
    kids = []
    for k in node.kids:
        if isinstance(k, Node):
            if drop(k):
                removed += 1
                continue
            removed += _prune(k, drop)
            if k.is_(MC_NS, 'AlternateContent') and not any(
                    c.elements() for c in k.elements()):
                removed += 1
                continue
        kids.append(k)
    node.kids = kids
    return removed


def _workbook(root, gone):
    for n in root.walk():
        if n.is_(MAIN_NS, 'definedName'):
            if AUTO_RUN.match(dict(n.attrs).get('name') or ''):
                refuse('autorun')
    return _prune(root, lambda n: n.is_(MAIN_NS, 'externalReferences') or n.is_(MAIN_NS, 'webPublishObjects')
                  or _rel_refs(n, gone))


def _drawing(root, gone):
    """Form control shapes (an a14:compatExt pointing at a VML control) and anything using a dropped part go."""
    anchors = {'twoCellAnchor', 'oneCellAnchor', 'absoluteAnchor'}

    def bad(n):
        return any(d.name()[1] == 'compatExt' or _rel_refs(d, gone) for d in n.walk())

    def drop(n):
        ns, local = n.name()
        return (local in anchors and ns == XDR_NS and bad(n)) or (n.is_(MC_NS, 'AlternateContent') and bad(n)) \
            or (local not in anchors and ns != MC_NS and _rel_refs(n, gone))
    removed = _prune(root, drop)
    for n in root.walk():  # a shape's macro assignment
        attrs = [(k, '' if k.rpartition(':')[2] == 'macro' and v else v) for k, v in n.attrs]
        if attrs != n.attrs:
            n.attrs = attrs
            removed += 1
    return removed


def _vml(root):
    """Shapes of form and ActiveX controls go; comment boxes stay. -> (removed, shapes left)."""
    # By local name, whatever prefix or namespace the VML uses (Excel's VML is loose about both)
    shapes = ('shape', 'rect', 'oval', 'roundrect', 'image', 'line', 'group', 'polyline', 'arc', 'curve')

    def drop(n):
        local = n.name()[1]
        if local == 'FmlaMacro':
            return True
        if local in shapes:
            data = [d for d in n.walk() if d.name()[1] == 'ClientData']
            return any(dict(d.attrs).get('ObjectType') not in VML_KEEP for d in data)
        return False
    removed = _prune(root, drop)
    left = sum(1 for n in root.walk() if n.name()[1] in shapes)
    return removed, left


class _SheetFilter(ContentHandler):
    """
    A worksheet, streamed: the cells (sheetData) are written straight through; every other top-level element
    is parsed into a tree, its active content removed, and written. Elements outside sheetData are capped.
    """

    def __init__(self, out, gone):
        super().__init__()
        self.out, self.gone = out, gone
        self.depth, self.root_ns, self.builder, self.passing, self.removed, self.count = 0, None, None, False, 0, 0
        self.buf = []

    def flush(self, force=False):
        if force or len(self.buf) > 4096:
            self.out.write(''.join(self.buf).encode('utf-8'))
            self.buf = []

    def startElement(self, name, attrs):
        self.depth += 1
        if self.depth == 1:
            items = list(attrs.items())
            self.root_ns = _scope({}, items)
            self.buf.append('<' + name + ''.join(f' {k}={quoteattr(v)}' for k, v in items) + '>')
            return
        if self.depth == 2 and name.rpartition(':')[2] == 'sheetData' and self.root_ns.get(
                name.rpartition(':')[0], '') == MAIN_NS:
            self.passing = True
        if self.passing:
            self.buf.append('<' + name + ''.join(f' {k}={quoteattr(v)}' for k, v in attrs.items()) + '>')
            self.flush()
            return
        if self.depth == 2:
            self.builder = _Builder(MAX_ELEMENTS - self.count)
            self.builder.stack = []
            parent = Node('', [], self.root_ns)
            self.builder.stack.append(parent)
            self.holder = parent
        self.builder.startElement(name, attrs)

    def endElement(self, name):
        if self.depth == 1:
            self.buf.append(f'</{name}>')
        elif self.passing:
            self.buf.append(f'</{name}>')
            if self.depth == 2:
                self.passing = False
        else:
            self.builder.endElement(name)
            if self.depth == 2:
                self.count += self.builder.count
                node = self.holder.kids[-1]
                self.builder = None
                if self.drop(node):
                    self.removed += 1
                else:
                    self.removed += _prune(node, lambda n: _rel_refs(n, self.gone) or (
                        n.name()[1] in SHEET_DROP))
                    if not (node.is_(MC_NS, 'AlternateContent') and not any(c.elements() for c in node.elements())):
                        out = []
                        node.write(out)
                        self.buf.append(''.join(out))
                    else:
                        self.removed += 1
        self.depth -= 1
        self.flush()

    def drop(self, node):
        return node.name()[1] in SHEET_DROP or _rel_refs(node, self.gone)

    def characters(self, content):
        if self.depth == 1 or self.passing:
            self.buf.append(escape(content))
        elif self.builder is not None:
            self.builder.characters(content)

    ignorableWhitespace = characters


def _entry(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _scan_dtd(pkg, key):
    """A part copied as it is: no DTD in it (streamed, so a big sheet costs no memory)."""
    tail = b''
    decoder = None
    for i, block in enumerate(pkg.chunks(key)):
        if i == 0 and block[:2] in (b'\xff\xfe', b'\xfe\xff'):
            decoder = codecs.getincrementaldecoder('utf-16')('replace')
        if decoder:
            block = decoder.decode(block).encode('utf-8')
        if DTD.search(tail + block):
            refuse('dtd')
        tail = block[-64:]


def _rels_xml(rows):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
           'openxmlformats.org/package/2006/relationships">']
    for rid, rtype, target, external in rows:
        out.append(f'<Relationship Id={quoteattr(rid)} Type={quoteattr(rtype)} Target={quoteattr(target)}'
                   + (' TargetMode="External"' if external else '') + '/>')
    out.append('</Relationships>')
    return ''.join(out).encode('utf-8')


def clean(data, max_unzipped=MAX_UNZIPPED, max_parts=MAX_PARTS, verify=True):
    """data -> (the rebuilt package's bytes, number of parts removed). Raises Refused."""
    pkg = _Package(data, max_unzipped, max_parts)
    keep, kept_rels, dropped = _walk(pkg)
    edited = {}
    # Legacy VML first: a VML left with no shapes goes, and so does the sheet's reference to it
    for key in list(keep):
        if key and pkg.ctype(key) == VML:
            try:
                root = parse(pkg.read(key))
            except Refused as e:
                if e.kind == 'dtd':
                    raise
                root = None  # VML Excel can't have written cleanly: its shapes go with it
            removed, left = (_vml(root) if root is not None else (1, 0))
            if root is None or not left:
                del keep[key]
                kept_rels.pop(key, None)
                for source, rows in kept_rels.items():
                    for row in [r for r in rows if not r[3] and (resolve(source, r[2]) or '').lower() == key]:
                        rows.remove(row)
                        dropped[source].add(row[0])
            elif removed:
                edited[key] = serialise(root)
    for key in list(keep):
        if not key:
            continue
        ctype = pkg.ctype(key)
        if ctype in MAINS:
            root = parse(pkg.read(key))
            if _workbook(root, dropped.get(key, set())):
                edited[key] = serialise(root)
        elif ctype == DRAWING:
            root = parse(pkg.read(key))
            if _drawing(root, dropped.get(key, set())):
                edited[key] = serialise(root)
    # Write the new package
    out = io.BytesIO()
    removed = sum(1 for k in pkg.parts if k != '[content_types].xml' and not k.endswith('.rels') and k not in keep)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.'
                 'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/'
                 'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/'
                 'xml"/>']
        for key, name in keep.items():
            if not key:
                continue
            ctype = pkg.ctype(key)
            written = XLSX_MAIN if ctype in MAINS else pkg.written.get(ctype, ctype)
            types.append(f'<Override PartName={quoteattr("/" + norm(name))} ContentType={quoteattr(written)}/>')
        types.append('</Types>')
        z.writestr(_entry('[Content_Types].xml'), ''.join(types).encode('utf-8'))
        for key, name in keep.items():
            rows = kept_rels.get(key) or []
            if key == '' or rows:
                z.writestr(_entry(rels_for(norm(name) if key else '')), _rels_xml(rows))
            if not key:
                continue
            name = norm(name)
            ctype = pkg.ctype(key)
            if key in edited:
                z.writestr(_entry(name), edited[key])
            elif ctype in (WORKSHEET, CHARTSHEET) and _needs_filter(pkg, key, dropped.get(key)):
                with z.open(_entry(name), 'w') as fh:
                    fh.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
                    f = _SheetFilter(fh, dropped.get(key) or set())
                    p = _parser(f)
                    try:
                        for block in pkg.chunks(key):
                            p.feed(block)
                        p.close()
                    except Refused:
                        raise
                    except ValueError:
                        refuse('dtd')
                    except SAXException:
                        refuse('unreadable')
                    f.flush(True)
            else:
                if ctype.endswith('xml') or ctype in (VML,):
                    _scan_dtd(pkg, key)
                with z.open(_entry(name), 'w') as fh:
                    for block in pkg.chunks(key):
                        fh.write(block)
    result = out.getvalue()
    if verify:
        again, more = clean(result, max_unzipped, max_parts, verify=False)
        if more:
            refuse('unknown')
    return result, removed


def _needs_filter(pkg, key, gone):
    """A sheet is parsed and filtered when it pointed at something dropped or holds a controls-type element."""
    if gone:
        return True
    tail = b''
    for block in pkg.chunks(key):
        look = tail + block
        if b'controls' in look or b'oleObjects' in look or b'webPublishItems' in look:
            return True
        tail = block[-32:]
    return False


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
