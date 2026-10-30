"""
Document numbering to BS EN ISO 19650-2 (UK National Annex), as pure functions.

A document number has seven fields joined by hyphens:

    SLN  - ABC        - ZZ     - 02    - DR   - A    - 1001
    project originator  volume  level   type   role   number

Revision (P03, C01) and suitability (S2, A1) are metadata, not part of the number, but
they often appear in file names after it: `SLN-ABC-ZZ-02-DR-A-1001_P03.pdf`,
`SLN-ABC-ZZ-02-DR-A-1001-S2-C01 Ground floor GA.pdf`.

Nothing here touches the database. Checks return plain-English warnings rather than
raising, because consultants deviate from the standard and the user may accept a
number anyway. The default code lists are a sensible UK starting set; each project can
change its own (documents.models.CodeList).
"""
import os
import re

FIELDS = ('project_code', 'originator', 'volume', 'level', 'doc_type', 'role', 'number')
FIELD_LABELS = {
    'project_code': 'Project', 'originator': 'Originator', 'volume': 'Volume / system', 'level': 'Level / location',
    'doc_type': 'Type', 'role': 'Role', 'number': 'Number', 'suitability': 'Suitability',
}
# Fields a project keeps a list of allowed codes for
CODE_FIELDS = ('originator', 'volume', 'level', 'doc_type', 'role', 'suitability')

# (shortest, longest) the UK National Annex expects. Outside these is a warning, not an error.
LENGTHS = {
    'project_code': (2, 6), 'originator': (3, 6), 'volume': (1, 2), 'level': (2, 2),
    'doc_type': (2, 2), 'role': (1, 2), 'number': (4, 6),
}
MAX_FIELD = 12  # what the model stores per field; anything longer can't be a field

DEFAULT_CODES = {
    'volume': [
        ('ZZ', 'All volumes'), ('XX', 'No volume applies'), ('01', 'Volume / zone 1'), ('02', 'Volume / zone 2'),
    ],
    'level': [
        ('ZZ', 'Multiple levels'), ('XX', 'No level applies'), ('B2', 'Basement 2'), ('B1', 'Basement 1'),
        ('00', 'Ground floor'), ('M0', 'Ground floor mezzanine'), ('01', 'Level 1'), ('02', 'Level 2'),
        ('03', 'Level 3'), ('04', 'Level 4'), ('05', 'Level 5'), ('06', 'Level 6'), ('07', 'Level 7'),
        ('08', 'Level 8'), ('09', 'Level 9'), ('10', 'Level 10'), ('RF', 'Roof'),
    ],
    'doc_type': [
        ('AF', 'Animation file'), ('BQ', 'Bill of quantities'), ('CA', 'Calculations'), ('CM', 'Combined model'),
        ('CO', 'Correspondence'), ('CP', 'Cost plan'), ('CR', 'Clash report'), ('DB', 'Database'),
        ('DR', 'Drawing'), ('FN', 'File note'), ('HS', 'Health and safety'), ('IE', 'Information exchange file'),
        ('M2', '2D model'), ('M3', '3D model'), ('MI', 'Minutes / action notes'), ('MR', 'Model rendition'),
        ('MS', 'Method statement'), ('PP', 'Presentation'), ('PR', 'Programme'), ('RD', 'Room data sheet'),
        ('RI', 'Request for information'), ('RP', 'Report'), ('SA', 'Schedule of accommodation'),
        ('SC', 'Schedule of works / scope (contractor code)'), ('SH', 'Schedule'), ('SK', 'Sketch'),
        ('SN', 'Snagging list'), ('SP', 'Specification'), ('SU', 'Survey'), ('VS', 'Visualisation'),
    ],
    'role': [
        ('A', 'Architect'), ('B', 'Building surveyor'), ('C', 'Civil engineer'), ('D', 'Drainage and highways engineer'),
        ('E', 'Electrical engineer'), ('F', 'Facilities manager'), ('G', 'Land surveyor'),
        ('H', 'Heating and ventilation designer'), ('I', 'Interior designer'), ('K', 'Client'),
        ('L', 'Landscape architect'), ('M', 'Mechanical engineer'), ('P', 'Public health engineer'),
        ('Q', 'Quantity surveyor'), ('S', 'Structural engineer'), ('T', 'Town planner'), ('W', 'Contractor'),
        ('X', 'Subcontractor'), ('Y', 'Specialist designer'), ('Z', 'General (more than one discipline)'),
    ],
    'suitability': [
        ('S0', 'Work in progress'), ('S1', 'Suitable for coordination'), ('S2', 'Suitable for information'),
        ('S3', 'Suitable for review and comment'), ('S4', 'Suitable for stage approval'),
        ('A1', 'Authorised and accepted, stage 1'), ('A2', 'Authorised and accepted, stage 2'),
        ('A3', 'Authorised and accepted, stage 3'), ('A4', 'Authorised and accepted, stage 4'),
        ('A5', 'Authorised and accepted, stage 5'),
        ('B1', 'Partially signed off with comments, stage 1'), ('B2', 'Partially signed off with comments, stage 2'),
        ('B3', 'Partially signed off with comments, stage 3'), ('B4', 'Partially signed off with comments, stage 4'),
        ('B5', 'Partially signed off with comments, stage 5'),
        ('CR', 'As constructed record'),
    ],
    # Originators are the companies on the job: nothing sensible to default to
    'originator': [],
}

