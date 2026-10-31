"""
The levelling sheet as the page and the exports see it, and the changes made to it.

build(sheet) gathers the sheet's lines, each quoting subcontractor's cells, adjustments and
exclusions, and runs maths.level over them. Every write goes through a function here so the
rules (what can be plugged, who can be carried, exclusions mirrored as adjustments) live in one
place.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from directory.models import Package

from . import importer, maths
from .models import Adjustment, Exclusion, LevellingSheet, QuoteCell, ScopeLine

# Packages that can't be levelled: they never priced it
NOT_PRICING = (Package.DECLINED,)
# Who may be carried in the tender: still pricing, or already appointed
CARRYABLE = (Package.QUOTED, Package.APPOINTED)


def sheet_for(project, trade):
    """The saved sheet, or an unsaved one (a page view never writes)."""
    return LevellingSheet.objects.filter(project=project, trade=trade).first() \
        or LevellingSheet(project=project, trade=trade)


def ensure_saved(sheet):
    if sheet.pk is None:
        sheet, _ = LevellingSheet.objects.get_or_create(project=sheet.project, trade=sheet.trade)
    return sheet


def trade_packages(project, trade):
    return list(Package.objects.filter(project=project, trade=trade).exclude(status__in=NOT_PRICING)
                .select_related('subcontractor').order_by('subcontractor__company_name'))


@dataclass
class Cell:
    package: object
    cell: object = None
    value: object = None        # the line's money for this sub, None if missing
    missing: bool = False
    plug: object = None         # Adjustment plugging it
    plug_stale: bool = False    # plugged, but now priced
    plug_average: object = None
    plug_highest: object = None


@dataclass
class Line:
    line: object
    cells: list = field(default_factory=list)
    lowest: object = None


@dataclass
class Column:
    package: object
    lines_total: object = None
    priced_lines: int = 0
    missing: int = 0
    quoted: object = None
    quoted_from: str = ''       # 'quote' (their tender sum) or 'lines' (sum of their lines)
    adjustments: list = field(default_factory=list)
    exclusions: list = field(default_factory=list)
    result: object = None

    @property
    def unpriced_exclusions(self):
        return [e for e in self.exclusions if e.status == Exclusion.OPEN]

    @property
    def carryable(self):
        return self.package.status in CARRYABLE and self.quoted is not None

    @property
    def lines_differ(self):
        """Their lines don't add up to their quote (and none are missing): an arithmetic error to correct."""
        return (self.quoted_from == 'quote' and self.lines_total is not None and not self.missing
                and self.lines_total != self.quoted)

    @property
    def correction(self):
        return self.lines_total - self.quoted if self.lines_differ else None

    @property
    def levelled(self):
        return self.result.levelled if self.result else None

    @property
    def is_levelled(self):
        """Counts towards "levelled" on the coverage board: has priced at least one scope line."""
        return self.priced_lines > 0


@dataclass
class SheetView:
    sheet: object
    lines: list
    columns: list
    summary: object
    others: list            # packages in the trade that haven't quoted yet
    appointed: object = None

    @property
    def ranked(self):
        return sorted((c for c in self.columns if c.result and c.result.rank),
                      key=lambda c: (c.result.rank, c.package.subcontractor.company_name))

    @property
    def carried(self):
        pk = self.sheet.carried_id
        return next((c for c in self.columns if c.package.pk == pk), None) if pk else None

    @property
    def levelled_count(self):
        return sum(1 for c in self.columns if c.is_levelled)

    @property
    def unpriced_total(self):
        return sum(len(c.unpriced_exclusions) for c in self.columns)

    @property
    def missing_total(self):
        return sum(c.missing for c in self.columns)

    @property
    def reason_needed(self):
        """The carried subcontractor is no longer lowest levelled and there's no reason on file."""
        c = self.carried
        return bool(c and c.result and c.result.rank and not c.result.is_lowest
                    and not self.sheet.carried_reason.strip())

    def column(self, package_id):
        return next((c for c in self.columns if c.package.pk == package_id), None)


