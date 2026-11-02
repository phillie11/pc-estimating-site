"""
The CVR's figures for a job and month, read from the modules that own them (cvr/rules.py says how
each is worked out):

    value, certified, paid, retention   payments (applications and payments received)
    cost, committed, budget              costs.ledger (LedgerEntry, dated on or before the month's end)
    contract sum and variations          the project and client variations approved by the month's end
                                         (as payments.models.approved_variations_to counts them)
    forecast final cost                  the cost ledger and manual forecasts, as costs.report does
    adjustments                          this app's CVRAdjustment rows for that month

    live_figures(projects, month, periods=None) -> {project_id: Figures}   fixed queries for any number
    figures_for(period)                  -> Figures   frozen ones once submitted, else live
    freeze(period, figures)              copy figures onto the period (on submission)

For a month that includes today, the live figures equal what each module shows itself
(payments.summary.certified_and_paid, costs.ledger.totals, costs.report.cost_report and
contracts.commercial.commercial_position): cvr/tests/test_reconcile.py holds them to that.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum

from costs.money import default_forecast, pennies

from . import rules

ZERO = Decimal('0.00')
SNAPSHOT_FIELDS = ('value_to_date', 'certified_to_date', 'paid_to_date', 'retention_held', 'cost_to_date',
                   'committed_to_date', 'budget_to_date', 'contract_sum', 'approved_variations', 'forecast_cost_base',
                   'value_adjustments', 'cost_adjustments', 'final_account_adjustments', 'final_cost_adjustments')
ADJ_FIELD = {'VALUE': 'value_adjustments', 'COST': 'cost_adjustments', 'FINAL_ACCOUNT': 'final_account_adjustments',
             'FINAL_COST': 'final_cost_adjustments'}


@dataclass
class Figures:
    project_id: int
    month: date
    value_to_date: Decimal = ZERO
    certified_to_date: Decimal = ZERO
    paid_to_date: Decimal = ZERO
    retention_held: Decimal = ZERO
    cost_to_date: Decimal = ZERO
    committed_to_date: Decimal = ZERO
    budget_to_date: Decimal = ZERO
    contract_sum: Decimal = ZERO
    approved_variations: Decimal = ZERO
    forecast_cost_base: Decimal = ZERO
    value_adjustments: Decimal = ZERO
    cost_adjustments: Decimal = ZERO
    final_account_adjustments: Decimal = ZERO
    final_cost_adjustments: Decimal = ZERO
    frozen: bool = False
    has_activity: bool = False  # any application, ledger entry or forecast by the month's end
    _derived: dict = field(default=None, repr=False)

    @property
    def month_end(self):
        return rules.month_end(self.month)

    @property
    def final_account_base(self):
        return self.contract_sum + self.approved_variations

    @property
    def derived(self):
        if self._derived is None:
            self._derived = rules.cvr(self.value_to_date, self.cost_to_date, self.final_account_base,
                                      self.forecast_cost_base, self.value_adjustments, self.cost_adjustments,
                                      self.final_account_adjustments, self.final_cost_adjustments)
        return self._derived

    def __getattr__(self, name):
        # value, cost, margin, margin_pct, final_account, final_cost, forecast_margin, forecast_margin_pct
        if name.startswith('_'):
            raise AttributeError(name)
        derived = self.derived
        if name in derived:
            return derived[name]
        raise AttributeError(name)

    def movement(self, previous):
        """{figure: this - last month} for the headline figures; None values with no last month."""
        keys = ('value', 'cost', 'margin', 'final_account', 'final_cost', 'forecast_margin')
        return {k: rules.movement(getattr(self, k), getattr(previous, k) if previous else None) for k in keys}


def _end_of(month):
    return rules.month_end(month)


def live_figures(projects, month, periods=None):
    """
    {project_id: Figures} worked out from the sources as at the month's end, for many jobs in a fixed
    number of queries. `periods` ({project_id: CVRPeriod}) adds each one's adjustments.
    """
    from costs.models import ForecastOverride, LedgerEntry
    from payments.models import Application, Payment
    from payments.rules import ex_vat
    from variations.models import Variation

    end = _end_of(month)
    projects = list(projects)
    ids = [p.pk for p in projects]
    out = {p.pk: Figures(project_id=p.pk, month=rules.month_start(month), contract_sum=pennies(p.value or 0))
           for p in projects}
    if not ids:
        return out

    # Payments: the latest counted application made up to the month's end, certified and paid to date
    apps = list(Application.objects.filter(project_id__in=ids, status__in=Application.COUNTED,
                                           valuation_date__lte=end))
    paid = dict(Payment.objects.filter(application__in=[a.pk for a in apps], reversed_at__isnull=True,
                                       received_on__lte=end).values('application_id')
                .annotate(s=Sum('amount')).values_list('application_id', 's')) if apps else {}
    latest = {}
    for a in apps:
        f = out[a.project_id]
        f.has_activity = True
        if a.is_certified:
            f.certified_to_date += a.notified_sum
        f.paid_to_date += ex_vat(paid.get(a.pk) or ZERO, a.vat_rate, a.reverse_charge)
        key = (a.valuation_date, a.number)
        if a.project_id not in latest or key > latest[a.project_id][0]:
            latest[a.project_id] = (key, a)
    for pid, (_, a) in latest.items():
        out[pid].value_to_date = pennies(a.gross_valuation)
        out[pid].retention_held = pennies(a.retention)

    # The cost ledger to the month's end, by code and kind (for the forecast final cost too)
    by_code = {}
    for row in LedgerEntry.objects.filter(project_id__in=ids, date__lte=end) \
            .values('project_id', 'cost_code_id', 'kind').annotate(s=Sum('amount')):
        by_code.setdefault(row['project_id'], {}).setdefault(row['cost_code_id'], {})[row['kind']] = row['s']
        f = out[row['project_id']]
        f.has_activity = True
        attr = {'ACTUAL': 'cost_to_date', 'COMMITMENT': 'committed_to_date', 'BUDGET': 'budget_to_date'}[row['kind']]
        setattr(f, attr, getattr(f, attr) + pennies(row['s']))
    overrides = {}
    for pid, code, amount in ForecastOverride.objects.filter(project_id__in=ids, created_at__date__lte=end) \
            .order_by('created_at', 'pk').values_list('project_id', 'cost_code_id', 'amount'):
        overrides[(pid, code)] = amount  # the latest by the month's end wins (None: back to the standard rule)
    for pid in ids:
        codes = set(by_code.get(pid, {})) | {c for (p, c) in overrides if p == pid}
        total = ZERO
        for code in codes:
            t = by_code.get(pid, {}).get(code, {})
            override = overrides.get((pid, code))
            total += override if override is not None else default_forecast(
                t.get('BUDGET') or ZERO, t.get('COMMITMENT') or ZERO, t.get('ACTUAL') or ZERO)
        out[pid].forecast_cost_base = pennies(total)
        if codes:
            out[pid].has_activity = True

    # Client variations approved by the month's end, as payments.models.approved_variations_to counts them
    approved = Variation.objects.filter(project_id__in=ids, status=Variation.STATUS_APPROVED).filter(
        Q(decided_at__date__lte=end) | Q(decided_at__isnull=True, updated_at__date__lte=end))
    for pid, s in approved.values('project_id').annotate(s=Sum('approved_value')).values_list('project_id', 's'):
        out[pid].approved_variations = pennies(s or 0)

    if periods:
        from .models import adjustment_totals
        by_period = adjustment_totals([p.pk for p in periods.values()])
        for pid, period in periods.items():
            if pid in out:
                for kind, total in by_period.get(period.pk, {}).items():
                    setattr(out[pid], ADJ_FIELD[kind], pennies(total))
    return out


def frozen_figures(period):
    f = Figures(project_id=period.project_id, month=period.month, frozen=True, has_activity=True)
    for name in SNAPSHOT_FIELDS:
        setattr(f, name, getattr(period, name) or ZERO)
    return f


def live_for(period, project=None):
    """The period's figures worked out now from the sources, with its adjustments (whatever its status)."""
    project = project or period.project
    return live_figures([project], period.month, {project.pk: period})[project.pk]


