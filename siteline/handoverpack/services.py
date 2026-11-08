"""
Uploading a revision, keeping the pack's clarifications and open items in step with it, handing it over
at the win, and issuing it to the delivery team.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from projects.models import Project

from . import codec, engine, reader
from .models import HandoverPack, OpenItem, PackClarification, PricingSettings, PricingWorkbook

PENNY = Decimal('0.01')
MAX_RECONCILE_ITEMS = 25
MAX_MONEY = Decimal('1e13')


def pennies(v):
    return None if v is None else Decimal(v).quantize(PENNY, ROUND_HALF_UP)


def settings_for(org):
    return PricingSettings.for_org(org)


def read(data, org):
    """Run the engine over a workbook's bytes with the company's mapping. Raises reader.WorkbookRefused."""
    from .mapping import MappingError
    s = settings_for(org)
    try:
        m = s.mapping()
    except MappingError as e:
        raise reader.WorkbookRefused(f'Your company\'s workbook layout can\'t be used: {e}')
    book = reader.open_book(data, engine.extents(m))
    suggested = _suggest(book, m)
    try:
        result = engine.run(book, m, (s.margin_tolerance or 0) / 100)
    except ValueError as e:  # not the pricing workbook (no summary sheet, or not this layout)
        if suggested:
            e = f'{e} It looks like the "{suggested["name"]}" layout: an owner can choose that under Settings > ' \
                'Pricing workbook.'
        raise reader.WorkbookRefused(str(e))
    result['suggested_layout'] = suggested
    if suggested:
        result['gaps'].append({
            'code': 'layout_mismatch', 'title': f'This looks like the "{suggested["name"]}" layout',
            'detail': f'Settings > Pricing workbook is set to "{m["name"]}". If the figures here look wrong, an owner '
                      'can change the layout there and the workbook can be uploaded again.',
            'sheet': '', 'cell': '', 'trade': '', 'amount': None, 'severity': 'info'})
    return result


def _suggest(book, m):
    """The built-in layout the file looks like, when it isn't the one in use: {key, name} or None."""
    from .mapping import PRESETS, detect
    summary = m['summary']['sheet']
    cells = [book.value(summary, r, c) for r in range(1, 13) for c in range(1, 12)]
    key = detect(book.sheetnames, cells)
    if key is None or key == m['key'] or key not in PRESETS:
        return None
    return {'key': key, 'name': PRESETS[key]['name']}


class Busy(reader.WorkbookRefused):
    """Someone else was saving to this project at the same moment."""


def _check_size(t):
    """Totals must fit the money columns (16 digits, 2 places): anything that big is a typo or an attack."""
    for key in ('cost', 'charge', 'profit', 'excel_charge'):
        v = t.get(key)
        if v is not None and abs(v) >= MAX_MONEY:
            raise reader.WorkbookRefused('That workbook\'s totals are far too large to be a real price. Check the '
                                         'quantities and rates, save, and upload it again.')


