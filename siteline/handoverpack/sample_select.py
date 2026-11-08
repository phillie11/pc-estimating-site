"""
Builds a pricing document in the supplier-select layout, from a short description, for tests.

    data = sample_select.build(sample_select.DEMO)               # .xlsx bytes with the layout's formulas
    data = sample_select.build(spec, cached=False)               # as a tool that doesn't calculate saves it
    xlsm = sample_select.with_macros(data)                       # the same workbook as Excel saves an .xlsm

The sheets, rows and formulas are the layout's own, cell for cell: PRICING SUMMARY with no Include?
column, trade tabs whose W2 names the supplier and whose Y and Z are Excel's FILTER of that supplier's
columns, numbered joinery workshop sheets linked from JOINERY SUMMARY, the Prelim Book summed by the
Prelim_Cost name, numbered clarifications. openpyxl can't calculate (and nothing here can run FILTER),
so the values Excel would save are worked out here, separately from the engine, and written into the
file next to each formula, as Excel does.

`break_formulas` clears a trade line's W, AA and AD formulas (a row someone has cleared);
`stale` changes some saved values; `typed` overtypes cells.

Every name and figure in DEMO is invented.
"""
import io
import re
import zipfile
from datetime import date
from decimal import Decimal as D

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.formula import ArrayFormula

from .sample import inject_cached

SUMMARY, PRELIMS, JOINERY, CLAR = 'PRICING SUMMARY', 'Prelim Book', 'JOINERY SUMMARY', 'Clarifications'
SLOTS = [('F', 'G', 'H', 'I'), ('J', 'K', 'L', 'M'), ('N', 'O', 'P', 'Q'), ('R', 'S', 'T', 'U')]
FILTER_Y = '=IFERROR(_xlfn._xlws.FILTER(F3:R{t}, (F2:R2=$W$2) * ISNUMBER(MATCH(COLUMN(F2:R2), {{6,10,14,18}}, 0))), "")'
FILTER_Z = '=IFERROR(_xlfn._xlws.FILTER(G3:S{t}, (F2:R2=$W$2) * ISNUMBER(MATCH(COLUMN(F2:R2), {{6,10,14,18}}, 0))), "")'
AE2 = "=IFERROR(INDEX('PRICING SUMMARY'!$F$7:$F$45,MATCH($B$2,'PRICING SUMMARY'!$D$7:$D$45,0)),0)"
PROJECT = ('=_xlfn.LET(_xlpm.full,CELL("filename",A1),_xlpm.name,MID(_xlpm.full,FIND("[",_xlpm.full)+1,'
           'FIND("]",_xlpm.full)-FIND("[",_xlpm.full)-1),_xlpm.noext,_xlfn.TEXTBEFORE(_xlpm.name,".",-1),'
           '_xlfn.TEXTAFTER(_xlpm.noext," ",5))')
WORKSHOP_PARTS = ['labour', 'materials', 'ancillary', 'finish', 'glass', 'fabric']
WORKSHOP_FORMULAS = {  # the workshop sheet's own build-up formulas (rows 2 to 7, 13, 14), cost R and charge T
    2: ('=SUM(W2*H2)+SUM(H3*W2)+SUM(H4*W4)+SUM(H5*W2)+SUM(H6*W2)', '=SUM(Y2*H2)+SUM(H3*Y2)+SUM(H4*Y4)+SUM(H5*Y2)'
        '+SUM(H6*Y2)'),
    3: ('=(D37)', '=R3+(R3*W6)'), 4: ('=O25', '=R4+(R4*W6)'), 5: ('=I22', '=R5+(R5*W7)'),
    6: ('=(O27)+(O26)', '=R6+(R6*W6)'), 7: (None, None),
    13: ('=SUM(H8*X23)+(G8*Y23)', '=R13+(R13*W7)'), 14: ('=H9*Z24', '=H9*Z25'),
}


def _n(v):
    return v if isinstance(v, D) else None


def _xl(v):
    return float(v) if isinstance(v, D) else v


def line(ref, desc, qty, unit, *slots):
    """A trade line: slots are (rate, est adj, comment) per supplier, in order."""
    return {'ref': ref, 'desc': desc, 'qty': None if qty is None else D(str(qty)), 'unit': unit,
            'slots': [(None if r is None else D(str(r)), None if a is None else D(str(a)), c) for r, a, c in slots]}


