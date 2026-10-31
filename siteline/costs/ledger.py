"""
The cost ledger: the one place money movements on a job are recorded. Other apps (sub
payments, CIS, CVR) post here; the cost report reads only from here.

    from costs import ledger

    entry = ledger.post(project, cost_code, ledger.ACTUAL, Decimal('1250.00'),
                        source=payment, actor=request.user, note='Lund Joinery valuation 3')
    ledger.reverse(entry, actor=request.user, reason='Posted to the wrong job')
    ledger.totals(project)  # {cost_code_id: {'BUDGET': Decimal, 'COMMITMENT': Decimal, 'ACTUAL': Decimal}}

Rules
  * kind is ledger.BUDGET, ledger.COMMITMENT or ledger.ACTUAL.
  * amount is a Decimal, int or string (never a float), rounded half up to 1p. Negative amounts
    are allowed (a budget transfer out, a credit note).
  * source is any saved model row that caused the entry (stored as a generic link), or None.
  * project and cost_code must belong to the same company, the current one where a company is
    current; an archived project is refused (LedgerError).
  * Entries are never edited or deleted (LedgerEntry is append-only). reverse() adds an equal and
    opposite entry linked to the original; an entry can be reversed once, and a reversal can't be
    reversed (post a fresh entry instead).
  * Posting runs in the caller's transaction, so a failed request leaves no half-posted entries.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from tenancy.context import get_current_org

from .models import LedgerEntry
from .money import pennies

BUDGET, COMMITMENT, ACTUAL = LedgerEntry.BUDGET, LedgerEntry.COMMITMENT, LedgerEntry.ACTUAL
KINDS = (BUDGET, COMMITMENT, ACTUAL)


class LedgerError(ValueError):
    """A posting the ledger won't take: wrong company, archived job, unknown kind, already reversed."""


def post(project, cost_code, kind, amount, *, source=None, actor=None, note='', date=None):
    """Add one entry to the ledger and return it. See the module docstring for the rules."""
    if kind not in KINDS:
        raise LedgerError(f'Unknown ledger kind {kind!r}; use BUDGET, COMMITMENT or ACTUAL.')
    amount = pennies(amount)
    if project.organization_id != cost_code.organization_id:
        raise LedgerError('The cost code belongs to a different company from the job.')
    current = get_current_org()
    if current is not None and current.pk != project.organization_id:
        raise LedgerError('That job belongs to a different company.')
    if project.archived_at is not None:
        raise LedgerError('This job is archived, so its costs are read only. Restore it to post costs.')
    if source is not None and source.pk is None:
        raise LedgerError('Save the source record before posting it to the ledger.')
    entry = LedgerEntry(
        organization_id=project.organization_id, project=project, cost_code=cost_code, kind=kind, amount=amount,
        date=date or timezone.localdate(), actor=actor if getattr(actor, 'pk', None) else None, note=(note or '')[:255])
    if source is not None:
        entry.source_type = ContentType.objects.get_for_model(source)
        entry.source_id = source.pk
    entry.save()
    return entry


def reverse(entry, actor, reason):
    """Cancel an entry with an equal and opposite one dated today. Returns the reversing entry."""
    reason = (reason or '').strip()
    if not reason:
        raise LedgerError('Say why the entry is being reversed.')
    if entry.reverses_id is not None:
        raise LedgerError('This entry is itself a reversal. Post a new entry instead.')
    if entry.project.archived_at is not None:
        raise LedgerError('This job is archived, so its costs are read only. Restore it to post costs.')
    try:
        with transaction.atomic():
            rev = LedgerEntry(
                organization_id=entry.organization_id, project_id=entry.project_id, cost_code_id=entry.cost_code_id,
                kind=entry.kind, amount=-entry.amount, date=timezone.localdate(), source_type_id=entry.source_type_id,
                source_id=entry.source_id, reverses=entry, actor=actor if getattr(actor, 'pk', None) else None,
                note=f'Reversal: {reason}'[:255])
            rev.save()
    except IntegrityError:  # reverses is one-to-one: someone reversed it first
        raise LedgerError('This entry has already been reversed.')
    return rev


def entries(project):
    return LedgerEntry.objects.filter(project=project)


def totals(project):
    """{cost_code_id: {'BUDGET': Decimal, 'COMMITMENT': Decimal, 'ACTUAL': Decimal}} for one job."""
    out = {}
    for row in entries(project).values('cost_code', 'kind').annotate(total=Sum('amount')):
        out.setdefault(row['cost_code'], {k: Decimal('0.00') for k in KINDS})[row['kind']] = pennies(row['total'])
    return out


def source_total(source, kind=None):
    """Net ledger total for one source record (e.g. what an order has posted), optionally one kind."""
    qs = LedgerEntry.objects.filter(source_type=ContentType.objects.get_for_model(source), source_id=source.pk)
    if kind:
        qs = qs.filter(kind=kind)
    return pennies(qs.aggregate(s=Sum('amount'))['s'] or 0)
