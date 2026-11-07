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


def certified_at(app, day, today):
    """
    The application's notified sum if it was certified by `day` (a month's end), as payments.summary counts
    it today: a payment or pay less notice given, paid in full, or standing as notified by default. For a
    month that has ended, only notices and payment by its last day count, and the default only once its
    deadline had passed by then: a notice given next month belongs to next month's figures.
    """
    from payments import rules as pay_rules
    if day >= today:
        return app.notified_sum if app.is_certified else ZERO
    notice_on = app.payment_notice_on if app.payment_notice_on and app.payment_notice_on <= day else None
    less_on = app.pay_less_on if app.pay_less_on and app.pay_less_on <= day else None
    paid = app.paid_in_full_on is not None and app.paid_in_full_on <= day
    if not (notice_on or less_on or paid or pay_rules.stands_as_notified(day, app.payment_notice_by, notice_on)):
        return ZERO
    return pay_rules.notified_sum(app.amount_due, app.payment_notice_amount if notice_on else None, notice_on,
                                  app.payment_notice_by, app.pay_less_amount if less_on else None, less_on,
                                  app.pay_less_by)[0]


def live_figures(projects, month, periods=None):
    """
    {project_id: Figures} worked out from the sources as at the month's end, for many jobs in a fixed
    number of queries. `periods` ({project_id: CVRPeriod}) adds each one's adjustments.
    """
    return live_figures_months(projects, [month], {rules.month_start(month): periods or {}})[rules.month_start(month)]


