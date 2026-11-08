"""
Drafting clarifications with AI, from the pack's own workbook (the seam this module always was).

On the Clarifications page, someone who may use AI drafting (ai/service.status) presses "Draft with AI". That
queues an AIRun; the worker (ai/tasks.py) calls run(), which builds the input below, asks the model through
ai/service.call with the company's clarifications playbook, and saves each suggestion as a PackClarification with
source DRAFT and approved=False. A person edits, approves or deletes each one. An unapproved draft never shows
in the pack, its export or an issued pack (HandoverPack.visible_clarifications).

What is sent, and only this (data minimisation), from the latest revision of this one pack:

  - each line's trade, the workbook's line ref, description, quantity, unit and status (priced, included,
    excluded, provisional sum, own allowance, by others, review), and its comment (joinery notes, prelims basis);
  - the charge on provisional sums and own allowances, and no other money: no cost, margin or markup;
  - the trades not included in the price;
  - the gaps Siteline found, except the margin and markup ones;
  - the site rules, and the clarifications already in the pack so they aren't repeated;
  - the company's clarifications playbook (ai/registry.py). Not the company profile or overheads and profit:
    those go only with tender readings.

In all of that text, every supplier name in the workbook's bidder slots becomes [supplier], email addresses
[email] and UK phone numbers [phone]; in comments, notes and basis, money and percentages become [amount].
Nothing from any other project or company.

Drafting again replaces only drafts nobody has touched: an edited or approved row is never overwritten.
"""
import re
from decimal import Decimal

from django.urls import reverse

MAX_LINES = 600
MAX_DESC = 300
MAX_RULES = 4000
MAX_DRAFTS = 60
MAX_TEXT = 2000
# Gaps about margin and markup are commercial, not scope: they are never sent
COMMERCIAL_GAPS = {'margin_below_target', 'no_target_margin', 'zero_markup'}

EMAIL = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')
# A UK number in any usual layout: +44 20 7946 0000, +44 (0)20 7946 0000, (020) 7946 0001, 07700-900123...
PHONE = re.compile(r'(?<![\w+])(?:\+\s?44[\s-]*(?:\(0\)[\s-]*)?|\(?0)[\d\s()\-]{8,18}\d(?!\w)')
MONEY = re.compile(r'£\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|bn)\b)?|\b\d[\d,]*(?:\.\d+)?\s?%|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b'
                   r'|\b\d+\.\d{2}\b|\b\d{4,}\b|\b(?:GBP|gbp)\s?\d[\d,.]*', re.I)


def _phone(m):
    digits = re.sub(r'\D', '', m.group(0))
    return '[phone]' if 10 <= len(digits) <= 13 else m.group(0)


def scrub(text, limit=None, names=None, money=False):
    """
    Plain text with email addresses and UK phone numbers taken out, and capped. `names` (a compiled pattern of
    supplier names) become [supplier]; with money=True, amounts and percentages become [amount].
    """
    text = PHONE.sub(_phone, EMAIL.sub('[email]', str(text or '')))
    if names is not None:
        text = names.sub('[supplier]', text)
    if money:
        text = MONEY.sub('[amount]', text)
    text = ' '.join(text.split()) if '\n' not in text else '\n'.join(' '.join(x.split()) for x in text.splitlines())
    return text[:limit] if limit else text


# Words a bidder slot holds when no company is named: kept, since they say what the line is
NOT_A_SUPPLIER = {'own allowance', 'own rate', 'provisional sum', 'provisional', 'prov sum', 'ps', 'tbc', 'tba',
                  'n/a', 'na', 'none', 'by others', 'by client', 'client', 'allowance', 'bidder'}


