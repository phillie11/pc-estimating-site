"""
The levelling maths, as pure functions on Decimals. Fixed test table: levelling/tests/test_maths.py.

Rules (plain words, for sign-off):
- Money is Decimal, never float, rounded half up to the penny at each step that produces money.
- A line's value for a subcontractor: "included" counts as £0.00; else their sum for the line if
  they gave one; else rate × qty; else, with no qty (a lump-sum line), the rate itself; else the
  line is missing.
- A missing line can be plugged with the average of the other subcontractors' values for that
  line, or the highest of them. Lines the others marked "included" are left out (they carry no
  price of their own). With nobody else pricing the line, there is nothing to plug from.
- Levelled total = quoted total + every adjustment (adjustments can be negative).
- Rank is by levelled total, lowest first. Equal totals share a rank and the next rank is skipped
  (1, 1, 3). A subcontractor with no quoted total isn't ranked.
- A plug counts only while its line is missing: once the subcontractor prices the line, the plug
  is removed (and never added on top of their price).
- Variance from lowest = levelled total − the lowest levelled total, and as a percentage of the
  lowest, to one decimal place (half up). No percentage when the lowest is £0 or less.
- Average = the mean of the ranked levelled totals, to the penny. Variance from average = levelled
  total − average, and as a percentage of the average, to one decimal place.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

PENNY = Decimal('0.01')
TENTH = Decimal('0.1')
ZERO = Decimal('0.00')

AVERAGE, HIGHEST = 'AVERAGE', 'HIGHEST'


def pennies(value):
    """Decimal to the penny, half up. Floats are refused: they can't hold pence exactly."""
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError('Money must be Decimal, int or str, never float.')
    return Decimal(value).quantize(PENNY, rounding=ROUND_HALF_UP)


def percent(part, whole):
    """part as a % of whole, to 0.1 half up; None when the whole is 0 or less (no meaningful percentage)."""
    if whole is None or part is None or whole <= 0:
        return None
    return (Decimal(part) / Decimal(whole) * 100).quantize(TENTH, rounding=ROUND_HALF_UP)


def line_value(qty=None, rate=None, amount=None, included=False):
    """A subcontractor's money for one line, or None when it is missing."""
    if included:
        return ZERO
    if amount is not None:
        return pennies(amount)
    if rate is None:
        return None
    if qty is None:
        return pennies(rate)
    return pennies(Decimal(qty) * Decimal(rate))


def plug_value(others, basis):
    """What to plug a missing line with, from the other subcontractors' values for it (None if none)."""
    values = [Decimal(v) for v in others if v is not None]
    if not values:
        return None
    if basis == HIGHEST:
        return pennies(max(values))
    if basis == AVERAGE:
        return pennies(sum(values) / len(values))
    raise ValueError(f'Unknown plug basis {basis!r}')


def plug_counts(line_value_now):
    """A plug counts only while its line is still missing. Once the line is priced, the plug is dropped."""
    return line_value_now is None


@dataclass
class Result:
    key: object
    quoted: Decimal = None
    adjustments: Decimal = ZERO
    levelled: Decimal = None
    rank: int = None
    tied: bool = False
    var_lowest: Decimal = None
    var_lowest_pct: Decimal = None
    var_average: Decimal = None
    var_average_pct: Decimal = None

    @property
    def is_lowest(self):
        return self.rank == 1


@dataclass
class Summary:
    results: list = field(default_factory=list)
    lowest: Decimal = None
    average: Decimal = None

    def get(self, key):
        return next((r for r in self.results if r.key == key), None)


def level(columns):
    """
    columns: [(key, quoted total or None, [adjustment amounts])], in display order.
    Returns a Summary with one Result per column in the same order.
    """
    results = []
    for key, quoted, adjustments in columns:
        adj = pennies(sum((Decimal(a) for a in adjustments), ZERO))
        r = Result(key=key, quoted=pennies(quoted), adjustments=adj)
        if r.quoted is not None:
            r.levelled = pennies(r.quoted + adj)
        results.append(r)
    ranked = [r for r in results if r.levelled is not None]
    if not ranked:
        return Summary(results=results)
    totals = sorted(r.levelled for r in ranked)
    lowest = totals[0]
    average = pennies(sum(totals) / len(totals))
    for r in ranked:
        r.rank = 1 + sum(1 for t in totals if t < r.levelled)
        r.tied = totals.count(r.levelled) > 1
        r.var_lowest = r.levelled - lowest
        r.var_lowest_pct = percent(r.var_lowest, lowest)
        r.var_average = r.levelled - average
        r.var_average_pct = percent(r.var_average, average)
    return Summary(results=results, lowest=lowest, average=average)
