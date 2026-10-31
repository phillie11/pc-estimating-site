"""
The site diary's date rules, as pure functions with fixed test tables (diary/tests/test_rules.py).

Edit window
    Staff can change an entry on its own day and the calendar day after. An entry written late
    (for a day already past) can also be changed on the day it was written, so a missing day can
    be caught up, and the register shows it was written late. After that it is locked; the
    project manager or an admin can unlock it for correction, with a reason, until the end of
    the next day.

Working days and the missing-diary rule ("Needs me")
    A working day is Monday to Friday, plus Saturday where the company says Saturday counts,
    and never a bank holiday (in the region of the job's contract, England and Wales if none).
    An entry for a working day is due by 10:00 on the next working day: Friday's by Monday
    10:00 (or Saturday 10:00 where Saturday counts). Only the last few working days are
    checked, so an old job doesn't fill "Needs me" with history.
"""
import datetime

from holidays.calendar import DEFAULT_REGION, is_excluded

DUE_HOUR = 10          # entries are due by 10:00 on the next working day
LOOKBACK_DAYS = 5      # how many past working days "Needs me" checks
UNLOCK_DAYS = 1        # an unlock lasts to the end of the next day
ONE_DAY = datetime.timedelta(days=1)


def edit_until(entry_date, written_on):
    """The last day an entry can be changed without an unlock."""
    return max(entry_date + ONE_DAY, written_on or entry_date)


def is_locked(entry_date, written_on, today, unlocked_until=None):
    if unlocked_until is not None and today <= unlocked_until:
        return False
    return today > edit_until(entry_date, written_on)


def unlock_until(today):
    return today + datetime.timedelta(days=UNLOCK_DAYS)


def is_working_day(day, saturday_counts=False, region=DEFAULT_REGION):
    if day.weekday() == 6 or (day.weekday() == 5 and not saturday_counts):
        return False
    return not is_excluded(day, region or DEFAULT_REGION)


def next_working_day(day, saturday_counts=False, region=DEFAULT_REGION):
    day += ONE_DAY
    while not is_working_day(day, saturday_counts, region):
        day += ONE_DAY
    return day


def due_at(day, saturday_counts=False, region=DEFAULT_REGION):
    """When the entry for working day `day` is due: 10:00 on the next working day (naive local time)."""
    return datetime.datetime.combine(next_working_day(day, saturday_counts, region), datetime.time(DUE_HOUR))


def expected_days(now, saturday_counts=False, region=DEFAULT_REGION, start=None, lookback=LOOKBACK_DAYS):
    """
    The working days, most recent first, whose entry is already due at `now` (a naive local
    datetime): at most `lookback` of them, none before `start` (the job's start on site).
    """
    out, day = [], now.date() - ONE_DAY
    checked = 0
    while checked < lookback * 3 and len(out) < lookback:  # 3x covers weekends and bank holiday runs
        checked += 1
        if start is not None and day < start:
            break
        if is_working_day(day, saturday_counts, region) and due_at(day, saturday_counts, region) <= now:
            out.append(day)
        day -= ONE_DAY
    return out