# Revision codes that can follow the number in a file name: P01, C02, T1 (tender issue)
REVISION_RE = re.compile(r'^[PCT]\d{1,3}$', re.IGNORECASE)
SUITABILITY_RE = re.compile(r'^(S\d|A\d{1,2}|B\d{1,2}|CR)$', re.IGNORECASE)
_FIELD_RE = re.compile(r'^[A-Z0-9]+$')

# Revision statuses (DocumentRevision.status)
WIP, SHARED, PUBLISHED, SUPERSEDED, WITHDRAWN = 'WIP', 'SHARED', 'PUBLISHED', 'SUPERSEDED', 'WITHDRAWN'


def status_for_suitability(code):
    """
    The status a suitability code implies: S0 is work in progress, S1-S4 shared,
    A and B codes and CR published. None for a blank or unknown code.
    """
    code = (code or '').strip().upper()
    if not code:
        return None
    if code == 'S0':
        return WIP
    if re.match(r'^S\d$', code):
        return SHARED
    if code == 'CR' or re.match(r'^[AB]\d{1,2}$', code):
        return PUBLISHED
    return None


def clean_code(value):
    return re.sub(r'\s+', '', value or '').upper()


def split_reference(text):
    """The seven fields of an ISO 19650 number, or None if it doesn't have seven hyphen-separated parts."""
    parts = [clean_code(p) for p in (text or '').strip().split('-')]
    if len(parts) != len(FIELDS) or not all(parts) or any(len(p) > MAX_FIELD for p in parts):
        return None
    if not all(_FIELD_RE.match(p) for p in parts):
        return None
    return dict(zip(FIELDS, parts))


def assemble(parts):
    """Join the fields back into a number. Blank if any field is missing."""
    values = [clean_code(parts.get(f, '')) for f in FIELDS]
    return '-'.join(values) if all(values) else ''


def validate(parts, codes=None, project_code=''):
    """
    Plain-English warnings for a parsed number (an empty list means it conforms).

    codes: {field: set of allowed codes}. A field with no list (or an empty one) is only
    checked for length. project_code: what this project's numbers should start with.
    """
    codes = codes or {}
    warnings = []
    for f in FIELDS:
        value = clean_code(parts.get(f, ''))
        label = FIELD_LABELS[f]
        if not value:
            warnings.append(f'{label} is missing.')
            continue
        lo, hi = LENGTHS[f]
        if f == 'number' and not value.isdigit():
            warnings.append(f'Number "{value}" should be digits only.')
        elif not lo <= len(value) <= hi:
            want = f'{lo}' if lo == hi else f'{lo} to {hi}'
            warnings.append(f'{label} "{value}" should be {want} characters.')
        allowed = codes.get(f)
        if allowed and value not in allowed:
            warnings.append(f'{label} "{value}" isn\'t in this project\'s code list.')
    expected = clean_code(project_code)
    got = clean_code(parts.get('project_code', ''))
    if expected and got and got != expected:
        warnings.append(f'Project code "{got}" doesn\'t match this project ({expected}).')
    return warnings


def validate_suitability(code, codes=None):
    code = clean_code(code)
    if not code:
        return []
    allowed = (codes or {}).get('suitability')
    if allowed and code not in allowed:
        return [f'Suitability "{code}" isn\'t in this project\'s code list.']
    if not allowed and status_for_suitability(code) is None:
        return [f'Suitability "{code}" isn\'t a recognised code (S0-S4, A1-An, B1-Bn or CR).']
    return []


def check_reference(text, codes=None, project_code=''):
    """(parts or None, warnings) for a document number typed by a person or found in a file name."""
    text = (text or '').strip()
    if not text:
        return None, []
    parts = split_reference(text)
    if parts is None:
        return None, [f'"{text}" doesn\'t follow ISO 19650 naming '
                      '(project-originator-volume-level-type-role-number).']
    return parts, validate(parts, codes, project_code)


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

DEFAULT_PATTERN = '{project}-{originator}-{volume}-{level}-{type}-{role}-{number}_{revision}'
PATTERN_TOKENS = {
    'project': 'project_code', 'originator': 'originator', 'volume': 'volume', 'level': 'level',
    'type': 'doc_type', 'role': 'role', 'number': 'number', 'revision': 'revision',
    'suitability': 'suitability', 'title': 'title', 'reference': 'reference',
}
_TOKEN_RE = re.compile(r'\{(\w+)\}')


