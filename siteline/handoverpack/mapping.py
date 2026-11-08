"""
Where things are in a company's pricing workbook, as data.

Every cell the reader looks at comes from a mapping, never from a cell reference written into the
reader. Two layouts are built in:

  STANDARD         the standard internal pricing workbook (PRICING SUMMARY with an Include? column, one
                   tab per trade with a bidder number in D4, Prelim Book, JOINERY SUMMARY, Clarifications)
  SUPPLIER_SELECT  the older pricing document: no Include? column, the supplier picked by name on each
                   trade tab (W2, through Excel's FILTER), joinery on numbered workshop sheets

A company whose workbook is laid out differently gets its own mapping (PricingSettings.custom_mapping,
the same shape as one of these), checked by validate() before it is used. There is no editor for it
yet. detect_layout() says which built-in layout an uploaded file looks like: when it is confident and the
company's setting is a different preset, the upload is read with the detected layout (services.read).

`calc` names how the workbook calculates (which formulas its cells hold): standard-v1 is engine.py,
select-v1 is supplier_select.py. A mapping can move cells around but must keep the same arithmetic until
another model is written.
"""
import copy
import re

from openpyxl.utils import column_index_from_string

CELL = re.compile(r'^\$?([A-Z]{1,3})\$?([1-9][0-9]{0,5})$')
COLUMN = re.compile(r'^[A-Z]{1,3}$')

CALC_STANDARD = 'standard-v1'

TRADE_TABS = [
    'Builderswork', 'BWIC', 'Cladding', 'Cleaning', 'Civils Groundworks', 'Decoration', 'Demolition', 'Electrical',
    'External Spraying', 'Fire Security', 'Firestopping', 'Flooring', 'Solar Film', 'Hoarding', 'Kitchen Extraction',
    'Lifts', 'Line Marking', 'Mechanical', 'MEP', 'Metalwork', 'Mezzanine', 'Partitions Ceilings',
    'Plastering Rendering', 'Plumbing', 'Resin Flooring', 'Roofing', 'Scaffolding', 'Screeding', 'Shopfronts', 'Doors',
    'Signage', 'Ironmongery', 'Prov Sums', 'Tiling', 'IPS', 'Wood Flooring',
]

STANDARD = {
    'key': 'standard',
    'name': 'Standard pricing workbook',
    'calc': CALC_STANDARD,
    'summary': {
        'sheet': 'PRICING SUMMARY',
        'header': {'project': 'D4', 'date': 'F4', 'client': 'D5', 'revision': 'F5', 'estimator': 'D6'},
        'default_markup': 'J4',
        'prelim_markup': 'J5',
        'target_margin': 'J6',
        # Summary row per trade tab, in the order the summary lists them
        'trades': [{'row': 11 + i, 'sheet': name} for i, name in enumerate(TRADE_TABS)],
        'cols': {'supplier': 'B', 'include': 'C', 'name': 'D', 'cost': 'E', 'markup': 'F', 'charge': 'G',
                 'profit': 'H'},
        'joinery_row': 47,
        'prelims_row': 48,
        'total_row': 50,
        'final_price': 'G54',
    },
    'trade': {
        'bidder': 'D4',
        'supplier_names': ['F5', 'J5', 'N5', 'R5'],
        'selected_name': 'W4',
        'markup': 'AE4',
        'first_row': 8,
        'last_row': 57,
        'cols': {'ref': 'B', 'description': 'C', 'qty': 'D', 'unit': 'E'},
        'slots': [
            {'rate': 'F', 'adj': 'G', 'quote': 'H', 'comment': 'I'},
            {'rate': 'J', 'adj': 'K', 'quote': 'L', 'comment': 'M'},
            {'rate': 'N', 'adj': 'O', 'quote': 'P', 'comment': 'Q'},
            {'rate': 'R', 'adj': 'S', 'quote': 'T', 'comment': 'U'},
        ],
        'selected': {'rate': 'Y', 'adj': 'Z', 'cost': 'AA', 'tender_rate': 'AC', 'charge': 'AD', 'status': 'AE'},
        'total_row': 58,
        'total_cols': {'cost': 'AA', 'charge': 'AD'},
        # Tabs whose lines are provisional sums, not measured work
        'provisional': ['Prov Sums'],
    },
    'prelims': {
        'sheet': 'Prelim Book',
        'duration': 'D4',
        'markup': 'I4',
        'first_row': 10,
        'last_row': 135,
        'cols': {'include': 'B', 'description': 'C', 'allocation': 'D', 'qty': 'E', 'unit': 'F',
                 'periods_input': 'G', 'periods_used': 'H', 'rate': 'I', 'cost': 'J', 'markup': 'K', 'charge': 'L',
                 'status': 'M', 'basis': 'N'},
        'weeks_unit': 'weeks',
        'total_row': 138,
        'total_cols': {'cost': 'J', 'charge': 'L'},
    },
    'joinery': {
        'sheet': 'JOINERY SUMMARY',
        'markups': {'material': 'D4', 'manufacture': 'H4', 'install': 'L4'},
        'first_row': 8,
        'last_row': 47,
        'cols': {'ref': 'B', 'description': 'C', 'qty': 'D', 'unit': 'E', 'material': 'F', 'ancillary': 'G',
                 'manufacture_hrs': 'H', 'hourly': 'I', 'install_hrs': 'J', 'install_hourly': 'K', 'cost': 'L',
                 'charge': 'M', 'status': 'N', 'notes': 'O'},
        'total_row': 48,
        'total_cols': {'cost': 'L', 'charge': 'M'},
    },
    'clarifications': {
        'sheet': 'Clarifications',
        'first_row': 8,
        'last_row': 200,
        'cols': {'ref': 'B', 'trade': 'C', 'text': 'D', 'response': 'E', 'status': 'F', 'cost_effect': 'G',
                 'owner': 'H'},
    },
    # The words the workbook uses in its Yes/No and status cells (compared without regard to case, as Excel does)
    'words': {'yes': 'Yes', 'no': 'No', 'priced': 'Priced', 'review': 'Review', 'excluded': 'Excluded'},
}

