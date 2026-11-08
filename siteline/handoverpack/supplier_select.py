"""
The supplier-select pricing document (mapping calc 'select-v1'): recompute what can be recomputed
from its inputs, take Excel's saved value where it can't, reconcile, and look for its own gaps.

    result = engine.run(reader.open_book(data, engine.extents(m)), m, margin_tolerance)

The result has the same shape as engine.Run's, so the pack, the pages and the export read either.
The arithmetic, cell for cell (Decimal throughout, nothing rounded until it is shown):

  trade tab    W2 names the selected supplier (a list of the names in row 2). Excel's FILTER copies
               that supplier's Rate and Est Adj columns into Y and Z. FILTER isn't reproduced: Y and Z
               are Excel's saved values, marked "from Excel" (and cross-checked against the slot W2
               names; a difference is noted). Then, as the sheet's formulas:
                 W = qty (D), AA = (Y + Z) x W, AC = (1 + markup) x (Y + Z) unless Y + Z = 0,
                 AD = AC x W, totals AA/AD = the sums of the lines, slot totals = sum of qty x (rate + adj)
               markup AE2 = the summary's markup (F) on the row whose name (D) is the tab's B2
  summary      a trade row is one whose name links to a tab (='Tab'!B2); E = the tab's AA total,
               G = its AD total. There is no Include? column: every row is in the total, and a trade
               counts as included when it costs or charges anything or has a supplier selected.
  joinery      JOINERY SUMMARY lines link to numbered workshop sheets: supply cost/charge R11/T11,
               install R14/T14. A workshop sheet's build-up (labour, materials, ancillary, finish,
               glass, fabric in R2:R7 and T2:T7; delivery R13/T13; install R14/T14) is Excel's; the
               sub totals R11/T11 and R16/T16 are recomputed from it. Rate J = (I + G) / D, total K = J x D.
               Summary Joinery = supply, Installation = install.
  prelims      a line's cost (K) is recomputed when its formula is plain arithmetic of cells and
               numbers (=I14*F14*D14); otherwise Excel's value is used and marked. Charge P = K x (1 +
               the summary's prelim markup). Total = every number in column K; summary charge =
               cost x (1 + markup).
  total        the sum of every summary row from the first trade to the row above Total; profit,
               percentage against the sheet's own target, Checked Total and its difference.

Error values (#VALUE!, #NAME?...) anywhere they matter are reconciliation issues or gaps, never a crash.
"""
import re
from decimal import Decimal, DecimalException, DivisionByZero

from . import engine
from . import mapping as maps
from .engine import (ALLOWANCE, BY_OTHERS, EXCLUDED, HALF_PENNY, INCLUDED, ONE, PRICED, PROVISIONAL, REF, REVIEW,
                     SUM_FORMULA, XL_ERRORS, ZERO, blank, cell, margin, num, same, text)

TOKEN = re.compile(r"\s*(?:(?P<ref>(?:'(?:[^']|'')+'!|[A-Za-z0-9_.]+!)?\$?[A-Z]{1,3}\$?[0-9]{1,6}(?![0-9A-Za-z_(]))"
                   r"|(?P<num>[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9]+)|(?P<op>[-+*/()]))")
CELLREF = re.compile(r"^(?:(?:'((?:[^']|'')+)'|([A-Za-z0-9_.]+))!)?\$?([A-Z]{1,3})\$?([0-9]{1,6})$")
MAX_FORMULA = 400
MAX_MONEY = Decimal('1e13')  # services.MAX_MONEY: any bigger is a typo or an attack


def is_error(v):
    return isinstance(v, str) and v.strip().upper() in XL_ERRORS


class XLError(Exception):
    """An Excel error value in the arithmetic (#VALUE!, #DIV/0!): the cell shows an error, or IFERROR's fallback."""


class Unsupported(Exception):
    """A formula the arithmetic evaluator doesn't read (a function, a range, a name)."""


def words_in(text_, words):
    t = (text_ or '').casefold()
    return any(re.search(rf'(?<![a-z0-9]){re.escape(w.casefold())}s?(?![a-z0-9])', t) for w in words)


