"""
The 2, 3 or 4 week look-ahead: the programme lines that need doing in the next few weeks.

    window(today, weeks)          -> (Monday this week, Sunday of the last week)
    lines(activities, start, end) -> [Line], soonest first

Rules (pure functions, fixed test table in projects/test_programme/test_lookahead.py):
  * The window starts on Monday of the current week and runs whole weeks: 2, 3 or 4 (default 3).
  * A line is in the look-ahead when it isn't 100% complete, isn't a summary (heading) line, and
    either overlaps the window or should already have finished ("behind").
  * Each line gets a cell per working day (Monday to Friday) in the window: on when the line is
    planned that day. Weekend work still shows in the dates; the grid is for the working week.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

WEEK_CHOICES = (2, 3, 4)
DEFAULT_WEEKS = 3


def weeks_param(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WEEKS
    return n if n in WEEK_CHOICES else DEFAULT_WEEKS


def window(today, weeks=DEFAULT_WEEKS):
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=7 * weeks - 1)


def working_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class Line:
    activity: object
    behind: bool
    starts_in_window: bool
    cells: list = field(default_factory=list)  # one bool per working day


def wanted(a, start, end):
    if a.progress >= 100 or getattr(a, 'is_summary', False):
        return False
    return (a.start <= end and a.finish >= start) or a.finish < start


def lines(activities, start, end, today=None):
    today = today or date.today()
    days = working_days(start, end)
    out = []
    for a in activities:
        if not wanted(a, start, end):
            continue
        out.append(Line(activity=a, behind=a.finish < today, starts_in_window=a.start >= start,
                        cells=[a.start <= d <= a.finish for d in days]))
    out.sort(key=lambda ln: (not ln.behind, ln.activity.start, ln.activity.finish, ln.activity.name))
    return out


def weeks_of(start, end):
    """[(Monday, [working days])] for the grid's header."""
    out, d = [], start
    while d <= end:
        out.append((d, working_days(d, min(d + timedelta(days=6), end))))
        d += timedelta(days=7)
    return out
