"""
Quote mapping: one subcontractor's quote read, line by line, onto a levelling sheet's scope lines.

An estimator presses "Map with AI" on a subcontractor's column. That queues an AIRun; the worker (ai/tasks.py)
calls read(), which:

  - takes that one Package's quote file and nothing from any other bidder;
  - refuses a file the virus check hasn't cleared (or blocked), and anything over the size limit;
  - a PDF goes as a document, measured by Anthropic's token count (or, without one, the larger of the pages
    found and one page per 30 KB) against AI_TENDER_MAX_PAGES, like the tender reader;
  - a workbook (.xlsx, .xlsm) is rebuilt by core.ooxml.clean (macros, links and the rest removed) and read with
    openpyxl, values only, as "Sheet!B12: text" lines; a .csv is read as text the same way. At most
    MAX_SHEETS sheets, MAX_ROWS rows and MAX_COLS columns each, and MAX_CHARS in all;
  - sends with it our scope lines for the trade (key S1.., ref, description, quantity, unit) and the quote total
    typed on the package, if any. No other bidder's prices, no levelling, no names of our staff;
  - checks the answer: every scope_key must be one we sent (each used once), money must be 0 or more and under
    £10,000,000,000, texts are capped; then saves a QuoteMapDraft with its lines and exclusions.

Nothing is written to the levelling (QuoteCell, Exclusion) until an estimator accepts a line, an exclusion, or
all of them (accept_line, accept_exclusion, accept_all). The review page works out the tie to the quote total
itself, from the lines, rather than trusting the model's own claim.
"""
import csv
import io
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import QUOTE_MAPPING, QuoteMapDraft, QuoteMapExclusion, QuoteMapLine

MAX_SCOPE_LINES = 1000
MAX_SHEETS = 10
MAX_ROWS = 2000
MAX_COLS = 26
MAX_CHARS = 250_000
MAX_CELL = 300
MAX_PENCE = 10 ** 12  # £10,000,000,000
MAX_UNMATCHED = 100
MAX_EXCLUSIONS = 100
PDF, WORKBOOK, CSV = '.pdf', ('.xlsx', '.xlsm'), '.csv'


def kind_of(name):
    name = (name or '').lower()
    if name.endswith(PDF):
        return 'pdf'
    if name.endswith(WORKBOOK):
        return 'xlsx'
    if name.endswith(CSV):
        return 'csv'
    return None


def can_map(package):
    return bool(package.quote_file) and kind_of(package.quote_file.name) is not None


def _col(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_text(v):
    if v is None:
        return ''
    if isinstance(v, float):
        v = Decimal(repr(v))
    return ' '.join(str(v).replace('\x00', '').split())[:MAX_CELL]


def _rows_to_text(sheets):
    """[(sheet name, rows)] to "Sheet!B12: value" lines, capped at MAX_CHARS. Returns (text, cut)."""
    out, size, cut = [], 0, False
    for name, rows in sheets:
        out.append(f'--- Sheet: {_cell_text(name)[:60]} ---')
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row, start=1):
                t = _cell_text(v)
                if not t:
                    continue
                line = f'{_cell_text(name)[:60]}!{_col(c)}{r}: {t}'
                size += len(line) + 1
                if size > MAX_CHARS:
                    return '\n'.join(out), True
                out.append(line)
    return '\n'.join(out), cut


def workbook_text(data):
    """A workbook's values as cell-referenced text, read only from the cleaned rebuild. Raises ValueError."""
    from levelling import importer
    try:
        data = importer._check_zip(data)  # core.ooxml.clean: macros, links and controls gone, sizes capped
    except importer.WorkbookRefused as e:
        raise ValueError(str(e))
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_vba=False,
                                    keep_links=False)
    except Exception:  # noqa: BLE001 - any damaged workbook means the same to the estimator
        raise ValueError('That workbook couldn\'t be read. Save it as .xlsx from Excel and attach it again.')
    try:
        sheets = []
        for ws in wb.worksheets[:MAX_SHEETS]:
            sheets.append((ws.title, list(ws.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS,
                                                       values_only=True))))
    except Exception:  # noqa: BLE001
        raise ValueError('That workbook couldn\'t be read. Save it as .xlsx from Excel and attach it again.')
    finally:
        wb.close()
    return _rows_to_text(sheets)


