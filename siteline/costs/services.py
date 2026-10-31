"""
Everything that changes money on a job, each in one transaction with its ledger entries.
Views call these; nothing else writes cost rows.

    set_original_budget(project, actor, basis_value, margin_pct, lines)    lines: [(cost_code, description, amount)]
    transfer_budget(project, actor, from_code, to_code, amount, reason)
    revise_budget(project, actor, cost_code, amount, reason, description='')
    commit_package(package, cost_code, actor)
    place_order(project, actor, cost_code, supplier, value, reference='', description='', order_date=None)
    adjust_commitment(commitment, actor, amount, reason, sub_variation=None)
    add_sub_variation(commitment, sub_variation, actor)
    record_actual(project, actor, cost_code, kind, supplier, amount, ...)
    set_forecast(project, actor, cost_code, amount_or_None, reason)
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Max

from . import ledger
from .models import ActualCost, BudgetChange, BudgetLine, Commitment, CommitmentAdjustment, ForecastOverride
from .money import pennies


class CostError(ValueError):
    """Something the user asked for that the cost rules don't allow; show it as a message."""


class AlreadyRecorded(CostError):
    """The same form was sent twice (a double click or a resubmit); .record is what the first one made."""

    def __init__(self, record):
        self.record = record
        super().__init__('That was already recorded, so it was not added again.')


def _lock(project):
    """Serialise numbering and the one-original-budget rule per job (a no-op on SQLite)."""
    from projects.models import Project
    return Project.objects.select_for_update().get(pk=project.pk)


def _next(model, project):
    return (model.objects.filter(project=project).aggregate(m=Max('number'))['m'] or 0) + 1


def has_budget(project):
    return BudgetChange.objects.filter(project=project, kind=BudgetChange.ORIGINAL).exists()


def _check_code(project, code):
    if project.archived_at is not None:
        raise CostError('This job is archived, so its costs are read only. Restore it to make changes.')
    if code.organization_id != project.organization_id:
        raise CostError('That cost code belongs to another company.')


def _post(*args, **kwargs):
    """ledger.post(), with its refusals turned into CostError so views show them as messages."""
    try:
        return ledger.post(*args, **kwargs)
    except ledger.LedgerError as e:
        raise CostError(str(e))


# --- Budget -----------------------------------------------------------------------------

@transaction.atomic
def set_original_budget(project, actor, basis_value, margin_pct, lines, reason=''):
    _lock(project)
    if has_budget(project):
        raise CostError('This job already has a budget. Change it with a budget transfer or revision.')
    lines = [(code, desc, pennies(amount)) for code, desc, amount in lines if pennies(amount) != 0]
    if not lines:
        raise CostError('Put an amount against at least one cost code.')
    change = BudgetChange.objects.create(project=project, number=_next(BudgetChange, project),
                                         kind=BudgetChange.ORIGINAL, basis_value=pennies(basis_value),
                                         margin_pct=pennies(margin_pct), reason=reason, created_by=actor)
    for code, desc, amount in lines:
        _budget_line(change, code, desc, amount, actor, f'Original budget: {desc or code.name}')
    return change


def _budget_line(change, code, desc, amount, actor, note):
    _check_code(change.project, code)
    line = BudgetLine.objects.create(change=change, project=change.project, cost_code=code,
                                     description=desc or code.name, amount=pennies(amount))
    _post(change.project, code, ledger.BUDGET, line.amount, source=line, actor=actor, note=note)
    return line


@transaction.atomic
def transfer_budget(project, actor, from_code, to_code, amount, reason):
    """Move budget between codes: the job's total budget doesn't change."""
    _lock(project)
    amount = pennies(amount)
    if not has_budget(project):
        raise CostError('Set the budget first.')
    if from_code.pk == to_code.pk:
        raise CostError('Pick two different cost codes.')
    if amount <= 0:
        raise CostError('Transfer a positive amount.')
    if not (reason or '').strip():
        raise CostError('Give a reason for the transfer.')
    change = BudgetChange.objects.create(project=project, number=_next(BudgetChange, project),
                                         kind=BudgetChange.TRANSFER, reason=reason, created_by=actor)
    _budget_line(change, from_code, f'Transfer to {to_code.code} {to_code.name}', -amount, actor,
                 f'{change.reference} to {to_code.code}: {reason}')
    _budget_line(change, to_code, f'Transfer from {from_code.code} {from_code.name}', amount, actor,
                 f'{change.reference} from {from_code.code}: {reason}')
    return change


@transaction.atomic
def revise_budget(project, actor, cost_code, amount, reason, description=''):
    """Add to (or take from, with a negative amount) one code's budget, changing the job's total budget."""
    _lock(project)
    amount = pennies(amount)
    if not has_budget(project):
        raise CostError('Set the budget first.')
    if amount == 0:
        raise CostError('A revision needs an amount (negative to reduce the budget).')
    if not (reason or '').strip():
        raise CostError('Give a reason for the revision.')
    change = BudgetChange.objects.create(project=project, number=_next(BudgetChange, project),
                                         kind=BudgetChange.REVISION, reason=reason, created_by=actor)
    _budget_line(change, cost_code, description or f'Revision: {reason}'[:200], amount, actor,
                 f'{change.reference}: {reason}')
    return change


# --- Commitments ------------------------------------------------------------------------

