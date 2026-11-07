"""
Where things are in a company's pricing workbook, as data.

Every cell the reader looks at comes from a mapping, never from a cell reference written into the
reader. The built-in preset below is the layout of the standard internal pricing workbook (PRICING
SUMMARY, one tab per trade, Prelim Book, JOINERY SUMMARY, Clarifications). A company whose workbook
is laid out differently gets its own mapping (CompanySettings.custom_mapping, the same shape as
STANDARD), checked by validate() before it is used. There is no editor for it yet.

`calc` names how the workbook calculates (which formulas its cells hold). The reader recomputes with
that model; a mapping can move cells around but must keep the same arithmetic until another model is
written.
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

PRESETS = {STANDARD['key']: STANDARD}
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


def get(key, custom=None):
    """The mapping a company uses: its own (validated) if it has one, else the preset it picked."""
    if custom:
        return validate(copy.deepcopy(custom))
    return copy.deepcopy(PRESETS.get(key) or STANDARD)


for _preset in PRESETS.values():
    validate(_preset)