def csv_text(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(text.replace('\x00', '')))):
        if i >= MAX_ROWS:
            break
        rows.append(row[:MAX_COLS])
    return _rows_to_text([('CSV', rows)])


def scope_text(lines):
    """(text, {key: ScopeLine}) for our scope lines, keyed S1, S2..."""
    keys, out = {}, ['Our scope lines (key | ref | description | quantity | unit):']
    for i, ln in enumerate(lines[:MAX_SCOPE_LINES], start=1):
        key = f'S{i}'
        keys[key] = ln
        qty = '' if ln.qty is None else f'{ln.qty.normalize():f}'
        out.append(' | '.join([key, _cell_text(ln.ref)[:40], _cell_text(ln.description)[:MAX_CELL], qty,
                               _cell_text(ln.unit)[:20]]))
    return '\n'.join(out), keys


def gather(package):
    """
    (content blocks, notes, size) for the package's quote file, or raises service.CallFailed with a plain
    message. size is {'tokens': n} for a PDF (counted or cautiously estimated) or {'chars': n} for a workbook.
    """
    from projects.files import scan_status
    from projects.models import FileScan

    from . import brief, service
    f = package.quote_file
    name = (f.name or '').rsplit('/', 1)[-1][:120]
    kind = kind_of(f.name) if f else None
    if kind is None:
        raise service.CallFailed('Only a PDF, Excel (.xlsx or .xlsm) or CSV quote can be mapped.')
    scan = scan_status(f, package.organization)
    if scan is None:
        raise service.CallFailed('The quote hasn\'t finished its virus check. Try again shortly.')
    if scan != FileScan.CLEAN:
        raise service.CallFailed('The quote was blocked by the virus check, so it wasn\'t read.')
    max_bytes = settings.AI_TENDER_MAX_MB * 1024 * 1024
    with f.open('rb') as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise service.CallFailed(f'The quote is over {settings.AI_TENDER_MAX_MB} MB, so it wasn\'t read.')
    notes = []
    if kind == 'pdf':
        if not data.startswith(b'%PDF'):
            raise service.CallFailed('The quote isn\'t a readable PDF.')
        counted = service.count_tokens([brief._block(data)])
        tokens = counted if counted is not None else brief.robust_pages(data) * service.TOKENS_PER_PAGE
        if tokens > settings.AI_TENDER_MAX_PAGES * service.TOKENS_PER_PAGE:
            raise service.CallFailed(f'The quote is longer than one reading allows (about '
                                     f'{settings.AI_TENDER_MAX_PAGES} pages). Attach just the priced pages.')
        return [{'type': 'text', 'text': f'The subcontractor\'s quote: {name}'}, brief._block(data)], notes, \
            {'tokens': tokens}
    try:
        text, cut = workbook_text(data) if kind == 'xlsx' else csv_text(data)
    except ValueError as e:
        raise service.CallFailed(str(e))
    if not text.strip():
        raise service.CallFailed('The quote workbook is empty. Ask for it to be saved again.')
    if cut:
        notes.append('The quote workbook is long: only its first part was read. Check the lines further down.')
    return [{'type': 'text', 'text': f'The subcontractor\'s quote ({name}), cell by cell:\n{text}'}], notes, \
        {'chars': len(text)}


