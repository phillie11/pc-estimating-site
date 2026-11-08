"""
Builds a pricing workbook in the standard layout, from a short description, for tests and the demo.

    data = sample.build(sample.DEMO)                 # .xlsx bytes, with Excel's formulas
    data = sample.build(spec, cached=False)          # as a tool that doesn't calculate would save it

The workbook has the template's sheets and the template's formulas (the same text, cell for cell).
openpyxl can't calculate, so with cached=True (the default) the values Excel would have saved are
worked out here, separately from the engine, and written into the file next to each formula, as
Excel does. `stale` changes some of those saved values (an out-of-date cache) and `typed` replaces a
formula with a typed number (someone overtyping a total), to test reconciliation.

Every name in DEMO is invented.
"""
import io
import re
import zipfile
from datetime import date
from decimal import Decimal as D
from xml.etree import ElementTree as ET  # nosec B405: only builds and writes XML; parsing is defusedxml

from defusedxml.ElementTree import fromstring
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .mapping import TRADE_TABS

PRELIM_BOOK = [
    ('1 Supervision/Management', [('Site Manager', 'weeks'), ('Project Manager', 'weeks'), ('QS', 'weeks'),
                                  ('Holiday Cover', 'weeks'), ('Out of Hours Working', 'weeks')]),
    ('2 Site Labourer/ Waste Removal', [('Key Op / Logistics', 'weeks'), ('Gateman / banksman', 'weeks'),
                                        ('Holiday cover', 'weeks'), ('Road Sweeper', 'visits'), ('Skips - 12yd', 'no'),
                                        ('Skips - 8yd', 'no'), ('Cabin waste', 'no'), ('Site Bins', 'no')]),
    ('3 Transport/Deliveries', [('Site Deliveries', 'nr'), ('Site Deliveries - mileage', 'miles')]),
    ('4 Site Accommodation', [('Office/meeting room - static', 'weeks'), ('Furniture', 'weeks'),
                              ('Mobile welfare unit', 'weeks'), ('WC block', 'weeks'), ('20FT Store', 'weeks'),
                              ('Site Delivery / Collection', 'Item'), ('Printing/Stationery', 'weeks'),
                              ('Welfare Consumables', 'weeks')]),
    ('5 Site Compound', [('Heras fencing/gates hire', 'm'), ('Solid timber painted hoarding', 'm'),
                         ('Pedestrian Access', 'nr')]),
    ('6 Temporary Services', [('Electricity Supply to Cabins', 'Lump Sum'), ('Temp Light/Heating - Refurb', 'Lump Sum')]),
    ('7 Tools / Equipment', [('Small Plant & Tools - Refurb', 'weeks'), ('Mewp', 'weeks'), ('Hoist', 'weeks'),
                             ('Correx Protection', 'item')]),
    ('8 Signage', [('Site Signs - Directional etc.', 'Lump Sum'), ('H&S Signage', 'Lump Sum')]),
    ('10 Site Security', [('Manned Guard', 'weeks'), ('CCTV monthly hire', 'months')]),
    ('11 Cleaning', [('Internal Clean', 'Lump Sum'), ('Builders clean', 'item')]),
    ('12 Health & Safety', [('Site Inspections', 'nr'), ('H&S Site Supplies', 'item'), ('H+S Plan', 'Lump Sum')]),
    ('13 Fees / expenses', [('Scaffold Inspections', 'nr'), ('Building Control fees', 'nr'),
                            ('Expenses/Hotels', 'nights'), ('Travel expense - Train', 'trips')]),
]

SUMMARY, PRELIMS, JOINERY, CLAR = 'PRICING SUMMARY', 'Prelim Book', 'JOINERY SUMMARY', 'Clarifications'
SLOT_COLS = [('F', 'G', 'H', 'I'), ('J', 'K', 'L', 'M'), ('N', 'O', 'P', 'Q'), ('R', 'S', 'T', 'U')]


def _n(v):
    return v if isinstance(v, (int, float, D)) and not isinstance(v, bool) else None


def _xl(v):
    """Decimal to float for openpyxl (Excel holds doubles); everything else as it is."""
    return float(v) if isinstance(v, D) else v