def figures_for(period, project=None):
    """The period's figures: frozen once submitted, otherwise live with its adjustments."""
    if period.is_frozen:
        return frozen_figures(period)
    return live_for(period, project)


def month_figures(projects, month, periods):
    """
    {project_id: Figures} for a month across jobs: a submitted or signed-off CVR's frozen figures,
    otherwise live ones with any draft CVR's adjustments. `periods` is {project_id: CVRPeriod}.
    """
    live_ids = [p for p in projects if not (periods.get(p.pk) and periods[p.pk].is_frozen)]
    out = live_figures(live_ids, month, {pid: per for pid, per in periods.items() if not per.is_frozen})
    for pid, per in periods.items():
        if per.is_frozen:
            out[pid] = frozen_figures(per)
    return out


def freeze(period, figures):
    for name in SNAPSHOT_FIELDS:
        setattr(period, name, getattr(figures, name))


def unfreeze(period):
    for name in SNAPSHOT_FIELDS:
        setattr(period, name, None)


def drift(period, project=None):
    """
    For a signed-off month: how the source figures for that month differ now from what was signed
    off (a ledger entry dated in the month but posted later, a reversed payment). Those changes show
    in the next open month's to-date figures; this says how much. {figure: difference}, non-zero only.
    """
    if not period.is_frozen:
        return {}
    project = project or period.project
    now = live_figures([project], period.month)[project.pk]
    out = {}
    then = frozen_figures(period)
    for name, label in (('value_to_date', 'value'), ('certified_to_date', 'certified'), ('paid_to_date', 'paid'),
                        ('cost_to_date', 'cost'), ('final_account_base', 'final account'),
                        ('forecast_cost_base', 'forecast final cost')):
        diff = getattr(now, name) - getattr(then, name)
        if diff:
            out[label] = diff
    return out
