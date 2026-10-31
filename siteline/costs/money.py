"""
Money rules for cost control. Pure functions, Decimal only, with a fixed test table in
costs/tests/test_money.py.

    pennies(value)                 -> Decimal rounded half up to 1p; floats are refused
    budget_from_tender(value, pct) -> value less pct% margin, to 1p
    margin(final_account, cost)    -> (margin £ to 1p, margin % to 0.1, or None when no final account)
    default_forecast(b, c, a)      -> the larger of budget, committed and actual to date
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PENNY = Decimal('0.01')
TENTH = Decimal('0.1')
ZERO = Decimal('0.00')
HUNDRED = Decimal('100')


def pennies(value):
    """A money amount as Decimal to the penny, half up. Floats are refused (they can't hold 0.1 exactly)."""
    if isinstance(value, float):
        raise TypeError('Money must be a Decimal, an int or a string, never a float.')
    if isinstance(value, bool) or value is None:
        raise TypeError('Money must be a Decimal, an int or a string.')
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(',', '').replace('£', ''))
    except InvalidOperation:
        raise ValueError(f'{value!r} is not an amount of money.')
    if not d.is_finite():
        raise ValueError(f'{value!r} is not an amount of money.')
    return d.quantize(PENNY, rounding=ROUND_HALF_UP)


def percent(value):
    """A percentage to 0.01, half up (margins and thresholds are entered like 12.5)."""
    return pennies(value)


def budget_from_tender(value, margin_pct):
    """The job's tender or contract value less the margin, to the penny: 1,240,000 less 12.5% is 1,085,000.00."""
    value, margin_pct = pennies(value), percent(margin_pct)
    return pennies(value * (HUNDRED - margin_pct) / HUNDRED)


def margin(final_account, cost):
    """Forecast margin: final account less cost (to 1p), and as a % of the final account (to 0.1%)."""
    final_account, cost = pennies(final_account), pennies(cost)
    amount = final_account - cost
    if final_account <= 0:
        return amount, None
    return amount, (amount * HUNDRED / final_account).quantize(TENTH, rounding=ROUND_HALF_UP)


def default_forecast(budget, committed, actual):
    """
    Forecast final cost for a code with no manual override: the larger of budget, committed and
    actual to date. Money already committed or spent can't come in under budget by itself.
    """
    return max(pennies(budget), pennies(committed), pennies(actual))