class PatternError(ValueError):
    pass


def compile_pattern(pattern):
    """
    A project's own file-name pattern as a regex. Tokens are {project} {originator} {volume}
    {level} {type} {role} {number} {revision} {suitability} {title} and {reference} (a whole
    number that doesn't follow ISO, e.g. "1203-A-100"). Hyphens and underscores in the pattern
    each match either; a space matches any run of spaces.
    """
    out, pos, seen = [], 0, set()
    for m in _TOKEN_RE.finditer(pattern):
        out.append(_literal(pattern[pos:m.start()]))
        token = m.group(1).lower()
        if token not in PATTERN_TOKENS:
            raise PatternError(f'Unknown part {{{token}}} in the pattern.')
        if token in seen:
            raise PatternError(f'{{{token}}} appears twice in the pattern.')
        seen.add(token)
        if token == 'title':
            out.append(r'(?P<title>.+?)')
        elif token == 'reference':
            out.append(r'(?P<reference>[A-Za-z0-9][A-Za-z0-9_\-.]*?)')
        else:
            out.append(rf'(?P<{token}>[A-Za-z0-9]+)')
        pos = m.end()
    out.append(_literal(pattern[pos:]))
    if not seen & {'number', 'reference'}:
        raise PatternError('The pattern needs {number} or {reference}.')
    return re.compile('^' + ''.join(out) + '$', re.IGNORECASE)


def _literal(text):
    return ''.join(r'[-_]' if c in '-_' else (r'\s+' if c == ' ' else re.escape(c)) for c in text)


def _strip_ext(filename):
    base = os.path.basename(filename or '')
    stem, ext = os.path.splitext(base)
    return stem if ext and len(ext) <= 6 else base


def parse_filename(filename, pattern=''):
    """
    Read a document number, revision, suitability and title out of a file name.

    Returns a dict: reference (assembled when the name follows ISO), parts (or None),
    revision, suitability, title and notes (things the reader should check). With a
    project pattern, the name must match it; without one the ISO number is read from the
    start of the name and anything after it that looks like a revision or suitability is used.
    """
    stem = _strip_ext(filename).strip()
    result = {'reference': '', 'parts': None, 'revision': '', 'suitability': '', 'title': '', 'notes': []}
    if pattern:
        m = compile_pattern(pattern).match(stem)
        if not m:
            result['notes'].append('The file name doesn\'t match this project\'s naming pattern.')
            return result
        g = {k: v for k, v in m.groupdict().items() if v}
        result['revision'] = clean_code(g.get('revision', ''))
        result['suitability'] = clean_code(g.get('suitability', ''))
        result['title'] = (g.get('title') or '').strip()
        if 'reference' in g:
            result['reference'] = g['reference'].strip()
            result['parts'] = split_reference(result['reference'])
        else:
            parts = {PATTERN_TOKENS[k]: clean_code(v) for k, v in g.items() if PATTERN_TOKENS.get(k) in FIELDS}
            result['parts'] = parts if all(parts.get(f) for f in FIELDS) else None
            result['reference'] = assemble(parts) if result['parts'] else '-'.join(parts.get(f, '') for f in FIELDS
                                                                                     if parts.get(f))
        return result

    # Default: "<number>[_-]<suitability>?[_-]<revision>? <title>?"
    code, _, title = stem.partition(' ')
    title = title.strip().lstrip('-_ ').strip()
    tokens = [t for t in re.split(r'[-_]', code) if t]
    extras = tokens[len(FIELDS):] if len(tokens) >= len(FIELDS) else []
    if len(tokens) >= len(FIELDS):
        parts = split_reference('-'.join(tokens[:len(FIELDS)]))
    else:
        parts = None
    if parts is None:
        # Not ISO: take a trailing revision (and suitability) off the end and keep the rest as the number
        rest = list(tokens)
        while rest and (REVISION_RE.match(rest[-1]) or SUITABILITY_RE.match(rest[-1])) and len(rest) > 1:
            tok = clean_code(rest.pop())
            if REVISION_RE.match(tok) and not result['revision']:
                result['revision'] = tok
            elif SUITABILITY_RE.match(tok) and not result['suitability']:
                result['suitability'] = tok
            else:
                rest.append(tok)
                break
        result['reference'] = '-'.join(rest)
        result['title'] = title
        return result
    result['parts'] = parts
    result['reference'] = assemble(parts)
    result['title'] = title
    for tok in extras:
        tok = clean_code(tok)
        if REVISION_RE.match(tok) and not result['revision']:
            result['revision'] = tok
        elif SUITABILITY_RE.match(tok) and not result['suitability']:
            result['suitability'] = tok
        else:
            result['notes'].append(f'"{tok}" in the file name wasn\'t recognised.')
    return result