CALC_SELECT = 'select-v1'

# The older pricing document: the supplier on each trade tab is chosen by name (W2) and Excel's FILTER function
# copies that supplier's Rate and Est Adj columns into Y and Z. The summary has no Include? column (every trade
# row counts), its rows are found by their labels, and each trade tab's total row is the one the summary links to.
# Joinery is priced on numbered workshop sheets ('1', '2'...) that JOINERY SUMMARY links to. See supplier_select.py.
SUPPLIER_SELECT = {
    'key': 'supplier-select',
    'name': 'Pricing document (supplier-select layout)',
    'calc': CALC_SELECT,
    'summary': {
        'sheet': 'PRICING SUMMARY',
        'header': {'project': 'D2', 'date': 'D3', 'revision': 'D4'},
        'default_markup': 'F4',
        'joinery_markups': {'material': 'E4', 'manufacture': 'G4'},
        'first_row': 7,
        'scan_to': 150,
        # The summary rows a trade tab's markup is looked up in (its AE2: INDEX/MATCH over D7:D45)
        'markup_lookup': [7, 45],
        'cols': {'supplier': 'B', 'name': 'D', 'cost': 'E', 'markup': 'F', 'charge': 'G', 'flag': 'H',
                 'notes': 'I'},
        # Rows found by the label in the name column (the first that starts with it, below the trades)
        'labels': {'joinery': 'Joinery', 'installation': 'Installation', 'prelims': 'Prelims', 'total': 'Total',
                   'profit': 'Profit', 'percentage': 'Percentage'},
        # On the Profit and Percentage rows: profit and margin in F, the sheet's own target in G, F - G in I
        'profit_col': 'F', 'margin_col': 'F', 'target_col': 'G', 'target_difference_col': 'I',
        # Below the total: labels in F, values in G
        'check_label_col': 'F', 'check_value_col': 'G',
        'check_labels': {'checked_total': 'Checked Total', 'client_total': 'Client Document Total',
                         'client_difference': 'Difference'},
    },
    'trade': {
        'name': 'B2',
        'selected_name': 'W2',
        'markup': 'AE2',
        'supplier_names': ['F2', 'J2', 'N2', 'R2'],
        'first_row': 4,
        'max_row': 400,
        'total_label_col': 'C',
        'total_label': 'TOTAL',
        'cols': {'ref': 'B', 'description': 'C', 'qty': 'D', 'unit': 'E'},
        'slots': [
            {'rate': 'F', 'adj': 'G', 'quote': 'H', 'comment': 'I'},
            {'rate': 'J', 'adj': 'K', 'quote': 'L', 'comment': 'M'},
            {'rate': 'N', 'adj': 'O', 'quote': 'P', 'comment': 'Q'},
            {'rate': 'R', 'adj': 'S', 'quote': 'T', 'comment': 'U'},
        ],
        'selected': {'qty': 'W', 'rate': 'Y', 'adj': 'Z', 'cost': 'AA', 'tender_rate': 'AC', 'charge': 'AD'},
        'total_cols': {'cost': 'AA', 'charge': 'AD'},
        'provisional': ['Prov Sums'],
    },
    'prelims': {
        'sheet': 'Prelim Book',
        'duration': 'C4',
        'first_row': 1,
        'last_row': 400,
        'cols': {'section': 'B', 'description': 'C', 'allocation': 'D', 'qty': 'F', 'unit': 'G', 'rate': 'I',
                 'cost': 'K', 'charge': 'P'},
        'weeks_unit': 'weeks',
    },
    'joinery': {
        'sheet': 'JOINERY SUMMARY',
        'first_row': 7,
        'last_row': 200,
        'total_label': 'Total',
        'cols': {'ref': 'B', 'description': 'C', 'qty': 'D', 'unit': 'E', 'supply_cost': 'F', 'supply_charge': 'G',
                 'install_cost': 'H', 'install_charge': 'I', 'rate': 'J', 'total': 'K'},
    },
    # Numbered workshop sheets: cost build-up in R, charge in T, by row
    'workshops': {
        'pattern': r'^[0-9]{1,3}$',
        'description': 'B1',
        'cost_col': 'R',
        'charge_col': 'T',
        'parts': {'labour': 2, 'materials': 3, 'ancillary': 4, 'finish': 5, 'glass': 6, 'fabric': 7},
        'rows': {'supply': 11, 'delivery': 13, 'install': 14, 'total': 16},
        'max_row': 60,
    },
    'clarifications': {
        'sheet': 'Clarifications',
        'first_row': 8,
        'last_row': 300,
        # A number in A starts a clarification; its heading is in B and the text in B on the rows below it
        'cols': {'ref': 'A', 'text': 'B'},
    },
    'words': {'yes': 'Yes', 'no': 'No', 'priced': 'Priced', 'review': 'Review', 'excluded': 'Excluded'},
    # A selected supplier with one of these names is the estimator's own allowance or a provisional sum
    'allowance_words': ['allowance'],
    'ps_words': ['provisional sum', 'prov sum', 'client ps', 'ps'],
}