def supplier_names(result):
    """A pattern matching every supplier named in the workbook's bidder slots (longest first), or None."""
    names = set()
    for tr in result['trades']:
        for name in [*tr.get('suppliers', []), tr.get('supplier') or '']:
            name = ' '.join(str(name or '').split())
            if len(name) >= 3 and name.casefold() not in NOT_A_SUPPLIER and not name.casefold().startswith('bidder '):
                names.add(name)
    if not names:
        return None
    return re.compile('|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)


def available():
    """Whether AI drafting can be offered at all on this server (an API key is set)."""
    from ai import service
    return service.server_ready()


def page_state(request, project, pack):
    """What the Clarifications page shows about drafting: may this person start one, why not, the latest run."""
    from ai import service
    from ai.models import AIRun, CLARIFICATIONS

    from . import access
    if not access.can_edit(project):
        return None
    usable, reason = service.status(request.org, request.membership)
    run = AIRun.objects.filter(project=project, feature=CLARIFICATIONS).only(
        'status', 'message', 'created_at', 'finished_at').first()
    drafts = pack.all_clarifications().filter(source='DRAFT', approved=False).count() if pack else 0
    return {'usable': usable, 'reason': reason, 'run': run, 'drafts': drafts,
            'url': reverse('ai_draft_clarifications', args=[project.organization.slug, project.pk])}


def _money(v):
    return f'£{Decimal(v):,.0f}' if v is not None else ''


def build_input(pack):
    """(text for the model, {line key: "Trade row N"}) from the pack's latest revision. See the module notes."""
    from . import engine
    r = pack.workbook.result
    names = supplier_names(r)

    def text(v, limit):  # descriptions, refs, trade names, rules: no personal details or supplier names
        return scrub(v, limit, names=names)

    def note(v):  # comments, joinery notes, prelims basis: no money either
        return scrub(v, 200, names=names, money=True)

    refs, out, n = {}, [], 0
    out.append(f'Project: {text(pack.project.name, 200)}')
    out.append('\nWorkbook lines (key | trade | ref | description | quantity | status | comment):')
    for tr in r['trades']:
        if not tr['lines']:
            continue
        if not tr['include']:
            out.append(f'Trade not included in the price: {text(tr["name"], 80)}')
        for ln in tr['lines']:
            if not ln['description'] or n >= MAX_LINES:
                continue
            n += 1
            key = f'L{n}'
            refs[key] = f'{tr["name"]} row {ln["row"]}'
            qty = ln['qty_text'] or (f'{ln["qty"]}' if ln['qty'] is not None else '')
            status = engine.CLASS_LABELS[ln['cls']]
            if ln['cls'] in (engine.PROVISIONAL, engine.ALLOWANCE) and ln['charge'] is not None:
                status += f' {_money(ln["charge"])}'
            out.append(' | '.join([key, text(tr['name'], 80), text(ln['ref'], 20), text(ln['description'], MAX_DESC),
                                   text(f'{qty} {ln["unit"] or ""}'.strip(), 40), status, note(ln['comment'])]))
    for ln in r['joinery']['lines']:
        if not ln['description'] or n >= MAX_LINES:
            continue
        n += 1
        key = f'L{n}'
        refs[key] = f'Joinery row {ln["row"]}'
        qty = f'{ln["qty"]}' if ln['qty'] is not None else ''
        out.append(' | '.join([key, 'Joinery', text(ln['ref'], 20), text(ln['description'], MAX_DESC),
                               text(f'{qty} {ln["unit"] or ""}'.strip(), 40), engine.CLASS_LABELS[ln['cls']],
                               note(ln['notes'])]))
    for ln in r['prelims']['lines']:
        if not ln['description'] or n >= MAX_LINES:
            continue
        n += 1
        key = f'L{n}'
        refs[key] = f'Preliminaries row {ln["row"]}'
        out.append(' | '.join([key, 'Preliminaries', '', text(ln['description'], MAX_DESC),
                               text(f'{ln["qty"] if ln["qty"] is not None else ""} {ln["unit"] or ""}'.strip(), 40),
                               engine.CLASS_LABELS[ln['cls']], note(ln['basis'])]))
    gaps = [g for g in r['gaps'] if g['code'] not in COMMERCIAL_GAPS]
    if gaps:
        out.append('\nGaps Siteline found in the workbook:')
        for g in gaps[:80]:
            out.append(f'- {scrub(g["title"], 200, names=names, money=True)}: '
                       f'{scrub(g["detail"], 300, names=names, money=True)}')
    if pack.site_rules.strip():
        out.append('\nSite rules:\n' + text(pack.site_rules, MAX_RULES))
    existing = [c for c in pack.all_clarifications() if not (c.is_ai_draft and not c.approved and not c.edited)]
    if existing:
        out.append('\nExisting clarifications (don\'t repeat these):')
        for c in existing[:150]:
            out.append(f'- {text(c.trade, 80)}: {text(c.text, 400)}')
    return '\n'.join(out), refs


def save(pack, data, refs, user=None):
    """Validate the model's answer and save it as unapproved AI drafts. Returns how many were saved."""
    from .models import PackClarification
    items = data.get('clarifications') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('no clarifications list')
    # Drafts nobody has touched are replaced; edited or approved rows stay as they are
    pack.clarifications.filter(source=PackClarification.FROM_DRAFT, approved=False, edited=False).delete()
    seen = {' '.join(c.text.casefold().split()) for c in pack.clarifications.filter(hidden=False)}
    saved = 0
    for i, item in enumerate(items):
        if saved >= MAX_DRAFTS or not isinstance(item, dict):
            continue
        text = item.get('text')
        trade = item.get('trade')
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()[:MAX_TEXT]
        key = ' '.join(text.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        basis_refs = item.get('basis_line_refs') if isinstance(item.get('basis_line_refs'), list) else []
        basis = '; '.join(dict.fromkeys(refs[k] for k in basis_refs if isinstance(k, str) and k in refs))[:300]
        PackClarification.objects.create(
            organization=pack.organization, pack=pack, source=PackClarification.FROM_DRAFT, approved=False,
            trade=(trade if isinstance(trade, str) else '').strip()[:80], text=text, basis=basis,
            order=200000 + i)
        saved += 1
    return saved


def draft(pack, user, run=None):
    """Ask the model for drafts and save them (the worker calls this through ai/tasks.py). Returns the count."""
    from ai import service
    from ai.models import CLARIFICATIONS
    text, refs = build_input(pack)
    data = service.call(org=pack.organization, user=user, task=CLARIFICATIONS, project=pack.project, run=run,
                        content=text, chars=len(text), note=f'pricing workbook revision {pack.workbook.revision}')
    try:
        return save(pack, data, refs, user)
    except ValueError:
        raise service.CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