def _prelim_rows():
    """[(row, kind, text, unit)] laid out as the template: section, its lines, a blank row."""
    rows, r = [], 10
    for section, items in PRELIM_BOOK:
        rows.append((r, 'S', section, ''))
        r += 1
        for name, unit in items:
            rows.append((r, 'I', name, unit))
            r += 1
        r += 1
    return rows


def build(spec, cached=True, stale=None, typed=None):
    wb = Workbook()
    ws = wb.active
    ws.title = SUMMARY
    values = {}  # sheet -> {coord: value Excel would save}

    def put(sheet, coord, formula, value):
        wb[sheet][coord] = formula
        values.setdefault(sheet, {})[coord] = value

    h = spec.get('header', {})
    for coord, label in (('B4', 'Project'), ('E4', 'Date'), ('H4', 'Default trade markup'), ('B5', 'Client'),
                         ('E5', 'Revision'), ('H5', 'Prelim markup'), ('B6', 'Estimator'), ('H6', 'Target margin'),
                         ('B10', 'Selected supplier'), ('C10', 'Include?'), ('D10', 'Description'), ('E10', 'Cost'),
                         ('F10', 'Markup'), ('G10', 'Charge'), ('H10', 'Profit'), ('I10', 'Margin')):
        ws[coord] = label
    ws['B2'] = 'PRICING SUMMARY'
    ws['D4'], ws['D5'], ws['D6'] = h.get('project', ''), h.get('client', ''), h.get('estimator', '')
    ws['F4'], ws['F5'] = h.get('date', date(2026, 10, 1)), h.get('revision', 'A')
    dm, pm, tm = spec.get('default_markup', D('0.1')), spec.get('prelim_markup', D('0.1')), spec.get('target_margin', D('0.15'))
    ws['J4'], ws['J5'], ws['J6'] = _xl(dm), _xl(pm), _xl(tm)

    trades = spec.get('trades', {})
    sum_e = sum_g = D(0)
    for i, name in enumerate(TRADE_TABS):
        row = 11 + i
        t = trades.get(name, {})
        tab = wb.create_sheet(name)
        tab['B2'] = f'{name.upper()} — SUBCONTRACTOR COMPARISON'
        tab['B4'] = 'Select bidder'
        tab['C58'] = 'TOTAL'
        for k, (rc, ac, qc, cc) in enumerate(SLOT_COLS):
            tab[f'{rc}4'] = str(k + 1)
        names = (t.get('suppliers') or []) + [''] * 4
        for k, (rc, _, _, _) in enumerate(SLOT_COLS):
            if names[k]:
                tab[f'{rc}5'] = names[k]
        bidder = t.get('bidder')
        if bidder:
            tab['D4'] = bidder
        w4 = names[bidder - 1] if bidder and 1 <= bidder <= 4 else ''
        w4_value = (w4 if w4 else 0) if bidder else ''
        put(name, 'W4', '=IF(ISNUMBER(D4),CHOOSE(D4,F5,J5,N5,R5),"")', w4_value)
        markup = t['markup'] if t.get('markup') is not None else dm
        include = 'Yes' if t.get('include') else t.get('include_text', 'No')
        ws[f'C{row}'] = include
        ws[f'D{row}'] = name
        if t.get('markup') is not None:
            ws[f'F{row}'] = _xl(t['markup'])
            values.setdefault(SUMMARY, {})[f'F{row}'] = t['markup']
        else:
            put(SUMMARY, f'F{row}', '=$J$4', dm)
        put(name, 'AE4', f"='{SUMMARY}'!F{row}", markup)
        aa_total = ad_total = D(0)
        slot_tot = [D(0)] * 4
        for li, line in enumerate(t.get('lines', [])):
            r = 8 + li
            tab[f'B{r}'] = line.get('ref', '')
            if line.get('desc'):
                tab[f'C{r}'] = line['desc']
            if line.get('qty') is not None:
                tab[f'D{r}'] = _xl(line['qty'])
            tab[f'E{r}'] = line.get('unit', '')
            slots = (line.get('slots') or []) + [(None, None, '')] * 4
            for k, (rc, ac, qc, cc) in enumerate(SLOT_COLS):
                rate, adj, comment = (list(slots[k]) + [None, None, ''])[:3]
                if rate is not None:
                    tab[f'{rc}{r}'] = _xl(rate)
                if adj is not None:
                    tab[f'{ac}{r}'] = _xl(adj)
                if comment:
                    tab[f'{cc}{r}'] = comment
        for r in range(8, 58):
            li = r - 8
            line = t.get('lines', [])[li] if li < len(t.get('lines', [])) else {}
            qty = _n(line.get('qty'))
            slots = (line.get('slots') or []) + [(None, None, '')] * 4
            for k, (rc, ac, qc, cc) in enumerate(SLOT_COLS):
                rate, adj = (list(slots[k]) + [None, None])[:2]
                q = qty * (_n(rate) + (_n(adj) or 0)) if qty is not None and _n(rate) is not None else ''
                if q != '':
                    slot_tot[k] += q
                put(name, f'{qc}{r}', f'=IF(AND(ISNUMBER(D{r}),ISNUMBER({rc}{r})),D{r}*({rc}{r}+{ac}{r}),"")', q)
            put(name, f'W{r}', f'=IF(ISNUMBER(D{r}),D{r},"")', qty if qty is not None else '')
            put(name, f'X{r}', f'=IF(E{r}="","",E{r})', line.get('unit', ''))
            sel = list(slots[bidder - 1]) + [None, None] if bidder else [None, None]
            y = _n(sel[0]) if bidder else None
            z = (_n(sel[1]) or D(0)) if bidder else ''
            put(name, f'Y{r}', f'=IF(ISNUMBER($D$4),IF(CHOOSE($D$4,COUNT(F{r}),COUNT(J{r}),COUNT(N{r}),COUNT(R{r}))=1,'
                f'CHOOSE($D$4,F{r},J{r},N{r},R{r}),""),"")', y if y is not None else '')
            put(name, f'Z{r}', f'=IF(ISNUMBER($D$4),SUM(CHOOSE($D$4,G{r},K{r},O{r},S{r})),"")', z)
            aa = qty * (y + z) if qty is not None and y is not None else ''
            ad = aa * (1 + markup) if aa != '' else ''
            put(name, f'AA{r}', f'=IF(AND(ISNUMBER(W{r}),ISNUMBER(Y{r})),W{r}*(Y{r}+Z{r}),"")', aa)
            put(name, f'AC{r}', f'=IF(ISNUMBER(AA{r}),(Y{r}+Z{r})*(1+$AE$4),"")',
                (y + z) * (1 + markup) if aa != '' else '')
            put(name, f'AD{r}', f'=IF(ISNUMBER(AA{r}),AA{r}*(1+$AE$4),"")', ad)
            status = '' if not line.get('desc') else ('Priced' if aa != '' and w4 else 'Review')
            put(name, f'AE{r}', f'=IF(C{r}="","",IF(AND(ISNUMBER(AA{r}),$W$4<>"",$W$4<>0),"Priced","Review"))', status)
            if aa != '':
                aa_total += aa
                ad_total += ad
        for k, (rc, ac, qc, cc) in enumerate(SLOT_COLS):
            put(name, f'{qc}58', f'=SUM({qc}8:{qc}57)', slot_tot[k])
        put(name, 'AA58', '=SUM(AA8:AA57)', aa_total)
        put(name, 'AD58', '=SUM(AD8:AD57)', ad_total)
        yes = include.strip().casefold() == 'yes'
        e, g = (aa_total, ad_total) if yes else (D(0), D(0))
        sum_e += e
        sum_g += g
        put(SUMMARY, f'B{row}', f"=IF('{name}'!W4=\"\",\"\",'{name}'!W4)", w4_value if w4_value != '' else '')
        put(SUMMARY, f'E{row}', f"=IF(C{row}=\"Yes\",'{name}'!AA58,0)", e)
        put(SUMMARY, f'G{row}', f"=IF(C{row}=\"Yes\",'{name}'!AD58,0)", g)
        put(SUMMARY, f'H{row}', f'=G{row}-E{row}', g - e)
        put(SUMMARY, f'I{row}', f'=IF(G{row}=0,"",H{row}/G{row})', (g - e) / g if g else '')

    # Prelim Book
    pb = wb.create_sheet(PRELIMS, 1)
    pb['B2'], pb['B4'], pb['E4'], pb['G4'] = 'PRELIM BOOK', 'Duration', 'weeks', 'Markup'
    pre = spec.get('prelims', {})
    duration = pre.get('duration')
    if duration is not None:
        pb['D4'] = duration
    put(PRELIMS, 'I4', f"='{SUMMARY}'!J5", pm)
    for coord, label in zip('BCDEFGHIJKLMN', ('Include?', 'Description', 'Allocation %', 'Quantity', 'Unit',
                                             'Periods input', 'Periods used', 'Rate', 'Cost', 'Markup', 'Charge',
                                             'Status', 'Basis / source')):
        pb[f'{coord}9'] = label
    j_total = l_total = D(0)
    chosen = dict(pre.get('lines', {}))
    for r, kind, text, unit in _prelim_rows():
        pb[f'C{r}'] = text
        if kind == 'S':
            continue
        line = chosen.pop(text, {})
        inc = line.get('include', 'No')
        pb[f'B{r}'] = inc
        pb[f'D{r}'] = _xl(line.get('allocation', 1))
        pb[f'F{r}'] = line.get('unit', unit)
        for key, c in (('qty', 'E'), ('periods', 'G'), ('rate', 'I')):
            if line.get(key) is not None:
                pb[f'{c}{r}'] = _xl(line[key])
        if line.get('basis'):
            pb[f'N{r}'] = line['basis']
        alloc, qty, per, rate = _n(line.get('allocation', 1)) or 0, _n(line.get('qty')), _n(line.get('periods')), _n(line.get('rate'))
        h_used = per if per is not None else (duration if line.get('unit', unit).lower() == 'weeks' and duration is not None else None)
        put(PRELIMS, f'H{r}', f'=IF(ISNUMBER(G{r}),G{r},IF(AND(F{r}="weeks",ISNUMBER($D$4)),$D$4,""))',
            h_used if h_used is not None else '')
        j = alloc * qty * h_used * rate if inc == 'Yes' and None not in (qty, h_used, rate) else ''
        put(PRELIMS, f'J{r}', f'=IF(B{r}<>"Yes","",IF(AND(ISNUMBER(E{r}),ISNUMBER(H{r}),ISNUMBER(I{r})),'
            f'D{r}*E{r}*H{r}*I{r},""))', j)
        put(PRELIMS, f'K{r}', '=$I$4', pm)
        lv = j * (1 + pm) if j != '' else ''
        put(PRELIMS, f'L{r}', f'=IF(ISNUMBER(J{r}),J{r}*(1+K{r}),"")', lv)
        put(PRELIMS, f'M{r}', f'=IF(B{r}="No","Excluded",IF(ISNUMBER(J{r}),"Priced","Review"))',
            'Excluded' if inc == 'No' else ('Priced' if j != '' else 'Review'))
        if j != '':
            j_total += j
            l_total += lv
    if chosen:
        raise ValueError(f'No such prelim line: {sorted(chosen)}')
    pb['C138'] = 'TOTAL PRELIMINARIES'
    put(PRELIMS, 'J138', '=SUM(J10:J135)', j_total)
    put(PRELIMS, 'L138', '=SUM(L10:L135)', l_total)

    # JOINERY SUMMARY
    js = wb.create_sheet(JOINERY, 2)
    js['B2'] = 'JOINERY SUMMARY'
    jm = spec.get('joinery_markups', {})
    mm, mf, mi = (jm.get(k, D(0)) for k in ('material', 'manufacture', 'install'))
    js['B4'], js['D4'], js['F4'], js['H4'], js['J4'], js['L4'] = ('Material markup', _xl(mm), 'Manufacture markup',
                                                                  _xl(mf), 'Install markup', _xl(mi))
    l48 = m48 = D(0)
    jlines = spec.get('joinery', [])
    keys = ('qty', 'material', 'ancillary', 'manufacture_hrs', 'hourly', 'install_hrs', 'install_hourly')
    for i in range(40):
        r = 8 + i
        line = jlines[i] if i < len(jlines) else {}
        for c, key in zip('BCDEFGHIJKO', ('ref', 'desc', 'qty', 'unit', 'material', 'ancillary', 'manufacture_hrs',
                                         'hourly', 'install_hrs', 'install_hourly', 'notes')):
            if line.get(key) not in (None, ''):
                js[f'{c}{r}'] = _xl(line[key])
        vals = [_n(line.get(k)) for k in keys]
        lv = mv = ''
        if line.get('desc') and None not in vals:
            q, f_, g_, h_, i_, j_, k_ = vals
            lv = q * (f_ + g_ + h_ * i_ + j_ * k_)
            mv = q * ((f_ + g_) * (1 + mm) + h_ * i_ * (1 + mf) + j_ * k_ * (1 + mi))
            l48 += lv
            m48 += mv
        put(JOINERY, f'L{r}', f'=IF(C{r}="","",IF(COUNT(D{r},F{r}:K{r})=7,D{r}*(F{r}+G{r}+H{r}*I{r}+J{r}*K{r}),""))', lv)
        put(JOINERY, f'M{r}', f'=IF(ISNUMBER(L{r}),D{r}*((F{r}+G{r})*(1+$D$4)+H{r}*I{r}*(1+$H$4)+J{r}*K{r}*(1+$L$4)),"")', mv)
        put(JOINERY, f'N{r}', f'=IF(C{r}="","",IF(ISNUMBER(L{r}),"Priced","Review"))',
            '' if not line.get('desc') else ('Priced' if lv != '' else 'Review'))
    js['C48'] = 'TOTAL'
    put(JOINERY, 'L48', '=SUM(L8:L47)', l48)
    put(JOINERY, 'M48', '=SUM(M8:M47)', m48)

    cl = wb.create_sheet(CLAR, 3)
    cl['B2'] = 'CLARIFICATIONS'
    for c, label in zip('BCDEFGH', ('Ref', 'Trade / section', 'Clarification / assumption', 'Response / resolution',
                                   'Status', 'Cost effect', 'Owner')):
        cl[f'{c}7'] = label
    for i, row in enumerate(spec.get('clarifications', [])):
        for c, v in zip('BCDEFGH', row):
            if v not in (None, ''):
                cl[f'{c}{8 + i}'] = v
                if isinstance(v, str) and v.startswith('='):
                    cl[f'{c}{8 + i}'].data_type = 's'  # typed text that looks like a formula, as a hostile cell would be
    ins = wb.create_sheet('Instructions', 3)
    ins['B2'] = 'HOW TO USE'

    # Summary rows 47-50, 53-57
    ws['B47'], ws['D47'], ws['B48'], ws['D48'], ws['D50'] = ('Own cost build-up', 'Joinery', 'Own cost build-up',
                                                             'Preliminaries', 'TOTAL')
    put(SUMMARY, 'E47', f"='{JOINERY}'!L48", l48)
    put(SUMMARY, 'G47', f"='{JOINERY}'!M48", m48)
    put(SUMMARY, 'H47', '=G47-E47', m48 - l48)
    put(SUMMARY, 'E48', f"='{PRELIMS}'!J138", j_total)
    put(SUMMARY, 'G48', f"='{PRELIMS}'!L138", l_total)
    put(SUMMARY, 'H48', '=G48-E48', l_total - j_total)
    e50, g50 = sum_e + l48 + j_total, sum_g + m48 + l_total
    put(SUMMARY, 'E50', '=SUM(E11:E48)', e50)
    put(SUMMARY, 'G50', '=SUM(G11:G48)', g50)
    put(SUMMARY, 'H50', '=SUM(H11:H48)', g50 - e50)
    put(SUMMARY, 'I50', '=IF(G50=0,"",H50/G50)', (g50 - e50) / g50 if g50 else '')
    ws['D53'], ws['D54'], ws['D55'] = 'Price at target margin', 'Final tender price (optional)', 'Final price used'
    put(SUMMARY, 'G53', '=IF(E50=0,"",E50/(1-$J$6))', e50 / (1 - tm) if e50 else '')
    if spec.get('final_price') is not None:
        ws['G54'] = _xl(spec['final_price'])
    final = spec.get('final_price') if spec.get('final_price') is not None else g50
    put(SUMMARY, 'G55', '=IF(ISNUMBER(G54),G54,G50)', final)
    put(SUMMARY, 'G56', '=G55-E50', final - e50)
    put(SUMMARY, 'G57', '=IF(G55=0,"",G56/G55)', (final - e50) / final if final else '')

    for (sheet, coord), v in (typed or {}).items():
        wb[sheet][coord] = _xl(v)
        values.setdefault(sheet, {}).pop(coord, None)
    for sheet in wb.sheetnames:
        wb[sheet].column_dimensions['C'].width = 40
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if cached:
        for (sheet, coord), v in (stale or {}).items():
            values[sheet][coord] = v
        data = inject_cached(data, values)
    return data


MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'


def _fmt(v):
    if isinstance(v, D):
        return repr(float(v))
    if isinstance(v, float):
        return repr(v)
    return str(int(v))


def inject_cached(data, values):
    """Write the saved values next to each formula, as Excel does: <c><f>..</f><v>value</v></c>."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    wbx = fromstring(zin.read('xl/workbook.xml'))
    rels = fromstring(zin.read('xl/_rels/workbook.xml.rels'))
    target = {r.get('Id'): r.get('Target') for r in rels.findall(f'{{{PKG}}}Relationship')}
    paths = {}
    for s in wbx.find(f'{{{MAIN}}}sheets'):
        t = target[s.get(f'{{{REL}}}id')]
        paths['xl/' + t.lstrip('/').removeprefix('xl/')] = s.get('name')
    ET.register_namespace('', MAIN)
    ET.register_namespace('r', REL)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            raw = zin.read(info)
            sheet = paths.get(info.filename)
            if info.filename == "xl/workbook.xml":  # Excel, unlike openpyxl, saves without asking for a recalculation
                raw = re.sub(rb"<calcPr[^>]*/>", b"<calcPr calcId=\"191029\"/>", raw)
            if sheet and values.get(sheet):
                root = fromstring(raw)
                want = values[sheet]
                for c in root.iter(f'{{{MAIN}}}c'):
                    f = c.find(f'{{{MAIN}}}f')
                    if f is None or c.get('r') not in want:
                        continue
                    v = want[c.get('r')]
                    for old in c.findall(f'{{{MAIN}}}v'):
                        c.remove(old)
                    el = ET.SubElement(c, f'{{{MAIN}}}v')
                    if isinstance(v, str):
                        c.set('t', 'str')
                        el.text = v
                    else:
                        c.attrib.pop('t', None)
                        el.text = _fmt(v)
                raw = ET.tostring(root, xml_declaration=True, encoding='UTF-8')
            zout.writestr(info, raw)
    return out.getvalue()


def line(ref, desc, qty, unit, *slots):
    """A trade line: slots are (rate, est adj, comment) per supplier, in order."""
    return {'ref': ref, 'desc': desc, 'qty': None if qty is None else D(str(qty)), 'unit': unit,
            'slots': [(None if r is None else D(str(r)), None if a is None else D(str(a)), c) for r, a, c in slots]}


def _l(ref, desc, qty, unit, *slots):
    return line(ref, desc, qty, unit, *slots)


# An invented office fit-out, priced the way an estimator would: most trades on quotes, some own
# allowances, a provisional sum, a trade left out, a few loose ends for the handover to catch.
DEMO = {
    'header': {'project': 'Harbour Lane offices, 3rd floor CAT B', 'client': 'Northgate Property Partners',
               'estimator': 'Tom Reilly', 'date': date(2026, 10, 14), 'revision': 'C'},
    'default_markup': D('0.12'), 'prelim_markup': D('0.10'), 'target_margin': D('0.12'),
    'trades': {
        'Partitions Ceilings': {
            'include': True, 'bidder': 2,
            'suppliers': ['Brightwall Interiors', 'Fernside Drylining', 'Coldharbour Ceilings'],
            'lines': [
                _l('PC1', 'Metal stud partition, 2x15mm board each side, to underside of slab (HL-A-201)', 186, 'm2',
                   (58.5, None, ''), (54.2, None, ''), (61.0, None, '')),
                _l('PC2', 'Acoustic partition to meeting rooms, Rw 45dB (HL-A-201)', 92, 'm2', (78.0, None, ''),
                   (74.5, 2.5, 'Extra board layer per spec'), (80.0, None, '')),
                _l('PC3', 'MF suspended ceiling, plasterboard, skim and decorate (HL-A-310)', 410, 'm2',
                   (36.0, None, ''), (33.8, None, ''), (35.5, None, '')),
                _l('PC4', 'Bulkheads to perimeter, 300mm drop', 64, 'm', (42.0, None, ''), (39.0, None, ''),
                   (None, None, 'Not priced')),
                _l('PC5', 'Access hatches 600x600', 12, 'nr', (None, None, ''), (None, None, ''),
                   (68.0, None, '')),
            ]},
        'Electrical': {
            'include': True, 'bidder': 1, 'suppliers': ['Voltline Electrical', 'Marsh & Pike Electrical'],
            'lines': [
                _l('E1', 'Strip out existing small power and lighting', 1, 'item', (3200, None, ''), (2950, None, '')),
                _l('E2', 'LED lighting with DALI controls to open plan (HL-E-401)', 410, 'm2', (48.0, None, ''),
                   (51.5, None, '')),
                _l('E3', 'Small power to desks via floor boxes', 120, 'nr', (185.0, None, ''), (176.0, None, '')),
                _l('E4', 'Fire alarm extension, L1, to landlord system', 1, 'item', (0, None, 'Included in E2'),
                   (4800, None, '')),
                _l('E5', 'Emergency lighting, testing and certification', 1, 'item', (2650, None, ''),
                   (2400, None, '')),
                _l('E6', 'Data cabling Cat6A', 120, 'nr', (0, None, 'By client IT contractor'), (0, None, '')),
            ]},
        'Mechanical': {
            'include': True, 'bidder': 1, 'suppliers': ['Airflow Building Services'],
            'lines': [
                _l('M1', 'Fan coil units, reposition and reconnect', 18, 'nr', (420, None, ''),),
                _l('M2', 'Commissioning and balancing', 1, 'item', (1850, None, ''),),
                _l('M3', 'Comfort cooling to comms room', 1, 'item', (None, None, 'Awaiting price'),),
            ]},
        'Decoration': {
            'include': True, 'bidder': 1, 'suppliers': ['Own allowance'],
            'lines': [
                _l('D1', 'Mist coat and two coats emulsion to new partitions', 556, 'm2', (0, 4.6, 'Own rate')),
                _l('D2', 'Gloss to door frames', 34, 'nr', (0, 18, 'Own rate')),
            ]},
        'Flooring': {
            'include': True, 'bidder': 1, 'suppliers': ['Tessera Floors', 'Lowland Flooring'], 'markup': D('0.15'),
            'lines': [
                _l('F1', 'Carpet tiles to open plan, including smoothing compound', 410, 'm2', (31.5, None, ''),
                   (29.9, None, '')),
                _l('F2', 'LVT to tea point', 26, 'm2', (48.0, None, ''), (46.0, None, '')),
                _l('F3', 'Entrance matting, recessed', 4, 'm2', (210.0, None, ''), (None, None, '')),
            ]},
        'Doors': {
            'include': True, 'bidder': 1, 'suppliers': ['Hartwell Doorsets'],
            'lines': [
                _l('DR1', 'FD30S doorsets, oak veneer, complete (HL-A-520)', 14, 'nr', (685, None, '')),
                _l('DR2', 'Glazed meeting room doorsets', 6, 'nr', (1240, None, '')),
            ]},
        'Prov Sums': {
            'include': True, 'bidder': 1, 'suppliers': ['Provisional sum'], 'markup': D('0'),
            'lines': [
                _l('PS1', 'Provisional sum: landlord\'s works to riser', 1, 'item', (5000, None, 'Client PS')),
                _l('PS2', 'Provisional sum: out of hours working', 1, 'item', (2500, None, 'Client PS')),
            ]},
        'Signage': {
            'include': False, 'bidder': 1, 'suppliers': ['Brandmark Signs'],
            'lines': [
                _l('S1', 'Manifestation to glazed screens', 48, 'm', (22, None, '')),
                _l('S2', 'Wayfinding signage package', 1, 'item', (3400, None, '')),
            ]},
        'Cleaning': {
            'include': True, 'bidder': None, 'suppliers': ['Spotless Sparkle Cleaning', 'Brightway Cleaning'],
            'lines': [
                _l('C1', 'Builders clean and sparkle clean before handover', 1, 'item', (1450, None, ''),
                   (1380, None, '')),
            ]},
    },
    'prelims': {
        'duration': 14,
        'lines': {
            'Site Manager': {'include': 'Yes', 'qty': 1, 'rate': 1650, 'basis': 'Company rate'},
            'Project Manager': {'include': 'Yes', 'allocation': D('0.4'), 'qty': 1, 'rate': 1900},
            'QS': {'include': 'Yes', 'allocation': D('0.2'), 'qty': 1, 'rate': 1700},
            'Key Op / Logistics': {'include': 'Yes', 'qty': 1, 'rate': 980},
            'Skips - 12yd': {'include': 'Yes', 'qty': 22, 'periods': 1, 'rate': 285},
            'Mobile welfare unit': {'include': 'No', 'qty': 1, 'rate': 210},
            'Welfare Consumables': {'include': 'Yes', 'qty': 1, 'rate': 45},
            'Small Plant & Tools - Refurb': {'include': 'Yes', 'qty': 1, 'rate': 160},
            'Mewp': {'include': 'Yes', 'qty': 1},
            'Correx Protection': {'include': 'Yes', 'qty': 1, 'periods': 1, 'rate': 1350},
            'Internal Clean': {'include': '', 'qty': 1, 'periods': 1},
        },
    },
    'joinery_markups': {'material': D('0.15'), 'manufacture': D('0.2'), 'install': D('0.2')},
    'joinery': [
        {'ref': 'J1', 'desc': 'Reception desk, oak veneer with solid surface top (HL-J-601)', 'qty': 1, 'unit': 'nr',
         'material': 2850, 'ancillary': 320, 'manufacture_hrs': 46, 'hourly': 38, 'install_hrs': 16,
         'install_hourly': 34},
        {'ref': 'J2', 'desc': 'Tea point base and wall units', 'qty': 8, 'unit': 'm', 'material': 260,
         'ancillary': 35, 'manufacture_hrs': 3, 'hourly': 38, 'install_hrs': 2, 'install_hourly': 34},
        {'ref': 'J3', 'desc': 'Window boards, MDF, painted', 'qty': 42, 'unit': 'm', 'material': 14, 'ancillary': 2,
         'manufacture_hrs': None, 'hourly': 38, 'install_hrs': D('0.4'), 'install_hourly': 34},
    ],
    'clarifications': [
        ('CL1', 'General', 'Works carried out during normal working hours, 08:00 to 18:00 Monday to Friday.', '',
         'Open', '', 'Estimating'),
        ('CL2', 'Electrical', 'Data cabling by the client\'s IT contractor; we provide containment only.', 'Agreed at '
         'mid-tender meeting', 'Agreed', '', 'Estimating'),
        ('CL3', 'Mechanical', 'Existing fan coil units assumed serviceable; no replacement allowed.', '', 'Open',
         'Risk', 'Estimating'),
        ('CL4', 'Prelims', 'Landlord welfare facilities available for our use free of charge.', '', 'Open', '', ''),
    ],
}


def demo_revision_b():
    """An earlier revision of the demo: no flooring markup override, mechanical cheaper, no cleaning bidder."""
    import copy
    spec = copy.deepcopy(DEMO)
    spec['header']['revision'] = 'B'
    spec['trades']['Flooring'].pop('markup')
    spec['trades']['Mechanical']['lines'][0] = _l('M1', 'Fan coil units, reposition and reconnect', 18, 'nr',
                                                  (395, None, ''),)
    spec['trades']['Partitions Ceilings']['bidder'] = 1
    return spec