@transaction.atomic
def commit_package(package, cost_code, actor):
    """'Commit this order': an appointed package becomes a subcontract order at its quoted value."""
    from directory.models import Package
    project = _lock(package.project)
    package = Package.objects.select_related('subcontractor', 'trade').get(pk=package.pk)
    if package.status != Package.APPOINTED:
        raise CostError('Only an appointed package can be committed.')
    if Commitment.objects.filter(package=package).exists():
        raise CostError('This package is already committed.')
    if not package.quote_value:
        raise CostError('This package has no quoted value to commit.')
    _check_code(project, cost_code)
    c = Commitment.objects.create(
        project=project, number=_next(Commitment, project), kind=Commitment.SUBCONTRACT, cost_code=cost_code,
        package=package, subcontractor=package.subcontractor, supplier=package.subcontractor.company_name,
        description=f'{package.trade or "Package"} subcontract', original_value=pennies(package.quote_value),
        created_by=actor)
    _post(project, cost_code, ledger.COMMITMENT, c.original_value, source=c, actor=actor,
                note=f'{c.ref} {c.supplier}: {c.description}')
    return c


@transaction.atomic
def place_order(project, actor, cost_code, supplier, value, reference='', description='', order_date=None,
                form_key=None):
    project = _lock(project)
    if form_key:
        first = Commitment.objects.filter(project=project, form_key=form_key).first()
        if first is not None:
            raise AlreadyRecorded(first)
    _check_code(project, cost_code)
    value = pennies(value)
    if value <= 0:
        raise CostError('An order needs a value above zero.')
    kwargs = {'order_date': order_date} if order_date else {}
    c = Commitment.objects.create(project=project, number=_next(Commitment, project), kind=Commitment.ORDER,
                                  cost_code=cost_code, supplier=supplier, reference=reference,
                                  description=description, original_value=value, created_by=actor,
                                  form_key=form_key or None, **kwargs)
    _post(project, cost_code, ledger.COMMITMENT, value, source=c, actor=actor, date=c.order_date,
                note=f'{c.ref} {supplier}: {description}'.rstrip(': '))
    return c


@transaction.atomic
def adjust_commitment(commitment, actor, amount, reason, sub_variation=None, form_key=None):
    # Lock the order so two adjustments at once can't both pass the not-below-zero check (or the same form twice)
    commitment = Commitment.objects.select_for_update().select_related('project', 'cost_code').get(pk=commitment.pk)
    if form_key:
        first = CommitmentAdjustment.objects.filter(commitment=commitment, form_key=form_key).first()
        if first is not None:
            raise AlreadyRecorded(first)
    _check_code(commitment.project, commitment.cost_code)
    amount = pennies(amount)
    if amount == 0:
        raise CostError('An adjustment needs an amount (negative to reduce the order).')
    if not (reason or '').strip():
        raise CostError('Give a reason for the adjustment.')
    if commitment.value + amount < 0:
        raise CostError('That would take the order below zero.')
    adj = CommitmentAdjustment.objects.create(commitment=commitment, project=commitment.project, amount=amount,
                                              reason=reason[:300], sub_variation=sub_variation, created_by=actor,
                                              form_key=form_key or None)
    _post(commitment.project, commitment.cost_code, ledger.COMMITMENT, amount, source=adj, actor=actor,
                note=f'{commitment.ref} adjusted: {reason}')
    return adj


def addable_sub_variations(commitment):
    """Approved sub variations on this order's package not yet added to it."""
    from delivery.models import SubVariation
    if commitment.package_id is None:
        return SubVariation.objects.none()
    return SubVariation.objects.filter(package_id=commitment.package_id, status=SubVariation.APPROVED,
                                       approved_value__isnull=False, cost_adjustment__isnull=True)


@transaction.atomic
def add_sub_variation(commitment, sub_variation, actor):
    from delivery.models import SubVariation
    sv = SubVariation.objects.select_for_update().get(pk=sub_variation.pk)
    if sv.package_id != commitment.package_id or commitment.package_id is None:
        raise CostError('That sub variation is on a different package.')
    if sv.status != SubVariation.APPROVED or sv.approved_value is None:
        raise CostError('Only an approved sub variation can be added to the order.')
    if CommitmentAdjustment.objects.filter(sub_variation=sv).exists():
        raise CostError(f'{sv.reference} is already on the order.')
    return adjust_commitment(commitment, actor, sv.approved_value, f'{sv.reference} {sv.title}', sub_variation=sv)


# --- Actuals and forecasts --------------------------------------------------------------

@transaction.atomic
def record_actual(project, actor, cost_code, kind, supplier, amount, reference='', cost_date=None, description='',
                  commitment=None, file=None):
    _check_code(project, cost_code)
    amount = pennies(amount)
    if amount == 0:
        raise CostError('Enter the amount (negative for a credit note).')
    if commitment is not None and commitment.project_id != project.pk:
        raise CostError('That order is on a different job.')
    a = ActualCost(project=project, cost_code=cost_code, kind=kind, supplier=supplier, amount=amount,
                   reference=reference, description=description, commitment=commitment, created_by=actor)
    if cost_date:
        a.cost_date = cost_date
    if file:
        a.file = file
        a.original_name = file.name[-200:]
    a.save()
    label = f'{a.get_kind_display()} {supplier}' + (f' {reference}' if reference else '')
    _post(project, cost_code, ledger.ACTUAL, amount, source=a, actor=actor, date=a.cost_date, note=label)
    return a


@transaction.atomic
def set_forecast(project, actor, cost_code, amount, reason):
    _check_code(project, cost_code)
    if not (reason or '').strip():
        raise CostError('Give a reason for the forecast.')
    return ForecastOverride.objects.create(project=project, cost_code=cost_code,
                                           amount=None if amount in (None, '') else pennies(amount),
                                           reason=reason[:300], created_by=actor)


def committed_value(commitment):
    """What the ledger holds for an order and its adjustments (equals commitment.value unless reversed)."""
    total = ledger.source_total(commitment, ledger.COMMITMENT)
    for adj in commitment.adjustments.all():
        total += ledger.source_total(adj, ledger.COMMITMENT)
    return total or Decimal('0.00')