def upload(project, user, f):
    """Read the file, store it as the next revision, and bring the pack up to date. Returns the PricingWorkbook."""
    from django.db import IntegrityError, OperationalError
    ext = reader.check_name(getattr(f, 'name', ''))
    # An .xlsm is stripped of its macros and controls first; only that .xlsx copy is read and stored
    data, removed = reader.prepare(reader.read_bytes(f), ext)
    result = read(data, project.organization)
    if ext == '.xlsm':
        result['notes'].insert(0, f'Uploaded as a macro-enabled workbook (.xlsm). Its macros and controls ({removed} '
                                  'part' + ('s' if removed != 1 else '') + ') were removed before it was read, and only '
                                  'that copy, saved as .xlsx, is kept.')
    t = result['totals']
    _check_size(t)
    stored = []
    try:
        with transaction.atomic():
            Project.objects.select_for_update().filter(pk=project.pk).first()  # one upload or issue at a time
            pack = HandoverPack.objects.select_for_update().filter(project=project).first()
            if pack is not None and pack.is_issued:  # issued while this file was being read
                raise reader.WorkbookRefused('The pack has been issued, so the workbook can\'t change.')
            n = (PricingWorkbook.objects.filter(project=project).aggregate(n=Max('revision'))['n'] or 0) + 1
            wb = PricingWorkbook(
                organization=project.organization, project=project, revision=n,
                original_name=(reader.xlsx_name(getattr(f, 'name', '')) or 'workbook.xlsx')[-200:], size=len(data),
                sha256=reader.sha256(data), uploaded_by=user, mapping_key=result['mapping'],
                label=result['header'].get('revision', '')[:40], cost=pennies(t['cost']), charge=pennies(t['charge']),
                profit=pennies(t['profit']), excel_charge=pennies(t['excel_charge']),
                difference=pennies(t['difference']), reconciled=t['reconciled'], cached=result['cached'],
                issue_count=len(result['issues']), gap_count=len(result['gaps']), data=codec.encode(result))
            wb.file.save(_file_name(wb.original_name, n), ContentFile(data), save=False)
            stored.append(wb.file.name)
            wb.save()
            if pack is None:
                pack = HandoverPack.objects.create(project=project, workbook=wb)
            else:
                pack.workbook = wb
                pack.save(update_fields=['workbook', 'updated_at'])
            sync_clarifications(pack, result)
            sync_items(pack, wb, result)
            if project.stage not in Project.ESTIMATING_STAGES and pack.released_at is None:
                release(project, user)
    except BaseException as e:
        for name in stored:  # the row was rolled back: don't leave its file behind
            try:
                PricingWorkbook._meta.get_field('file').storage.delete(name)
            except Exception:  # nosec B110: best effort; an orphaned private file is harmless
                pass
        if isinstance(e, (IntegrityError, OperationalError)):  # SQLite/MySQL lock, or the revision number taken
            raise Busy('Someone else saved to this project at the same moment. Upload it again.') from e
        raise
    return wb


def _file_name(name, n):
    import os
    import re
    stem = re.sub(r'[^A-Za-z0-9 ._-]+', '', os.path.splitext(os.path.basename(name))[0]).strip()[:80]
    return f'{stem or "pricing-workbook"}-r{n}.xlsx'


def sync_clarifications(pack, result):
    """Workbook rows replace the ones from the last upload, except any someone has changed here."""
    existing = {c.workbook_row: c for c in pack.clarifications.filter(source=PackClarification.FROM_WORKBOOK)}
    statuses = {s.casefold(): s for s, _ in PackClarification.STATUS_CHOICES}
    seen = set()
    for i, row in enumerate(result['clarifications']):
        seen.add(row['row'])
        c = existing.get(row['row'])
        if c is not None and c.edited:
            continue
        c = c or PackClarification(pack=pack, source=PackClarification.FROM_WORKBOOK, workbook_row=row['row'])
        c.ref, c.trade, c.text = row['ref'][:20], row['trade'][:80], row['text'] or '(no text)'
        c.response, c.cost_effect, c.owner_text = row['response'], row['cost_effect'][:200], row['owner'][:120]
        c.status = statuses.get(row['status'].casefold(), PackClarification.OPEN)
        c.order = row['row']
        c.save()
    for r, c in existing.items():
        if r not in seen and not c.edited:
            c.delete()


def _where(sheet, cell):
    return f'{sheet}!{cell}' if sheet and cell else sheet