PRESETS = {STANDARD['key']: STANDARD, SUPPLIER_SELECT['key']: SUPPLIER_SELECT}
PRESET_CHOICES = [(k, v['name']) for k, v in PRESETS.items()]
MAX_ROW = 2000
MAX_TRADES = 80


class MappingError(ValueError):
    """The mapping can't be used. The message is for whoever set it up."""


def split(coord):
    """'AE4' -> (4, 31). Raises MappingError for anything that isn't a single cell."""
    m = CELL.match(str(coord or '').strip().upper())
    if not m:
        raise MappingError(f'{coord!r} is not a cell reference such as D4.')
    row = int(m.group(2))
    if row > MAX_ROW:
        raise MappingError(f'{coord} is further down than the reader looks (row {MAX_ROW}).')
    return row, column_index_from_string(m.group(1))


def col(letter):
    if not COLUMN.match(str(letter or '')):
        raise MappingError(f'{letter!r} is not a column letter such as C.')
    return column_index_from_string(letter)


def _need(d, path, keys):
    for k in keys:
        if k not in d:
            raise MappingError(f'The mapping is missing {path}{k}.')


def validate(m):
    """Check a mapping's shape and every cell and column in it. Returns it; raises MappingError."""
    if not isinstance(m, dict):
        raise MappingError('A mapping is a JSON object.')
    _need(m, '', ('key', 'name', 'calc', 'summary', 'trade', 'prelims', 'joinery', 'clarifications', 'words'))
    if m['calc'] == CALC_SELECT:
        return validate_select(m)
    if m['calc'] != CALC_STANDARD:
        raise MappingError(f'Unknown calculation model {m["calc"]!r}.')
    s, t, p, j, c = m['summary'], m['trade'], m['prelims'], m['joinery'], m['clarifications']
    _need(s, 'summary.', ('sheet', 'header', 'default_markup', 'prelim_markup', 'target_margin', 'trades', 'cols',
                          'joinery_row', 'prelims_row', 'total_row', 'final_price'))
    for ref in [s['default_markup'], s['prelim_markup'], s['target_margin'], s['final_price'], *s['header'].values()]:
        split(ref)
    _need(s['cols'], 'summary.cols.', ('supplier', 'include', 'name', 'cost', 'markup', 'charge', 'profit'))
    for letter in s['cols'].values():
        col(letter)
    if not isinstance(s['trades'], list) or not s['trades'] or len(s['trades']) > MAX_TRADES:
        raise MappingError(f'summary.trades must list between 1 and {MAX_TRADES} trade tabs.')
    for row in [s['joinery_row'], s['prelims_row'], s['total_row'], *(x['row'] for x in s['trades'])]:
        split(f'A{row}')
    names = [x['sheet'] for x in s['trades']]
    if len(set(names)) != len(names) or not all(isinstance(n, str) and n.strip() for n in names):
        raise MappingError('Each trade tab must be named once.')
    _need(t, 'trade.', ('bidder', 'supplier_names', 'selected_name', 'markup', 'first_row', 'last_row', 'cols',
                        'slots', 'selected', 'total_row', 'total_cols', 'provisional'))
    for ref in [t['bidder'], t['selected_name'], t['markup'], *t['supplier_names']]:
        split(ref)
    if not 1 <= len(t['slots']) <= 8 or len(t['slots']) != len(t['supplier_names']):
        raise MappingError('trade.slots and trade.supplier_names must list the same suppliers (1 to 8).')
    for slot in t['slots']:
        _need(slot, 'trade.slots[].', ('rate', 'adj', 'quote', 'comment'))
    for letter in [*t['cols'].values(), *t['selected'].values(), *t['total_cols'].values(),
                   *(v for slot in t['slots'] for v in slot.values())]:
        col(letter)
    for sec, path in ((t, 'trade.'), (p, 'prelims.'), (j, 'joinery.'), (c, 'clarifications.')):
        _need(sec, path, ('first_row', 'last_row', 'cols'))
        split(f'A{sec["first_row"]}'), split(f'A{sec["last_row"]}')
        if sec['first_row'] > sec['last_row']:
            raise MappingError(f'{path}first_row is below last_row.')
    _need(p, 'prelims.', ('sheet', 'duration', 'markup', 'weeks_unit', 'total_row', 'total_cols'))
    split(p['duration']), split(p['markup'])
    _need(j, 'joinery.', ('sheet', 'markups', 'total_row', 'total_cols'))
    for ref in j['markups'].values():
        split(ref)
    for sec in (p, j, c):
        for letter in sec['cols'].values():
            col(letter)
    for sec in (p, j):
        for letter in sec['total_cols'].values():
            col(letter)
    _need(m['words'], 'words.', ('yes', 'no', 'priced', 'review', 'excluded'))
    return m


