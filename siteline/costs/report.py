"""
The cost report for one job, read only from the ledger (plus forecast overrides and the
commercial position), so its totals always reconcile to costs.ledger.totals().

    cost_report(project) -> {
        'rows': [Row, ...]          one per cost code with any ledger entry or forecast
        'total': Row                 sums of the rows
        'final_account': Decimal     forecast final account (contracts.commercial.commercial_position)
        'final_cost': Decimal        forecast final cost (= total.forecast)
        'margin': Decimal            final account less final cost, to 1p
        'margin_pct': Decimal|None   to 0.1%; None with no final account
        'threshold': Decimal         the company's margin warning %
        'below': bool                margin under the threshold, compared unrounded (or costs with no final account)
        'has_costs': bool
    }

Forecast final cost per code: the latest manual forecast if one is set, otherwise the larger of
budget, committed and actual to date (costs.money.default_forecast). Variance = budget less forecast
(negative means the code is forecast to overspend).
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from contracts.commercial import commercial_position

from . import ledger
from .access import settings_for
from .models import CostCode, ForecastOverride, LedgerEntry
from .money import HUNDRED, ZERO, default_forecast, margin, pennies


@dataclass
class Row:
    code: object = None
    budget: Decimal = ZERO
    committed: Decimal = ZERO
    actual: Decimal = ZERO
    forecast: Decimal = ZERO
    override: object = None  # the ForecastOverride in force, if any
    history: list = field(default_factory=list)

    @property
    def variance(self):
        return self.budget - self.forecast

    @property
    def overspent(self):
        return self.variance < 0

    @property
    def uncommitted(self):
        """Budget not yet ordered (negative when orders exceed the budget)."""
        return self.budget - self.committed


def latest_overrides(project):
    out = {}
    for ov in ForecastOverride.objects.filter(project=project).select_related('created_by').order_by('created_at', 'pk'):
        out.setdefault(ov.cost_code_id, []).append(ov)
    return out


def cost_report(project):
    totals = ledger.totals(project)
    overrides = latest_overrides(project)
    codes = CostCode.objects.filter(pk__in=set(totals) | set(overrides)).order_by('code')
    rows, total = [], Row()
    for code in codes:
        t = totals.get(code.pk, {})
        row = Row(code=code, budget=t.get(ledger.BUDGET, ZERO), committed=t.get(ledger.COMMITMENT, ZERO),
                  actual=t.get(ledger.ACTUAL, ZERO), history=overrides.get(code.pk, []))
        current = row.history[-1] if row.history else None
        row.forecast = forecast_for(row.budget, row.committed, row.actual, current.amount if current else None)
        if current is not None and current.amount is not None:
            row.override = current
        rows.append(row)
        for attr in ('budget', 'committed', 'actual', 'forecast'):
            setattr(total, attr, getattr(total, attr) + getattr(row, attr))
    position = commercial_position(project)
    final_account = position['forecast']
    amount, pct = margin(final_account, total.forecast)
    threshold = settings_for(project.organization).margin_threshold
    has_costs = bool(rows)
    below = has_costs and is_below(final_account, total.forecast, threshold)
    return {'rows': rows, 'total': total, 'position': position, 'final_account': final_account,
            'final_cost': total.forecast, 'margin': amount, 'margin_pct': pct, 'threshold': threshold,
            'below': below, 'has_costs': has_costs}


def forecast_for(budget, committed, actual, override_amount=None):
    """One code's forecast final cost: the latest manual forecast, else the standard one."""
    return override_amount if override_amount is not None else default_forecast(budget, committed, actual)


def is_below(final_account, final_cost, threshold):
    """
    Margin under the threshold, compared exactly (not on the rounded %): a 9.96% margin is under
    10% even though it shows as 10.0%. No final account at all counts as under.
    """
    final_account, final_cost = pennies(final_account), pennies(final_cost)
    if final_account <= 0:
        return True
    return (final_account - final_cost) * HUNDRED < pennies(threshold) * final_account


def forecast_costs(project_ids):
    """
    {project_id: forecast final cost} for many jobs in two queries (the grouped ledger and the
    forecasts), with the same rules as cost_report(). Jobs with no ledger entry or forecast are left out.
    """
    totals, latest = {}, {}
    for row in LedgerEntry.objects.filter(project_id__in=project_ids).values('project', 'cost_code', 'kind') \
            .annotate(total=Sum('amount')):
        totals.setdefault(row['project'], {}).setdefault(row['cost_code'], {})[row['kind']] = row['total']
    for p, code, amount in ForecastOverride.objects.filter(project_id__in=project_ids) \
            .order_by('created_at', 'pk').values_list('project', 'cost_code', 'amount'):
        latest[(p, code)] = amount
    out = {}
    for p in set(totals) | {p for p, _ in latest}:
        codes = set(totals.get(p, {})) | {c for q, c in latest if q == p}
        cost = ZERO
        for code in codes:
            t = totals.get(p, {}).get(code, {})
            cost += forecast_for(t.get(ledger.BUDGET, ZERO), t.get(ledger.COMMITMENT, ZERO),
                                 t.get(ledger.ACTUAL, ZERO), latest.get((p, code)))
        out[p] = pennies(cost)
    return out


def final_accounts(projects):
    """{project_id: forecast final account} as contracts.commercial.commercial_position works it out, in one query."""
    from variations.models import Variation
    approved = dict(Variation.objects.filter(project__in=[p.pk for p in projects], status=Variation.STATUS_APPROVED)
                    .values('project').annotate(s=Sum('approved_value')).values_list('project', 's'))
    return {p.pk: (p.value or Decimal('0')) + (approved.get(p.pk) or Decimal('0')) for p in projects}