def build(spec, cached=True, stale=None, typed=None, break_formulas=None):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Front Page'
    ws['A1'] = 'Internal pricing document'
    sm = wb.create_sheet(SUMMARY)
    values = {}

    def put(sheet, coord, formula, value):
        wb[sheet][coord] = formula
        values.setdefault(sheet, {})[coord] = value

    h = spec.get('header', {})
    dm = spec.get('default_markup', D('0.1'))
    jm, lm = spec.get('joinery_material_markup', D('0.25')), spec.get('joinery_labour_markup', D('1.5'))
    im, pm = spec.get('install_markup', D('0.8')), spec.get('prelim_markup', D('0.85'))
    target, duration = spec.get('target', D('0.2')), spec.get('duration', 8)
    sm['B1'] = 'PRICING SUMMARY'
    sm['B2'], sm['B3'], sm['B4'] = 'PROJECT:', 'DATE:', 'REV:'
    if h.get('project_error'):
        put(SUMMARY, 'D2', PROJECT, h['project_error'])
    else:
        sm['D2'] = h.get('project', '')
    sm['D3'] = h.get('date', date(2026, 10, 31))
    sm['D4'] = h.get('revision', 0)
    sm['E3'], sm['F3'], sm['G3'] = 'Joinery Material Markup', 'SUBCONTRACTOR MARKUP', 'Joinery Manufacture Markup'
    sm['E4'], sm['F4'], sm['G4'] = _xl(jm), _xl(dm), _xl(lm)
    sm['B5'], sm['D5'], sm['E5'], sm['G5'], sm['I5'] = 'REF', 'DESCRIPTION', 'COST', 'CHARGE', 'NOTES'

    order = spec.get('order') or list(spec['trades'])
    trades = spec['trades']
    srow = {name: 7 + i for i, name in enumerate(order)}
    # Markup per summary row: typed, or =$F$4
    markups = {}
    for name in order:
        t = trades.get(name, {})
        r = srow[name]
        if t.get('markup') is not None:
            sm[f'F{r}'] = _xl(t['markup'])
            markups[name] = t['markup']
        else:
            put(SUMMARY, f'F{r}', '=$F$4', dm)
            markups[name] = dm
        if t.get('notes'):
            sm[f'I{r}'] = t['notes']
        sm[f'H{r}'] = bool(t.get('checked'))

    sum_e = sum_g = D(0)
    checked = D(0)
    for name in order:
        t = trades.get(name, {})
        tab = wb.create_sheet(name)
        lines = t.get('lines', [])
        tr = max(35, 4 + len(lines) + 1)  # total row: the template's 35, further down for a long trade
        names = (t.get('suppliers') or []) + [''] * 4
        selected = t.get('selected', '')
        slot = next((i for i, n in enumerate(names[:4]) if n and selected and n.casefold() == selected.casefold()),
                    None)
        markup = markups[name]
        tab['A1'] = 'Ref'
        tab['C1'] = 'SUBCONTRACTOR COMPARISON SHEET'
        tab['B2'], tab['C2'], tab['D2'], tab['E2'] = name, 'Item', 'Qty', 'Unit'
        for k, (rc, _, _, _) in enumerate(SLOTS):
            if names[k]:
                tab[f'{rc}2'] = names[k]
        if selected:
            tab['W2'] = selected
        tab['AC2'] = 'With Markup'
        put(name, 'AE2', AE2, markup)
        tab['A3'] = 'Trade:'
        for rc, ac, qc, cc in SLOTS:
            tab[f'{rc}3'], tab[f'{ac}3'], tab[f'{qc}3'], tab[f'{cc}3'] = 'Rate', 'Est Adj', 'Quotation', 'Comments'
        tab['W3'], tab['X3'], tab['AA3'], tab['AC3'], tab['AD3'] = 'Area', 'Unit', 'Document', 'Rate', 'TOTAL'
        tab['Y3'] = ArrayFormula(f'Y3:Y{tr}', FILTER_Y.format(t=tr))
        tab['Z3'] = ArrayFormula(f'Z3:Z{tr}', FILTER_Z.format(t=tr))
        values.setdefault(name, {}).update({'Y3': 'Rate', 'Z3': 'Est Adj'})
        slot_tot = [D(0)] * 4
        aa_tot = ad_tot = D(0)
        broken = set((break_formulas or {}).get(name, []))
        for r in range(4, tr):
            ln = lines[r - 4] if r - 4 < len(lines) else None
            qty = ln['qty'] if ln else None
            slots = ((ln['slots'] if ln else []) + [(None, None, '')] * 4)[:4]
            if ln:
                tab[f'B{r}'] = ln['ref']
                if ln['desc']:
                    tab[f'C{r}'] = ln['desc']
                if qty is not None:
                    tab[f'D{r}'] = _xl(qty)
                tab[f'E{r}'] = ln['unit']
                for k, (rc, ac, qc, cc) in enumerate(SLOTS):
                    rate, adj, comment = slots[k]
                    if rate is not None:
                        tab[f'{rc}{r}'] = _xl(rate)
                    if adj is not None:
                        tab[f'{ac}{r}'] = _xl(adj)
                    if comment:
                        tab[f'{cc}{r}'] = comment
            for k, (rc, ac, qc, cc) in enumerate(SLOTS):
                rate, adj, _ = slots[k]
                quote = '' if qty is None else qty * ((rate or 0) + (adj or 0))
                put(name, f'{qc}{r}', f'=IFERROR(IF(D{r}="","",D{r}*({rc}{r}+{ac}{r})),0)', quote)
                if quote != '':
                    slot_tot[k] += quote
            y = (slots[slot][0] or D(0)) if slot is not None else D(0)
            z = (slots[slot][1] or D(0)) if slot is not None else D(0)
            if cached:
                tab[f'Y{r}'], tab[f'Z{r}'] = _xl(y), _xl(z)
            ac = '' if (y + z) == 0 else (1 + markup) * (y + z)
            put(name, f'AC{r}', f'=IFERROR(IF((Y{r}+Z{r})=0,"",($AE$2+1)*(Y{r}+Z{r})),"")', ac)
            if r in broken:  # W, X, AA and AD cleared, as someone deleting a row's contents leaves it
                continue
            w = qty if qty is not None else ''
            put(name, f'W{r}', f'=IF(D{r}="","",D{r})', w)
            put(name, f'X{r}', f'=E{r}', (ln['unit'] if ln else '') or 0)
            aa = (y + z) * qty if qty is not None else ''
            ad = ac * qty if ac != '' and qty is not None else ''
            put(name, f'AA{r}', f'=IFERROR(SUM(Y{r}+Z{r})*W{r},"")', aa)
            put(name, f'AD{r}', f'=IFERROR(AC{r}*W{r},"")', ad)
            aa_tot += aa if aa != '' else 0
            ad_tot += ad if ad != '' else 0
        tab[f'C{tr}'] = 'TOTAL'
        for k, (rc, ac, qc, cc) in enumerate(SLOTS):
            put(name, f'{rc}{tr}', f'=SUM({qc}4:{qc}{tr - 1})', slot_tot[k])
            put(name, f'{rc}{tr + 1}', f'={rc}2', names[k] or 0)
        yt = slot_tot[slot] if slot is not None else D(0)
        if cached:
            tab[f'Y{tr}'], tab[f'Z{tr}'] = _xl(yt), 0
        put(name, f'AA{tr}', f'=SUM(AA4:AA{tr - 1})', aa_tot)
        put(name, f'AC{tr}', f'=IFERROR(IF((Y{tr}+Z{tr})=0,"",($AE$2+1)*(Y{tr}+Z{tr})),"")',
            '' if yt == 0 else (1 + markup) * yt)
        put(name, f'AD{tr}', f'=SUM(AD4:AD{tr - 1})', ad_tot)
        tab[f'C{tr + 3}'] = 'QUOTE REGISTER & QUALIFICATIONS'
        r = srow[name]
        put(SUMMARY, f'B{r}', f'={_q(name)}!W2', selected or 0)
        put(SUMMARY, f'D{r}', f'={_q(name)}!B2', name)
        put(SUMMARY, f'E{r}', f'={_q(name)}!AA{tr}', aa_tot)
        put(SUMMARY, f'G{r}', f'={_q(name)}!AD{tr}', ad_tot)
        sum_e += aa_tot
        sum_g += ad_tot
        if t.get('checked'):
            checked += ad_tot

    # Joinery: numbered workshop sheets, JOINERY SUMMARY linking R11/T11 (supply) and R14/T14 (install)
    jw = wb.create_sheet('TEMPLATE')
    jw['A1'] = 'WORK'
    js = wb.create_sheet(JOINERY, index=wb.sheetnames.index(order[0]))
    js['B1'] = 'JOINERY SUMMARY'
    js['B2'], js['B3'], js['B4'] = 'PROJECT:', 'DATE:', 'REV:'
    js['F5'], js['H5'] = 'SUPPLY', 'INSTALL'
    for col_, head in zip('BCDEFGHIJK', ('REF', 'DESCRIPTION', 'QTY', 'UNIT', 'COST', 'CHARGE', 'COST', 'CHARGE',
                                          'RATE TO TENDER', 'TOTAL')):
        js[f'{col_}6'] = head
    jrow = 9
    jt = {'F': D(0), 'G': D(0), 'H': D(0), 'I': D(0), 'K': D(0)}
    for i, shop in enumerate(spec.get('workshops', []), start=1):
        n = str(i)
        sh = wb.create_sheet(n, index=wb.sheetnames.index(JOINERY))
        sh['A1'] = 'WORK'
        put(n, 'B1', f"='{JOINERY}'!C{jrow}", shop['desc'])
        sh['Q2'], sh['Q3'], sh['Q4'], sh['Q11'], sh['Q13'], sh['Q14'] = ('Labour Cost', 'Material Cost',
                                                                       'Ancillary Cost', 'SUB TOTAL', 'DELIVERY',
                                                                       'INSTALLATION')
        sh['W6'] = 0.2
        parts = shop.get('parts', {})
        supply = [D(0), D(0)]
        for row, key in enumerate(WORKSHOP_PARTS, start=2):
            cost, charge = parts.get(key, (D(0), D(0)))
            fr, ft = WORKSHOP_FORMULAS[row]
            if fr:
                put(n, f'R{row}', fr, cost)
                put(n, f'T{row}', ft, charge)
            supply[0] += cost
            supply[1] += charge
        dl, ins = shop.get('delivery', (D(0), D(0))), shop.get('install', (D(0), D(0)))
        for row, (cost, charge) in ((13, dl), (14, ins)):
            put(n, f'R{row}', WORKSHOP_FORMULAS[row][0], cost)
            put(n, f'T{row}', WORKSHOP_FORMULAS[row][1], charge)
        put(n, 'R11', '=SUM(R2:R7)', supply[0])
        put(n, 'T11', '=SUM(T2:T7)', supply[1])
        put(n, 'R16', '=SUM(R11,R13,R14)', supply[0] + dl[0] + ins[0])
        put(n, 'T16', '=SUM(T11,T13,T14)', supply[1] + dl[1] + ins[1])
        links = shop.get('links', {'F': 'R11', 'G': 'T11', 'H': 'R14', 'I': 'T14'})
        got = {'F': supply[0], 'G': supply[1], 'H': ins[0], 'I': ins[1], 'R16': None}
        cellv = {'R11': supply[0], 'T11': supply[1], 'R14': ins[0], 'T14': ins[1], 'R13': dl[0], 'T13': dl[1],
                 'R16': supply[0] + dl[0] + ins[0], 'T16': supply[1] + dl[1] + ins[1]}
        js[f'B{jrow}'], js[f'C{jrow}'], js[f'D{jrow}'], js[f'E{jrow}'] = i, shop['desc'], _xl(shop['qty']), shop['unit']
        for col_ in 'FGHI':
            ref = links.get(col_)
            got[col_] = cellv[ref] if ref else D(0)
            if ref:
                put(JOINERY, f'{col_}{jrow}', f"='{n}'!${ref[0]}${ref[1:]}", got[col_])
            else:
                js[f'{col_}{jrow}'] = 0
        _joinery_rate(put, jrow, got, shop['qty'], jt)
        jrow += 1
    for extra in spec.get('joinery_extra', []):
        js[f'B{jrow}'], js[f'C{jrow}'], js[f'D{jrow}'], js[f'E{jrow}'] = extra['ref'], extra['desc'], \
            _xl(extra['qty']), extra['unit']
        got = dict(zip('FGHI', (*extra['supply'], *extra['install'])))
        for col_ in 'FGHI':
            js[f'{col_}{jrow}'] = _xl(got[col_])
        _joinery_rate(put, jrow, got, extra['qty'], jt)
        jrow += 1
    js[f'C{jrow}'] = 'Last Row (DO NOT DELETE)'
    jtot = jrow + 1
    js[f'C{jtot}'] = 'Total'
    for col_ in 'FGHIK':
        put(JOINERY, f'{col_}{jtot}', f'=SUM({col_}9:{col_}{jrow})', jt[col_])
    js[f'J{jtot + 2}'] = 'CHECK MATCH'
    put(JOINERY, f'K{jtot + 2}', f'=SUM(G{jtot}+I{jtot})', jt['G'] + jt['I'])

    # The summary below the trades
    last = 7 + len(order) - 1
    rj, ri, rp = last + 2, last + 3, last + 4
    rt = rp + 2
    sm[f'B{rj}'], sm[f'D{rj}'] = 'OWN', 'Joinery'
    put(SUMMARY, f'E{rj}', f"='{JOINERY}'!F{jtot}", jt['F'])
    put(SUMMARY, f'F{rj}', f'=IFERROR((((G{rj}-E{rj}))/(E{rj}/100)/100),0)',
        (jt['G'] - jt['F']) / jt['F'] if jt['F'] else 0)
    put(SUMMARY, f'G{rj}', f"='{JOINERY}'!G{jtot}", jt['G'])
    sm[f'B{ri}'], sm[f'D{ri}'], sm[f'F{ri}'] = 'OWN', 'Installation', _xl(im)
    put(SUMMARY, f'E{ri}', f"='{JOINERY}'!H{jtot}", jt['H'])
    put(SUMMARY, f'G{ri}', f"='{JOINERY}'!I{jtot}", jt['I'])
    sm[f'B{rp}'], sm[f'D{rp}'], sm[f'F{rp}'] = duration, 'Prelims (see prelim book)', _xl(pm)
    for r in range(7, rt):
        if sm[f'H{r}'].value is None and r not in (rj - 1,):
            sm[f'H{r}'] = False

    # Prelim Book: line cost in K (plain arithmetic, a function now and then), charge P, summed by name
    pb = wb.create_sheet(PRELIMS, index=2)
    pb['A3'], pb['A4'], pb['D4'] = 'Contract:', 'Duration ', 'weeks'
    put(PRELIMS, 'C4', f"='{SUMMARY}'!B{rp}", duration)
    pb['L6'], pb['N6'] = 'Sub total', 'With Markup'
    r = 10
    p_cost = p_charge = D(0)
    for section, items in spec.get('prelims', []):
        pb[f'B{r}'] = section
        r += 1
        first = r
        sec_cost = sec_charge = D(0)
        for item in items:
            desc, qty, unit, rate = item['desc'], item.get('qty'), item['unit'], item.get('rate')
            pb[f'C{r}'], pb[f'G{r}'], pb[f'H{r}'] = desc, unit, 'x'
            if item.get('allocation') is not None:
                pb[f'D{r}'] = _xl(item['allocation'])
            if item.get('weekly'):
                put(PRELIMS, f'F{r}', '=C4', D(duration))
                qty = D(duration)
            elif qty is not None:
                pb[f'F{r}'] = _xl(qty)
            if item.get('rate_formula'):
                put(PRELIMS, f'I{r}', item['rate_formula'], rate)
            elif rate is not None:
                pb[f'I{r}'] = _xl(rate)
            kind = item.get('formula', 'IF')
            if kind == 'IFD':  # =I*F*D, allocation as a share of time
                cost = (rate or 0) * (qty or 0) * (item.get('allocation') or 0)
                formula = f'=I{r}*F{r}*D{r}'
            elif kind == 'ROUND':  # a function: Excel's value is used
                cost = D(round((rate or 0) * (qty or 0)))
                formula = f'=ROUND(I{r}*F{r},0)'
            else:
                cost = (rate or 0) * (qty or 0)
                formula = f'=I{r}*F{r}'
            cost = D(cost)
            put(PRELIMS, f'K{r}', formula, cost)
            charge = cost + cost * pm
            put(PRELIMS, f'P{r}', f"=IF(K{r}=\"\",\"\",K{r}+K{r}*'{SUMMARY}'!$F${rp})", charge)
            sec_cost += cost
            sec_charge += charge
            r += 1
        put(PRELIMS, f'L{r}', f'=SUM(K{first}:K{r - 1})', sec_cost)
        put(PRELIMS, f'Q{r}', f'=SUM(P{first}:P{r - 1})', sec_charge)
        p_cost += sec_cost
        p_charge += sec_charge
        r += 2
    pb[f'H{r}'] = 'Cost'
    put(PRELIMS, f'L{r}', f'=SUM(L5:L{r - 1})', p_cost)
    put(PRELIMS, f'Q{r}', f'=SUM(Q5:Q{r - 1})', p_charge)
    ptot = r + 3
    put(PRELIMS, f'J{ptot}', '=SUM(Prelim_Cost)', p_cost)
    pb[f'R{ptot}'] = 'To tender'
    put(PRELIMS, f'T{ptot}', '=SUM(Prelim_Charge)', p_charge)
    wb.defined_names['Prelim_Cost'] = DefinedName('Prelim_Cost', attr_text=f"'{PRELIMS}'!$K:$K")
    wb.defined_names['Prelim_Charge'] = DefinedName('Prelim_Charge', attr_text=f"'{PRELIMS}'!$P:$P")
    put(SUMMARY, f'E{rp}', f"='{PRELIMS}'!J{ptot}", p_cost)
    pg = p_cost + p_cost * pm
    put(SUMMARY, f'G{rp}', f'=E{rp}+(E{rp}*F{rp})', pg)

    # Totals, profit, margin against the sheet's target, the client document check
    cost = sum_e + jt['F'] + jt['H'] + p_cost
    charge = sum_g + jt['G'] + jt['I'] + pg
    for extra_row, (e, g) in (spec.get('extra_rows') or {}).items():
        sm[f'D{extra_row}'] = 'Adjustment'
        sm[f'E{extra_row}'], sm[f'G{extra_row}'] = _xl(e), _xl(g)
        cost, charge = cost + e, charge + g
    sm[f'D{rt}'] = 'Total'
    put(SUMMARY, f'E{rt}', f'=SUM(E7:E{rt - 1})', cost)
    put(SUMMARY, f'G{rt}', f'=SUM(G7:G{rt - 1})', charge)
    sm[f'D{rt + 1}'] = 'Profit'
    put(SUMMARY, f'F{rt + 1}', f'=G{rt}-E{rt}', charge - cost)
    sm[f'D{rt + 2}'] = 'Percentage'
    pct = (charge - cost) / charge if charge else D(0)
    put(SUMMARY, f'F{rt + 2}', f'=IFERROR((((G{rt}-E{rt}))/(G{rt}/100)/100),0)', pct)
    sm[f'G{rt + 2}'] = _xl(target)
    put(SUMMARY, f'I{rt + 2}', f'=F{rt + 2}-G{rt + 2}', pct - target)
    rc = rt + 4
    sm[f'F{rc}'], sm[f'F{rc + 1}'], sm[f'F{rc + 2}'] = 'Checked Total', 'Client Document Total', 'Difference'
    put(SUMMARY, f'G{rc}', f'=SUMIF(H5:H{rt - 1},TRUE,G5:G{rt - 1})', checked)
    client = spec.get('client_total')
    if isinstance(client, str):
        put(SUMMARY, f'G{rc + 1}', '=1/0', client)
    elif client is not None:
        sm[f'G{rc + 1}'] = _xl(client)
    sm[f'I{rc}'] = 'Form of Tender words'
    if spec.get('words_error'):
        put(SUMMARY, f'I{rc + 1}', '=_xlfn.WORDS(G' + str(rc + 1) + ')', spec['words_error'])
    put(SUMMARY, f'G{rc + 2}', f'=G{rc + 1}-G{rc}', ((client if isinstance(client, D) else D(0)) - checked)
        if not isinstance(client, str) else client)

    # Clarifications: a number in A, the heading in B, the text below it
    cl = wb.create_sheet(CLAR, index=3)
    cl['B2'] = 'Contractor\'s Clarifications'
    put(CLAR, 'B3', f'=CONCATENATE("Project:"," ", \'{SUMMARY}\'!D2)', 'Project: ' + h.get('project', ''))
    r = 8
    for i, (title, body) in enumerate(spec.get('clarifications', []), start=1):
        cl[f'A{r}'], cl[f'B{r}'], cl[f'B{r + 1}'] = i, title, body
        r += 3

    for (sheet, coord), v in (typed or {}).items():
        wb[sheet][coord] = _xl(v)
        values.setdefault(sheet, {}).pop(coord, None)
    if spec.get('note', True):  # a cell comment, so the sheet has a legacy VML drawing as Excel writes it
        wb.worksheets[4]['A2'].comment = Comment('Checked against the quotes', 'Estimator')
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if cached:
        for (sheet, coord), v in (stale or {}).items():
            values[sheet][coord] = v
        data = inject_cached(data, values)
    return data


