"""
The handover pack as an Excel workbook: a new file built from the pack (never a copy of the upload),
eleven sheets in the order of the page, totals as formulas, headers frozen, text wrapped, landscape A4,
and INTERNAL ONLY in every page header and footer.

Text is someone's typing (a description, a supplier's name, a clarification), so every text cell goes
through core.exports.safe() first, as the CSV exports do: a value starting = + - @ is kept as text with a
leading apostrophe and never runs as a formula. Numbers are written as numbers.
"""
import io
from decimal import ROUND_HALF_UP, Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.exports import safe
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

MONEY = '#,##0.00;[Red]-#,##0.00'
PCT = '0.0%'
QTY = '#,##0.###'
HEAD_FILL = PatternFill('solid', fgColor='1F2937')
HEAD_FONT = Font(bold=True, color='FFFFFF')
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical='top')
THIN = Border(bottom=Side(style='thin', color='D1D5DB'))
BANNER = 'INTERNAL ONLY: cost, margin and buy-out targets. Not for clients or subcontractors.'


def txt(v):
    """Typed text, safe for a spreadsheet: never a formula (core.exports.safe) and no characters Excel refuses."""
    return ILLEGAL_CHARACTERS_RE.sub('', safe(v))


def _n(v):
    """Decimal to float for Excel, to the penny where it is money (Excel holds doubles anyway)."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _m(v):
    return None if v is None else float(Decimal(v).quantize(Decimal('0.01'), ROUND_HALF_UP))


class Sheet:
    def __init__(self, wb, title, columns, first=False):
        self.ws = wb.active if first else wb.create_sheet()
        self.ws.title = title
        self.columns = columns  # [(header, width, kind)] kind: text, money, pct, qty, date
        self.row = 1

    def title(self, text, sub=''):
        self.ws.cell(self.row, 1, txt(text)).font = Font(bold=True, size=14)
        self.row += 1
        if sub:
            self.ws.cell(self.row, 1, txt(sub)).font = Font(italic=True, color='6B7280')
            self.row += 1
        self.ws.cell(self.row, 1, BANNER).font = Font(bold=True, color='B91C1C')
        self.row += 2

    def header(self):
        for i, (head, width, _) in enumerate(self.columns, start=1):
            c = self.ws.cell(self.row, i, head)
            c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, WRAP
            self.ws.column_dimensions[get_column_letter(i)].width = width
        self.ws.freeze_panes = self.ws.cell(self.row + 1, 1)
        self.ws.print_title_rows = f'{self.row}:{self.row}'
        self.head_row = self.row
        self.row += 1
        self.first_data = self.row

    def add(self, values, bold=False):
        for i, ((_, _, kind), v) in enumerate(zip(self.columns, values), start=1):
            if isinstance(v, Formula):  # written here, never from anyone's typing
                value = str(v)
            elif isinstance(v, (Decimal, int, float)) and not isinstance(v, bool) and kind != 'text':
                value = _m(v) if kind == 'money' else _n(v)
            else:
                value = txt(v) if v not in (None, '') else None
            c = self.ws.cell(self.row, i, value)
            c.alignment = WRAP
            c.border = THIN
            if kind == 'money':
                c.number_format = MONEY
            elif kind == 'pct':
                c.number_format = PCT
            elif kind == 'qty':
                c.number_format = QTY
            if bold:
                c.font = BOLD
        self.row += 1

    def total(self, label_col, sum_cols, label='Total'):
        """A totals row of SUM formulas over the rows added since the header."""
        last = self.row - 1
        values = []
        for i, (_, _, kind) in enumerate(self.columns, start=1):
            letter = get_column_letter(i)
            if i == label_col:
                values.append(label)
            elif i in sum_cols:
                values.append(Formula(f'=SUM({letter}{self.first_data}:{letter}{max(last, self.first_data)})'
                                      if last >= self.first_data else '=0'))
            else:
                values.append(None)
        self.add(values, bold=True)
        return self.row - 1

    def finish(self):
        ws = self.ws
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.gridLines = False
        ws.oddHeader.center.text = 'INTERNAL ONLY'
        ws.oddHeader.center.font = 'Arial,Bold'
        ws.oddHeader.right.text = '&A'
        ws.oddFooter.center.text = 'INTERNAL ONLY. Not for clients or subcontractors.'
        ws.oddFooter.right.text = 'Page &P of &N'


class Formula(str):
    """A formula this module wrote itself (the only text that may start with =)."""


def _pct(v):
    return None if v is None else float(v)


def build(ctx, org_name=''):
    p, wb_rev, project = ctx['pack'], ctx['workbook'], ctx['project']
    h, t, st = ctx['header'], ctx['totals'], ctx['settings']
    book = Workbook()
    status = 'Issued' if p and p.is_issued else 'Draft'

    # 1 Cover
    s = Sheet(book, 'Cover', [('', 34, 'text'), ('', 70, 'text')], first=True)
    s.ws.cell(1, 1, 'INTERNAL ONLY').font = Font(bold=True, size=22, color='B91C1C')
    s.ws.cell(2, 1, 'Estimating to delivery handover pack').font = Font(bold=True, size=16)
    s.row = 4
    rows = [
        ('Company', org_name), ('Project', f'{project.reference} {project.name}'),
        ('Client', project.client_name or h.get('client', '')), ('Pack status', status),
        ('Built from', f'Pricing workbook revision {wb_rev.revision}' + (f' (workbook revision {wb_rev.label})'
                                                                         if wb_rev.label else '')),
        ('Uploaded', f'{wb_rev.uploaded_at:%d %b %Y %H:%M}' + (f' by {wb_rev.uploaded_by.full_name}'
                                                                if wb_rev.uploaded_by else '')),
        ('Issued', f'{p.issued_at:%d %b %Y}' + (f' by {p.issued_by.full_name}' if p.issued_by else '')
         if p and p.is_issued else 'Not yet'),
        ('Reconciliation', 'Reconciled to the penny with the pricing summary' if ctx['reconciled'] else (
            'Not checked: the workbook had no saved values from Excel' if ctx['reconciled'] is None
            else f'NOT RECONCILED: {abs(ctx["difference"]):,} out')),
        ('Who may see this', 'Staff who see this job\'s commercial figures. Never send it to a client or '
                             'subcontractor.'),
    ]
    for label, value in rows:
        s.ws.cell(s.row, 1, label).font = BOLD
        s.ws.cell(s.row, 2, txt(value)).alignment = WRAP
        s.row += 1
    s.ws.column_dimensions['A'].width, s.ws.column_dimensions['B'].width = 26, 80
    s.ws.cell(s.row + 1, 1, 'Contents').font = BOLD
    for i, (_, title) in enumerate(ctx['sections'][1:], start=2):
        s.ws.cell(s.row + i, 1, f'{i}. {title}')
    s.finish()

    # 2 Project summary
    s = Sheet(book, 'Project summary', [('Item', 34, 'text'), ('Value', 40, 'text'), ('Figure', 18, 'money')])
    s.title('Project summary', f'{project.reference} {project.name}')
    s.header()
    for label, value in (('Project (Siteline)', project.name), ('Client', project.client_name),
                         ('Site address', project.site_address), ('Project manager',
                                                                  project.manager.full_name if project.manager_id else ''),
                         ('Start on site', f'{project.start_date:%d %b %Y}' if project.start_date else ''),
                         ('Completion', f'{project.completion_date:%d %b %Y}' if project.completion_date else ''),
                         ('Handover notes', project.handover_notes),
                         ('Workbook project', h.get('project')), ('Workbook client', h.get('client')),
                         ('Estimator (workbook)', h.get('estimator')), ('Workbook date', h.get('date')),
                         ('Workbook revision', h.get('revision'))):
        s.add([label, value or '', None])
    s.add(['Default trade markup', '', None])
    s.ws.cell(s.row - 1, 3, _pct(st.get('default_markup'))).number_format = PCT
    s.add(['Prelim markup', '', None])
    s.ws.cell(s.row - 1, 3, _pct(st.get('prelim_markup'))).number_format = PCT
    s.add(['Target margin', '', None])
    s.ws.cell(s.row - 1, 3, _pct(st.get('target_margin'))).number_format = PCT
    s.add(['Programme (weeks)', '', None])
    s.ws.cell(s.row - 1, 3, _n(st.get('duration_weeks'))).number_format = '0'
    s.add(['Total cost', '', t['cost']], bold=True)
    s.add(['Total charge (tender sum)', '', t['charge']], bold=True)
    s.add(['Profit', '', t['profit']], bold=True)
    s.add(['Margin', '', None])
    s.ws.cell(s.row - 1, 3, _pct(t['margin'])).number_format = PCT
    s.add(['Pricing summary total charge (Excel)', '', t['excel_charge']])
    s.add(['Price at target margin', '', t['price_at_target']])
    if t.get('final_price') is not None:
        s.add(['Final tender price (workbook)', '', t['final_price']])
    s.finish()

    # 3 Price summary
    s = Sheet(book, 'Price summary', [('Trade', 28, 'text'), ('Included', 10, 'text'), ('Supplier', 30, 'text'),
                                      ('Cost', 16, 'money'), ('Charge', 16, 'money'), ('Margin £', 16, 'money'),
                                      ('Margin %', 11, 'pct')])
    s.title('Price summary by trade', 'Margin is profit / charge.')
    s.header()
    for row in ctx['prices']:
        r = s.row
        s.add([row['name'], 'Yes' if row['include'] else 'No', row['supplier'], row['cost'], row['charge'],
               Formula(f'=E{r}-D{r}'), Formula(f'=IF(E{r}=0,"",F{r}/E{r})')])
    tr = s.total(1, {4, 5, 6})
    s.ws.cell(tr, 7, f'=IF(E{tr}=0,"",F{tr}/E{tr})').number_format = PCT
    s.finish()

    # 4 Scope allowed
    s = Sheet(book, 'Scope allowed', [('Section', 22, 'text'), ('Ref', 8, 'text'), ('Description', 60, 'text'),
                                      ('Qty', 9, 'qty'), ('Unit', 8, 'text'), ('Status', 16, 'text'),
                                      ('Cost', 14, 'money'), ('Charge', 14, 'money'), ('Comment', 30, 'text'),
                                      ('Source', 20, 'text')])
    s.title('Scope allowed', 'Every line carried in the tender, with its status.')
    s.header()
    for row in ctx['scope']:
        s.add([row['section'], row['ref'], row['description'], row['qty'] if row['qty'] is not None
               else row['qty_text'] or None, row['unit'], row['label'], row['cost'], row['charge'], row['comment'],
               row['source']])
    s.total(3, {7, 8})
    s.finish()

    # 5 Not allowed
    s = Sheet(book, 'Not allowed', [('Section', 24, 'text'), ('Ref', 8, 'text'), ('Description', 60, 'text'),
                                    ('Why', 40, 'text'), ('Value if included', 16, 'money'), ('Source', 20, 'text')])
    s.title('Not allowed', 'Excluded trades, work by others and preliminaries marked No.')
    s.header()
    for row in ctx['excluded'] + ctx['prelims_no']:
        s.add([row['section'], row['ref'], row['description'], row['why'], row.get('cost'), row['source']])
    s.finish()

    # 6 Clarifications
    s = Sheet(book, 'Clarifications', [('Ref', 8, 'text'), ('Trade / section', 20, 'text'),
                                       ('Clarification / assumption', 60, 'text'), ('Response', 40, 'text'),
                                       ('Status', 10, 'text'), ('Cost effect', 18, 'text'), ('Owner', 18, 'text'),
                                       ('Source', 14, 'text')])
    s.title('Clarifications')
    s.header()
    for c in ctx['clarifications']:
        s.add([c.ref, c.trade, c.text, c.response, c.get_status_display(), c.cost_effect, c.owner_text,
               c.get_source_display()])
    s.finish()

    # 7 Drawings
    s = Sheet(book, 'Drawings', [('Drawing no.', 30, 'text'), ('Title', 50, 'text'), ('Current revision', 14, 'text'),
                                 ('In the register', 14, 'text'), ('Mentioned in workbook', 40, 'text')])
    s.title('Drawings', 'The project\'s drawing register, and drawing numbers the workbook mentions.')
    s.header()
    for d in ctx['register']:
        s.add([d['doc'].reference, d['doc'].title, d['current'].revision if d['current'] else '', 'Yes', ''])
    for d in ctx['drawings_missing']:
        s.add([d['ref'], '', '', 'NO: not in the register', ', '.join(d['where'][:6])])
    s.finish()

    # 8 Suppliers
    s = Sheet(book, 'Suppliers', [('Trade', 24, 'text'), ('Selected supplier', 28, 'text'), ('Quote', 16, 'money'),
                                  ('Lines priced', 12, 'text'), ('Other bidders', 50, 'text'),
                                  ('Lowest other bid', 16, 'money'), ('Own allowance', 12, 'text')])
    s.title('Suppliers', 'The supplier carried per trade and what the other bidders came in at.')
    s.header()
    for row in ctx['suppliers']:
        others = '; '.join(f'{o["name"]}: {o["total"]:,.2f} ({o["lines"]} lines)' if o['total'] is not None
                           else f'{o["name"]}: no prices' for o in row['others'])
        low = min((o['total'] for o in row['others'] if o['total'] is not None), default=None)
        s.add([row['trade'], row['supplier'] or 'None selected', row['quote'],
               f'{row["lines"]} of {row["line_count"]}', others, low, 'Yes' if row['own_allowance'] else ''])
    s.finish()

    # 9 Site rules
    s = Sheet(book, 'Site rules', [('Site rules', 120, 'text')])
    s.title('Site rules')
    s.header()
    for para in (ctx['site_rules'] or 'None recorded.').splitlines() or ['']:
        s.add([para])
    s.finish()

    # 10 Buy-out targets
    s = Sheet(book, 'Buy-out targets', [('Trade', 24, 'text'), ('Supplier', 26, 'text'), ('Allowed cost', 15, 'money'),
                                        ('Charge', 15, 'money'), ('Margin £', 15, 'money'), ('Margin %', 10, 'pct'),
                                        ('Next cheapest bid', 15, 'money'), ('Next bidder', 22, 'text'),
                                        ('Headroom', 15, 'money'), ('Own allowances (no quote)', 16, 'money')])
    s.title('Buy-out targets', 'Allowed cost is the budget to buy the trade for. Headroom: allowed cost less the next '
                               'cheapest bid (positive means a cheaper price is on the table).')
    s.header()
    for row in ctx['buyout']:
        r = s.row
        nxt = row['next']
        s.add([row['trade'], row['supplier'] or 'None selected', row['cost'], row['charge'], Formula(f'=D{r}-C{r}'),
               Formula(f'=IF(D{r}=0,"",E{r}/D{r})'), nxt['total'] if nxt else None, nxt['name'] if nxt else '',
               Formula(f'=IF(G{r}="","",C{r}-G{r})'), row['allowance_total'] or None])
    tr = s.total(1, {3, 4, 5, 10})
    s.ws.cell(tr, 6, f'=IF(D{tr}=0,"",E{tr}/D{tr})').number_format = PCT
    s.finish()

    # 11 Open items
    s = Sheet(book, 'Open items', [('Item', 50, 'text'), ('Detail', 50, 'text'), ('Trade', 18, 'text'),
                                   ('Where', 22, 'text'), ('Money', 15, 'money'), ('Owner', 20, 'text'),
                                   ('Due', 12, 'text'), ('Status', 10, 'text')])
    s.title('Open items', 'Ranked by money.')
    s.header()
    for i in ctx['items']:
        s.add([i.title, i.detail, i.trade, i.source, i.amount, i.owner.full_name if i.owner else '',
               f'{i.due_date:%d %b %Y}' if i.due_date else '', i.get_status_display()])
    s.finish()

    book.properties.title = f'Handover pack {project.reference} (INTERNAL ONLY)'
    book.properties.creator = 'Siteline'
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