class SelectRun(engine.Run):
    def __init__(self, book, m, margin_tolerance=ZERO):
        self.book, self.m = book, m
        self.words = m['words']
        self.by_others = tuple(w.casefold() for w in m.get('by_others_words', engine.BY_OTHERS_WORDS))
        self.ps_words = m.get('ps_words', [])
        self.allowance_words = m.get('allowance_words', [])
        self.tolerance = Decimal(margin_tolerance or 0)
        self.issues, self.gaps, self.notes, self.from_excel = [], [], [], []
        self.cached = True
        self.stale = self.form_bad = False
        self.trades_below = []

    # --- reading -------------------------------------------------------------------------
    def col(self, letter):
        return maps.col(letter)

    def excel(self, what, where):
        self.from_excel.append({'what': what, 'where': where})

    def link(self, sheet, row, col):
        """(sheet, row, col) a formula that is just a reference points at, or None."""
        f = self.book.formula(sheet, row, col)
        if not isinstance(f, str):
            return None
        m = REF.match(f)
        if not m:
            return None
        target = (m.group(1) or '').replace("''", "'") or (m.group(2) or '').strip() or sheet
        return target, int(m.group(4)), maps.col(m.group(3))

    def evaluate(self, sheet, row, col, depth=0):
        """
        A cell's value as Excel would work it out, when its formula is plain arithmetic of numbers and cells
        (+ - * / and brackets): blank counts 0, text or an error value is #VALUE!. Raises XLError or Unsupported.
        """
        if not self.book.within(sheet, row, col):
            raise Unsupported(f'{sheet}!{cell(row, col)}')  # beyond what was read: its value isn't known
        f = self.book.formula(sheet, row, col)
        saved = self.book.value(sheet, row, col)
        if not (isinstance(f, str) and f.startswith('=')) or f == saved:
            v = saved
            if v is None or (isinstance(v, str) and v == ''):
                return ZERO
            if isinstance(v, bool):
                return ONE if v else ZERO
            if isinstance(v, Decimal):
                if abs(v) >= MAX_MONEY:
                    raise Unsupported(f'{sheet}!{cell(row, col)}')
                return v
            raise XLError(v)
        if depth > 6 or len(f) > MAX_FORMULA:
            raise Unsupported(f)
        return self.arith(f[1:], sheet, depth)

    def arith(self, expr, sheet, depth):
        pos, tokens = 0, []
        expr = expr.strip()
        while pos < len(expr):
            m = TOKEN.match(expr, pos)
            if not m or m.end() == pos:
                raise Unsupported(expr)
            tokens.append((m.lastgroup, m.group(m.lastgroup)))
            pos = m.end()
            if len(tokens) > 200:
                raise Unsupported(expr)
        i = 0

        def peek():
            return tokens[i] if i < len(tokens) else (None, None)

        def take():
            nonlocal i
            i += 1
            return tokens[i - 1]

        def factor():
            kind, v = take() if i < len(tokens) else (None, None)
            if kind == 'op' and v in '+-':
                x = factor()
                return -x if v == '-' else x
            if kind == 'num':
                x = Decimal(v)
                if not x.is_finite() or abs(x) >= MAX_MONEY:
                    raise Unsupported(v)
                return x
            if kind == 'ref':
                m = CELLREF.match(v.replace('$', ''))
                target = ((m.group(1) or '').replace("''", "'") or (m.group(2) or '')) or sheet
                if not self.book.has(target):
                    raise Unsupported(v)
                return self.evaluate(target, int(m.group(4)), maps.col(m.group(3)), depth + 1)
            if kind == 'op' and v == '(':
                x = expr_()
                if take() != ('op', ')'):
                    raise Unsupported(expr)
                return x
            raise Unsupported(expr)

        def term():
            x = factor()
            while peek() in (('op', '*'), ('op', '/')):
                _, op = take()
                y = factor()
                if op == '*':
                    x = x * y
                else:
                    if y == ZERO:
                        raise XLError('#DIV/0!')
                    x = x / y
            return x

        def expr_():
            x = term()
            while peek() in (('op', '+'), ('op', '-')):
                _, op = take()
                y = term()
                x = x + y if op == '+' else x - y
            return x

        try:
            out = expr_()
        except (DivisionByZero, IndexError):
            raise XLError('#VALUE!')
        except DecimalException:  # overflow, underflow, an invalid operation: nothing Excel would hold
            raise Unsupported(expr)
        if i != len(tokens):
            raise Unsupported(expr)
        if abs(out) >= MAX_MONEY:
            raise Unsupported(expr)
        return out

    def figure(self, sheet, row, col, what):
        """
        (value, from_excel): a typed number as typed; plain arithmetic recomputed; any other formula Excel's saved
        value, marked. An error (in the cell or its arithmetic) is returned as its text.
        """
        f = self.book.formula(sheet, row, col)
        saved = self.book.value(sheet, row, col)
        if not (isinstance(f, str) and f.startswith('=')) or f == saved:
            return saved, False
        try:
            return self.evaluate(sheet, row, col), False
        except XLError as e:
            return (str(e.args[0]) if e.args and is_error(e.args[0]) else '#VALUE!'), False
        except Unsupported:
            self.excel(what, f'{sheet}!{cell(row, col)}')
            return saved, True

    # --- the summary's rows --------------------------------------------------------------------
    @staticmethod
    def label_is(label, word):
        """The label is the word, or starts with it as a whole word ('Prelims (see prelim book)')."""
        return label == word or (label.startswith(word) and not label[len(word)].isalnum())

    def locate(self):
        s = self.m['summary']
        sheet = s['sheet']
        c = {k: maps.col(v) for k, v in s['cols'].items()}
        labels = {k: v.casefold() for k, v in s['labels'].items()}
        found, trades, special = {k: [] for k in labels}, [], {self.m['prelims']['sheet'], self.m['joinery']['sheet'],
                                                               self.m['clarifications']['sheet'], sheet}
        sheets = set(self.book.sheetnames)
        for r in range(s['first_row'], s['scan_to'] + 1):
            name_v = self.book.value(sheet, r, c['name'])
            target = self.link(sheet, r, c['name'])
            label = text(name_v).casefold()
            if target and target[0] != sheet and target[0] not in special:
                trades.append({'row': r, 'sheet': target[0]})
                continue
            hit = next((k for k, v in labels.items() if self.label_is(label, v)), None)
            if hit:
                found[hit].append(r)
                continue
            if label and text(name_v) in sheets and text(name_v) not in special:
                trades.append({'row': r, 'sheet': text(name_v)})
        rows = {}
        # The total is the Total row whose charge sums the block of trade rows above it
        candidates = found['total']
        for r in candidates:
            span = self.sum_span(sheet, r, c['charge'])
            above = [t['row'] for t in trades if t['row'] < r]
            if span and above and span[0] <= min(above) and span[1] >= max(above) and span[1] < r:
                rows['total'] = r
                break
        if 'total' not in rows and candidates:
            rows['total'] = candidates[0]
        total = rows.get('total')
        for key, hits in found.items():
            if key == 'total' or not hits:
                continue
            below = key in ('profit', 'percentage')
            pick = [r for r in hits if total is None or (r > total if below else r < total)]
            if pick:
                rows[key] = pick[0]
        self.trades_below = [t for t in trades if total is not None and t['row'] > total]
        trades = [t for t in trades if total is None or t['row'] < total]
        checks = {}
        lc, vc = maps.col(s['check_label_col']), maps.col(s['check_value_col'])
        wanted = {k: v.casefold() for k, v in s['check_labels'].items()}
        for r in range((total or s['first_row']) + 1, s['scan_to'] + 1):
            label = text(self.book.value(sheet, r, lc)).casefold()
            hit = next((k for k, v in wanted.items() if k not in checks and label == v), None)
            if hit:
                checks[hit] = (r, vc)
        return rows, trades, checks

    def sum_span(self, sheet, row, col):
        """(first row, last row) when the cell is =SUM(X a:X b) over its own column, else None."""
        f = self.book.formula(sheet, row, col)
        m = SUM_FORMULA.match(f) if isinstance(f, str) else None
        if not m or maps.col(m.group(1)) != col or maps.col(m.group(3)) != col:
            return None
        return int(m.group(2)), int(m.group(4))

    # --- trades ---------------------------------------------------------------------------------
    def total_row(self, sheet, srow):
        """The tab's total row: the one the summary's cost links to, else the row labelled TOTAL."""
        s, t = self.m['summary'], self.m['trade']
        target = self.link(s['sheet'], srow, maps.col(s['cols']['cost']))
        if target and target[0] == sheet and target[2] == maps.col(t['total_cols']['cost']):
            return target[1]
        lc = maps.col(t['total_label_col'])
        for r in range(t['first_row'], t['max_row'] + 1):
            if same(self.book.value(sheet, r, lc), t['total_label']):
                return r
        return None

    def markup_for(self, name):
        """The tab's AE2: INDEX(summary F, MATCH(B2, summary D, 0)), 0 when there's no match."""
        s = self.m['summary']
        sheet, c = s['sheet'], {k: maps.col(v) for k, v in s['cols'].items()}
        first, last = s['markup_lookup']
        for r in range(first, last + 1):
            v = self.resolve(sheet, r, c['name'])
            if not blank(v) and text(v).casefold() == (name or '').casefold():
                found = num(self.resolve(sheet, r, c['markup']))
                return found if found is not None else ZERO
        return ZERO

    def ps_like(self, *texts):
        return any(words_in(t, self.ps_words) for t in texts if t)

    def trade(self, entry):
        t, s = self.m['trade'], self.m['summary']
        sheet, srow = entry['sheet'], entry['row']
        sc = {k: maps.col(v) for k, v in s['cols'].items()}
        name = text(self.book.value(s['sheet'], srow, sc['name'])) or sheet
        notes = text(self.book.value(s['sheet'], srow, sc['notes']))
        out = {'name': name, 'sheet': sheet, 'summary_row': srow, 'lines': [], 'missing': not self.book.has(sheet),
               'notes': notes, 'from_excel': False}
        n = len(t['slots'])
        names = [text(self.raw(sheet, r)) for r in t['supplier_names']]
        names = ['' if x == '0' else x for x in names]
        w2_raw = self.raw(sheet, t['selected_name'])
        w2 = '' if is_error(w2_raw) else text(w2_raw)
        selected = w2 not in ('', '0')
        slot = next((i for i, x in enumerate(names) if x and x.casefold() == w2.casefold()), None) if selected else None
        tab_name = text(self.raw(sheet, t['name'])) or name
        markup = self.markup_for(tab_name)
        out.update(suppliers=names, bidder=slot + 1 if slot is not None else None, supplier=w2 if selected else '',
                   markup=markup, selected=selected)
        own = selected and words_in(w2, self.allowance_words)
        out['provisional'] = sheet in t['provisional'] or (selected and self.ps_like(w2))
        out['own_allowance'] = own
        total_row = None if out['missing'] else self.total_row(sheet, srow)
        out['total_row'] = total_row
        cols = {k: maps.col(v) for k, v in t['cols'].items()}
        sel = {k: maps.col(v) for k, v in t['selected'].items()}
        slots = [{k: maps.col(v) for k, v in sl.items()} for sl in t['slots']]
        slot_totals, slot_counts = [None] * n, [0] * n
        slot_sums = [ZERO] * n
        cost_total = charge_total = ZERO
        mismatched = []
        last = (total_row - 1) if total_row else t['max_row']
        for row in range(t['first_row'], last + 1) if not out['missing'] else ():
            v = lambda c: self.book.value(sheet, row, c)  # noqa: E731
            desc, qty_raw, ref = v(cols['description']), v(cols['qty']), text(v(cols['ref']))
            slot_vals = []
            for sl in slots:
                rate, adj = v(sl['rate']), v(sl['adj'])
                slot_vals.append({'rate': num(rate), 'adj': num(adj), 'comment': text(v(sl['comment'])),
                                  'rate_text': '' if num(rate) is not None or blank(rate) else text(rate)})
            if blank(desc) and blank(qty_raw) and not ref and not any(
                    sv['rate'] is not None or sv['adj'] is not None or sv['comment'] for sv in slot_vals):
                continue
            qty = num(qty_raw)
            for i, sv in enumerate(slot_vals):
                # H = IFERROR(IF(D="","",D*(F+G)),0): blank counts 0, text is an error (0)
                sv['quote'] = None
                if qty is not None and (sv['rate'] is not None or sv['adj'] is not None) and not sv['rate_text']:
                    sv['quote'] = qty * ((sv['rate'] or ZERO) + (sv['adj'] or ZERO))
                    slot_totals[i] = (slot_totals[i] or ZERO) + sv['quote']
                    slot_counts[i] += 1
                    slot_sums[i] += sv['quote']
            # Y and Z: Excel's FILTER of the selected supplier's columns
            emulated = (ZERO, ZERO)
            if slot is not None:
                ch = slot_vals[slot]
                emulated = (ch['rate'] if ch['rate'] is not None else (None if ch['rate_text'] else ZERO),
                            ch['adj'] if ch['adj'] is not None else ZERO)
            if self.cached:
                y_raw, z_raw = v(sel['rate']), v(sel['adj'])
                y = ZERO if blank(y_raw) else num(y_raw)
                z = ZERO if blank(z_raw) else num(z_raw)
                if (y, z) != emulated:
                    mismatched.append(row)
                    for k, got, want in (('rate', y, emulated[0]), ('adj', z, emulated[1])):
                        if got != want:
                            self.issues.append({
                                'sheet': sheet, 'cell': cell(row, sel[k]), 'computed': want, 'excel': got,
                                'what': f'{name} line {ref or row} selected {"rate" if k == "rate" else "Est Adj"} '
                                        f'(Excel\'s copy of {w2 or "no supplier"})',
                                'excel_text': '' if got is not None else 'not a number',
                                'difference': abs((got or ZERO) - (want or ZERO))})
                            self.stale = True
            else:
                y, z = emulated
            s_ = y + z if y is not None and z is not None else None
            cost = s_ * qty if s_ is not None and qty is not None else None
            tender = (ONE + markup) * s_ if s_ is not None and s_ != ZERO else None
            charge = tender * qty if tender is not None and qty is not None else None
            chosen = slot_vals[slot] if slot is not None else None
            comment = chosen['comment'] if chosen else ''
            status = '' if blank(desc) else (self.words['priced'] if cost is not None and selected
                                             else self.words['review'])
            line = {'row': row, 'ref': ref, 'description': text(desc), 'qty': qty,
                    'qty_text': '' if qty is not None else text(qty_raw), 'unit': text(v(cols['unit'])),
                    'slots': slot_vals, 'rate': y, 'adj': z, 'cost': cost, 'charge': charge, 'tender_rate': tender,
                    'status': status, 'comment': comment, 'from_excel': ['rate', 'adj'] if self.cached else []}
            others = [sv for i, sv in enumerate(slot_vals) if i != slot and ((sv['rate'] or ZERO) != ZERO
                                                                              or (sv['adj'] or ZERO) != ZERO)]
            line['others_only'] = bool(others) and (s_ is None or s_ == ZERO)
            line['ps'] = self.ps_like(line['description'], comment, *(sv['comment'] for sv in others))
            # The line's own formulas cleared (or overtyped with nothing): what it prices is left out of the tab total
            line['no_formula'] = self.cached and bool(cost) and not self.book.is_formula(sheet, row, sel['cost']) \
                and blank(v(sel['cost']))
            if cost is not None:
                cost_total += cost
            if charge is not None:
                charge_total += charge
            what = f'{name} line {ref or row}'
            self.check(sheet, row, sel['cost'], cost, f'{what} cost')
            self.check(sheet, row, sel['tender_rate'], tender, f'{what} rate with markup')
            self.check(sheet, row, sel['charge'], charge, f'{what} charge')
            out['lines'].append(line)
        if mismatched:
            self.notes.append(f'{name}: Excel\'s copy of the selected supplier\'s rates (columns '
                              f'{t["selected"]["rate"]} and {t["selected"]["adj"]}) differs from the "{w2 or "blank"}" '
                              f'columns at row{"s" if len(mismatched) != 1 else ""} '
                              f'{", ".join(str(r) for r in mismatched[:8])}, so the workbook was not recalculated '
                              'after a change. Recalculate it in Excel, save, and upload it again.')
        if self.cached and out['lines']:
            out['from_excel'] = True
            self.excel(f'{name}: the selected supplier\'s rates and Est Adj (Excel\'s FILTER)',
                       f'{sheet}!{t["selected"]["rate"]}:{t["selected"]["adj"]}')
        if total_row:
            tc = {k: maps.col(v) for k, v in t['total_cols'].items()}
            self.check(sheet, total_row, tc['cost'], cost_total, f'{name} total cost')
            self.check(sheet, total_row, tc['charge'], charge_total, f'{name} total charge')
            for i, sl in enumerate(slots):
                self.check(sheet, total_row, sl['rate'], slot_sums[i], f'{name} {names[i] or f"supplier {i + 1}"} '
                                                                        'quotation total')
        if not out['missing']:
            self.check(sheet, *maps.split(t['markup']), markup, f'{name} markup')
        include = selected or cost_total != ZERO or charge_total != ZERO
        out.update(include=include, include_text='Supplier selected' if selected else 'No supplier selected',
                   why_excluded='no supplier is selected on its tab and nothing is priced',
                   tab_cost=cost_total, tab_charge=charge_total, slot_totals=slot_totals, slot_counts=slot_counts,
                   cost=cost_total, charge=charge_total)
        out['profit'] = out['charge'] - out['cost']
        out['margin'] = margin(out['profit'], out['charge'])
        for ln in out['lines']:
            ln['cls'] = self.classify(out, ln)
        out['line_count'] = sum(1 for ln in out['lines'] if ln['description'])
        out['review_count'] = sum(1 for ln in out['lines'] if ln['cls'] == REVIEW and ln['description'])
        summary = s['sheet']
        if out['missing']:
            for key in ('cost', 'charge'):
                saved = num(self.book.value(summary, srow, sc[key]))
                out[key] = saved if saved is not None else ZERO
            if out['cost'] or out['charge']:
                out['from_excel'] = True
                self.excel(f'{name}: its tab is missing, so the summary\'s own cost and charge',
                           f'{summary}!{s["cols"]["cost"]}{srow}:{s["cols"]["charge"]}{srow}')
        else:
            self.check(summary, srow, sc['cost'], out['cost'], f'Summary {name} cost')
            self.check(summary, srow, sc['charge'], out['charge'], f'Summary {name} charge')
            self.check_text(summary, srow, sc['supplier'], w2 if selected else '0', f'Summary {name} supplier')
        out['profit'] = out['charge'] - out['cost']
        out['margin'] = margin(out['profit'], out['charge'])
        self.trade_gaps(out)
        return out

    def classify(self, trade, line):
        if not trade['include']:
            return EXCLUDED
        if line['cost'] is None:
            return REVIEW
        if line['others_only']:
            return REVIEW
        rate, adj, comment = line['rate'] or ZERO, line['adj'] or ZERO, line['comment'].casefold()
        if rate + adj == ZERO:
            if comment and any(re.search(rf'\b{re.escape(w)}\b', comment) for w in self.by_others):
                return BY_OTHERS
            return INCLUDED
        if trade['provisional'] or line['ps']:
            return PROVISIONAL
        if trade['own_allowance'] or (rate == ZERO and adj != ZERO):
            return ALLOWANCE
        return PRICED

    @staticmethod
    def refs(lines):
        refs = ', '.join(ln['ref'] or f'row {ln["row"]}' for ln in lines[:8])
        return refs + ('…' if len(lines) > 8 else '')

    def trade_gaps(self, tr):
        name, sheet, t = tr['name'], tr['sheet'], self.m['trade']
        lines = [ln for ln in tr['lines'] if ln['description']]
        if tr['missing']:
            if tr['cost'] or tr['charge']:
                self.gap('missing_sheet', f'{name}: tab missing', f'The summary links {name} to a "{sheet}" tab the '
                         'workbook doesn\'t have.', self.m['summary']['sheet'], '', name, tr['charge'] or None, 'bad')
            return
        if lines and not tr['selected']:
            cheapest = min((x for x in tr['slot_totals'] if x), default=None)
            self.gap('no_supplier', f'{name}: {len(lines)} item{"s" if len(lines) != 1 else ""} but no supplier '
                     'selected', f'{t["selected_name"]} is blank, so Excel picks no rates and the trade adds nothing '
                     'to the tender. Choose the supplier, or delete the items if the trade is not in the tender.',
                     sheet, t['selected_name'], name, cheapest, 'bad')
        if tr['selected'] and tr['bidder'] is None:
            self.gap('supplier_not_found', f'{name}: selected supplier "{tr["supplier"]}" is not one of the names in '
                     'row 2', 'Excel finds no columns for that name, so every line is costed at nothing.', sheet,
                     t['selected_name'], name, min((x for x in tr['slot_totals'] if x), default=None), 'bad')
        if not tr['include']:
            return
        others = [ln for ln in lines if ln['others_only']] if tr['bidder'] else []
        ps_others = [ln for ln in others if ln['ps']]
        rest = [ln for ln in others if not ln['ps']]

        def quotes(ln, pick):
            q = [sv['quote'] for i, sv in enumerate(ln['slots']) if i != tr['bidder'] - 1 and sv['quote']]
            return pick(q) if q else ZERO
        if ps_others:
            self.gap('ps_unselected', f'{name}: provisional sum{"s" if len(ps_others) != 1 else ""} only in an '
                     'unselected supplier\'s columns', 'A provisional sum entered against another supplier is not in '
                     'the tender: ' + self.refs(ps_others) + '. Enter it in the selected supplier\'s Est Adj column.',
                     sheet, '', name, sum((quotes(ln, max) for ln in ps_others), ZERO) or None, 'bad')
        if rest:
            self.gap('unselected_rates', f'{name}: {len(rest)} line{"s" if len(rest) != 1 else ""} priced only by an '
                     'unselected supplier', f'{tr["supplier"]} has no rate where another supplier does, so these add '
                     'nothing: ' + self.refs(rest) + '. Check they are in the selected price, or add an Est Adj.',
                     sheet, '', name, sum((quotes(ln, min) for ln in rest), ZERO) or None)
        cleared = [ln for ln in tr['lines'] if ln['no_formula']]
        if cleared:
            sel = self.m['trade']['selected']
            self.gap('formula_missing', f'{name}: line formulas missing on {len(cleared)} '
                     f'row{"s" if len(cleared) != 1 else ""}', f'The selected supplier prices '
                     f'{self.refs(cleared)} but the row\'s {sel["cost"]} and {sel["charge"]} formulas are blank, so it '
                     'is left out of the tab total and the tender. Copy the formulas down from the row above.', sheet,
                     f'{sel["cost"]}{cleared[0]["row"]}', name, sum((ln['charge'] or ln['cost'] for ln in cleared),
                                                                     ZERO), 'bad')
        unpriced_ps = [ln for ln in lines if self.ps_like(ln['description']) and not ln['comment'] and not ln['others_only']
                       and ln['cost'] == ZERO]
        if unpriced_ps:
            self.gap('ps_unpriced', f'{name}: provisional sum line{"s" if len(unpriced_ps) != 1 else ""} with no sum',
                     'A line that calls for a provisional sum has nothing in it: ' + self.refs(unpriced_ps)
                     + '. Enter the sum in the selected supplier\'s Est Adj column, or note why it is nil.', sheet, '',
                     name)
        review = [ln for ln in lines if ln['cost'] is None]
        if review:
            self.gap('review_lines', f'{name}: {len(review)} line{"s" if len(review) != 1 else ""} not costed',
                     'No quantity, or a quantity or rate that isn\'t a number: ' + self.refs(review), sheet, '', name)
        silent = [ln for ln in lines if ln['cls'] == INCLUDED and not ln['comment'] and not ln['ps']]
        if silent:
            self.gap('zero_no_comment', f'{name}: {len(silent)} zero rate{"s" if len(silent) != 1 else ""} with no '
                     'comment', 'A zero should say where the work is included: ' + self.refs(silent), sheet, '', name,
                     None, 'info')
        nameless = [ln for ln in tr['lines'] if not ln['description'] and ln['cost']]
        if nameless:
            self.gap('no_description', f'{name}: priced line with no description', 'Rows ' + ', '.join(
                str(ln['row']) for ln in nameless[:8]) + ' are in the total but say nothing.', sheet, '', name,
                sum((ln['cost'] for ln in nameless), ZERO))
        if tr['markup'] == ZERO and tr['cost'] > ZERO:
            ps = tr['provisional'] or self.ps_like(tr['notes'])
            cell_ = f"{self.m['summary']['cols']['markup']}{tr['summary_row']}"
            if ps:
                self.gap('ps_face_value', f'{name}: provisional sum carried at face value', 'Markup is 0 on a '
                         'trade labelled as a provisional sum, so it is passed on at cost.', self.m['summary']['sheet'],
                         cell_, name, tr['cost'], 'info')
            else:
                self.gap('zero_markup', f'{name}: markup is zero', 'A trade that isn\'t a provisional sum carried at '
                         'cost.', self.m['summary']['sheet'], cell_, name, tr['cost'])

    # --- joinery --------------------------------------------------------------------------------
    def workshops(self):
        w = self.m.get('workshops')
        out = {}
        if not w:
            return out
        pat = re.compile(w['pattern'])
        cc, tc = maps.col(w['cost_col']), maps.col(w['charge_col'])
        for sheet in self.book.sheetnames:
            if not pat.match(sheet or '') or not self.book.has(sheet):
                continue
            parts = {}
            for key, row in w['parts'].items():
                parts[key] = (num(self.book.value(sheet, row, cc)), num(self.book.value(sheet, row, tc)))
            vals = {}
            for key in ('delivery', 'install'):
                row = w['rows'][key]
                vals[key] = (num(self.book.value(sheet, row, cc)) or ZERO, num(self.book.value(sheet, row, tc)) or ZERO)
            supply = (sum((p[0] or ZERO for p in parts.values()), ZERO), sum((p[1] or ZERO for p in parts.values()), ZERO))
            total = tuple(supply[i] + vals['delivery'][i] + vals['install'][i] for i in (0, 1))
            for key, value in (('supply', supply), ('total', total)):
                row = w['rows'][key]
                self.check(sheet, row, cc, value[0], f'Workshop sheet {sheet} {key} cost')
                self.check(sheet, row, tc, value[1], f'Workshop sheet {sheet} {key} charge')
            if self.cached:
                self.excel(f'Workshop sheet {sheet}: the cost build-up (labour, materials, ancillaries, finish, '
                           'delivery, installation)', f'{sheet}!{w["cost_col"]}2:{w["charge_col"]}{w["rows"]["install"]}')
            cells = {}
            for key, value in (('supply', supply), ('delivery', vals['delivery']), ('install', vals['install']),
                               ('total', total)):
                cells[(w['rows'][key], cc)] = value[0]
                cells[(w['rows'][key], tc)] = value[1]
            out[sheet] = {'sheet': sheet, 'supply': supply, 'delivery': vals['delivery'], 'install': vals['install'],
                          'total': total, 'cells': cells, 'description': text(self.raw(sheet, w['description'])),
                          'linked': set()}
        return out

    def joinery(self, shops):
        j, sheet = self.m['joinery'], self.m['joinery']['sheet']
        out = {'sheet': sheet, 'lines': [], 'missing': not self.book.has(sheet), 'markups': {}}
        zero = dict(cost=ZERO, charge=ZERO, supply_cost=ZERO, supply_charge=ZERO, install_cost=ZERO,
                    install_charge=ZERO)
        if out['missing']:
            out.update(zero)
            return out
        c = {k: maps.col(v) for k, v in j['cols'].items()}
        total_row = next((r for r in range(j['first_row'], j['last_row'] + 1)
                          if same(self.book.value(sheet, r, c['description']), j['total_label'])), None)
        parts = ('supply_cost', 'supply_charge', 'install_cost', 'install_charge')
        sums = {k: ZERO for k in parts}
        k_total = ZERO
        for row in range(j['first_row'], (total_row or j['last_row'] + 1)):
            vals, from_excel = {}, []
            for k in parts:
                target = self.link(sheet, row, c[k])
                if target and target[0] in shops and (target[1], target[2]) in shops[target[0]]['cells']:
                    shops[target[0]]['linked'].add(target[1])
                    vals[k] = shops[target[0]]['cells'][(target[1], target[2])]
                else:
                    v, fx = self.figure(sheet, row, c[k], f'Joinery row {row}')
                    vals[k] = v
                    if fx:
                        from_excel.append(k)
            ref, desc = text(self.book.value(sheet, row, c['ref'])), text(self.book.value(sheet, row, c['description']))
            qty_raw = self.book.value(sheet, row, c['qty'])
            if not ref and all(blank(vals[k]) for k in parts) and blank(qty_raw):
                continue
            qty = num(qty_raw)
            nums = {k: num(vals[k]) for k in parts}
            errors = [k for k in parts if not blank(vals[k]) and nums[k] is None]
            # J = IFERROR(SUM(I+G)/D,""), K = IFERROR(J*D,"")
            rate = None
            if not errors and qty is not None and qty != ZERO:
                rate = ((nums['install_charge'] or ZERO) + (nums['supply_charge'] or ZERO)) / qty
            total = rate * qty if rate is not None else None
            cost = (nums['supply_cost'] or ZERO) + (nums['install_cost'] or ZERO)
            charge = (nums['supply_charge'] or ZERO) + (nums['install_charge'] or ZERO)
            for k in parts:
                if nums[k] is not None:
                    sums[k] += nums[k]
                if not (from_excel and k in from_excel):
                    self.check(sheet, row, c[k], nums[k], f'Joinery {ref or row} {k.replace("_", " ")}')
            self.check(sheet, row, c['rate'], rate, f'Joinery {ref or row} rate')
            self.check(sheet, row, c['total'], total, f'Joinery {ref or row} total')
            if total is not None:
                k_total += total
            figures = any(nums[k] for k in parts)
            ps = self.ps_like(desc)
            cls = PROVISIONAL if ps and figures else (PRICED if figures and qty is not None else REVIEW)
            line = {'row': row, 'ref': ref, 'description': desc, 'unit': text(self.book.value(sheet, row, c['unit'])),
                    'qty': qty, **nums, 'rate': rate, 'cost': cost if figures else None,
                    'charge': charge if figures else None, 'status': self.words['priced'] if cls != REVIEW else
                    self.words['review'], 'notes': '', 'cls': cls, 'ps': ps, 'from_excel': from_excel,
                    'missing': [k for k in parts if nums[k] is None] + ([] if qty is not None else ['qty'])}
            out['lines'].append(line)
        if total_row:
            for k in parts:
                self.check(sheet, total_row, c[k], sums[k], f'Joinery total {k.replace("_", " ")}')
            self.check(sheet, total_row, c['total'], k_total, 'Joinery total')
        out.update(supply_cost=sums['supply_cost'], supply_charge=sums['supply_charge'],
                   install_cost=sums['install_cost'], install_charge=sums['install_charge'],
                   cost=sums['supply_cost'] + sums['install_cost'], charge=sums['supply_charge'] + sums['install_charge'],
                   total_row=total_row)
        incomplete = [ln for ln in out['lines'] if ln['description'] and ln['cls'] == REVIEW]
        if incomplete:
            self.gap('joinery_incomplete', f'Joinery: {len(incomplete)} item{"s" if len(incomplete) != 1 else ""} '
                     'incomplete', 'No quantity, or no cost or charge carried: ' + ', '.join(
                         ln['ref'] or ln['description'][:30] for ln in incomplete[:8]), sheet, '', 'Joinery', None,
                     'bad')
        face = [ln for ln in out['lines'] if ln['ps'] and ln['cost'] and ln['charge'] == ln['cost']]
        if face:
            self.gap('ps_face_value', f'Joinery: {len(face)} provisional sum{"s" if len(face) != 1 else ""} carried at '
                     'face value', 'Charge equals cost (no markup): ' + ', '.join(
                         ln['ref'] or ln['description'][:30] for ln in face[:8]), sheet, '', 'Joinery',
                     sum((ln['cost'] for ln in face), ZERO), 'info')
        self.joinery_gaps(shops)
        return out

    def joinery_gaps(self, shops):
        w = self.m['workshops']
        rows, sheet = w['rows'], self.m['joinery']['sheet']
        for ws in shops.values():
            linked, n = ws['linked'], ws['sheet']
            label = f'Workshop sheet {n}' + (f' ({ws["description"][:50]})' if ws['description'] and
                                             not is_error(ws['description']) else '')
            if not linked:
                if any(ws['total']):
                    self.gap('joinery_sheet_not_carried', f'{label} is not on JOINERY SUMMARY',
                             'Nothing on the summary links to this sheet, so its price is not in the tender.', n,
                             f'{w["charge_col"]}{rows["total"]}', 'Joinery', ws['total'][1] or ws['total'][0], 'bad')
                continue
            whole = rows['total'] in linked
            for key, what in (('delivery', 'delivery'), ('install', 'installation')):
                cost, charge = ws[key]
                if (cost or charge) and not whole and rows[key] not in linked:
                    self.gap(f'joinery_{key}_not_carried', f'{label}: {what} not carried to JOINERY SUMMARY',
                             f'The sheet prices {what} ({w["cost_col"]}{rows[key]}/{w["charge_col"]}{rows[key]}) but '
                             f'{sheet} links only to rows {", ".join(str(r) for r in sorted(linked))}, so it is not in '
                             'the tender.', n, f'{w["charge_col"]}{rows[key]}', 'Joinery', charge or cost, 'bad')

    # --- prelims --------------------------------------------------------------------------------
    def prelims(self, markup, total_cell):
        p, sheet = self.m['prelims'], self.m['prelims']['sheet']
        out = {'sheet': sheet, 'lines': [], 'missing': not self.book.has(sheet), 'sections': [], 'markup': markup}
        if out['missing']:
            self.gap('missing_sheet', f'{sheet} tab missing', '', sheet, severity='bad')
            out.update(cost=ZERO, charge=ZERO, duration=None)
            return out
        c = {k: maps.col(v) for k, v in p['cols'].items()}
        duration = num(self.resolve_ref(sheet, p['duration']))
        cost_total = ZERO
        section = ''
        unread = 0
        # The total is every number in the cost column (the workbook sums the whole column)
        for row in range(p['first_row'], p['last_row'] + 1):
            v = lambda k: self.book.value(sheet, row, c[k])  # noqa: E731
            is_item = self.book.is_formula(sheet, row, c['cost']) or num(v('cost')) is not None
            if not is_item:
                if text(v('section')):
                    section = text(v('section'))
                continue
            desc = text(v('description'))
            cost, fx = self.figure(sheet, row, c['cost'], f'Prelim {desc[:40] or row}')
            unread += fx
            cost = num(cost) if not blank(cost) else None
            charge = cost * (ONE + markup) if cost is not None else None
            if not fx:
                self.check(sheet, row, c['cost'], cost, f'Prelim {desc[:40] or row} cost')
            self.check(sheet, row, c['charge'], charge, f'Prelim {desc[:40] or row} charge')
            if cost is not None:
                cost_total += cost
            if not desc and not cost:
                continue
            qty, rate = num(v('qty')), num(v('rate'))
            priced = cost is not None and cost != ZERO
            line = {'row': row, 'section': section, 'include': '', 'description': desc or f'Row {row}',
                    'allocation': num(v('allocation')), 'qty': qty, 'unit': text(v('unit')), 'periods': None,
                    'rate': rate, 'cost': cost, 'markup': markup, 'charge': charge,
                    'status': self.words['priced'] if priced else self.words['excluded'],
                    'cls': PRICED if priced else EXCLUDED, 'basis': text(v('allocation')) if num(v('allocation'))
                    is None else '', 'has_inputs': rate is not None and rate != ZERO and not priced,
                    'why': 'Nil in the prelim book' + (' (it has a rate but no quantity)' if rate and not priced
                                                         else ''), 'from_excel': ['cost'] if fx else []}
            out['lines'].append(line)
        if total_cell and total_cell[0] == sheet:
            self.check(sheet, total_cell[1], total_cell[2], cost_total, 'Preliminaries total cost')
        out.update(cost=cost_total, charge=cost_total * (ONE + markup), duration=duration)
        if duration is None or duration == ZERO:
            self.gap('prelim_no_duration', 'Preliminaries: no programme duration', 'The prelim book has no duration '
                     'in weeks, so weekly lines may be nil.', sheet, p['duration'], 'Preliminaries', None)
        if cost_total > ZERO and markup == ZERO:
            self.gap('zero_markup', 'Preliminaries: markup is zero', 'Preliminaries carried at cost.',
                     self.m['summary']['sheet'], '', 'Preliminaries', cost_total)
        return out

    # --- clarifications ---------------------------------------------------------------------------
    def clarifications(self):
        cl, sheet = self.m['clarifications'], self.m['clarifications']['sheet']
        if not self.book.has(sheet):
            return []
        rc, tc = maps.col(cl['cols']['ref']), maps.col(cl['cols']['text'])
        out, current = [], None
        for row in range(cl['first_row'], cl['last_row'] + 1):
            ref, body = self.book.value(sheet, row, rc), text(self.book.value(sheet, row, tc))
            if num(ref) is not None or (isinstance(ref, str) and ref.strip()):
                current = {'row': row, 'ref': text(ref)[:20], 'trade': body[:80], 'text': '', 'response': '',
                           'status': '', 'cost_effect': '', 'owner': ''}
                out.append(current)
            elif body and current is not None:
                current['text'] = f'{current["text"]}\n{body}'.strip()
        for c in out:
            if not c['text']:
                c['text'] = c['trade']
        return out

    # --- the whole workbook -----------------------------------------------------------------------
    def run(self):
        m, s = self.m, self.m['summary']
        sheet = s['sheet']
        if not self.book.has(sheet):
            raise ValueError(f'This doesn\'t look like the pricing workbook: it has no "{sheet}" sheet. Check the '
                             'workbook layout chosen in Settings > Pricing workbook.')
        sc = {k: maps.col(v) for k, v in s['cols'].items()}
        rows, entries, checks = self.locate()
        if not entries or 'total' not in rows:
            raise ValueError(f'This doesn\'t look like the {m["name"]}: its "{sheet}" sheet has no trade rows linked '
                             'to trade tabs, or no Total row. Check the workbook layout chosen in Settings > Pricing '
                             'workbook.')
        trow = rows['total']
        for t in self.trades_below:
            self.gap('trade_below_total', f'Summary row {t["row"]} ({t["sheet"]}) is below the total',
                     'A trade row under the Total row is not in the tender. Move it above the total, or into the '
                     'block the total sums.', sheet, f'{s["cols"]["charge"]}{t["row"]}', t['sheet'],
                     num(self.book.value(sheet, t['row'], sc['charge'])), 'bad')
        self.cached = not (self.book.is_formula(sheet, trow, sc['charge']) and blank(self.book.value(sheet, trow,
                                                                                                    sc['charge'])))
        if not self.cached:
            self.notes.append('This file was saved without calculated values (it was not last saved by Excel), so '
                              'the figures here are Siteline\'s own recalculation and could not be checked against '
                              'Excel\'s. Open it in Excel, save it, and upload it again to check it.')
        header = {k: '' for k in ('project', 'date', 'client', 'revision', 'estimator')}  # what the pack shows
        for k, ref in s['header'].items():
            v = self.raw(sheet, ref)
            if is_error(v):
                header[k] = ''
                self.gap('header_error', f'The summary\'s {k} shows {text(v)}', f'{sheet}!{ref} is an error in '
                         'Excel, so the pack has no ' + k + '. Correct the formula or type the value.', sheet, ref,
                         '', None)
            else:
                header[k] = text(v)
        default_markup = num(self.resolve_ref(sheet, s['default_markup'])) or ZERO
        trades = [self.trade(e) for e in entries]
        shops = self.workshops()
        joinery = self.joinery(shops)
        prelim_row = rows.get('prelims')
        prelim_markup = num(self.resolve(sheet, prelim_row, sc['markup'])) if prelim_row else None
        total_cell = self.link(sheet, prelim_row, sc['cost']) if prelim_row else None
        prelims = self.prelims(prelim_markup or ZERO, total_cell)

        parts = []  # (row, cost, charge, label) for every summary row in the total
        for tr in trades:
            parts.append((tr['summary_row'], tr['cost'], tr['charge'], tr['name']))
        for key, cost, charge, label in (('joinery', joinery['supply_cost'], joinery['supply_charge'], 'Joinery'),
                                         ('installation', joinery['install_cost'], joinery['install_charge'],
                                          'Installation'),
                                         ('prelims', prelims['cost'], prelims['charge'], 'Preliminaries')):
            r = rows.get(key)
            if r is None:
                if cost or charge:
                    self.gap('summary_row_missing', f'{label} is not on the summary', f'No "{s["labels"][key]}" row '
                             f'on {sheet}, so {label.lower()} ({charge:,.2f}) is not in the total.', sheet, '', label,
                             charge, 'bad')
                continue
            self.check(sheet, r, sc['cost'], cost, f'Summary {label} cost')
            self.check(sheet, r, sc['charge'], charge, f'Summary {label} charge')
            parts.append((r, cost, charge, label))
        if rows.get('joinery') and joinery['supply_cost']:
            j = joinery
            self.check(sheet, rows['joinery'], sc['markup'], (j['supply_charge'] - j['supply_cost']) / j['supply_cost'],
                       'Summary Joinery markup')
        known = {p[0] for p in parts}
        for r in range(s['first_row'], trow):
            if r in known:
                continue
            ce, cg = self.book.value(sheet, r, sc['cost']), self.book.value(sheet, r, sc['charge'])
            if num(ce) or num(cg) or is_error(ce) or is_error(cg):
                label = text(self.book.value(sheet, r, sc['name'])) or f'Row {r}'
                parts.append((r, num(ce) or ZERO, num(cg) or ZERO, label))
                self.excel(f'Summary row {r} ({label[:40]})', f'{sheet}!{s["cols"]["cost"]}{r}:{s["cols"]["charge"]}{r}')
                self.gap('unrecognised_row', f'Summary row {r} ({label[:40]}) is in the total', 'It isn\'t a trade, '
                         'joinery, installation or prelims row, so Siteline takes Excel\'s figures for it.', sheet,
                         f'{s["cols"]["charge"]}{r}', '', num(cg))

        self.check_total_form(sheet, trow, (s['cols']['cost'], s['cols']['charge']), [p[0] for p in parts])
        cost = sum((p[1] for p in parts), ZERO)
        charge = sum((p[2] for p in parts), ZERO)
        profit = charge - cost
        excel = {k: num(self.book.value(sheet, trow, sc[k])) for k in ('cost', 'charge')}
        self.check(sheet, trow, sc['cost'], cost, 'Total cost')
        self.check(sheet, trow, sc['charge'], charge, 'Total charge')
        excel['profit'] = None
        if rows.get('profit'):
            pc = maps.col(s['profit_col'])
            excel['profit'] = num(self.book.value(sheet, rows['profit'], pc))
            self.check(sheet, rows['profit'], pc, profit, 'Profit')
        target = None
        our_margin = margin(profit, charge)
        if rows.get('percentage'):
            r = rows['percentage']
            mc = maps.col(s['margin_col'])
            self.check(sheet, r, mc, our_margin if our_margin is not None else ZERO, 'Percentage (margin)')
            target_raw = self.resolve(sheet, r, maps.col(s['target_col']))
            target = num(target_raw)
            if target is not None:
                self.check(sheet, r, maps.col(s['target_difference_col']), (our_margin or ZERO) - target,
                           'Margin against target')
        # Checked Total = the charges on rows ticked in the flag column; Difference = Client Document Total - it
        checked = sum((p[2] for p in parts if self.book.value(sheet, p[0], sc['flag']) is True), ZERO)
        client = None
        if 'checked_total' in checks:
            self.check(sheet, *checks['checked_total'], checked, 'Checked Total')
        if 'client_total' in checks:
            r, c = checks['client_total']
            client_raw = self.resolve(sheet, r, c)
            client = num(client_raw)
            where = cell(r, c)
            if is_error(client_raw) or (not blank(client_raw) and client is None):
                self.gap('client_total_error', f'Client Document Total shows {text(client_raw)[:30]}', 'The figure '
                         'the client sees should be checked against the pack total; it isn\'t a number.', sheet, where,
                         '', None, 'bad')
            elif client is None or client == ZERO:
                self.gap('client_total_missing', 'Client Document Total is not filled in', 'Enter the total from the '
                         'client\'s pricing document (form of tender or contract sum analysis) so it can be checked '
                         'against the pricing summary.', sheet, where, '', None)
            elif abs(client - charge) >= HALF_PENNY:
                self.gap('client_total_differs', 'Client Document Total differs from the pricing summary',
                         f'The client document says {client:,.2f}; the pricing summary totals {charge:,.2f}.', sheet,
                         where, '', abs(client - charge), 'bad')
            if 'client_difference' in checks and client is not None:
                self.check(sheet, *checks['client_difference'], client - checked, 'Client Document Total less '
                           'Checked Total')
        self.cell_errors(rows, checks)
        totals = {
            'cost': cost, 'charge': charge, 'profit': profit, 'margin': our_margin,
            'excel_cost': excel['cost'], 'excel_charge': excel['charge'], 'excel_profit': excel['profit'],
            'target_margin': target, 'price_at_target': (cost / (ONE - target)) if cost and target is not None
            and target < ONE else None, 'final_price': client if client else None,
            'final_margin': margin(client - cost, client) if client else None,
            'charge_cell': f'{s["cols"]["charge"]}{trow}', 'checked_total': checked, 'client_total': client,
        }
        saved_total = self.book.value(sheet, trow, sc['charge'])
        totals['excel_text'] = '' if excel['charge'] is not None else (text(saved_total)[:60] or 'blank')
        if not self.cached:
            totals['difference'], totals['reconciled'] = None, None
        elif excel['charge'] is None:
            totals['difference'], totals['reconciled'] = None, False
        else:
            totals['difference'] = charge - excel['charge']
            totals['reconciled'] = abs(totals['difference']) < HALF_PENNY
        self.settle(totals)
        if target is not None and our_margin is not None and our_margin < target - self.tolerance:
            short = (totals['price_at_target'] - charge) if totals['price_at_target'] is not None else None
            r = rows['percentage']
            self.gap('margin_below_target', f'Margin {our_margin * 100:.1f}% is below the sheet\'s {target * 100:.1f}% '
                     'target', 'Overall margin (profit / charge) against the target on the Percentage row.', sheet,
                     f'{s["target_col"]}{r}', '', short, 'bad')
        if target is None:
            self.gap('no_target_margin', 'No target margin set', 'The Percentage row has no target beside it.', sheet,
                     f'{s["target_col"]}{rows.get("percentage") or ""}', '', None, 'info')
        joinery['markups'] = {k: num(self.resolve_ref(sheet, ref)) for k, ref in s['joinery_markups'].items()}
        return {
            'mapping': m['key'], 'calc': m['calc'], 'header': header,
            'settings': {'default_markup': default_markup, 'prelim_markup': prelim_markup, 'target_margin': target,
                         'duration_weeks': prelims.get('duration')},
            'cached': self.cached, 'trades': trades, 'prelims': prelims, 'joinery': joinery,
            'workshops': [{k: v for k, v in ws.items() if k not in ('cells', 'linked')} | {'linked': sorted(ws['linked'])}
                          for ws in shops.values()],
            'clarifications': self.clarifications(), 'totals': totals,
            'issues': sorted(self.issues, key=lambda i: -i['difference']),
            'gaps': sorted(self.gaps, key=lambda g: (g['amount'] is None, -(g['amount'] or ZERO))),
            'notes': list(dict.fromkeys(self.notes)), 'sheetnames': self.book.sheetnames[:200],
            'from_excel': list({(x['what'], x['where']): x for x in self.from_excel}.values())[:200],
        }

    def cell_errors(self, rows, checks):
        """Error values on the summary outside the trade rows (the trade rows reconcile cell by cell)."""
        s = self.m['summary']
        sheet = s['sheet']
        seen = 0
        g = self.book.values.get(sheet)
        if g is None:
            return
        header = {maps.split(ref) for ref in s['header'].values()}
        check_cells = {v for v in checks.values()}
        for (r, c), v in sorted(g.cells.items()):
            if not is_error(v) or (r, c) in header or (r, c) in check_cells or seen >= 10:
                continue
            if r < rows.get('total', 0) and r >= s['first_row']:
                continue  # inside the total: a reconciliation issue already
            label = text(self.book.value(sheet, r, maps.col(s['check_label_col']))) or \
                text(self.book.value(sheet, r, maps.col(s['cols']['name']))) or f'row {r}'
            self.gap('cell_error', f'{sheet}!{cell(r, c)} shows {text(v)}', f'An error in Excel beside "{label[:40]}".',
                     sheet, cell(r, c), '', None)
            seen += 1


