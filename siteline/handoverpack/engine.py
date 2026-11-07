"""
Recompute a pricing workbook from its inputs, reconcile it with what Excel saved, classify every
line and look for the gaps that catch estimators out.

    result = engine.run(reader.open_book(data, engine.extents(m)), m, margin_tolerance)

Everything is Decimal, from the typed inputs, with no rounding until a figure is shown, so a total
is the exact sum of its lines. The arithmetic is the workbook's own (mapping calc 'standard-v1'),
cell for cell:

  trade line   selected rate Y = the chosen bidder's rate, if a number; adj Z = their Est Adj (blank = 0)
               cost AA = qty x (Y + Z), only when qty and Y are numbers; charge AD = AA x (1 + markup)
               status AE = Priced when costed and a supplier is named, Review otherwise (blank row: none)
  trade        totals AA58 / AD58 = sums of the lines; on the summary only if Include? = Yes
  prelim line  periods H = periods input, else the duration for weekly lines; cost J = D x E x H x I,
               only for Include? = Yes and when E, H and I are numbers; charge L = J x (1 + markup)
  joinery line cost L = qty x (material + ancillary + hrs x rate + install hrs x rate), only when all
               seven inputs are numbers; charge M marks each part up with its own markup
  summary      total cost, charge and profit = the included trades + joinery + prelims

Where the file holds Excel's saved values, every recomputed cell is compared with the one Excel saved;
a difference over 1p is a reconciliation issue naming the sheet and cell. Without saved values (a file
written by a tool that doesn't calculate) the recomputed figures are used and the result says so.

Markups are inputs that the template fills by formula (='PRICING SUMMARY'!F11, =$J$4). A formula
that is just a reference to another cell is followed; anything else falls back to Excel's saved value
and is noted.
"""
import re
from decimal import Decimal

from openpyxl.utils import get_column_letter

from . import mapping as maps

PENNY = Decimal('0.01')
ZERO = Decimal('0')
ONE = Decimal('1')
REF = re.compile(r"^=\s*(?:(?:'((?:[^']|'')+)'|([A-Za-z0-9_.]+))!)?\$?([A-Z]{1,3})\$?([0-9]{1,6})\s*$")

PRICED, INCLUDED, EXCLUDED, REVIEW, PROVISIONAL, ALLOWANCE, BY_OTHERS = (
    'priced', 'included', 'excluded', 'review', 'provisional', 'allowance', 'by_others')
CLASS_LABELS = {
    PRICED: 'Priced', INCLUDED: 'Included (zero rate)', EXCLUDED: 'Excluded', REVIEW: 'Review',
    PROVISIONAL: 'Provisional sum', ALLOWANCE: 'Own allowance', BY_OTHERS: 'By others',
}
CLASS_TONES = {PRICED: 'good', INCLUDED: 'info', EXCLUDED: 'muted', REVIEW: 'bad', PROVISIONAL: 'warn',
               ALLOWANCE: 'warn', BY_OTHERS: 'muted'}
# Words in a zero-rated line's comment that mean someone else provides it, not that it is included
BY_OTHERS_WORDS = ('by others', 'by client', 'by the client', 'by landlord', 'by the landlord', 'by tenant',
                   'by the tenant', 'client direct', 'excluded', 'exclusion', 'not included', 'omitted', 'nic')


def num(v):
    """Excel's ISNUMBER: a number, never text that looks like one, never TRUE/FALSE."""
    return v if isinstance(v, Decimal) else None


def blank(v):
    return v is None or (isinstance(v, str) and v == '')


def same(text, word):
    """Excel compares text without regard to case."""
    return isinstance(text, str) and text.strip().casefold() == word.casefold()


def text(v):
    if v is None:
        return ''
    if isinstance(v, Decimal):
        return format(v.normalize(), 'f') if v == v.to_integral() else str(v)
    return str(v).strip()


def cell(row, col):
    return f'{get_column_letter(col)}{row}'


def margin(profit, charge):
    return (profit / charge) if charge else None


