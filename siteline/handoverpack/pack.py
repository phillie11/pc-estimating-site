"""
The handover pack's eleven sections, put together from a workbook revision and what Siteline already
knows about the project (drawing register, subcontract packages, quote levelling). The page and the
Excel export both show exactly this.

  1 Cover            INTERNAL ONLY, project, revision, status
  2 Project summary  the job, the workbook's header, markups, totals, reconciliation
  3 Price summary    cost, charge, margin (pounds and percent) per trade, joinery and prelims
  4 Scope allowed    every line in the tender, with its status
  5 Not allowed      excluded trades, lines by others, prelims marked No, with where each came from
  6 Clarifications   from the workbook, edited in Siteline
  7 Drawings         the project's drawing register; drawing numbers the workbook mentions that it hasn't got
  8 Suppliers        the selected supplier per trade, their quote, the other bidders' totals, links
  9 Site rules       free text
  10 Buy-out targets allowed cost, charge, margin and headroom against the next cheapest bid per trade
  11 Open items      ranked by money: gaps, review lines, reconciliation issues
"""
import re
from decimal import Decimal

from django.urls import reverse

from . import engine

ZERO = Decimal('0')
SECTIONS = [
    ('cover', 'Cover'), ('summary', 'Project summary'), ('prices', 'Price summary'), ('scope', 'Scope allowed'),
    ('excluded', 'Not allowed'), ('clarifications', 'Clarifications'), ('drawings', 'Drawings'),
    ('suppliers', 'Suppliers'), ('rules', 'Site rules'), ('buyout', 'Buy-out targets'), ('items', 'Open items'),
]
# A drawing number: upper-case letters and digits in two or more hyphenated parts, with a digit somewhere
DRAWING = re.compile(r'(?<![A-Za-z0-9-])([A-Z0-9]{1,10}(?:-[A-Z0-9]{1,10}){1,7})(?![A-Za-z0-9-])')


def _norm(ref):
    return re.sub(r'[^A-Z0-9]', '', (ref or '').upper())


def drawing_refs(texts):
    found = {}
    for where, text in texts:
        for m in DRAWING.finditer(text or ''):
            token = m.group(1)
            if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
                found.setdefault(token, []).append(where)
    return found


def _margin(profit, charge):
    return profit / charge if charge else None


