"""
The money and date rules for the monthly CVR, the cashflow forecast and dayworks.

Pure functions of numbers and dates: no database and no "now" (callers pass `today`). Each is held
to a fixed table in cvr/tests/test_rules.py, which is what the product owner signs off. Money is
Decimal only, rounded half up to 1p (costs.money.pennies refuses floats). Advisory: check your
contract.

CVR (cost value reconciliation) for a month, all "to date" figures as at the month's last day:

  value to date          gross valuation of the latest application for payment made up to a date in
                         or before the month (submitted, certified or paid; drafts and withdrawn
                         ones don't count): work done + approved variations + materials, before
                         retention (payments.rules.valuation)
  CVR value              value to date + this month's value adjustments
  cost to date           every actual cost on the cost ledger dated on or before the month's end
                         (invoices, timesheets, sub payments at gross before CIS, less reversals)
  CVR cost               cost to date + this month's cost accruals and adjustments
  margin to date         CVR value - CVR cost, and as a % of CVR value (to 0.1%, half up)
  forecast final account contract sum + client variations approved by the month's end
                         (payments.models.approved_variations_to) + this month's final account
                         adjustments
  forecast final cost    per cost code: the latest manual forecast set by the month's end, else the
                         larger of budget, committed and actual at the month's end
                         (costs.money.default_forecast); summed, + this month's final cost adjustments
  forecast margin        forecast final account - forecast final cost, and as a % of the final account
  movement               this month's figure - last month's (last month's signed-off figures if it
                         was signed off, otherwise as they stand now)
"""
import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from costs.money import ZERO, margin, pennies

ADVISORY = 'Advisory: check your contract.'
HUNDRED = Decimal('100')
TENTH = Decimal('0.1')


# ---------------------------------------------------------------------------
# Months
# ---------------------------------------------------------------------------

