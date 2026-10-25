"""
Lay out bars for a Gantt chart.

Positions are percentages of the visible date range, so the chart is plain
HTML/CSS: no charting library, prints cleanly and scrolls sideways on a phone.
"""
from datetime import date, timedelta

WEEK_PX = 56    # minimum width of one week when weeks are labelled, so labels never collide
MONTH_WEEK_PX = 18  # when months are labelled
MONTH_TICKS_AFTER = 26  # weeks; beyond this, label months instead of weeks


def _pct(n):
    # Pre-formatted so template localisation can never turn the dot into a comma
    return f'{n:.3f}'


def _month_starts(lo, hi):
    d = date(lo.year, lo.month, 1)
    while d <= hi:
        if d >= lo:
            yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def build(items, start=lambda i: i.start, finish=lambda i: i.finish, marks=None, today=None):
    """
    items: anything with a start and finish date (callables pick them out).
    marks: optional callable giving (date, label, kind) milestones to pin on each row.
    Returns None when nothing has dates, else the chart's ticks and rows.
    """
    today = today or date.today()
    dated = [i for i in items if start(i) and finish(i)]
    if not dated:
        return None
    marks = marks or (lambda i: [])
    mark_dates = [d for i in dated for d, _, _ in marks(i)]
    lo = min([start(i) for i in dated] + mark_dates)
    hi = max([max(finish(i), start(i)) for i in dated] + mark_dates)
    lo -= timedelta(days=lo.weekday())                  # back to Monday
    hi += timedelta(days=6 - hi.weekday())              # on to Sunday
    days = (hi - lo).days + 1
    weeks = days // 7

    if weeks > MONTH_TICKS_AFTER:
        ticks = [{'left': _pct((d - lo).days / days * 100), 'label': d.strftime('%b %y')}
                 for d in _month_starts(lo, hi)]
    else:
        ticks = [{'left': _pct(w * 7 / days * 100), 'label': f'{(lo + timedelta(weeks=w)).day} {(lo + timedelta(weeks=w)):%b}'}
                 for w in range(weeks)]

    rows = []
    for i in dated:
        s, f = start(i), max(finish(i), start(i))
        rows.append({
            'item': i,
            'left': _pct((s - lo).days / days * 100),
            'width': _pct(max(((f - s).days + 1) / days * 100, 0.4)),
            # Centre of the day, so a milestone sits in the middle of its date
            'marks': [{'left': _pct(((d - lo).days + 0.5) / days * 100), 'label': label, 'kind': kind, 'date': d}
                      for d, label, kind in marks(i)],
            'elapsed': _pct(min(max(((today - s).days + 1) / ((f - s).days + 1), 0), 1) * 100),
        })
    return {
        'ticks': ticks,
        'rows': rows,
        'start': lo,
        'end': hi,
        'today': _pct((today - lo).days / days * 100) if lo <= today <= hi else None,
        'min_width': weeks * (MONTH_WEEK_PX if weeks > MONTH_TICKS_AFTER else WEEK_PX),
    }
