"""
Standard payment, retention and defects terms per form of contract, used to pre-fill a
project's contract set-up. Every value is a starting point that the team edits to match the
signed contract: ADVISORY, check your contract (the Contract Particulars or Contract Data and
any amendments override all of this).

Fixed data plus pure functions, so the tests can hold them to a table.

Payment timetable, in days counted as the Construction Act counts them (s116: a period runs
from the day after, and Christmas Day, Good Friday and bank holidays in the contract's part of
the UK are left out; holidays/calendar.py):
  valuation (or assessment) date
    + due_date_offset_days   -> due date
    + final_date_offset_days -> final date for payment (counted from the due date)
    - pay_less_notice_days   -> last day for a pay less notice (counted back from the final date)
  and, counted from the due date,
    + payment_notice_days    -> last day for the payer's payment notice (HGCRA s110A: not later than
                                5 days after the due date; the Interim Certificate under JCT, the
                                Project Manager's certificate under NEC4)

Where the terms come from (as published; amendments are common):
  JCT MW/MWD 2016 and 2024: monthly due dates; final date 14 days after the due date; pay less
    notice no later than 5 days before the final date; retention 5% (half released at
    Practical Completion); Rectification Period 3 months if none is stated.
  JCT IC/ICD 2016 and 2024: due date 7 days after each Interim Valuation Date; final date 14
    days after the due date; pay less notice 5 days before the final date; interim payments at
    95% of value unless the Contract Particulars say otherwise, so 5% retention (half at Practical
    Completion; confirmed by the product owner); Rectification Period 6 months if none stated.
  JCT SBC and DB 2016 and 2024: as IC for the payment periods; Retention Percentage 3% unless the
    Contract Particulars state less; Rectification Period 6 months if none stated.
  NEC4 ECC with Option Y(UK)2: payment due 7 days after the assessment date; final date 14 days
    after it becomes due; pay less notice 7 days before the final date. Retention only under
    Option X16 and defects dates are in Contract Data, so neither is pre-filled.
  Payment notice: JCT (every form) and the Scheme, no later than 5 days after the due date (JCT
    MW 4.7, IC 4.9.2, SBC/DB 4.10; Scheme Part II para 9). NEC4: the Project Manager certifies within
    one week of the assessment date (clause 51.1), which under Y(UK)2 is the due date, so 0 days.
  Scheme for Construction Contracts (the fallback where a contract has no compliant payment
    terms): an interim payment is due 7 days after the end of the relevant period, or when the
    claim is made if that is later (Part II para 4; the 30 days in para 5 is for the final
    payment); final date 17 days after the due date (para 8); pay less notice 7 days before the
    final date (para 10).
"""
import calendar
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from holidays.calendar import DEFAULT_REGION, add_days, subtract_days

ADVISORY = 'Advisory: check your contract.'


@dataclass(frozen=True)
class Preset:
    label: str
    due_date_offset_days: int
    final_date_offset_days: int
    pay_less_notice_days: int
    payment_notice_days: int = 5
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
# IC/ICD: interim certificates are for 95% of the value before Practical Completion unless the
# Contract Particulars state another percentage, i.e. 5% retention. Default confirmed by the product
# owner (30 Oct 2026) for the 2016 and 2024 editions.
_IC = {**_JCT, 'retention_percent': Decimal('5')}
IC_NOTE = 'Retention: 5% (95% of value certified) unless the Contract Particulars say otherwise.'

SCHEME = Preset('Scheme for Construction Contracts (fallback)', due_date_offset_days=7, final_date_offset_days=17,
                pay_less_notice_days=7, note='Applies where the contract has no compliant payment terms.')

PRESETS = {
    'JCT_MW': Preset('JCT Minor Works', **_MW, note='Due dates monthly from the Date for Commencement.'),
    'JCT_MWD': Preset("JCT Minor Works with contractor's design", **_MW,
                      note='Due dates monthly from the Date for Commencement.'),
    'JCT_IC': Preset('JCT Intermediate', **_IC, note=IC_NOTE),
    'JCT_ICD': Preset("JCT Intermediate with contractor's design", **_IC, note=IC_NOTE),
    'JCT_SBC': Preset('JCT Standard Building Contract', **_JCT),
    'JCT_DB': Preset('JCT Design and Build', **_JCT),
    'NEC4_ECC': Preset('NEC4 ECC with Option Y(UK)2', due_date_offset_days=7, final_date_offset_days=14,
                       pay_less_notice_days=7, payment_notice_days=0, lads_period='DAY',
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


def payment_timetable(valuation_date, due_offset, final_offset, pay_less_days, region=DEFAULT_REGION):
    """
    Due date, final date for payment and last day for a pay less notice. Days are counted leaving
    out Christmas Day, Good Friday and the region's bank holidays (HGCRA s116). Advisory.
    """
    if valuation_date is None or None in (due_offset, final_offset, pay_less_days):
        return None
    due = add_days(valuation_date, due_offset, region)
    final = add_days(due, final_offset, region)
    return PaymentTimetable(valuation_date, due, final, subtract_days(final, pay_less_days, region))