def read(sheet, package, user, run=None):
    """Read the package's quote onto the sheet's lines and save a draft (the worker calls this)."""
    from . import service
    lines = list(sheet.lines.all())
    if not lines:
        raise service.CallFailed('The sheet has no scope lines to map onto. Add or import them first.')
    blocks, notes, size = gather(package)
    scope, keys = scope_text(lines)
    typed = (f'The quote total typed in Siteline: £{package.quote_value:,.2f}' if package.quote_value is not None
             else 'No quote total has been typed in Siteline.')
    content = [*blocks, {'type': 'text', 'text': f'{scope}\n\n{typed}\nMap the quote onto our lines.'}]
    system = service.system_for(sheet.organization, QUOTE_MAPPING)
    tokens = None
    if 'tokens' in size:
        tokens = size['tokens'] + service.input_estimate(chars=len(system) + len(scope) + 400)
    data = service.call(org=sheet.organization, user=user, task=QUOTE_MAPPING, project=sheet.project, run=run,
                        content=content, input_tokens=tokens, chars=size.get('chars', 0) + len(scope),
                        system=system, note=f'{len(keys)} scope lines, {"PDF" if "tokens" in size else "workbook"}')
    try:
        draft = save(sheet, package, data, keys, user, run=run,
                     source_label=(package.quote_file.name or '').rsplit('/', 1)[-1][:200])
    except ValueError:
        raise service.CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
    return draft, notes


def _pounds(pence):
    """Whole pence from the model to pounds, or None for "not given" (-1) or anything out of range."""
    if isinstance(pence, bool) or not isinstance(pence, int) or pence < 0 or pence >= MAX_PENCE:
        return None
    return (Decimal(pence) / 100).quantize(Decimal('0.01'))


def _signed_pounds(pence):
    if isinstance(pence, bool) or not isinstance(pence, int) or abs(pence) >= MAX_PENCE:
        return None
    return (Decimal(pence) / 100).quantize(Decimal('0.01'))


def _s(v, limit):
    return ' '.join(v.split())[:limit] if isinstance(v, str) else ''


def save(sheet, package, data, keys, user=None, run=None, source_label=''):
    """Validate the model's answer and save it as a draft. Earlier open drafts for this quote are discarded."""
    if not isinstance(data, dict) or not isinstance(data.get('lines'), list):
        raise ValueError('no lines')
    with transaction.atomic():
        QuoteMapDraft.objects.filter(sheet=sheet, package=package, status=QuoteMapDraft.OPEN) \
            .update(status=QuoteMapDraft.DISCARDED)
        unmatched = []
        for item in (data.get('unmatched_quote_items') or [])[:MAX_UNMATCHED]:
            if isinstance(item, dict) and _s(item.get('text'), 500):
                amount = _pounds(item.get('amount_pence'))
                unmatched.append({'text': _s(item.get('text'), 500), 'source': _s(item.get('source'), 200),
                                  'amount': str(amount) if amount is not None else ''})
        diff = _signed_pounds(data.get('difference_pence'))
        draft = QuoteMapDraft.objects.create(
            organization=sheet.organization, project=sheet.project, sheet=sheet, package=package, run=run,
            source_label=source_label, quote_total=_pounds(data.get('quote_total_pence')),
            total_source=_s(data.get('total_source'), 200),
            model_ties=data.get('ties_to_total') if isinstance(data.get('ties_to_total'), bool) else None,
            model_difference=diff, unmatched=unmatched, created_by=user)
        seen, rows = set(), []
        for item in data['lines']:
            if not isinstance(item, dict):
                continue
            key = item.get('scope_key')
            if not isinstance(key, str) or key not in keys or key in seen:
                continue  # only keys we sent, each once
            rate, amount = _pounds(item.get('rate_pence')), _pounds(item.get('amount_pence'))
            included = item.get('included_elsewhere') is True and rate is None and amount is None
            if rate is None and amount is None and not included:
                continue
            seen.add(key)
            rows.append(QuoteMapLine(organization=sheet.organization, draft=draft, line=keys[key], rate=rate,
                                     amount=amount, included=included, source=_s(item.get('source'), 200),
                                     note=_s(item.get('note'), 500)))
        QuoteMapLine.objects.bulk_create(rows)
        exclusions = []
        for item in (data.get('exclusions') or [])[:MAX_EXCLUSIONS]:
            text = _s(item.get('text'), 500) if isinstance(item, dict) else ''
            if text:
                exclusions.append(QuoteMapExclusion(organization=sheet.organization, draft=draft, text=text,
                                                    source=_s(item.get('source'), 200)))
        QuoteMapExclusion.objects.bulk_create(exclusions)
    return draft


