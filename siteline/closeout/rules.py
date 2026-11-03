"""
Close-out date rules as pure functions, with their fixed test table in closeout/tests/test_rules.py.
Everything here is advisory: check your contract.

  defects_end(pc, months)        The defects period (JCT Rectification Period, NEC defects date)
                                 ends `months` calendar months after practical completion. Where
                                 that month has no such day (31 Aug + 6 months), it is the last day
                                 of the month (28 or 29 Feb). No months set: no end date.
  reminder_from(end)             The end-of-defects reminder goes on Needs me REMINDER_DAYS (42,
                                 six weeks) before the end date, so there is time to book the
                                 inspection and get subcontractors back. It stays, overdue after the
                                 end date, until the making-good date is recorded.
  om_due_default(today, done)    When O&M information is asked for: two weeks before completion,
                                 if that is at least OM_MIN_WORKING_DAYS (10) working days away;
                                 otherwise 10 working days from today.
  is_defect_phase(pc, raised)    A snag raised on or after the whole-works practical completion
                                 date is a defect.
"""
import calendar
from datetime import date, timedelta

from holidays.calendar import DEFAULT_REGION

ADVISORY = 'Advisory: check your contract.'
REMINDER_DAYS = 42
OM_LEAD_DAYS = 14
OM_MIN_WORKING_DAYS = 10


def add_months(start, months):
    """`start` plus whole calendar months; a day past the month's end becomes its last day."""
    if start is None or months is None:
        return None
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def defects_end(pc_date, months):
    if pc_date is None or not months:
        return None
    return add_months(pc_date, months)


def reminder_from(end):
    return None if end is None else end - timedelta(days=REMINDER_DAYS)


def reminder_due(end, made_good_on, today):
    """True when the end-of-defects reminder should be on Needs me today."""
    if end is None or made_good_on is not None:
        return False
    return today >= reminder_from(end)


def is_defect_phase(pc_date, raised_on):
    return pc_date is not None and raised_on is not None and raised_on >= pc_date


def om_due_default(today, completion, region=DEFAULT_REGION):
    from procurement.dates import add_working_days
    floor = add_working_days(today, OM_MIN_WORKING_DAYS, region)
    if completion is not None:
        wanted = completion - timedelta(days=OM_LEAD_DAYS)
        if wanted >= floor:
            return wanted
    return floor
