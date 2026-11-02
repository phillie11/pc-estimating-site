"""
The cashflow chart, drawn on the server as inline SVG (no script, nothing from a CDN, the
Content-Security-Policy unchanged). One £ axis: money in as bars above the line, money out as
bars below it, and the running total as a line. Forecast months are paler and sit right of a
"Today" marker, so actual and forecast never rely on colour alone. Each month has a hit area with
a <title>, which browsers show as a tooltip; the table under the chart has every figure.

    geometry(months) -> dict for cvr/_cashflow_chart.html
"""
from decimal import Decimal

WIDTH, HEIGHT = 760, 300
LEFT, RIGHT, TOP, BOTTOM = 64, 12, 16, 40
TICKS = 5


def _nice(span):
    """A round step for about TICKS gridlines over span (1, 2 or 5 times a power of ten)."""
    if span <= 0:
        return Decimal('1000')
    raw = Decimal(span) / TICKS
    power = Decimal(10) ** (len(str(int(raw))) - 1)
    for m in (1, 2, 5, 10):
        if raw <= power * m:
            return power * m
    return power * 10


def short(value):
    v = abs(value)
    sign = '−' if value < 0 else ''
    if v >= 1_000_000:
        return f'{sign}£{v / 1_000_000:.1f}m'
    if v >= 1000:
        return f'{sign}£{v / 1000:.0f}k'
    return f'{sign}£{v:.0f}'


def geometry(months):
    if not months:
        return None
    top = max([m.money_in for m in months] + [m.cumulative for m in months] + [Decimal('0')])
    bottom = min([-m.money_out for m in months] + [m.cumulative for m in months] + [Decimal('0')])
    step = _nice(top - bottom)
    hi = (top / step).to_integral_value(rounding='ROUND_CEILING') * step
    lo = (bottom / step).to_integral_value(rounding='ROUND_FLOOR') * step
    if hi == lo:
        hi = lo + step
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def y(v):
        return float(TOP + (hi - Decimal(v)) / (hi - lo) * plot_h)

    col = plot_w / len(months)
    bar = max(min(col * 0.6, 28), 2)
    zero = y(0)
    cols, points, today_x = [], [], None
    label_every = max(1, -(-len(months) // 12))  # at most about 12 month labels
    for i, m in enumerate(months):
        x0 = LEFT + i * col
        cx = x0 + col / 2
        if m.is_current:
            today_x = x0
        y_in, y_out = y(m.money_in), y(-m.money_out)
        cols.append({
            'x': round(x0, 1), 'w': round(col, 1), 'cx': round(cx, 1), 'bar_x': round(cx - bar / 2, 1),
            'bar_w': round(bar, 1),
            'in_y': round(y_in, 1), 'in_h': round(max(zero - y_in, 0), 1),
            'out_y': round(zero, 1), 'out_h': round(max(y_out - zero, 0), 1),
            'forecast': not m.is_past and (m.in_forecast or m.out_forecast) and not (m.in_actual or m.out_actual),
            'label': f'{m.month:%b %y}' if i % label_every == 0 else '',
            'tip': (f'{m.month:%B %Y}: in {short(m.money_in)}, out {short(m.money_out)}, net {short(m.net)}, '
                    f'running total {short(m.cumulative)}' + (' (forecast)' if not m.is_past else '')),
            'month': m,
        })
        points.append(f'{cx:.1f},{y(m.cumulative):.1f}')
    ticks = []
    v = lo
    while v <= hi:
        ticks.append({'y': round(y(v), 1), 'label': short(v)})
        v += step
    return {'width': WIDTH, 'height': HEIGHT, 'left': LEFT, 'right': WIDTH - RIGHT, 'top': TOP,
            'bottom': HEIGHT - BOTTOM, 'zero': round(zero, 1), 'cols': cols, 'line': ' '.join(points),
            'ticks': ticks, 'today_x': round(today_x, 1) if today_x is not None else None,
            'label_y': HEIGHT - BOTTOM + 18, 'plot_h': plot_h, 'tick_x': LEFT - 6}