def extents(m):
    """{sheet: (max_row, max_col)} the reader needs for this mapping."""
    def far(refs, cols, last_row):
        rows = [maps.split(r)[0] for r in refs] + [last_row]
        columns = [maps.split(r)[1] for r in refs] + [maps.col(c) for c in cols]
        return max(rows), max(columns)

    s, t, p, j, c = m['summary'], m['trade'], m['prelims'], m['joinery'], m['clarifications']
    out = {s['sheet']: far([s['default_markup'], s['prelim_markup'], s['target_margin'], s['final_price'],
                            *s['header'].values()], s['cols'].values(), s['total_row'])}
    trade_cols = [*t['cols'].values(), *t['selected'].values(), *t['total_cols'].values(),
                  *(v for slot in t['slots'] for v in slot.values())]
    trade_ext = far([t['bidder'], t['selected_name'], t['markup'], *t['supplier_names']], trade_cols,
                    max(t['last_row'], t['total_row']))
    for tr in s['trades']:
        out[tr['sheet']] = trade_ext
    out[p['sheet']] = far([p['duration'], p['markup']], [*p['cols'].values(), *p['total_cols'].values()],
                          max(p['last_row'], p['total_row']))
    out[j['sheet']] = far(list(j['markups'].values()), [*j['cols'].values(), *j['total_cols'].values()],
                          max(j['last_row'], j['total_row']))
    out[c['sheet']] = far([], c['cols'].values(), c['last_row'])
    return out