def build(sheet):
    packages = trade_packages(sheet.project, sheet.trade)
    lines = list(sheet.lines.all()) if sheet.pk else []
    cells, adjustments, exclusions = {}, {}, {}
    if sheet.pk:
        for c in QuoteCell.objects.filter(line__sheet=sheet):
            cells[(c.line_id, c.package_id)] = c
        for a in sheet.adjustments.select_related('line', 'exclusion'):
            adjustments.setdefault(a.package_id, []).append(a)
        for e in sheet.exclusions.all():
            exclusions.setdefault(e.package_id, []).append(e)
    has_cells = {pid for (_, pid), c in cells.items() if not c.is_blank}
    columns = [Column(package=p, adjustments=adjustments.get(p.pk, []), exclusions=exclusions.get(p.pk, []))
               for p in packages if p.quote_value is not None or p.pk in has_cells]
    others = [p for p in packages if p.quote_value is None and p.pk not in has_cells]

    rows = []
    for line in lines:
        row = Line(line=line)
        for col in columns:
            c = cells.get((line.pk, col.package.pk))
            value = maths.line_value(line.qty, c.rate, c.amount, c.included) if c else None
            row.cells.append(Cell(package=col.package, cell=c, value=value))
        priced = [x.value for x in row.cells if x.value is not None and not (x.cell and x.cell.included)]
        row.lowest = min(priced) if len(priced) > 1 else None
        for x in row.cells:
            plugs = [a for a in adjustments.get(x.package.pk, []) if a.kind == Adjustment.PLUG and a.line_id == line.pk]
            x.plug = plugs[0] if plugs else None
            x.missing = x.value is None and x.plug is None
            x.plug_stale = x.value is not None and x.plug is not None
            if x.value is None:
                rest = [y.value for y in row.cells if y is not x and y.value is not None
                        and not (y.cell and y.cell.included)]
                x.plug_average = maths.plug_value(rest, maths.AVERAGE)
                x.plug_highest = maths.plug_value(rest, maths.HIGHEST)
        rows.append(row)

    for i, col in enumerate(columns):
        values = [r.cells[i].value for r in rows if r.cells[i].value is not None]
        col.priced_lines = len(values)
        col.missing = sum(1 for r in rows if r.cells[i].missing)
        col.lines_total = maths.pennies(sum(values, maths.ZERO)) if values else None
        if col.package.quote_value is not None:
            col.quoted, col.quoted_from = maths.pennies(col.package.quote_value), 'quote'
        elif col.lines_total is not None:
            col.quoted, col.quoted_from = col.lines_total, 'lines'
    priced = {(r.line.pk, x.package.pk) for r in rows for x in r.cells if not maths.plug_counts(x.value)}
    summary = maths.level([(c.package.pk, c.quoted, [a.amount for a in c.adjustments
                                                     if not (a.kind == Adjustment.PLUG and (a.line_id, c.package.pk) in priced)])
                           for c in columns])
    for col in columns:
        col.result = summary.get(col.package.pk)
    appointed = next((p for p in packages if p.status == Package.APPOINTED), None)
    return SheetView(sheet=sheet, lines=rows, columns=columns, summary=summary, others=others, appointed=appointed)


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

class Refused(ValueError):
    """A change the rules don't allow; the message is for the estimator."""


MONEY_MAX = Decimal('1000000000000')   # DECIMAL(14, 2) holds less than 10^12


def check_money(value, what='That amount'):
    if value is not None and abs(value) >= MONEY_MAX:
        raise Refused(f'{what} is too large to record.')
    return value


def next_position(sheet):
    return (sheet.lines.aggregate(m=Max('position'))['m'] or 0) + 1


def add_line(sheet, ref, description, qty, unit):
    return ScopeLine.objects.create(sheet=sheet, position=next_position(sheet), ref=ref, description=description,
                                    qty=qty, unit=unit)


def set_cell(line, package, rate=None, amount=None, included=False, dropped=None):
    """
    Save one subcontractor's price for a line (or clear it). A plug on that line for them is removed
    once it is priced, so it is never counted on top of their price; each one removed is appended to
    `dropped` (if given) for the caller to log.
    """
    rate, amount = maths.pennies(rate), maths.pennies(amount)
    for v in (rate, amount):
        if v is not None and abs(v) >= MONEY_MAX:
            raise Refused('That price is too large.')
    if rate is None and amount is None and not included:
        QuoteCell.objects.filter(line=line, package=package).delete()
        return None
    cell, _ = QuoteCell.objects.update_or_create(line=line, package=package,
                                                 defaults={'rate': rate, 'amount': amount, 'included': included})
    for plug in Adjustment.objects.filter(sheet_id=line.sheet_id, package=package, kind=Adjustment.PLUG, line=line):
        if dropped is not None:
            dropped.append(plug)
        plug.delete()
    return cell


