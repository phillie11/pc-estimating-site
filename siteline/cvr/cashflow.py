"""
A job's monthly cashflow: money in from the client and out on costs, actual to date and forecast
to the end of the job. All excluding VAT. The rules (also in cvr/rules.py and the rule table):

  Money in, actual      payments received from the client, excluding the VAT in them, in the month
                        received (reversed payments don't count). Adds up to "paid to date" in
                        payments exactly.
  Money out, actual     actual costs on the cost ledger, by the date they were posted (invoices,
                        timesheets, sub payments at gross before CIS, less reversals). Adds up to the
                        ledger's actual cost exactly.
  In, forecast          1. what the client still owes on applications already made: the notified sum
                           less what was paid, in the month of its final date for payment (this month
                           if that has passed);
                        2. the rest of the forecast final account (final account less the latest
                           application's gross value), spread over the months to the end of the
                           programme by the work the programme has left in each month (evenly to the
                           completion date if there is no programme), less the retention added on it
                           (retention on the cumulative gross after the month less that before it, so
                           the months add up exactly), received in the month of each month's final date
                           for payment under the contract's terms;
                        3. retention: the share released at practical completion, then the rest at the
                           end of the defects period, each received at the final date for payment
                           after it (advisory: check your contract). Retention is worked out as each
                           application does, on the cumulative gross at the stage each date is at, so
                           2. and 3. add up to the rest of the final account exactly.
  Out, forecast         1. what we still owe subcontractors on applications we have: what we pay
                           less what we have paid, in the month of its final date for payment;
                        2. the rest of the forecast final cost (less cost to date and 1.), spread like
                           2. above, paid at the final date for payment under the company's default
                           subcontract terms. Forecast payments are at full value (no subcontract
                           retention held back).
  Net and cumulative    in less out each month, and the running total from the first month.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth

from costs.money import pennies

from . import rules

ZERO = Decimal('0.00')
MAX_MONTHS_BACK, MAX_MONTHS_AHEAD = 36, 36


@dataclass
class Month:
    month: object
    in_actual: Decimal = ZERO
    in_forecast: Decimal = ZERO
    out_actual: Decimal = ZERO
    out_forecast: Decimal = ZERO
    cumulative: Decimal = ZERO
    is_past: bool = False
    is_current: bool = False

    @property
    def money_in(self):
        return self.in_actual + self.in_forecast

    @property
    def money_out(self):
        return self.out_actual + self.out_forecast

    @property
    def net(self):
        return self.money_in - self.money_out

    @property
    def label(self):
        return f'{self.month:%b %Y}'


def _month(d):
    return rules.month_start(d)


def _not_before(d, today):
    """A date already passed is expected this month (it is overdue)."""
    return d if d >= today else today


def build(project, today):
    from contracts.models import MainContract
    from costs.models import LedgerEntry
    from payments import rules as pay_rules
    from payments.models import Application, Payment
    from projects.models import Activity
    from subpayments.models import SubApplication
    from subpayments.models import settings_for as sub_settings
    from subpayments.rules import sub_cycle, sub_terms
    from costs.report import final_accounts, forecast_costs

    this_month = _month(today)
    month_end = rules.month_end(today)
    contract = MainContract.objects.filter(project=project).first()
    terms = pay_rules.terms_for(contract)
    # The same figures as the cost report and the commercial position (costs.report), two queries
    final_account = pennies(final_accounts([project])[project.pk])
    final_cost = forecast_costs([project.pk]).get(project.pk, ZERO)
    notes = []
    flows = {}  # month -> Month

    def at(month):
        return flows.setdefault(month, Month(month=month))

    # --- actual money in: receipts excluding VAT, stepped so the months add up to paid to date ----------
    apps = {a.pk: a for a in Application.objects.filter(project=project, status__in=Application.COUNTED)}
    by_app = {}
    for app_id, received_on, amount in Payment.objects.filter(application__in=list(apps), reversed_at__isnull=True) \
            .order_by('received_on', 'pk').values_list('application_id', 'received_on', 'amount'):
        by_app.setdefault(app_id, []).append((received_on, amount))
    for app_id, rows in by_app.items():
        a = apps[app_id]
        months = sorted({_month(d) for d, _ in rows})
        cum, steps = [], []
        for m in months:
            inc = sum((amt for d, amt in rows if _month(d) <= m), ZERO)
            cum.append(pay_rules.ex_vat(inc, a.vat_rate, a.reverse_charge))
        for m, step in zip(months, rules.ex_vat_steps(cum)):
            at(m).in_actual += step

    # --- actual money out: the cost ledger's actual costs by month --------------------------------------
    cost_to_date = ZERO
    for row in LedgerEntry.objects.filter(project=project, kind=LedgerEntry.ACTUAL).annotate(m=TruncMonth('date')) \
            .values('m').annotate(s=Sum('amount')).order_by('m'):
        at(_month(row['m'])).out_actual += pennies(row['s'])
        if _month(row['m']) <= this_month:
            cost_to_date += pennies(row['s'])
    counted = [a for a in apps.values() if a.valuation_date <= month_end]
    latest = max(counted, key=lambda a: (a.valuation_date, a.number), default=None)
    value_to_date = pennies(latest.gross_valuation) if latest else ZERO

    # --- forecast in: owed on applications already made --------------------------------------------------
    owed_in = ZERO
    for a in apps.values():
        if a.status not in Application.AWAITING_PAYMENT:
            continue
        a._paid_total = sum((amt for _, amt in by_app.get(a.pk, [])), ZERO)
        left = a.notified_sum - a.paid_ex_vat
        if left > 0 and a.final_date:
            at(_month(_not_before(a.final_date, today))).in_forecast += left
            owed_in += left

    # --- forecast out: owed on subcontractors' applications ---------------------------------------------
    owed_out = ZERO
    sub_apps = list(SubApplication.objects.filter(project=project, status__in=SubApplication.AWAITING).annotate(
        paid=Sum('payments__gross', filter=Q(payments__reversed_at__isnull=True))))
    for sa in sub_apps:
        sa._paid_gross = sa.paid or ZERO
        left = sa.payable - sa._paid_gross
        if left > 0 and sa.final_date:
            at(_month(_not_before(sa.final_date, today))).out_forecast += left
            owed_out += left

    # --- the rest of the job: spread by the programme -----------------------------------------------------
    activities = list(Activity.objects.filter(project=project).values_list('start', 'finish', 'progress',
                                                                         'is_milestone'))
    finish = max((f for _, f, _, _ in activities), default=None) or project.completion_date
    uses_programme = bool(activities) and any(not m for *_, m in activities)
    if finish is None:
        finish = rules.add_months(today, 6)
        notes.append('The job has no completion date or programme, so the rest is spread over the next six months. '
                     'Add a completion date or a programme to sharpen it.')
    last_applied = max((a.valuation_date for a in apps.values()), default=None)
    first_val = this_month if last_applied is None or _month(last_applied) < this_month \
        else rules.add_months(last_applied, 1)
    work_months = rules.months_between(first_val, max(finish, first_val))
    weights = rules.programme_weights(work_months, activities, today) if uses_programme else []
    if not weights or not any(weights):
        if uses_programme:
            notes.append('The programme has no work left after today, so the rest is spread evenly to completion.')
        weights = rules.even_weights(work_months, min(today, work_months[0]), finish)
    remaining_value = max(final_account - value_to_date, ZERO)
    retention_pct = (contract.retention_percent or ZERO) if contract else ZERO
    released_pct = contract.retention_released_at_pc_percent if contract else Decimal('50')
    val_day = (contract.valuation_day if contract and contract.valuation_day else 31)
    from contracts.presets import valuation_date_in

    pc = ((contract.practical_completion_on if contract else None) or project.completion_date or finish) \
        if retention_pct else None
    defects_months = contract.defects_period_months if contract else None
    end = pay_rules.defects_end(pc, defects_months)

    # Each future valuation and each retention release is worked out the way an application is: retention
    # held on the cumulative gross at the stage that date is at (before completion, after it, after the
    # defects period), and what is paid is the change in (gross less retention) since the last one. So the
    # retention taken off each month, released at completion and at the end of defects all come from one
    # running figure: nothing is lost to rounding month by month or counted twice, and once the defects
    # period ends the whole final account has come in.
    events = [(valuation_date_in(m.year, m.month, val_day), v) for m, v in
              zip(work_months, rules.spread(remaining_value, weights))]
    if pc is not None and final_account > 0:
        events.append((pc, None))
        if end is not None:
            events.append((end, None))
    gross = value_to_date
    held = pennies(latest.retention) if latest else ZERO
    for when, v in sorted(events, key=lambda e: (e[0], e[1] is None)):
        gross += v or ZERO
        now_held = pay_rules.retention_held(gross, retention_pct, released_pct,
                                            pay_rules.retention_stage(when, pc, defects_months))
        net = (v or ZERO) - (now_held - held)
        held = now_held
        if not net:
            continue
        paid_on = pay_rules.payment_cycle(when, terms).final_date
        at(_month(_not_before(paid_on, today))).in_forecast += net
    if retention_pct and final_account > 0 and end is None:
        notes.append('No defects period is set on the contract, so the last of the retention is not forecast.')

    remaining_cost = max(final_cost - cost_to_date - owed_out, ZERO)
    defaults = sub_settings(project.organization, create=False)
    sterms = sub_terms(defaults.due_date_offset_days, defaults.payment_notice_days, defaults.final_date_offset_days,
                       defaults.pay_less_notice_days, contract.bank_holiday_region if contract else 'EW')
    sub_day = defaults.valuation_day or 25
    for m, v in zip(work_months, rules.spread(remaining_cost, weights)):
        if not v:
            continue
        paid_on = sub_cycle(valuation_date_in(m.year, m.month, sub_day), sterms).final_date
        at(_month(_not_before(paid_on, today))).out_forecast += v

    # --- the months, in order, capped either side --------------------------------------------------------
    first = max(min(list(flows) + [this_month, _month(project.start_date or today)]),
                rules.add_months(this_month, -MAX_MONTHS_BACK))
    last = min(max(list(flows) + [this_month]), rules.add_months(this_month, MAX_MONTHS_AHEAD))
    out, running = [], ZERO
    for m in rules.months_between(first, last):
        row = flows.get(m, Month(month=m))
        row.is_past, row.is_current = m < this_month, m == this_month
        running += row.net
        row.cumulative = running
        out.append(row)
    beyond = [f for k, f in flows.items() if k > last or k < first]
    if beyond:
        notes.append('Some money falls outside the three years either side of this month and is not shown.')
    totals = {
        'in_actual': sum((r.in_actual for r in out), ZERO), 'in_forecast': sum((r.in_forecast for r in out), ZERO),
        'out_actual': sum((r.out_actual for r in out), ZERO), 'out_forecast': sum((r.out_forecast for r in out), ZERO),
    }
    totals['in'] = totals['in_actual'] + totals['in_forecast']
    totals['out'] = totals['out_actual'] + totals['out_forecast']
    totals['net'] = totals['in'] - totals['out']
    return {'months': out, 'totals': totals, 'notes': notes, 'finish': finish, 'uses_programme': uses_programme,
            'final_account': final_account, 'final_cost': final_cost, 'owed_in': owed_in, 'owed_out': owed_out, 'remaining_value': remaining_value,
            'remaining_cost': remaining_cost, 'retention_pct': retention_pct, 'contract': contract,
            'lowest': min((r.cumulative for r in out), default=ZERO)}