class Run:
    def __init__(self, book, m, margin_tolerance=ZERO):
        self.book, self.m = book, m
        self.words = m['words']
        self.by_others = tuple(w.casefold() for w in m.get('by_others_words', BY_OTHERS_WORDS))
        self.tolerance = Decimal(margin_tolerance or 0)
        self.issues, self.gaps, self.notes = [], [], []
        summary = m['summary']['sheet']
        g50 = maps.split(f"{m['summary']['cols']['charge']}{m['summary']['total_row']}")
        self.cached = num(book.value(summary, *g50)) is not None or not book.is_formula(summary, *g50)
        if book.has(summary) and not self.cached:
            self.notes.append('This file was saved without calculated values (it was not last saved by Excel), so '
                              'the figures here are Siteline\'s own recalculation and could not be checked against '
                              'Excel\'s. Open it in Excel, save it, and upload it again to check it.')

    # --- reading -------------------------------------------------------------------------
    def raw(self, sheet, ref):
        return self.book.value(sheet, *maps.split(ref))

    def resolve(self, sheet, row, col, depth=0):
        """A cell's value, following a formula that is just a reference (='PRICING SUMMARY'!F11, =$J$4)."""
        f = self.book.formula(sheet, row, col)
        if not (isinstance(f, str) and f.startswith('=')) or f == self.book.value(sheet, row, col):
            return self.book.value(sheet, row, col)
        m = REF.match(f)
        if m and depth < 6:
            target = (m.group(1) or '').replace("''", "'") or (m.group(2) or '').strip() or sheet
            if self.book.has(target):
                return self.resolve(target, int(m.group(4)), maps.col(m.group(3)), depth + 1)
        saved = self.book.value(sheet, row, col)
        self.notes.append(f'{sheet}!{cell(row, col)} holds a formula Siteline doesn\'t follow ({f[:60]}); '
                          f'Excel\'s saved value ({text(saved) or "blank"}) was used.')
        return saved

    def resolve_ref(self, sheet, ref):
        return self.resolve(sheet, *maps.split(ref))

    # --- reconciling ----------------------------------------------------------------------
    def check(self, sheet, row, col, computed, what):
        """Compare a recomputed figure with the one Excel saved; over 1p apart is an issue."""
        if not self.cached or not self.book.has(sheet):
            return
        saved = self.book.value(sheet, row, col)
        saved_n = num(saved)
        if saved_n is None and blank(saved) and computed in (None, ZERO):
            return
        if computed is None and saved_n is None:
            return
        a, b = computed if computed is not None else ZERO, saved_n if saved_n is not None else ZERO
        if saved_n is None and not blank(saved):
            self.issues.append({'sheet': sheet, 'cell': cell(row, col), 'what': what, 'computed': computed,
                                'excel': None, 'excel_text': text(saved)[:60], 'difference': abs(a)})
            return
        if abs(a - b) > PENNY:
            self.issues.append({'sheet': sheet, 'cell': cell(row, col), 'what': what, 'computed': computed,
                                'excel': saved_n, 'excel_text': '', 'difference': abs(a - b)})

    def check_text(self, sheet, row, col, computed, what):
        if not self.cached or not self.book.has(sheet):
            return
        saved = self.book.value(sheet, row, col)
        if text(saved).casefold() != (computed or '').casefold():
            self.issues.append({'sheet': sheet, 'cell': cell(row, col), 'what': what, 'computed': None,
                                'excel': None, 'excel_text': f'Excel shows "{text(saved)[:30]}", the workbook\'s '
                                                              f'rules give "{computed or "blank"}"',
                                'difference': ZERO})

    def gap(self, code, title, detail='', sheet='', ref='', trade='', amount=None, severity='warn'):
        self.gaps.append({'code': code, 'title': title, 'detail': detail, 'sheet': sheet, 'cell': ref,
                          'trade': trade, 'amount': amount, 'severity': severity})

    # --- trades ---------------------------------------------------------------------------
    def trade(self, entry, default_markup):
        m, t, s = self.m, self.m['trade'], self.m['summary']
        sheet, srow = entry['sheet'], entry['row']
        sc = {k: maps.col(v) for k, v in s['cols'].items()}
        name = text(self.raw(s['sheet'], f"{s['cols']['name']}{srow}")) or sheet
        include_raw = self.book.value(s['sheet'], srow, sc['include'])
        include = same(include_raw, self.words['yes'])
        out = {'name': name, 'sheet': sheet, 'summary_row': srow, 'include': include,
               'include_text': text(include_raw), 'provisional': sheet in t['provisional'], 'lines': [],
               'missing': not self.book.has(sheet)}
        n = len(t['slots'])
        names = [text(self.raw(sheet, r)) for r in t['supplier_names']]
        bidder = num(self.raw(sheet, t['bidder']))
        bidder = int(bidder) if bidder is not None and 1 <= int(bidder) <= n else None
        selected_name = names[bidder - 1] if bidder else ''
        markup = num(self.resolve_ref(sheet, t['markup']))
        if markup is None:
            markup = ZERO
        out.update(suppliers=names, bidder=bidder, supplier=selected_name, markup=markup)
        cols = {k: maps.col(v) for k, v in t['cols'].items()}
        sel = {k: maps.col(v) for k, v in t['selected'].items()}
        slots = [{k: maps.col(v) for k, v in slot.items()} for slot in t['slots']]
        slot_totals, slot_counts = [None] * n, [0] * n
        cost_total = charge_total = ZERO
        for row in range(t['first_row'], t['last_row'] + 1):
            v = lambda c: self.book.value(sheet, row, c)  # noqa: E731
            desc, qty_raw = v(cols['description']), v(cols['qty'])
            slot_vals = [{'rate': num(v(sl['rate'])), 'adj': num(v(sl['adj'])), 'comment': text(v(sl['comment'])),
                          'rate_text': '' if num(v(sl['rate'])) is not None else text(v(sl['rate']))}
                         for sl in slots]
            ref = text(v(cols['ref']))
            if blank(desc) and blank(qty_raw) and not ref and not any(
                    sv['rate'] is not None or sv['adj'] is not None or sv['comment'] for sv in slot_vals):
                continue
            qty = num(qty_raw)
            for i, sv in enumerate(slot_vals):
                sv['quote'] = qty * (sv['rate'] + (sv['adj'] or ZERO)) if qty is not None and sv['rate'] is not None else None
                if sv['quote'] is not None:
                    slot_totals[i] = (slot_totals[i] or ZERO) + sv['quote']
                    slot_counts[i] += 1
            chosen = slot_vals[bidder - 1] if bidder else None
            rate = chosen['rate'] if chosen else None
            adj = (chosen['adj'] or ZERO) if chosen else None
            cost = qty * (rate + adj) if qty is not None and rate is not None else None
            charge = cost * (ONE + markup) if cost is not None else None
            tender_rate = (rate + adj) * (ONE + markup) if cost is not None else None
            if blank(desc):
                status = ''
            elif cost is not None and selected_name not in ('', '0'):
                status = self.words['priced']
            else:
                status = self.words['review']
            comment = chosen['comment'] if chosen else ''
            line = {'row': row, 'ref': ref, 'description': text(desc), 'qty': qty, 'qty_text': '' if qty is not None
                    else text(qty_raw), 'unit': text(v(cols['unit'])), 'slots': slot_vals, 'rate': rate, 'adj': adj,
                    'cost': cost, 'charge': charge, 'tender_rate': tender_rate, 'status': status, 'comment': comment}
            line['others_only'] = rate is None and any(sv['rate'] is not None for i, sv in enumerate(slot_vals)
                                                       if i != (bidder or 0) - 1)
            line['cls'] = self.classify(out, line)
            if cost is not None:
                cost_total += cost
                charge_total += charge
            for key, c in (('cost', sel['cost']), ('charge', sel['charge'])):
                self.check(sheet, row, c, line[key], f'{name} line {ref or row} {key}')
            self.check_text(sheet, row, sel['status'], status, f'{name} line {ref or row} status')
            out['lines'].append(line)
        tcols = {k: maps.col(v) for k, v in t['total_cols'].items()}
        self.check(sheet, t['total_row'], tcols['cost'], cost_total, f'{name} total cost')
        self.check(sheet, t['total_row'], tcols['charge'], charge_total, f'{name} total charge')
        out.update(tab_cost=cost_total, tab_charge=charge_total, slot_totals=slot_totals, slot_counts=slot_counts,
                   cost=cost_total if include else ZERO, charge=charge_total if include else ZERO)
        out['profit'] = out['charge'] - out['cost']
        out['margin'] = margin(out['profit'], out['charge'])
        out['line_count'] = sum(1 for ln in out['lines'] if ln['description'])
        out['review_count'] = sum(1 for ln in out['lines'] if ln['cls'] == REVIEW and ln['description']) if include else 0
        self.check(s['sheet'], srow, sc['cost'], out['cost'], f'Summary {name} cost')
        self.check(s['sheet'], srow, sc['charge'], out['charge'], f'Summary {name} charge')
        self.check(s['sheet'], srow, sc['profit'], out['profit'], f'Summary {name} profit')
        self.trade_gaps(out, default_markup)
        return out

    def classify(self, trade, line):
        if not trade['include']:
            return EXCLUDED
        rate, adj, comment = line['rate'], line['adj'] or ZERO, line['comment'].casefold()
        if line['status'] == self.words['review'] or line['cost'] is None:
            return ALLOWANCE if adj != ZERO and (rate is None or rate == ZERO) else REVIEW
        if rate == ZERO and adj != ZERO:
            return ALLOWANCE
        if rate == ZERO and line['cost'] == ZERO:
            if comment and any(re.search(rf'\b{re.escape(w)}\b', comment) for w in self.by_others):
                return BY_OTHERS
            return INCLUDED
        if trade['provisional']:
            return PROVISIONAL
        return PRICED

    def trade_gaps(self, tr, default_markup):
        name, sheet = tr['name'], tr['sheet']
        lines = [ln for ln in tr['lines'] if ln['description']]
        t = self.m['trade']
        if tr['missing']:
            if tr['include']:
                self.gap('missing_sheet', f'{name}: tab missing', f'The summary includes {name} but the workbook has '
                         f'no "{sheet}" tab.', sheet, severity='bad')
            return
        if lines and not tr['include']:
            value = tr['tab_charge'] or tr['tab_cost'] or max((x for x in tr['slot_totals'] if x), default=None)
            self.gap('excluded_with_items', f'{name} has {len(lines)} item{"s" if len(lines) != 1 else ""} but '
                     f'Include? is "{tr["include_text"] or "blank"}"', 'Nothing for this trade is in the tender. '
                     'Check it really is excluded, or set Include? to Yes.', self.m['summary']['sheet'],
                     f"{self.m['summary']['cols']['include']}{tr['summary_row']}", name, value, 'bad')
        if not tr['include']:
            return
        if not lines:
            self.gap('included_empty', f'{name} is included but has no items', 'Include? is Yes but the tab has no '
                     'item descriptions, so nothing is priced for it.', sheet, '', name, None)
            return
        if not tr['bidder'] or tr['supplier'] in ('', '0'):
            cheapest = min((x for x in tr['slot_totals'] if x is not None), default=None)
            why = 'no bidder is selected' if not tr['bidder'] else f'bidder {tr["bidder"]} has no supplier name'
            self.gap('no_bidder', f'{name}: included but {why}', 'Every line shows Review until a bidder with a '
                     'name is chosen.', sheet, t['bidder'], name, cheapest, 'bad')
        review = [ln for ln in lines if ln['cls'] == REVIEW]
        if review and tr['bidder'] and tr['supplier'] not in ('', '0'):
            exposure = sum((max((sv['quote'] for sv in ln['slots'] if sv['quote'] is not None), default=ZERO)
                            for ln in review), ZERO)
            self.gap('review_lines', f'{name}: {len(review)} line{"s" if len(review) != 1 else ""} to review',
                     'No rate from the selected supplier: ' + ', '.join(ln['ref'] or f'row {ln["row"]}'
                                                                       for ln in review[:8])
                     + ('…' if len(review) > 8 else ''), sheet, '', name, exposure or None)
        others = [ln for ln in lines if ln['others_only']] if tr['bidder'] else []
        if others:
            value = sum((min(sv['quote'] for sv in ln['slots'] if sv['quote'] is not None) for ln in others
                         if any(sv['quote'] is not None for sv in ln['slots'])), ZERO)
            self.gap('unselected_rates', f'{name}: {len(others)} line{"s" if len(others) != 1 else ""} priced only by '
                     'another bidder', 'The selected supplier has no rate where another bidder does: '
                     + ', '.join(ln['ref'] or f'row {ln["row"]}' for ln in others[:8]), sheet, '', name, value or None)
        loose = [ln for ln in lines if ln['cls'] == ALLOWANCE and ln['cost'] is None]
        if loose:
            value = sum((ln['qty'] * ln['adj'] for ln in loose if ln['qty'] is not None), ZERO)
            self.gap('allowance_not_costed', f'{name}: Est Adj with no rate, left out of the total',
                     'The workbook only costs a line with a rate, so these own allowances add nothing: '
                     + ', '.join(ln['ref'] or f'row {ln["row"]}' for ln in loose[:8])
                     + '. Enter a rate of 0 to carry the allowance.', sheet, '', name, value or None, 'bad')
        silent = [ln for ln in lines if ln['cls'] == INCLUDED and not ln['comment']]
        if silent:
            self.gap('zero_no_comment', f'{name}: {len(silent)} zero rate{"s" if len(silent) != 1 else ""} with no '
                     'comment', 'A zero should say where the work is included: '
                     + ', '.join(ln['ref'] or f'row {ln["row"]}' for ln in silent[:8]), sheet, '', name, None, 'info')
        nameless = [ln for ln in tr['lines'] if not ln['description'] and ln['cost']]
        if nameless:
            self.gap('no_description', f'{name}: priced line with no description',
                     'Rows ' + ', '.join(str(ln['row']) for ln in nameless[:8]) + ' are in the total but say nothing.',
                     sheet, '', name, sum((ln['cost'] for ln in nameless), ZERO))
        if tr['markup'] == ZERO and tr['cost'] > ZERO:
            self.gap('zero_markup', f'{name}: markup is zero', 'An included trade carried at cost.', sheet,
                     t['markup'], name, tr['cost'], 'info' if tr['provisional'] else 'warn')

    # --- prelims --------------------------------------------------------------------------
    def prelims(self):
        p, sheet = self.m['prelims'], self.m['prelims']['sheet']
        out = {'sheet': sheet, 'lines': [], 'missing': not self.book.has(sheet), 'sections': []}
        if out['missing']:
            self.gap('missing_sheet', f'{sheet} tab missing', '', sheet, severity='bad')
            out.update(cost=ZERO, charge=ZERO, duration=None, markup=ZERO)
            return out
        c = {k: maps.col(v) for k, v in p['cols'].items()}
        duration = num(self.resolve_ref(sheet, p['duration']))
        default_markup = num(self.resolve_ref(sheet, p['markup'])) or ZERO
        cost_total = charge_total = ZERO
        section = ''
        for row in range(p['first_row'], p['last_row'] + 1):
            v = lambda k: self.book.value(sheet, row, c[k])  # noqa: E731
            inc, desc = v('include'), text(v('description'))
            is_item = (not blank(inc)) or self.book.is_formula(sheet, row, c['cost']) or any(
                num(v(k)) is not None for k in ('qty', 'rate', 'periods_input'))
            if not is_item:
                if desc:
                    section = desc
                continue
            alloc_raw = v('allocation')
            alloc = ZERO if blank(alloc_raw) else num(alloc_raw)
            qty, rate, periods_in = num(v('qty')), num(v('rate')), num(v('periods_input'))
            unit = text(v('unit'))
            periods = periods_in if periods_in is not None else (
                duration if same(unit, p['weeks_unit']) and duration is not None else None)
            markup_raw = self.resolve(sheet, row, c['markup'])
            markup = num(markup_raw) if num(markup_raw) is not None else (ZERO if blank(markup_raw) else default_markup)
            yes, no = same(inc, self.words['yes']), same(inc, self.words['no'])
            cost = None
            if yes and qty is not None and periods is not None and rate is not None:
                if alloc is None:
                    self.gap('cell_error', f'{sheet} row {row}: allocation is not a number',
                             f'"{text(alloc_raw)}" makes Excel show an error here.', sheet, cell(row, c['allocation']),
                             severity='bad')
                else:
                    cost = alloc * qty * periods * rate
            charge = cost * (ONE + markup) if cost is not None else None
            status = self.words['excluded'] if no else (self.words['priced'] if cost is not None else self.words['review'])
            cls = EXCLUDED if no else (PRICED if cost is not None else REVIEW)
            line = {'row': row, 'section': section, 'include': text(inc), 'description': desc, 'allocation': alloc,
                    'qty': qty, 'unit': unit, 'periods': periods, 'rate': rate, 'cost': cost, 'markup': markup,
                    'charge': charge, 'status': status, 'cls': cls, 'basis': text(v('basis')),
                    'has_inputs': any(x is not None for x in (qty, rate, periods_in))}
            if cost is not None:
                cost_total += cost
                charge_total += charge
            self.check(sheet, row, c['cost'], cost, f'Prelim {desc[:40] or row} cost')
            self.check(sheet, row, c['charge'], charge, f'Prelim {desc[:40] or row} charge')
            self.check_text(sheet, row, c['status'], status, f'Prelim {desc[:40] or row} status')
            out['lines'].append(line)
        tc = {k: maps.col(v) for k, v in p['total_cols'].items()}
        self.check(sheet, p['total_row'], tc['cost'], cost_total, 'Preliminaries total cost')
        self.check(sheet, p['total_row'], tc['charge'], charge_total, 'Preliminaries total charge')
        out.update(cost=cost_total, charge=charge_total, duration=duration, markup=default_markup)
        no_rate = [ln for ln in out['lines'] if ln['cls'] == REVIEW and same(ln['include'], self.words['yes'])]
        if no_rate:
            self.gap('prelim_no_rate', f'Preliminaries: {len(no_rate)} line{"s" if len(no_rate) != 1 else ""} marked '
                     'Yes with no rate or quantity', ', '.join(ln['description'][:40] for ln in no_rate[:8]), sheet,
                     '', 'Preliminaries', None, 'bad')
        undecided = [ln for ln in out['lines'] if ln['cls'] == REVIEW and not same(ln['include'], self.words['yes'])]
        if undecided:
            self.gap('prelim_undecided', f'Preliminaries: {len(undecided)} line{"s" if len(undecided) != 1 else ""} '
                     'neither Yes nor No', ', '.join(ln['description'][:40] for ln in undecided[:8]), sheet, '',
                     'Preliminaries')
        if duration is None and any(same(ln['unit'], p['weeks_unit']) and same(ln['include'], self.words['yes'])
                                    for ln in out['lines']):
            self.gap('prelim_no_duration', 'Preliminaries: no programme duration', 'Weekly lines have no periods.',
                     sheet, p['duration'], 'Preliminaries', None, 'bad')
        if cost_total > ZERO and all(ln['markup'] == ZERO for ln in out['lines'] if ln['cost']):
            self.gap('zero_markup', 'Preliminaries: markup is zero', 'Preliminaries carried at cost.', sheet,
                     p['markup'], 'Preliminaries', cost_total)
        return out

    # --- joinery --------------------------------------------------------------------------
    def joinery(self):
        j, sheet = self.m['joinery'], self.m['joinery']['sheet']
        out = {'sheet': sheet, 'lines': [], 'missing': not self.book.has(sheet)}
        if out['missing']:
            out.update(cost=ZERO, charge=ZERO, markups={})
            return out
        c = {k: maps.col(v) for k, v in j['cols'].items()}
        mk = {k: num(self.resolve_ref(sheet, ref)) or ZERO for k, ref in j['markups'].items()}
        parts = ('qty', 'material', 'ancillary', 'manufacture_hrs', 'hourly', 'install_hrs', 'install_hourly')
        cost_total = charge_total = ZERO
        for row in range(j['first_row'], j['last_row'] + 1):
            v = lambda k: self.book.value(sheet, row, c[k])  # noqa: E731
            desc = text(v('description'))
            vals = {k: num(v(k)) for k in parts}
            if not desc and all(x is None for x in vals.values()):
                continue
            cost = charge = None
            if desc and all(x is not None for x in vals.values()):
                q = vals['qty']
                cost = q * (vals['material'] + vals['ancillary'] + vals['manufacture_hrs'] * vals['hourly']
                            + vals['install_hrs'] * vals['install_hourly'])
                charge = q * ((vals['material'] + vals['ancillary']) * (ONE + mk['material'])
                              + vals['manufacture_hrs'] * vals['hourly'] * (ONE + mk['manufacture'])
                              + vals['install_hrs'] * vals['install_hourly'] * (ONE + mk['install']))
            status = '' if not desc else (self.words['priced'] if cost is not None else self.words['review'])
            line = {'row': row, 'ref': text(v('ref')), 'description': desc, 'unit': text(v('unit')), **vals,
                    'cost': cost, 'charge': charge, 'status': status, 'notes': text(v('notes')),
                    'cls': PRICED if cost is not None else REVIEW,
                    'missing': [k for k in parts if vals[k] is None]}
            if cost is not None:
                cost_total += cost
                charge_total += charge
            self.check(sheet, row, c['cost'], cost, f'Joinery {line["ref"] or row} cost')
            self.check(sheet, row, c['charge'], charge, f'Joinery {line["ref"] or row} charge')
            if desc:
                self.check_text(sheet, row, c['status'], status, f'Joinery {line["ref"] or row} status')
            out['lines'].append(line)
        tc = {k: maps.col(v) for k, v in j['total_cols'].items()}
        self.check(sheet, j['total_row'], tc['cost'], cost_total, 'Joinery total cost')
        self.check(sheet, j['total_row'], tc['charge'], charge_total, 'Joinery total charge')
        out.update(cost=cost_total, charge=charge_total, markups=mk)
        incomplete = [ln for ln in out['lines'] if ln['description'] and ln['cost'] is None]
        if incomplete:
            self.gap('joinery_incomplete', f'Joinery: {len(incomplete)} item{"s" if len(incomplete) != 1 else ""} '
                     'incomplete', 'Every cost part needs a number (0 if unused): '
                     + ', '.join(ln['ref'] or ln['description'][:30] for ln in incomplete[:8]), sheet, '', 'Joinery',
                     None, 'bad')
        headless = [ln for ln in out['lines'] if not ln['description']]
        if headless:
            self.gap('joinery_incomplete', 'Joinery: figures with no description', 'Rows '
                     + ', '.join(str(ln['row']) for ln in headless[:8]) + ' are not costed.', sheet, '', 'Joinery')
        if cost_total > ZERO and all(x == ZERO for x in mk.values()):
            self.gap('zero_markup', 'Joinery: markups are zero', 'Joinery carried at cost.', sheet,
                     j['markups']['material'], 'Joinery', cost_total)
        return out

    # --- clarifications -------------------------------------------------------------------
    def clarifications(self):
        cl, sheet = self.m['clarifications'], self.m['clarifications']['sheet']
        if not self.book.has(sheet):
            return []
        c = {k: maps.col(v) for k, v in cl['cols'].items()}
        out = []
        for row in range(cl['first_row'], cl['last_row'] + 1):
            vals = {k: text(self.book.value(sheet, row, col)) for k, col in c.items()}
            if not vals['text'] and not vals['response']:
                continue
            out.append({'row': row, **vals})
        return out

    # --- the whole workbook ---------------------------------------------------------------
    def run(self):
        m, s = self.m, self.m['summary']
        sheet = s['sheet']
        if not self.book.has(sheet):
            raise ValueError(f'This doesn\'t look like the pricing workbook: it has no "{sheet}" sheet. Check the '
                             'workbook layout chosen in Settings > Pricing workbook.')
        header = {k: text(self.raw(sheet, ref)) for k, ref in s['header'].items()}
        default_markup = num(self.resolve_ref(sheet, s['default_markup'])) or ZERO
        prelim_markup = num(self.resolve_ref(sheet, s['prelim_markup']))
        target = num(self.resolve_ref(sheet, s['target_margin']))
        trades = [self.trade(entry, default_markup) for entry in s['trades']]
        prelims, joinery = self.prelims(), self.joinery()
        sc = {k: maps.col(v) for k, v in s['cols'].items()}
        for row, part, label in ((s['joinery_row'], joinery, 'Joinery'), (s['prelims_row'], prelims, 'Preliminaries')):
            self.check(sheet, row, sc['cost'], part['cost'], f'Summary {label} cost')
            self.check(sheet, row, sc['charge'], part['charge'], f'Summary {label} charge')
            self.check(sheet, row, sc['profit'], part['charge'] - part['cost'], f'Summary {label} profit')
        cost = sum((t['cost'] for t in trades), ZERO) + joinery['cost'] + prelims['cost']
        charge = sum((t['charge'] for t in trades), ZERO) + joinery['charge'] + prelims['charge']
        profit = charge - cost
        trow = s['total_row']
        excel = {k: num(self.book.value(sheet, trow, sc[k])) for k in ('cost', 'charge', 'profit')}
        for k, v in (('cost', cost), ('charge', charge), ('profit', profit)):
            self.check(sheet, trow, sc[k], v, f'Total {k}')
        final = num(self.raw(sheet, s['final_price']))
        totals = {
            'cost': cost, 'charge': charge, 'profit': profit, 'margin': margin(profit, charge),
            'excel_cost': excel['cost'], 'excel_charge': excel['charge'], 'excel_profit': excel['profit'],
            'target_margin': target, 'price_at_target': (cost / (ONE - target)) if cost and target is not None
            and target < ONE else None, 'final_price': final,
            'final_margin': margin(final - cost, final) if final else None,
            'charge_cell': f"{s['cols']['charge']}{trow}",
        }
        if self.cached and excel['charge'] is not None:
            totals['difference'] = charge.quantize(PENNY) - excel['charge'].quantize(PENNY)
        else:
            totals['difference'] = None
        totals['reconciled'] = totals['difference'] == ZERO if totals['difference'] is not None else None
        if target is not None and totals['margin'] is not None and totals['margin'] < target - self.tolerance:
            short = (totals['price_at_target'] - charge) if totals['price_at_target'] is not None else None
            self.gap('margin_below_target', f'Margin {totals["margin"] * 100:.1f}% is below the {target * 100:.1f}% '
                     'target', 'Overall margin (profit / charge) against the target margin on the summary.', sheet,
                     s['target_margin'], '', short, 'bad')
        if target is None:
            self.gap('no_target_margin', 'No target margin set', '', sheet, s['target_margin'], '', None, 'info')
        return {
            'mapping': m['key'], 'calc': m['calc'], 'header': header,
            'settings': {'default_markup': default_markup, 'prelim_markup': prelim_markup, 'target_margin': target,
                         'duration_weeks': prelims.get('duration')},
            'cached': self.cached, 'trades': trades, 'prelims': prelims, 'joinery': joinery,
            'clarifications': self.clarifications(), 'totals': totals,
            'issues': sorted(self.issues, key=lambda i: -i['difference']),
            'gaps': sorted(self.gaps, key=lambda g: (g['amount'] is None, -(g['amount'] or ZERO))),
            'notes': list(dict.fromkeys(self.notes)), 'sheetnames': self.book.sheetnames[:200],
        }


def run(book, m, margin_tolerance=ZERO):
    return Run(book, m, margin_tolerance).run()