def month_start(day):
    return day.replace(day=1)


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day, months):
    """The first of the month `months` after the month `day` is in (negative goes back)."""
    m0 = day.month - 1 + months
    return date(day.year + m0 // 12, m0 % 12 + 1, 1)


def months_between(first, last):
    """First-of-month dates from first's month to last's month, both included ([] if last is earlier)."""
    out, m = [], month_start(first)
    while m <= month_start(last):
        out.append(m)
        m = add_months(m, 1)
    return out


def month_label(day):
    return f'{day:%B %Y}'


def has_ended(month, today):
    """A CVR month can be submitted once its last day has passed."""
    return month_end(month) < today


def due_on(month, due_day):
    """When a month's CVR is due: day `due_day` of the next month (above the month's length, its last day)."""
    nxt = add_months(month, 1)
    return nxt.replace(day=min(max(int(due_day or 1), 1), calendar.monthrange(nxt.year, nxt.month)[1]))


# ---------------------------------------------------------------------------
# The CVR
# ---------------------------------------------------------------------------

def percent_of(amount, of):
    """amount as a % of `of`, to 0.1% half up; None when `of` is nil or negative."""
    amount, of = pennies(amount), pennies(of)
    if of <= 0:
        return None
    return (amount * HUNDRED / of).quantize(TENTH, rounding=ROUND_HALF_UP)


def cvr(value_to_date, cost_to_date, final_account, final_cost, value_adj=ZERO, cost_adj=ZERO,
        final_account_adj=ZERO, final_cost_adj=ZERO):
    """
    The CVR's derived figures from its sources and adjustments. Returns a dict of Decimals (to 1p)
    and percentages (to 0.1%, or None): value, cost, margin, margin_pct, final_account, final_cost,
    forecast_margin, forecast_margin_pct.
    """
    value = pennies(value_to_date) + pennies(value_adj)
    cost = pennies(cost_to_date) + pennies(cost_adj)
    ffa = pennies(final_account) + pennies(final_account_adj)
    ffc = pennies(final_cost) + pennies(final_cost_adj)
    m, m_pct = margin(value, cost)
    fm, fm_pct = margin(ffa, ffc)
    return {'value': value, 'cost': cost, 'margin': m, 'margin_pct': m_pct, 'final_account': ffa,
            'final_cost': ffc, 'forecast_margin': fm, 'forecast_margin_pct': fm_pct}


def movement(this, previous):
    """This month less last month, to 1p; None when there is no last month to compare with."""
    if previous is None:
        return None
    return pennies(this) - pennies(previous)


def latest_valuation(applications, month_end_day):
    """
    Gross value to date: from (valuation_date, gross) pairs of counted applications, the gross of the
    latest one made up to a date on or before the month's end (the highest number on that date). Nil
    when there is none.
    """
    best = None
    for valuation_date, gross, number in applications:
        if valuation_date <= month_end_day and (best is None or (valuation_date, number) > best[:2]):
            best = (valuation_date, number, gross)
    return pennies(best[2]) if best else ZERO


# ---------------------------------------------------------------------------
# Spreading a sum over months (the cashflow forecast)
# ---------------------------------------------------------------------------

def spread(amount, weights):
    """
    Share `amount` across months in proportion to `weights` (non-negative numbers, one per month).
    Each share is rounded half up to 1p and the last month with any weight takes the pennies left, so
    the shares always add up to the amount exactly. All-zero weights share it equally.
    """
    amount = pennies(amount)
    n = len(weights)
    if n == 0:
        return []
    weights = [Decimal(w) if w > 0 else Decimal('0') for w in weights]
    total = sum(weights)
    if total == 0:
        weights, total = [Decimal('1')] * n, Decimal(n)
    last = max(i for i, w in enumerate(weights) if w > 0)
    shares = [pennies(amount * w / total) if i != last else ZERO for i, w in enumerate(weights)]
    shares[last] = amount - sum(shares)
    return shares


def programme_weights(months, activities, today):
    """
    How much work the programme has left in each month (in activity-days): each activity's
    unfinished share (100% less its progress) spread evenly over its days from today (or its start,
    if later) to its finish. Milestones carry no work. `activities` are (start, finish, progress %,
    is_milestone). Months before today's get nothing.
    """
    out = [Decimal('0')] * len(months)
    index = {m: i for i, m in enumerate(months)}
    for start, finish, progress, milestone in activities:
        if milestone or finish < today:
            continue
        left = (HUNDRED - min(Decimal(progress or 0), HUNDRED)) / HUNDRED
        if left <= 0:
            continue
        first = max(start, today)
        days = (finish - first).days + 1
        if days <= 0:
            continue
        per_day = left * Decimal((finish - start).days + 1) / days
        day = first
        while day <= finish:
            m = index.get(month_start(day))
            if m is not None:
                out[m] += per_day
            day += timedelta(days=1)
    return out


def even_weights(months, today, finish):
    """No programme: equal shares for each month from today's to the month the job finishes."""
    if finish is None:
        finish = today
    return [Decimal('1') if month_start(today) <= m <= month_start(max(finish, today)) else Decimal('0')
            for m in months]


def less_retention(amount, retention_percent):
    """What is paid on a valuation increment once retention is held back: amount x (100 - r%), to 1p."""
    if not retention_percent:
        return pennies(amount)
    return pennies(pennies(amount) * (HUNDRED - Decimal(retention_percent)) / HUNDRED)


def ex_vat_steps(cumulative):
    """
    Monthly receipts excluding VAT from the cumulative ex-VAT totals at each month's end: the
    difference month on month, so the months add up to the total exactly (no penny lost to rounding).
    """
    out, before = [], ZERO
    for c in cumulative:
        out.append(pennies(c) - before)
        before = pennies(c)
    return out


# ---------------------------------------------------------------------------
# Dayworks
# ---------------------------------------------------------------------------

LABOUR, PLANT, MATERIALS = 'LABOUR', 'PLANT', 'MATERIALS'
KINDS = (LABOUR, PLANT, MATERIALS)


def line_amount(quantity, unit_rate):
    """Hours or quantity x rate, half up to 1p."""
    return pennies(Decimal(quantity) * pennies(unit_rate))


def daywork_price(lines, percentages):
    """
    A daywork sheet priced at the contract's daywork rates: `lines` are (kind, quantity, unit rate);
    `percentages` {kind: % addition} (the contract's percentage additions on prime cost). Per kind:
    prime cost = the sum of its line amounts; addition = prime cost x % (half up to 1p).
    Returns {'kinds': {kind: (prime, pct, addition)}, 'prime': Decimal, 'additions': Decimal,
    'total': Decimal}.
    """
    kinds = {}
    for kind in KINDS:
        prime = sum((line_amount(q, r) for k, q, r in lines if k == kind), ZERO)
        pct = Decimal(percentages.get(kind) or 0)
        kinds[kind] = (prime, pct, pennies(prime * pct / HUNDRED))
    prime = sum((v[0] for v in kinds.values()), ZERO)
    additions = sum((v[2] for v in kinds.values()), ZERO)
    return {'kinds': kinds, 'prime': prime, 'additions': additions, 'total': prime + additions}
