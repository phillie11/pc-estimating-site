"""
The programme strip on the dashboard: each job as a bar on a shared month axis, with a line for today.

Pure functions, no queries. `strip(projects, today)` takes anything with `start_date` and `completion_date` and gives
the positions in percent of the chart width, so the template only has to place boxes. A job that starts before the
window or finishes after it is clipped to the edge and flagged, so the bar still reads as "carries on".
"""
from datetime import date

MONTHS = 9          # the window: the month before today and the eight after
LEAD = 1


def _first(d):
    return date(d.year, d.month, 1)


def _add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def window(today, months=MONTHS, lead=LEAD):
    start = _add_months(_first(today), -lead)
    return start, _add_months(start, months)


def _pct(d, start, end):
    return (d - start).days / (end - start).days * 100


def strip(projects, today, months=MONTHS, lead=LEAD):
    start, end = window(today, months, lead)
    axis = []
    for i in range(months):
        a, b = _add_months(start, i), _add_months(start, i + 1)
        axis.append({'label': a.strftime('%b'), 'year': f'\u2019{a.year % 100:02d}' if (i == 0 or a.month == 1) else None,
                     'left': round(_pct(a, start, end), 3), 'width': round(_pct(b, start, end) - _pct(a, start, end), 3)})
    rows = []
    for p in projects:
        s, c = p.start_date, p.completion_date
        row = {'project': p, 'dated': bool(s and c and c >= s), 'visible': False}
        if row['dated']:
            row['visible'] = c >= start and s < end
            if row['visible']:
                a, b = max(s, start), min(c, end)
                left = _pct(a, start, end)
                row.update(left=round(left, 3), width=round(max(_pct(b, start, end) - left, 1.2), 3),
                           early=s < start, late=c >= end,
                           done=round(min(max((today - s).days / max((c - s).days, 1), 0), 1) * 100, 1),
                           started=s <= today)
        rows.append(row)
    return {'axis': axis, 'today': round(_pct(today, start, end), 3), 'rows': rows,
            'from': start, 'to': end}