def _q(name):
    return f"'{name}'" if re.search(r'[^A-Za-z0-9_]', name) else name


def _joinery_rate(put, row, got, qty, jt):
    qty = D(qty)
    rate = (got['I'] + got['G']) / qty if qty else ''
    total = rate * qty if rate != '' else ''
    put(JOINERY, f'J{row}', f'=IFERROR(SUM(I{row}+G{row})/D{row},"")', rate)
    put(JOINERY, f'K{row}', f'=IFERROR(J{row}*D{row},"")', total)
    for col_ in 'FGHI':
        jt[col_] += got[col_]
    jt['K'] += total if total != '' else 0


# --- an .xlsm, as Excel saves one --------------------------------------------------------------------

VBA_BIN = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 504 + b'Attribute VB_Name = "Module1"\r\nSub Go()\r\nEnd Sub'
ACTIVEX_XML = (b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<ax:ocx ax:classid="{D7053240-CE69-11CD-A777-'
               b'00DD01143C57}" ax:persistence="persistStreamInit" r:id="rId1" xmlns:ax="http://schemas.microsoft.com/'
               b'office/2006/activeX" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>')
ACTIVEX_RELS = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
                b'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.microsoft'
                b'.com/office/2006/relationships/activeXControlBinary" Target="activeX1.bin"/></Relationships>')