def add_adjustment(sheet, package, kind, amount, reason, actor):
    if kind == Adjustment.PLUG:
        raise Refused('Plug missing items from the sheet, so the line is recorded.')
    if kind == Adjustment.EXCLUSION:
        exc = Exclusion.objects.create(sheet=sheet, package=package, text=reason, status=Exclusion.PRICED,
                                       amount=amount, created_by=actor)
        return save_exclusion(exc, actor)
    return Adjustment.objects.create(sheet=sheet, package=package, kind=kind, amount=maths.pennies(amount),
                                     reason=reason, created_by=actor)


def save_exclusion(exc, actor):
    """An exclusion priced by us is mirrored by one EXCLUSION adjustment; any other status has none."""
    if exc.status != Exclusion.PRICED:
        exc.amount = None
    exc.save()
    adj = Adjustment.objects.filter(exclusion=exc).first()
    if exc.status == Exclusion.PRICED:
        if adj is None:
            adj = Adjustment(sheet=exc.sheet, package=exc.package, kind=Adjustment.EXCLUSION, exclusion=exc,
                             created_by=actor)
        adj.amount, adj.reason = maths.pennies(exc.amount), exc.text
        adj.save()
        return adj
    if adj is not None:
        adj.delete()
    return None


def split_exclusions(text):
    """A quote's free-text exclusions as separate lines: one per line, semicolon or sentence."""
    parts = re.split(r'[\r\n;]+|(?<=[a-z0-9)])\.\s+(?=[A-Z])', text or '')
    return [p.strip(' .-•*\t')[:500] for p in parts if p.strip(' .-•*\t')]


def copy_exclusions(sheet, package, actor):
    """Bring the subcontractor's own exclusions from their portal quote into the register, unpriced."""
    have = {e.text.lower() for e in sheet.exclusions.filter(package=package)}
    made = 0
    for text in split_exclusions(package.exclusions):
        if text.lower() not in have:
            Exclusion.objects.create(sheet=sheet, package=package, text=text, created_by=actor)
            have.add(text.lower())
            made += 1
    return made


def plug(sheet, line, package, basis, actor):
    """Plug a missing line for one subcontractor with the average or highest of the others, as an adjustment."""
    view = build(sheet)
    row = next((r for r in view.lines if r.line.pk == line.pk), None)
    cell = next((c for c in row.cells if c.package.pk == package.pk), None) if row else None
    if cell is None:
        raise Refused('That subcontractor has no quote on this sheet.')
    if not cell.missing:
        raise Refused('That line is already priced or plugged.')
    value = cell.plug_average if basis == Adjustment.AVERAGE else cell.plug_highest
    if value is None:
        raise Refused('Nobody else has priced that line, so there is nothing to plug it from. '
                      'Add an adjustment with your own figure instead.')
    check_money(value, 'That plug')
    label = dict(Adjustment.BASIS_CHOICES)[basis]
    return Adjustment.objects.create(
        sheet=sheet, package=package, kind=Adjustment.PLUG, line=line, basis=basis, amount=value,
        reason=f'{line.ref + " " if line.ref else ""}{line.description} not priced: plugged at the {label}',
        created_by=actor)


@transaction.atomic
def carry(sheet, package, reason, actor):
    """Mark one subcontractor as carried in our tender. A reason is needed unless they are lowest levelled."""
    view = build(sheet)
    col = view.column(package.pk)
    if col is None or not col.carryable:
        raise Refused('Only a subcontractor with a quote in (not declined or unsuccessful) can be carried.')
    if view.appointed and view.appointed.pk != package.pk:
        raise Refused(f'{view.appointed.subcontractor} is already appointed for {sheet.trade}.')
    reason = (reason or '').strip()
    if not col.result.is_lowest and not reason:
        raise Refused(f'{package.subcontractor} isn\'t the lowest levelled price. Say why you are carrying them.')
    sheet.carried, sheet.carried_reason = package, reason
    sheet.carried_by, sheet.carried_at = actor, timezone.now()
    sheet.save()
    return col