def _rows(*rows):
    for r in rows:
        if not isinstance(r, int) or isinstance(r, bool):
            raise MappingError(f'{r!r} is not a row number.')
        split(f'A{r}')


def _texts(d, path):
    if not isinstance(d, dict) or not d or not all(isinstance(v, str) and v.strip() for v in d.values()):
        raise MappingError(f'{path} must name each label.')


def validate_select(m):
    """The supplier-select layout's mapping (calc select-v1)."""
    s, t, p, j, c, w = m['summary'], m['trade'], m['prelims'], m['joinery'], m['clarifications'], m.get('workshops')
    _need(s, 'summary.', ('sheet', 'header', 'default_markup', 'joinery_markups', 'first_row', 'scan_to',
                          'markup_lookup', 'cols', 'labels', 'profit_col', 'margin_col', 'target_col',
                          'target_difference_col', 'check_label_col', 'check_value_col', 'check_labels'))
    for ref in [s['default_markup'], *s['header'].values(), *s['joinery_markups'].values()]:
        split(ref)
    _need(s['cols'], 'summary.cols.', ('supplier', 'name', 'cost', 'markup', 'charge', 'flag', 'notes'))
    for letter in [*s['cols'].values(), s['profit_col'], s['margin_col'], s['target_col'], s['target_difference_col'],
                   s['check_label_col'], s['check_value_col']]:
        col(letter)
    if not isinstance(s['markup_lookup'], list) or len(s['markup_lookup']) != 2:
        raise MappingError('summary.markup_lookup is [first row, last row].')
    _rows(s['first_row'], s['scan_to'], *s['markup_lookup'])
    if s['first_row'] > s['scan_to']:
        raise MappingError('summary.first_row is below summary.scan_to.')
    _need(s['labels'], 'summary.labels.', ('joinery', 'installation', 'prelims', 'total', 'profit', 'percentage'))
    _texts(s['labels'], 'summary.labels')
    _need(s['check_labels'], 'summary.check_labels.', ('checked_total', 'client_total', 'client_difference'))
    _texts(s['check_labels'], 'summary.check_labels')
    _need(t, 'trade.', ('name', 'selected_name', 'markup', 'supplier_names', 'first_row', 'max_row', 'total_label_col',
                        'total_label', 'cols', 'slots', 'selected', 'total_cols', 'provisional'))
    for ref in [t['name'], t['selected_name'], t['markup'], *t['supplier_names']]:
        split(ref)
    _rows(t['first_row'], t['max_row'])
    if not 1 <= len(t['slots']) <= 8 or len(t['slots']) != len(t['supplier_names']):
        raise MappingError('trade.slots and trade.supplier_names must list the same suppliers (1 to 8).')
    for slot in t['slots']:
        _need(slot, 'trade.slots[].', ('rate', 'adj', 'quote', 'comment'))
    _need(t['cols'], 'trade.cols.', ('ref', 'description', 'qty', 'unit'))
    _need(t['selected'], 'trade.selected.', ('qty', 'rate', 'adj', 'cost', 'tender_rate', 'charge'))
    _need(t['total_cols'], 'trade.total_cols.', ('cost', 'charge'))
    for letter in [t['total_label_col'], *t['cols'].values(), *t['selected'].values(), *t['total_cols'].values(),
                   *(v for slot in t['slots'] for v in slot.values())]:
        col(letter)
    _need(p, 'prelims.', ('sheet', 'duration', 'first_row', 'last_row', 'cols', 'weeks_unit'))
    split(p['duration'])
    _need(p['cols'], 'prelims.cols.', ('section', 'description', 'allocation', 'qty', 'unit', 'rate', 'cost', 'charge'))
    _need(j, 'joinery.', ('sheet', 'first_row', 'last_row', 'total_label', 'cols'))
    _need(j['cols'], 'joinery.cols.', ('ref', 'description', 'qty', 'unit', 'supply_cost', 'supply_charge',
                                       'install_cost', 'install_charge', 'rate', 'total'))
    _need(c, 'clarifications.', ('sheet', 'first_row', 'last_row', 'cols'))
    _need(c['cols'], 'clarifications.cols.', ('ref', 'text'))
    for sec, path in ((p, 'prelims.'), (j, 'joinery.'), (c, 'clarifications.')):
        _rows(sec['first_row'], sec['last_row'])
        if sec['first_row'] > sec['last_row']:
            raise MappingError(f'{path}first_row is below last_row.')
        for letter in sec['cols'].values():
            col(letter)
    if w is not None:
        _need(w, 'workshops.', ('pattern', 'description', 'cost_col', 'charge_col', 'parts', 'rows', 'max_row'))
        try:
            re.compile(w['pattern'])
        except (re.error, TypeError):
            raise MappingError('workshops.pattern is not a pattern.')
        split(w['description'])
        col(w['cost_col']), col(w['charge_col'])
        _need(w['rows'], 'workshops.rows.', ('supply', 'delivery', 'install', 'total'))
        _rows(w['max_row'], *w['parts'].values(), *w['rows'].values())
    _need(m['words'], 'words.', ('yes', 'no', 'priced', 'review', 'excluded'))
    for key in ('allowance_words', 'ps_words'):
        if not isinstance(m.get(key, []), list):
            raise MappingError(f'{key} is a list of words.')
    return m


