"""
Procurement dates, as pure functions with a fixed test table (procurement/tests/test_dates.py).
ADVISORY: a planning aid. Check the dates against your subcontract orders and the programme.

Working weeks
    A week is a working week: five working days, which is seven calendar days. So going back
    N weeks means going back N x 7 days. If the answer lands on a day the office is shut
    (Saturday, Sunday, Christmas Day, Good Friday or a bank holiday in the contract's part of
    the UK, from holidays.calendar), it moves back to the working day before. Moving back,
    never forward, means rounding can only make a date earlier, never late.

    order_by        = required on site - lead time (weeks)
    submittals_due  = order_by - approval period (weeks)

    A missing required-on-site date gives no dates. A missing lead time or approval period
    counts as 0 weeks.

Working days (review periods)
    add_working_days(start, n) is the date n working days after start; start itself is not
    counted. Ten working days from a Monday is the Monday two weeks later, one day more for
    each bank holiday in between.

RAG (red / amber / green), from today against the order-by date
    Ordered or delivered  -> done (nothing more to order)
    No order-by date      -> none
    Order-by before today -> red    (late to order)
    Within 14 days        -> amber  (order soon; today itself is amber)
    Otherwise             -> green  (on track)
"""
from datetime import timedelta

from holidays.calendar import DEFAULT_REGION, is_excluded

AMBER_DAYS = 14   # amber when the order-by date is this many days away or fewer
NEEDS_ME_DAYS = 7  # the project manager is asked to act this many days before the order-by date

RED, AMBER, GREEN, DONE, NONE = 'red', 'amber', 'green', 'done', 'none'
RAG_CHOICES = [(RED, 'Red'), (AMBER, 'Amber'), (GREEN, 'Green'), (DONE, 'Ordered'), (NONE, 'No date')]
RAG_LABELS = {RED: 'Red: late to order', AMBER: 'Amber: order soon', GREEN: 'Green: on track',
              DONE: 'Ordered', NONE: 'No date yet'}
RAG_TONES = {RED: 'bad', AMBER: 'warn', GREEN: 'good', DONE: 'muted', NONE: 'muted'}


def is_working_day(day, region=DEFAULT_REGION):
    return day.weekday() < 5 and not is_excluded(day, region)


def previous_working_day(day, region=DEFAULT_REGION):
    """day itself if it is a working day, else the last working day before it."""
    while not is_working_day(day, region):
        day -= timedelta(days=1)
    return day


def weeks_before(day, weeks, region=DEFAULT_REGION):
    """`weeks` working weeks before `day`, moved back to a working day."""
    if day is None:
        return None
    return previous_working_day(day - timedelta(weeks=weeks or 0), region)


def order_by(required_on_site, lead_time_weeks, approval_weeks, region=DEFAULT_REGION):
    """The last date to place the order so it arrives when needed. The approval period doesn't move it
    (it comes before the order: see submittals_due); it is taken so both functions share a signature."""
    if required_on_site is None:
        return None
    return weeks_before(required_on_site, lead_time_weeks or 0, region)


def submittals_due(required_on_site, lead_time_weeks, approval_weeks, region=DEFAULT_REGION):
    """When drawings, samples and data must be in, leaving the approval period before the order-by date."""
    ob = order_by(required_on_site, lead_time_weeks, approval_weeks, region)
    return weeks_before(ob, approval_weeks or 0, region) if ob else None


def add_working_days(start, days, region=DEFAULT_REGION):
    """The date `days` working days after `start` (start not counted; 0 days is start itself)."""
    day, left = start, days
    while left > 0:
        day += timedelta(days=1)
        if is_working_day(day, region):
            left -= 1
    return day


def rag(order_by_date, done, today):
    """RED, AMBER, GREEN, DONE or NONE (see the module docstring)."""
    if done:
        return DONE
    if order_by_date is None:
        return NONE
    if order_by_date < today:
        return RED
    if (order_by_date - today).days <= AMBER_DAYS:
        return AMBER
    return GREEN


def needs_ordering_soon(order_by_date, done, today):
    """True when the project manager should be asked: not ordered, and the order-by date is within 7 days or past."""
    return not done and order_by_date is not None and (order_by_date - today).days <= NEEDS_ME_DAYS