def extents(m):
    s, t, p, j, c = m['summary'], m['trade'], m['prelims'], m['joinery'], m['clarifications']
    far = lambda refs, cols, last: (max([maps.split(r)[0] for r in refs] + [last]),  # noqa: E731
                                    max([maps.split(r)[1] for r in refs] + [maps.col(x) for x in cols]))
    trade_cols = [*t['cols'].values(), *t['selected'].values(), *t['total_cols'].values(), t['total_label_col'],
                  *(v for slot in t['slots'] for v in slot.values())]
    out = {
        s['sheet']: far([s['default_markup'], *s['header'].values(), *s['joinery_markups'].values()],
                        [*s['cols'].values(), s['profit_col'], s['margin_col'], s['target_col'],
                         s['target_difference_col'], s['check_label_col'], s['check_value_col']], s['scan_to']),
        p['sheet']: far([p['duration']], p['cols'].values(), p['last_row']),
        j['sheet']: far([], j['cols'].values(), j['last_row']),
        c['sheet']: far([], c['cols'].values(), c['last_row']),
        '*': far([t['name'], t['selected_name'], t['markup'], *t['supplier_names']], trade_cols, t['max_row']),
    }
    w = m.get('workshops')
    if w:
        wr, wc = far([w['description']], [w['cost_col'], w['charge_col']], w['max_row'])
        out['*'] = (max(out['*'][0], wr), max(out['*'][1], wc))
    return out
