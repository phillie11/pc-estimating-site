"""
Standard payment, retention and defects terms per form of contract, used to pre-fill a
project's contract set-up. Every value is a starting point that the team edits to match the
signed contract: ADVISORY, check your contract (the Contract Particulars or Contract Data and
any amendments override all of this).

Fixed data plus pure functions, so the tests can hold them to a table.

Payment timetable, in calendar days:
  valuation (or assessment) date
    + due_date_offset_days   -> due date
    + final_date_offset_days -> final date for payment (counted from the due date)
    - pay_less_notice_days   -> last day for a pay less notice (counted back from the final date)

Where the terms come from (as published; amendments are common):
  JCT MW/MWD 2016 and 2024: monthly due dates; final date 14 days after the due date; pay less
    notice no later than 5 days before the final date; retention 5% (half released at
    Practical Completion); Rectification Period 3 months if none is stated.
  JCT IC/ICD, SBC and DB 2016 and 2024: due date 7 days after each Interim Valuation Date;
    final date 14 days after the due date; pay less notice 5 days before the final date;
    retention 3% (half at Practical Completion); Rectification Period 6 months if none stated.
  NEC4 ECC with Option Y(UK)2: payment due 7 days after the assessment date; final date 14 days
    after it becomes due; pay less notice 7 days before the final date. Retention only under
    Option X16 and defects dates are in Contract Data, so neither is pre-filled.
  Scheme for Construction Contracts (the fallback where a contract has no compliant payment
    terms): due 30 days after the end of the period (or the claim, if later); final date 17
    days after the due date; pay less notice 7 days before the final date.

The Construction Act and the JCT forms leave bank holidays out when counting periods of days.
These dates are plain calendar days, which is one more reason they are advisory.
"""
import calendar
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal

ADVISORY = 'Advisory: check your contract.'


@dataclass(frozen=True)
class Preset:
    label: str
    due_date_offset_days: int
    final_date_offset_days: int
    pay_less_notice_days: int
    retention_percent: Decimal | None = None
    retention_released_at_pc_percent: Decimal = Decimal('50')
    defects_period_months: int | None = None
    lads_period: str = 'WEEK'
    note: str = ''

    def values(self):
        """The MainContract fields this preset fills."""
        d = asdict(self)
        d.pop('label')
        d.pop('note')
        return d


_MW = dict(due_date_offset_days=0, final_date_offset_days=14, pay_less_notice_days=5,
           retention_percent=Decimal('5'), defects_period_months=3)
_JCT = dict(due_date_offset_days=7, final_date_offset_days=14, pay_less_notice_days=5,
            retention_percent=Decimal('3'), defects_period_months=6)

SCHEME = Preset('Scheme for Construction Contracts (fallback)', due_date_offset_days=30, final_date_offset_days=17,
                pay_less_notice_days=7, note='Applies where the contract has no compliant payment terms.')

PRESETS = {
    'JCT_MW': Preset('JCT Minor Works', **_MW, note='Due dates monthly from the Date for Commencement.'),
    'JCT_MWD': Preset("JCT Minor Works with contractor's design", **_MW,
                      note='Due dates monthly from the Date for Commencement.'),
    'JCT_IC': Preset('JCT Intermediate', **_JCT),
    'JCT_ICD': Preset("JCT Intermediate with contractor's design", **_JCT),
    'JCT_SBC': Preset('JCT Standard Building Contract', **_JCT),
    'JCT_DB': Preset('JCT Design and Build', **_JCT),
    'NEC4_ECC': Preset('NEC4 ECC with Option Y(UK)2', due_date_offset_days=7, final_date_offset_days=14,
                       pay_less_notice_days=7, lads_period='DAY',
                       note='Assessment dates at intervals of no more than five weeks. Retention only under '
                            'Option X16; the defects date is in Contract Data.'),
    'BESPOKE': SCHEME,
}


def preset_for(form):
    """The standard terms for a MainContract.form value; the Scheme for anything unknown or bespoke."""
    return PRESETS.get(form, SCHEME)


def valuation_date_in(year, month, day):
    """The valuation date in a month for 'day N of the month'; a day past the month's end means its last day."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_valuation_date(today, day):
    """The first valuation date on or after today for 'day N of each month' (N above 28 snaps to month end)."""
    if not day:
        return None
    this_month = valuation_date_in(today.year, today.month, day)
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return valuation_date_in(year, month, day)


@dataclass(frozen=True)
class PaymentTimetable:
    valuation_date: date
    due_date: date
    final_date: date
    pay_less_by: date


def payment_timetable(valuation_date, due_offset, final_offset, pay_less_days):
    """Due date, final date for payment and last day for a pay less notice, in calendar days. Advisory."""
    if valuation_date is None or None in (due_offset, final_offset, pay_less_days):
        return None
    due = valuation_date + timedelta(days=due_offset)
    final = due + timedelta(days=final_offset)
    return PaymentTimetable(valuation_date, due, final, final - timedelta(days=pay_less_days))