def item_rows(wb, result):
    """(key, fields) for every open item this revision raises: gaps, reconciliation issues, a total that's out."""
    rows = []
    t = result['totals']
    if t['reconciled'] is False:
        saved = f'{pennies(t["excel_charge"]):,}' if t['excel_charge'] is not None else \
            f'"{t.get("excel_text") or "blank"}"'
        same_total = t['difference'] is not None and abs(t['difference']) < Decimal('0.005')
        rows.append(('rec:total', {
            'kind': OpenItem.RECONCILE, 'code': 'total_differs', 'severity': 'bad', 'trade': '',
            'title': 'The workbook does not reconcile cell by cell' if same_total else
                     'Pack total does not match the pricing summary',
            'detail': ('The totals agree, but cells listed with the reconciliation (a stale copy of a supplier\'s '
                       'rates, or a total that isn\'t the sum of its rows) don\'t. Recalculate in Excel, correct the '
                       'workbook, save and upload it again.' if same_total else
                       f'Recalculated total charge {pennies(t["charge"]):,} against {saved} on the summary '
                       f'({t["charge_cell"]}). The pack can\'t be issued until they agree.'),
            'source': _where('PRICING SUMMARY', t['charge_cell']),
            'amount': pennies(abs(t['difference'])) if t['difference'] is not None and not same_total else None}))
    if t['reconciled'] is None and t.get('unchecked_reason'):
        rows.append(('rec:calc', {
            'kind': OpenItem.RECONCILE, 'code': 'not_calculated', 'severity': 'bad', 'trade': '',
            'title': 'The workbook\'s saved values may be out of date', 'detail': t['unchecked_reason'],
            'source': '', 'amount': None}))
    for issue in result['issues'][:MAX_RECONCILE_ITEMS]:
        saved = f'{pennies(issue["excel"]):,}' if issue['excel'] is not None else (issue['excel_text'] or 'blank')
        worked = f'{pennies(issue["computed"]):,}' if issue['computed'] is not None else 'blank'
        detail = issue['excel_text'] if issue['computed'] is None and issue['excel'] is None else \
            f'Excel saved {saved}; the inputs give {worked}.'
        rows.append((f'rec:{issue["sheet"]}!{issue["cell"]}', {
            'kind': OpenItem.RECONCILE, 'code': 'cell_differs', 'severity': 'bad', 'trade': '',
            'title': f'{issue["what"][:150]} does not reconcile', 'detail': detail,
            'source': _where(issue['sheet'], issue['cell']), 'amount': pennies(issue['difference']) or None}))
    more = len(result['issues']) - MAX_RECONCILE_ITEMS
    if more > 0:
        rows.append(('rec:more', {'kind': OpenItem.RECONCILE, 'code': 'cell_differs', 'severity': 'bad', 'trade': '',
                                  'title': f'{more} more cells do not reconcile',
                                  'detail': 'See the revision page for the full list.', 'source': '',
                                  'amount': None}))
    keys = set()
    for g in result['gaps']:
        if g['severity'] == 'info':
            continue
        key = f'gap:{g["code"]}:{g["trade"]}:{g["cell"]}'[:200]
        if key in keys:  # two gaps of a kind with no trade or cell (two missing tabs): tell them apart by sheet
            key = f'{key}:{g["sheet"]}'[:190]
            n = 2
            while key in keys:
                key, n = f'{key.rsplit("#", 1)[0]}#{n}', n + 1
        keys.add(key)
        rows.append((key, {
            'kind': OpenItem.GAP, 'code': g['code'], 'severity': g['severity'], 'trade': g['trade'][:80],
            'title': g['title'][:200], 'detail': g['detail'], 'source': _where(g['sheet'], g['cell'])[:120],
            'amount': pennies(g['amount'])}))
    return rows


def sync_items(pack, wb, result):
    """New items are added; items this revision no longer raises are closed; ones that come back reopen."""
    current = {i.key: i for i in OpenItem.objects.filter(pack=pack)}
    seen = set()
    for key, fields in item_rows(wb, result):
        seen.add(key)
        item = current.get(key)
        if item is None:
            item = OpenItem(pack=pack, project=pack.project, key=key)
            if pack.released_at:
                _defaults(item, pack.project)
        elif item.cleared_by_upload:
            item.status, item.cleared_by_upload, item.closed_at, item.closed_by = OpenItem.OPEN, False, None, None
        for k, v in fields.items():
            setattr(item, k, v)
        item.save()
    now = timezone.now()
    for key, item in current.items():
        if key not in seen and item.status == OpenItem.OPEN:
            item.status, item.cleared_by_upload, item.closed_at = OpenItem.DONE, True, now
            item.note = f'Cleared by revision {wb.revision}.'
            item.save()


def _defaults(item, project):
    s = settings_for(project.organization)
    if item.owner_id is None and s.assign_to_manager and project.manager_id:
        item.owner_id = project.manager_id
    if item.due_date is None:
        item.due_date = timezone.localdate() + timedelta(days=s.item_due_days)


def _fix_owners(pack, project):
    """Open items whose owner can't open the pack (say an estimator, once the job is won) go to the default."""
    from .access import possible_owners
    allowed = set(possible_owners(project).values_list('pk', flat=True))
    for item in OpenItem.objects.filter(pack=pack, status=OpenItem.OPEN, owner__isnull=False) \
            .exclude(owner_id__in=allowed):
        item.owner_id = None
        _defaults(item, project)
        if item.owner_id not in allowed:
            item.owner_id = None
        item.save(update_fields=['owner', 'due_date', 'updated_at'])