def uncarry(sheet):
    sheet.carried, sheet.carried_reason, sheet.carried_by, sheet.carried_at = None, '', None, None
    sheet.save()


@transaction.atomic
def import_scope(sheet, rows):
    """Add or update scope lines from imported rows. Returns (added, updated). Existing lines are never removed."""
    existing = list(sheet.lines.all())
    matches = importer.match_lines([(x.pk, x.ref, x.description) for x in existing], rows)
    by_pk = {x.pk: x for x in existing}
    added = updated = 0
    pos = next_position(sheet)
    for i, r in enumerate(rows):
        line = by_pk.get(matches.get(i))
        if line is None:
            ScopeLine.objects.create(sheet=sheet, position=pos, ref=r.ref, description=r.description, qty=r.qty,
                                     unit=r.unit)
            pos += 1
            added += 1
            continue
        changed = False
        for f in ('ref', 'description', 'unit', 'qty'):
            new = getattr(r, f)
            if new not in (None, '') and getattr(line, f) != new:
                setattr(line, f, new)
                changed = True
        if changed:
            line.save()
            updated += 1
    return added, updated


@dataclass
class PricedImport:
    filled: int = 0
    dropped: list = field(default_factory=list)     # plugs removed because the line is now priced
    unmatched: list = field(default_factory=list)   # their rows with no line of ours
    sums_differ: list = field(default_factory=list)  # qty x rate isn't their sum


@transaction.atomic
def import_priced(sheet, package, rows):
    """Fill one subcontractor's column from their priced copy of the scope, matched by ref then description."""
    lines = list(sheet.lines.all())
    matches = importer.match_lines([(x.pk, x.ref, x.description) for x in lines], rows)
    by_pk = {x.pk: x for x in lines}
    out = PricedImport()
    for i, r in enumerate(rows):
        line = by_pk.get(matches.get(i))
        if line is None:
            if r.priced:
                out.unmatched.append(r)
            continue
        if not r.priced:
            continue
        rate, total = maths.pennies(r.rate), maths.pennies(r.total)
        if total is not None and rate is not None and line.qty is not None \
                and maths.line_value(line.qty, rate) != total:
            out.sums_differ.append((line, total, maths.line_value(line.qty, rate)))
        set_cell(line, package, rate=rate, amount=total, included=r.included and rate is None and total is None,
                 dropped=out.dropped)
        out.filled += 1
    return out


# ---------------------------------------------------------------------------
# What other pages read
# ---------------------------------------------------------------------------

def carried_for(project):
    """{package id: levelled total} for every subcontractor carried in this bid's tender."""
    out = {}
    for sheet in LevellingSheet.objects.filter(project=project, carried__isnull=False).select_related('project', 'trade'):
        view = build(sheet)
        col = view.carried
        if col is not None:
            out[sheet.carried_id] = col.levelled
    return out


def levelled_totals(project):
    """
    {package id: levelled total} for every subcontractor with a levelled total on this project's
    sheets (quoted total plus the estimator's adjustments). A package with no sheet for its trade,
    or no column on it, is left out: the caller falls back to the quoted value.
    """
    out = {}
    for sheet in LevellingSheet.objects.filter(project=project).select_related('project', 'trade'):
        for col in build(sheet).columns:
            if col.levelled is not None:
                out[col.package.pk] = col.levelled
    return out


def reasons_needed(project):
    """Sheets on this bid whose carried subcontractor is no longer lowest levelled and has no reason."""
    out = []
    for sheet in LevellingSheet.objects.filter(project=project, carried__isnull=False, carried_reason='') \
            .select_related('project', 'trade', 'carried__subcontractor'):
        if build(sheet).reason_needed:
            out.append(sheet)
    return out


def levelled_counts(project_ids):
    """{(project id, trade id): subcontractors who have priced at least one scope line}, for the coverage board."""
    priced = QuoteCell.objects.filter(line__sheet__project_id__in=list(project_ids)) \
        .filter(Q(rate__isnull=False) | Q(amount__isnull=False) | Q(included=True)) \
        .exclude(package__status__in=NOT_PRICING) \
        .values_list('line__sheet__project_id', 'line__sheet__trade_id', 'package_id').distinct()
    out = {}
    for project_id, trade_id, _ in priced:
        out[(project_id, trade_id)] = out.get((project_id, trade_id), 0) + 1
    return out