CTRLPROP = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<formControlPr xmlns="http://schemas.microsoft'
            b'.com/office/spreadsheetml/2009/9/main" objectType="CheckBox" fmlaLink="$A$31" lockText="1" noThreeD="1"/>')
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
# A form-control check box as Excel draws it in the sheet's legacy VML, with the macro it runs
VML_CHECKBOX = (b'<v:shape xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
                b'xmlns:x="urn:schemas-microsoft-com:office:excel" id="_x0000_s2049" type="#_x0000_t201" '
                b'style="position:absolute;margin-left:300pt;'
                b'margin-top:20pt;width:120pt;height:14pt;z-index:5" o:button="t" filled="f" stroked="f">'
                b'<v:textbox><div style="text-align:left"><font face="Segoe UI" size="160">Include trade</font></div>'
                b'</v:textbox><x:ClientData ObjectType="Checkbox"><x:Anchor>9, 25, 48, 3, 9, 197, 49, 2</x:Anchor>'
                b'<x:FmlaMacro>[0]!ToggleTrade</x:FmlaMacro><x:FmlaLink>$A$31</x:FmlaLink><x:NoThreeD/>'
                b'</x:ClientData></v:shape>')
CONTROLS = ('<controls><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            '<mc:Choice Requires="x14"><control shapeId="1025" r:id="rIdAx1" name="CommandButton1" xmlns:r="' + REL +
            '"><controlPr defaultSize="0" print="0" autoFill="0" autoLine="0"/></control></mc:Choice><mc:Fallback>'
            '<control shapeId="1025" r:id="rIdAx1" name="CommandButton1" xmlns:r="' + REL + '"/></mc:Fallback>'
            '</mc:AlternateContent></controls>').encode()