def detect(sheetnames, summary_cells):
    """
    Which built-in layout a workbook looks like, from its sheet names and the top of its summary sheet, or None.
    summary_cells: the text of the summary's first rows. See detect_layout() for how sure it is.
    """
    return detect_layout(sheetnames, summary_cells)[0]


def detect_layout(sheetnames, summary_cells):
    """
    (key, confident): the built-in layout the workbook looks like (or None) and whether the evidence is strong
    enough to read the file that way without asking. Confident means the summary carries the one header only
    that layout has (Include? for standard, Subcontractor Markup for supplier-select) and not the other's. A
    supplier-select guess from numbered sheets alone is a suggestion, never a switch.
    """
    words = {str(v).strip().casefold() for v in summary_cells if isinstance(v, str)}
    include, sub_markup = 'include?' in words, 'subcontractor markup' in words
    if include:
        return STANDARD['key'], not sub_markup
    numbered = any(re.match(SUPPLIER_SELECT['workshops']['pattern'], n or '') for n in sheetnames)
    if STANDARD['summary']['sheet'] in sheetnames and (numbered or sub_markup):
        return SUPPLIER_SELECT['key'], sub_markup
    return None, False


# How an upload message names a layout it switched to ("Read as the supplier-select layout")
SHORT_NAMES = {STANDARD['key']: 'standard', SUPPLIER_SELECT['key']: 'supplier-select'}


def get(key, custom=None):
    """The mapping a company uses: its own (validated) if it has one, else the preset it picked."""
    if custom:
        return validate(copy.deepcopy(custom))
    return copy.deepcopy(PRESETS.get(key) or STANDARD)


for _preset in PRESETS.values():
    validate(_preset)
