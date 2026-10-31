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
        'below': bool                margin_pct under the threshold (or a cost with no final account)
        'has_costs': bool
    }

Forecast final cost per code: the latest manual forecast if one is set, otherwise the larger of
budget, committed and actual to date (costs.money.default_forecast). Variance = budget less forecast
(negative means the code is forecast to overspend).
"""
from dataclasses import dataclass, field
from decimal import Decimal

from contracts.commercial import commercial_position

from . import ledger
from .access import settings_for
from .models import CostCode, ForecastOverride
from .money import ZERO, default_forecast, margin


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
        if current is not None and current.amount is not None:
            row.override, row.forecast = current, current.amount
        else:
            row.forecast = default_forecast(row.budget, row.committed, row.actual)
        rows.append(row)
        for attr in ('budget', 'committed', 'actual', 'forecast'):
            setattr(total, attr, getattr(total, attr) + getattr(row, attr))
    position = commercial_position(project)
    final_account = position['forecast']
    amount, pct = margin(final_account, total.forecast)
    threshold = settings_for(project.organization).margin_threshold
    has_costs = bool(rows)
    below = has_costs and (pct is None or pct < threshold)
    return {'rows': rows, 'total': total, 'position': position, 'final_account': final_account,
            'final_cost': total.forecast, 'margin': amount, 'margin_pct': pct, 'threshold': threshold,
            'below': below, 'has_costs': has_costs}