def with_macros(data, sheet_index=5):
    """
    The workbook as Excel saves it macro-enabled: a VBA project (with a signature), the workbook's macro-enabled
    content type, an ActiveX command button and a form-control check box on one sheet, and the .bin default
    content type pointing at the VBA project, as Excel writes it. The cells are untouched.
    """
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    sheet = f'xl/worksheets/sheet{sheet_index}.xml'
    sheet_rels = f'xl/worksheets/_rels/sheet{sheet_index}.xml.rels'
    names = [i.filename for i in zin.infolist()]
    vml = None
    if sheet_rels in names:  # the sheet's legacy VML drawing (its comment boxes): the check box goes in it
        m = re.search(rb'Target="/?(?:\.\./)?([^"]+\.vml)"', zin.read(sheet_rels))
        if m:
            vml = 'xl/' + m.group(1).decode().removeprefix('xl/')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for info in zin.infolist():
            raw = zin.read(info)
            if info.filename == vml:
                raw = raw.replace(b'</xml>', VML_CHECKBOX + b'</xml>')
            if info.filename == '[Content_Types].xml':
                raw = raw.replace(b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                                  b'application/vnd.ms-excel.sheet.macroEnabled.main+xml')
                raw = raw.replace(b'</Types>', (
                    b'<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
                    b'<Override PartName="/xl/vbaProjectSignature.bin" ContentType="application/vnd.ms-office.'
                    b'vbaProjectSignature"/>'
                    b'<Override PartName="/xl/activeX/activeX1.xml" ContentType="application/vnd.ms-office.activeX+xml"/>'
                    b'<Override PartName="/xl/activeX/activeX1.bin" ContentType="application/vnd.ms-office.activeX"/>'
                    b'<Override PartName="/xl/ctrlProps/ctrlProp1.xml" ContentType="application/vnd.ms-excel.'
                    b'controlproperties+xml"/></Types>'))
            elif info.filename == 'xl/_rels/workbook.xml.rels':
                raw = raw.replace(b'</Relationships>', b'<Relationship Id="rIdVba" Type="http://schemas.microsoft.com/'
                                  b'office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>')
            elif info.filename == sheet:
                raw = raw.replace(b'</worksheet>', CONTROLS + b'</worksheet>')
            elif info.filename == sheet_rels:
                raw = raw.replace(b'</Relationships>', _sheet_rels_extra() + b'</Relationships>')
            z.writestr(info, raw)
        if sheet_rels not in names:
            z.writestr(sheet_rels, b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="'
                       b'http://schemas.openxmlformats.org/package/2006/relationships">' + _sheet_rels_extra()
                       + b'</Relationships>')
        z.writestr('xl/vbaProject.bin', VBA_BIN)
        z.writestr('xl/vbaProjectSignature.bin', b'\x01\x00\x00\x00' + b'\x00' * 60)
        z.writestr('xl/_rels/vbaProject.bin.rels', b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   b'Id="rId1" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProjectSignature" '
                   b'Target="vbaProjectSignature.bin"/></Relationships>')
        z.writestr('xl/activeX/activeX1.xml', ACTIVEX_XML)
        z.writestr('xl/activeX/activeX1.bin', b'\x40\x32\x05\xd7' + b'\x00' * 112)
        z.writestr('xl/activeX/_rels/activeX1.xml.rels', ACTIVEX_RELS)
        z.writestr('xl/ctrlProps/ctrlProp1.xml', CTRLPROP)
    return out.getvalue()