def build(project, pack, wb, request=None, clarifications=None, items=None):
    from directory.models import Package
    from levelling.models import LevellingSheet
    from projects.models import Document
    r = wb.result
    org = project.organization.slug
    t = r['totals']
    trades = [tr for tr in r['trades'] if tr['lines'] or tr['include']]
    included = [tr for tr in trades if tr['include']]

    prices = []
    for tr in trades:
        prices.append({'name': tr['name'], 'include': tr['include'], 'supplier': tr['supplier'], 'cost': tr['cost'],
                       'charge': tr['charge'], 'profit': tr['profit'], 'margin': tr['margin'],
                       'markup': tr['markup'], 'provisional': tr['provisional']})
    for name, part, markup in (('Joinery', r['joinery'], None), ('Preliminaries', r['prelims'],
                                                                 r['prelims'].get('markup'))):
        prices.append({'name': name, 'include': True, 'supplier': 'Own cost build-up', 'cost': part['cost'],
                       'charge': part['charge'], 'profit': part['charge'] - part['cost'],
                       'margin': _margin(part['charge'] - part['cost'], part['charge']), 'markup': markup,
                       'provisional': False})

    scope, excluded = [], []
    for tr in trades:
        for ln in tr['lines']:
            if not ln['description'] and ln['cost'] is None:
                continue
            row = {'section': tr['name'], 'ref': ln['ref'], 'description': ln['description'] or '(no description)',
                   'qty': ln['qty'], 'qty_text': ln['qty_text'], 'unit': ln['unit'], 'rate': ln['tender_rate'],
                   'cost': ln['cost'], 'charge': ln['charge'], 'cls': ln['cls'], 'comment': ln['comment'],
                   'label': engine.CLASS_LABELS[ln['cls']], 'tone': engine.CLASS_TONES[ln['cls']],
                   'source': f'{tr["sheet"]} row {ln["row"]}'}
            if ln['cls'] == engine.EXCLUDED:
                row['why'] = f'{tr["name"]} not included (Include? is "{tr["include_text"] or "blank"}")'
                excluded.append(row)
            elif ln['cls'] == engine.BY_OTHERS:
                row['why'] = ln['comment'] or 'By others'
                excluded.append(row)
            else:
                scope.append(row)
    for ln in r['joinery']['lines']:
        scope.append({'section': 'Joinery', 'ref': ln['ref'], 'description': ln['description'] or '(no description)',
                      'qty': ln['qty'], 'qty_text': '', 'unit': ln['unit'], 'rate': None, 'cost': ln['cost'],
                      'charge': ln['charge'], 'cls': ln['cls'], 'comment': ln['notes'],
                      'label': engine.CLASS_LABELS[ln['cls']], 'tone': engine.CLASS_TONES[ln['cls']],
                      'source': f'{r["joinery"]["sheet"]} row {ln["row"]}'})
    prelims_no = []
    for ln in r['prelims']['lines']:
        row = {'section': f'Preliminaries: {ln["section"]}' if ln['section'] else 'Preliminaries', 'ref': '',
               'description': ln['description'], 'qty': ln['qty'], 'qty_text': '', 'unit': ln['unit'],
               'rate': ln['rate'], 'cost': ln['cost'], 'charge': ln['charge'], 'cls': ln['cls'],
               'comment': ln['basis'], 'label': engine.CLASS_LABELS[ln['cls']],
               'tone': engine.CLASS_TONES[ln['cls']], 'source': f'{r["prelims"]["sheet"]} row {ln["row"]}',
               'periods': ln['periods']}
        if ln['cls'] == engine.EXCLUDED:
            row['why'] = 'Marked No' + (' (it had figures in it)' if ln['has_inputs'] else '')
            row['had_inputs'] = ln['has_inputs']
            prelims_no.append(row)
        else:
            scope.append(row)
    prelims_no.sort(key=lambda x: not x['had_inputs'])

    # Drawings: the register, and numbers the workbook mentions
    docs = list(Document.objects.filter(project=project, category__in=('DRAWING', 'TENDER')).prefetch_related('revisions')
                .order_by('reference', 'title')[:500])
    mentioned = drawing_refs([(f'{tr["name"]} {ln["ref"] or ln["row"]}', ln['description'])
                              for tr in trades for ln in tr['lines']]
                             + [(f'Joinery {ln["ref"] or ln["row"]}', ln['description'])
                                for ln in r['joinery']['lines']]
                             + [(f'Clarification {c["ref"] or c["row"]}', c['text']) for c in r['clarifications']])
    known = {_norm(d.reference): d for d in docs if d.reference}
    drawings_mentioned = []
    for token, where in sorted(mentioned.items()):
        n = _norm(token)
        doc = known.get(n) or next((d for k, d in known.items() if n and (k.endswith(n) or n in k)), None)
        drawings_mentioned.append({'ref': token, 'where': where, 'doc': doc,
                                   'url': reverse('document_detail', args=[org, project.pk, doc.pk]) if doc else ''})
    register = [{'doc': d, 'current': d.current, 'url': reverse('document_detail', args=[org, project.pk, d.pk])}
                for d in docs]

    # Suppliers and buy-out targets
    packages = list(Package.objects.filter(project=project).select_related('subcontractor', 'trade'))
    sheets = {s.trade.name.casefold(): s for s in LevellingSheet.objects.filter(project=project).select_related('trade')}
    suppliers, buyout = [], []
    for tr in included:
        others = []
        for i, name in enumerate(tr['suppliers']):
            total = tr['slot_totals'][i]
            if i == (tr['bidder'] or 0) - 1 or (not name and total is None):
                continue
            others.append({'slot': i + 1, 'name': name or f'Bidder {i + 1}', 'total': total,
                           'lines': tr['slot_counts'][i]})
        priced_others = [o for o in others if o['total'] is not None]
        nxt = min(priced_others, key=lambda o: o['total']) if priced_others else None
        trade_key = tr['name'].casefold()
        pkg_for = [p for p in packages if p.trade and p.trade.name.casefold() == trade_key]
        chosen_pkg = next((p for p in pkg_for if tr['supplier'] and
                           p.subcontractor.company_name.casefold() == tr['supplier'].casefold()), None)
        sheet = sheets.get(trade_key)
        lines = [ln for ln in tr['lines'] if ln['description']]
        allowances = [ln for ln in lines if ln['cls'] == engine.ALLOWANCE]
        quote = tr['slot_totals'][tr['bidder'] - 1] if tr['bidder'] else None
        suppliers.append({
            'trade': tr['name'], 'supplier': tr['supplier'] or '', 'bidder': tr['bidder'], 'quote': quote,
            'lines': tr['slot_counts'][tr['bidder'] - 1] if tr['bidder'] else 0, 'line_count': len(lines),
            'others': others, 'package': chosen_pkg, 'packages': pkg_for,
            'package_url': reverse('package_list', args=[org, project.pk]) if pkg_for else '',
            'levelling_url': reverse('levelling_sheet', args=[org, project.pk, sheet.trade_id]) if sheet else '',
            'own_allowance': bool(allowances) or not tr['supplier'],
        })
        headroom = (tr['cost'] - nxt['total']) if nxt is not None else None
        buyout.append({
            'trade': tr['name'], 'supplier': tr['supplier'], 'cost': tr['cost'], 'charge': tr['charge'],
            'margin': tr['profit'], 'margin_pct': tr['margin'], 'next': nxt, 'headroom': headroom,
            'allowances': allowances, 'allowance_total': sum((ln['cost'] or ZERO for ln in allowances), ZERO),
            'no_quote': not tr['supplier'],
            'provisional': tr['provisional'],
        })

    if clarifications is None:
        clarifications = list(pack.clarifications.all()) if pack else []
    if items is None:
        items = list(pack.items.select_related('owner').all()) if pack else []
    open_items = sorted(items, key=lambda i: (i.status != i.OPEN, i.amount is None, -(i.amount or ZERO), i.pk))

    return {
        'sections': SECTIONS, 'result': r, 'totals': t, 'header': r['header'], 'settings': r['settings'],
        'prices': prices, 'price_total': {'cost': t['cost'], 'charge': t['charge'], 'profit': t['profit'],
                                          'margin': t['margin']},
        'scope': scope, 'excluded': excluded, 'prelims_no': prelims_no, 'clarifications': clarifications,
        'register': register, 'drawings_mentioned': drawings_mentioned,
        'drawings_missing': [d for d in drawings_mentioned if d['doc'] is None],
        'suppliers': suppliers, 'buyout': buyout, 'items': open_items,
        'open_count': sum(1 for i in items if i.status == i.OPEN),
        'site_rules': pack.site_rules if pack else '', 'pack': pack, 'workbook': wb, 'project': project,
        'reconciled': wb.reconciled, 'difference': wb.difference, 'notes': r['notes'], 'issues': r['issues'],
        'gaps': r['gaps'],
    }