def live_figures_months(projects, months, periods=None):
    """
    {month: {project_id: Figures}} for several months at once, in the same six or seven queries as one
    (each source is summed to every month's end with a conditional sum). `periods` is
    {month: {project_id: CVRPeriod}} for the adjustments.
    """
    from django.db.models.functions import TruncDate

    from costs.models import ForecastOverride, LedgerEntry
    from payments.models import Application, Payment
    from payments.rules import ex_vat
    from variations.models import Variation

    from django.utils import timezone
    today = timezone.localdate()
    months = sorted({rules.month_start(m) for m in months})
    ends = [_end_of(m) for m in months]
    last = max(ends)
    projects = list(projects)
    ids = [p.pk for p in projects]
    out = {m: {p.pk: Figures(project_id=p.pk, month=m, contract_sum=pennies(p.value or 0)) for p in projects}
           for m in months}
    if not ids:
        return out

    def by_month(field, condition=None):
        """Conditional sums of `field`, one per month's end, named m0, m1..."""
        return {f'm{i}': Sum(field, filter=(condition(e) if condition else Q())) for i, e in enumerate(ends)}

    # Payments: the latest counted application made up to each month's end, certified and paid to date
    apps = list(Application.objects.filter(project_id__in=ids, status__in=Application.COUNTED,
                                           valuation_date__lte=last))
    paid = {row['application_id']: row for row in Payment.objects.filter(
        application__in=[a.pk for a in apps], reversed_at__isnull=True).values('application_id')
        .annotate(**by_month('amount', lambda e: Q(received_on__lte=e)))} if apps else {}
    for i, (m, e) in enumerate(zip(months, ends)):
        latest = {}
        for a in apps:
            if a.valuation_date > e:
                continue
            f = out[m][a.project_id]
            f.has_activity = True
            f.certified_to_date += certified_at(a, e, today)
            f.paid_to_date += ex_vat((paid.get(a.pk) or {}).get(f'm{i}') or ZERO, a.vat_rate, a.reverse_charge)
            key = (a.valuation_date, a.number)
            if a.project_id not in latest or key > latest[a.project_id][0]:
                latest[a.project_id] = (key, a)
        for pid, (_, a) in latest.items():
            out[m][pid].value_to_date = pennies(a.gross_valuation)
            out[m][pid].retention_held = pennies(a.retention)

    # The cost ledger to each month's end, by code and kind (for the forecast final cost too)
    ledger_rows = list(LedgerEntry.objects.filter(project_id__in=ids, date__lte=last)
                       .values('project_id', 'cost_code_id', 'kind').annotate(**by_month('amount', lambda e: Q(date__lte=e))))
    override_rows = list(ForecastOverride.objects.filter(project_id__in=ids, created_at__date__lte=last)
                         .annotate(day=TruncDate('created_at')).order_by('created_at', 'pk')
                         .values_list('project_id', 'cost_code_id', 'amount', 'day'))
    attr = {'ACTUAL': 'cost_to_date', 'COMMITMENT': 'committed_to_date', 'BUDGET': 'budget_to_date'}
    for i, (m, e) in enumerate(zip(months, ends)):
        by_code = {}
        for row in ledger_rows:
            amount = row[f'm{i}']
            if amount is None:
                continue  # nothing on this code and kind by this month's end
            by_code.setdefault(row['project_id'], {}).setdefault(row['cost_code_id'], {})[row['kind']] = amount
            f = out[m][row['project_id']]
            f.has_activity = True
            setattr(f, attr[row['kind']], getattr(f, attr[row['kind']]) + pennies(amount))
        overrides = {}
        for pid, code, amount, day in override_rows:
            if day <= e:
                overrides[(pid, code)] = amount  # the latest by the month's end wins (None: back to the standard rule)
        for pid in ids:
            codes = set(by_code.get(pid, {})) | {c for (q, c) in overrides if q == pid}
            total = ZERO
            for code in codes:
                t = by_code.get(pid, {}).get(code, {})
                override = overrides.get((pid, code))
                total += override if override is not None else default_forecast(
                    t.get('BUDGET') or ZERO, t.get('COMMITMENT') or ZERO, t.get('ACTUAL') or ZERO)
            out[m][pid].forecast_cost_base = pennies(total)
            if codes:
                out[m][pid].has_activity = True

    # Client variations approved by each month's end, as payments.models.approved_variations_to counts them
    for row in Variation.objects.filter(project_id__in=ids, status=Variation.STATUS_APPROVED) \
            .values('project_id').annotate(**by_month('approved_value', lambda e: (
                Q(decided_at__date__lte=e) | Q(decided_at__isnull=True, updated_at__date__lte=e)))):
        for i, m in enumerate(months):
            out[m][row['project_id']].approved_variations = pennies(row[f'm{i}'] or 0)

    every = [per for by_pid in (periods or {}).values() for per in by_pid.values()]
    if every:
        from .models import adjustment_totals
        by_period = adjustment_totals([p.pk for p in every])
        for m, by_pid in periods.items():
            for pid, period in by_pid.items():
                if m in out and pid in out[m]:
                    for kind, total in by_period.get(period.pk, {}).items():
                        setattr(out[m][pid], ADJ_FIELD[kind], pennies(total))
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


def month_figures_pair(projects, month, periods, prev_month, prev_periods):
    """month_figures() for a month and the one before, with the live figures for both worked out together."""
    want = {}
    for m, by_pid in ((month, periods), (prev_month, prev_periods)):
        want[m] = {pid: per for pid, per in by_pid.items() if not per.is_frozen}
    live_ids = [p for p in projects if not all(by.get(p.pk) and by[p.pk].is_frozen for by in (periods, prev_periods))]
    live = live_figures_months(live_ids, [month, prev_month], want)
    out = []
    for m, by_pid in ((month, periods), (prev_month, prev_periods)):
        got = dict(live.get(m, {}))
        for pid, per in by_pid.items():
            if per.is_frozen:
                got[pid] = frozen_figures(per)
        out.append(got)
    return out


def freeze(period, figures):
    for name in SNAPSHOT_FIELDS:
        setattr(period, name, getattr(figures, name))


def unfreeze(period):
    for name in SNAPSHOT_FIELDS:
        setattr(period, name, None)


def drift(period, project=None, now=None):
    """
    For a signed-off month: how the source figures for that month differ now from what was signed
    off (a ledger entry dated in the month but posted later, a reversed payment). Those changes show
    in the next open month's to-date figures; this says how much. {figure: difference}, non-zero only.
    `now` is the month's live figures without adjustments, if the caller has already worked them out.
    """
    if not period.is_frozen:
        return {}
    project = project or period.project
    if now is None:
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