def release(project, user):
    """The bid is won (projects.views.project_handover): the pack reaches delivery. Safe to call twice."""
    from tenancy import notify
    pack = HandoverPack.objects.filter(project=project).select_related('workbook').first()
    if pack is None or pack.released_at is not None:
        return pack
    pack.released_at = timezone.now()
    pack.save(update_fields=['released_at', 'updated_at'])
    _fix_owners(pack, project)
    for item in OpenItem.objects.filter(pack=pack, status=OpenItem.OPEN):
        before = (item.owner_id, item.due_date)
        _defaults(item, project)
        if (item.owner_id, item.due_date) != before:
            item.save(update_fields=['owner', 'due_date', 'updated_at'])
    if project.manager_id:
        notify.alert([project.manager], f'Handover pack ready: {project.name}', pack.get_absolute_url(),
                     project=project, actor=user, org=project.organization)
    return pack


class CannotIssue(ValueError):
    pass


def why_not_issue(pack, wb, project):
    """The reason the pack can't be issued from this revision, or None."""
    if pack.is_issued:
        return 'It has already been issued.'
    if project.stage in Project.ESTIMATING_STAGES:
        return 'The pack is issued once the bid is won.'
    if wb.reconciled is True:
        return None
    if wb.reconciled is None:
        reason = (wb.result.get('totals') or {}).get('unchecked_reason')
        if reason:  # manual calculation: the saved values are suspect, whatever the setting for unchecked files
            return reason
        if settings_for(project.organization).unchecked_issue == PricingSettings.WARN:
            return None
        return ('The workbook has no values saved by Excel, so its total can\'t be checked against the pricing '
                'summary. Open it in Excel, save it, and upload it again.')
    if wb.difference is None:
        return 'The pricing summary\'s total isn\'t a number (an error or text in Excel). Correct the workbook and ' \
               'upload it again.'
    if abs(wb.difference) < Decimal('0.005'):
        return 'The totals agree, but the workbook doesn\'t reconcile cell by cell (see the reconciliation). ' \
               'Recalculate it in Excel, correct it, save, and upload it again.'
    return f'The pack total is {abs(wb.difference):,} out from the pricing summary. Correct the workbook and upload ' \
           'it again.'


def issue(pack, user):
    """Issue the pack from its current revision. Takes the project lock, so it can't cross an upload."""
    with transaction.atomic():
        Project.objects.select_for_update().filter(pk=pack.project_id).first()
        pack = HandoverPack.objects.select_for_update().select_related('project').get(pk=pack.pk)
        wb = PricingWorkbook.objects.get(pk=pack.workbook_id)
        reason = why_not_issue(pack, wb, pack.project)
        if reason:
            raise CannotIssue(reason)
        pack.issued_at, pack.issued_by, pack.issued_workbook = timezone.now(), user, wb
        pack.save(update_fields=['issued_at', 'issued_by', 'issued_workbook', 'updated_at'])
    return pack


def deltas(current, previous):
    """Trade totals against the previous revision: [{name, cost, charge, before_cost, before_charge, ...}]."""
    def parts(r):
        out = {t['name']: (t['cost'], t['charge']) for t in r['trades']}
        out['Joinery'] = (r['joinery']['cost'], r['joinery']['charge'])
        out['Preliminaries'] = (r['prelims']['cost'], r['prelims']['charge'])
        return out
    now, then = parts(current), parts(previous) if previous else {}
    rows = []
    for name in dict.fromkeys([*now, *then]):
        cost, charge = now.get(name, (Decimal(0), Decimal(0)))
        b_cost, b_charge = then.get(name, (Decimal(0), Decimal(0)))
        if not (cost or charge or b_cost or b_charge):
            continue
        rows.append({'name': name, 'cost': cost, 'charge': charge, 'before_cost': b_cost, 'before_charge': b_charge,
                     'cost_delta': cost - b_cost, 'charge_delta': charge - b_charge,
                     'changed': pennies(cost) != pennies(b_cost) or pennies(charge) != pennies(b_charge)})
    return rows


def item_url(item):
    return reverse('hpack_items', args=[item.project.organization.slug, item.project_id]) + f'#item-{item.pk}'