def _sheet_rels_extra():
    return (b'<Relationship Id="rIdAx1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            b'control" Target="../activeX/activeX1.xml"/><Relationship Id="rIdCp1" Type="http://schemas.openxmlformats'
            b'.org/officeDocument/2006/relationships/ctrlProp" Target="../ctrlProps/ctrlProp1.xml"/>')


def _pl(desc, unit, rate=None, qty=None, **kw):
    return {'desc': desc, 'unit': unit, 'rate': None if rate is None else D(str(rate)),
            'qty': None if qty is None else D(str(qty)), **kw}


# An invented clinic fit-out priced in this layout: quotes on most trades, own allowances, a client
# provisional sum at face value, joinery on workshop sheets, a prelim book with a large markup.
DEMO = {
    'header': {'project': 'Unit 4 Abbey Court, dental clinic fit-out', 'date': date(2026, 9, 18), 'revision': 2},
    'default_markup': D('0.1'), 'prelim_markup': D('0.85'), 'target': D('0.12'), 'duration': 6,
    'order': ['Builderswork', 'Cleaning', 'Decoration', 'Electrical', 'Flooring', 'Mechanical', 'Plumbing',
              'Sprinklers', 'Tiling', 'Wood Flooring'],
    'trades': {
        'Builderswork': {'suppliers': ['Harrow Build Ltd'], 'selected': 'Harrow Build Ltd', 'lines': [
            line('A1', 'Form new door opening in blockwork to treatment room', 1, 'Item', (1340.5, None, 'Quote item 3')),
            line('A2', 'Lintel and making good to new opening', 1, 'Item', (420, None, '')),
            line('A3', 'Weekend working premium for coring', 1, 'Item', (None, 900, 'Own allowance: not quoted')),
            line('B1', 'Steel goalpost frame to carry the X-ray arm', 1, 'Item', (2860.25, None, '')),
            line('B2', 'Fixings and packers to steelwork', 1, 'Item', (None, None, 'Included in rates')),
        ]},
        'Cleaning': {'suppliers': ['Own Allowance'], 'selected': 'Own Allowance', 'lines': [
            line('C1', 'Clean on completion', 1, 'Item', (580, None, 'Own allowance')),
            line('C2', 'Deep clean of clinical rooms before handover', 1, 'Item', (760, None, 'Own allowance')),
        ]},
        'Decoration': {'suppliers': ['Brushwork Decorators'], 'selected': 'Brushwork Decorators', 'lines': [
            line('D1', 'Mist and two coats to new walls', 310, 'm2', (4.6, None, '')),
            line('D2', 'Anti-bacterial paint to treatment rooms', 64, 'm2', (None, 7.25, 'Own allowance: not quoted')),
        ]},
        'Electrical': {'suppliers': ['Brightline Electrical', 'Volt & Co', 'Ampwell Services'],
                       'selected': 'Brightline Electrical', 'lines': [
            line('E1', 'Strip out redundant small power', 1, 'Item', (1180, None, ''), (1260, None, ''),
                 (1095, None, '')),
            line('E2', 'LED panels to treatment rooms and corridor', 96, 'nr', (88.5, None, ''), (92, None, ''),
                 (85.75, None, '')),
            line('E3', 'Power and data to dental chairs, client nominated units', 1, 'Item',
                 (None, 9600, 'Own Est Adj'), (9350, None, ''), (None, None, '')),
            line('E4', 'Fire alarm extension and certification', 1, 'Item', (2340, None, ''), (2510, None, ''),
                 (2420, None, '')),
        ]},
        'Flooring': {'suppliers': ['Floorcraft Contracts'], 'selected': 'Floorcraft Contracts', 'lines': [
            line('F1', 'Welded safety vinyl with coved skirting to clinical rooms', 14, 'm2',
                 (26, 12, 'Est Adj for coving')),
            line('F2', 'Carpet tiles to waiting area', 32, 'm2', (38.4, None, '')),
        ]},
        'Mechanical': {'suppliers': ['Coolflow Air', 'Northfield HVAC'], 'selected': 'Coolflow Air', 'lines': [
            line('M1', 'Comfort cooling cassettes to treatment rooms', 1, 'Item', (17450, None, ''),
                 (18980, None, '')),
            line('M2', 'Extract ventilation to decontamination room', 1, 'Item', (3850, None, ''), (3640, None, '')),
            line('M3', 'Condensate pumps', 1, 'Item', (None, 650, 'Own allowance'), (None, None, '')),
        ]},
        'Plumbing': {'suppliers': ['Coldwater Plumbing', 'Pipewise Ltd'], 'selected': 'Coldwater Plumbing', 'lines': [
            line('P1', 'Clinical hand wash basins, supply and connect', 1, 'Item', (2875.4, None, ''),
                 (3360, None, '')),
            line('P2', 'Drain points to dental chairs', 1, 'Item', (412.6, None, ''), (395, None, '')),
        ]},
        'Sprinklers': {'suppliers': ['Client PS'], 'selected': 'Client PS', 'markup': D('0'),
                       'notes': 'Provisional sum from the client, no markup', 'lines': [
            line('S1', 'Alterations to sprinkler heads - Provisional Sum', 1, 'Item', (12000, None, 'Client PS')),
        ]},
        'Tiling': {'suppliers': ['Mosaic Tiling Co', 'Harrow Build Ltd'], 'selected': 'Mosaic Tiling Co', 'lines': [
            line('T1', 'Porcelain floor tiles to reception', 150, 'm2', (64.2, None, ''), (69, None, '')),
            line('T2', 'Wall tiles to WC', 18, 'm2', (52, None, ''), (55, None, '')),
        ]},
        'Wood Flooring': {},
    },
    'workshops': [
        {'desc': 'Reception desk with solid surface top', 'qty': D('1'), 'unit': 'item',
         'parts': {'labour': (D('18'), D('54')), 'materials': (D('1180'), D('1416'))},
         'install': (D('500'), D('1000'))},
        {'desc': 'Display plinths, painted MDF', 'qty': D('1'), 'unit': 'nr',
         'parts': {'labour': (D('120'), D('360')), 'materials': (D('640.5'), D('768.6')),
                   'ancillary': (D('85'), D('102'))}, 'install': (D('250'), D('500'))},
        {'desc': 'Stock room shelving with glass shelves', 'qty': D('4'), 'unit': 'nr',
         'parts': {'labour': (D('60'), D('180')), 'materials': (D('920'), D('1104')), 'glass': (D('260'), D('312'))},
         'install': (D('375'), D('750'))},
    ],
    'joinery_extra': [
        {'ref': 4, 'desc': 'Window blinds to treatment rooms - PROVISIONAL SUM 400 each', 'qty': D('4'), 'unit': 'nr',
         'supply': (D('1600'), D('1600')), 'install': (D('0'), D('0'))},
    ],
    'prelims': [
        ('1 Supervision/Management', [
            _pl('Site Manager', 'weeks', rate_formula='=48000*1.3/52', rate=D('1200'), weekly=True,
                allocation=D('1'), formula='IFD'),
            _pl('Project Manager', 'weeks', allocation=D('1'), formula='IFD'),
            _pl('Out of Hours Working', 'weeks', rate=1200, allocation=D('0'), formula='IFD'),
        ]),
        ('2 Site Labourer/ Waste Removal', [
            _pl('Key Op / Logistics', 'weeks', rate=690, weekly=True, allocation=D('1'), formula='IFD'),
            _pl('Skips - 8yd', 'no', rate=310, qty=4, allocation='Mixed waste at the rear yard'),
            _pl('Wheeled bins', 'no', rate=85, qty=3),
        ]),
        ('3 Transport/Deliveries', [
            _pl('Site Deliveries', 'nr', rate=140, qty=6),
            _pl('Mileage, rounded', 'miles', rate=D('0.42'), qty=1250, formula='ROUND'),
        ]),
        ('12 Health & Safety', [
            _pl('H&S Site Supplies', 'item', rate=220, qty=1),
        ]),
    ],
    'client_total': None,
    'clarifications': [('Programme', 'The programme assumes possession of the unit on the agreed start date.'),
                       ('Access', 'Access to the clinic out of hours is assumed to be available.'),
                       ('Services', 'Existing incoming services are assumed to be adequate and in working order.')],
}