def tie(draft, lines):
    """
    Siteline's own check of the mapping against the quote total: (mapped total, unmatched total, difference).
    difference is quote total less mapped and unmatched items; None when the quote gave no total.
    """
    from levelling import maths
    mapped = sum((maths.line_value(m.line.qty, m.rate, m.amount, m.included) or Decimal('0') for m in lines),
                 Decimal('0'))
    extra = sum((Decimal(u['amount']) for u in draft.unmatched if u.get('amount')), Decimal('0'))
    total = draft.quote_total if draft.quote_total is not None else draft.package.quote_value
    diff = None if total is None else maths.pennies(total - mapped - extra)
    return maths.pennies(mapped), maths.pennies(extra), diff, total


def _decide(obj, status, user):
    obj.status, obj.decided_by, obj.decided_at = status, user, timezone.now()
    obj.save(update_fields=['status', 'decided_by', 'decided_at'])


def accept_line(m, user, dropped=None):
    """Write one mapped line into the levelling as this subcontractor's price."""
    from levelling import services
    services.set_cell(m.line, m.draft.package, rate=m.rate, amount=m.amount, included=m.included, dropped=dropped)
    _decide(m, QuoteMapLine.ACCEPTED, user)


def accept_exclusion(x, user):
    """Add one exclusion to the levelling's register (not priced yet), unless the same words are already there."""
    from levelling.models import Exclusion
    from levelling import services
    d = x.draft
    if not Exclusion.objects.filter(sheet=d.sheet, package=d.package, text__iexact=x.text).exists():
        services.save_exclusion(Exclusion(sheet=d.sheet, package=d.package, text=x.text, status=Exclusion.OPEN,
                                          created_by=user), user)
    _decide(x, QuoteMapExclusion.ACCEPTED, user)


def reject(obj, user):
    _decide(obj, obj.REJECTED if hasattr(obj, 'REJECTED') else QuoteMapLine.REJECTED, user)


def accept_all(draft, user):
    """Every line and exclusion still to check. Returns (lines, exclusions, plugs dropped)."""
    dropped = []
    with transaction.atomic():
        lines = list(draft.lines.filter(status=QuoteMapLine.PENDING).select_related('line', 'draft__package'))
        for m in lines:
            accept_line(m, user, dropped)
        excs = list(draft.exclusions.filter(status=QuoteMapExclusion.PENDING).select_related('draft'))
        for x in excs:
            accept_exclusion(x, user)
        draft.status = QuoteMapDraft.DONE
        draft.save(update_fields=['status'])
    return len(lines), len(excs), dropped


def sheet_state(request, project, trade, sheet, packages, editable):
    """
    What the levelling sheet shows about AI mapping, per package with a quote file: may this person start one
    (and if not why), and the latest draft to review. Estimating staff only.
    """
    from django.urls import reverse

    from . import service
    m = request.membership
    if m is None or not m.is_internal or not m.can_estimate:
        return None
    usable, reason = service.status(request.org, m)
    latest = {}
    if sheet.pk:
        for d in QuoteMapDraft.objects.filter(sheet=sheet).exclude(status=QuoteMapDraft.DISCARDED).only(
                'package_id', 'status', 'created_at'):
            latest.setdefault(d.package_id, d)
    org = project.organization.slug
    rows = {}
    for p in packages:
        if not can_map(p) and p.pk not in latest:
            continue
        args = [org, project.pk, trade.pk, p.pk]
        rows[p.pk] = {'package': p, 'mappable': can_map(p), 'draft': latest.get(p.pk),
                      'start': reverse('ai_quote_map_start', args=args), 'review': reverse('ai_quote_map', args=args)}
    return {'usable': usable and editable and bool(sheet.pk), 'reason': reason, 'rows': rows}
