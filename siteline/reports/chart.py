"""
Simple bar charts for the reports, drawn on the server as inline SVG like the cashflow chart
(cvr/chart.py): no script, nothing from a CDN, the Content-Security-Policy unchanged, colours from
the existing tokens. Each bar has a <title> (a browser tooltip) and the full table sits under every
chart, so nothing depends on seeing the chart or on colour: negative bars are also drawn the other
side of the zero line and labelled with a minus sign.

    bars(items, label_width=...) -> dict for reports/_bars.html
        items: [(label, value: Decimal or int, tip, text)] in the order to draw them
"""
from decimal import Decimal

WIDTH = 720
ROW, GAP, TOP = 22, 8, 8


def bars(items, label_width=200):
    if not items:
        return None
    values = [Decimal(v) for _, v, _, _ in items]
    hi = max(values + [Decimal(0)])
    lo = min(values + [Decimal(0)])
    span = (hi - lo) or Decimal(1)
    plot_x, plot_w = label_width + 8, WIDTH - label_width - 8 - 90  # room for the value text on the right

    def x(v):
        return float(plot_x + (Decimal(v) - lo) / span * plot_w)

    zero = x(0)
    rows = []
    for i, ((label, value, tip, text), v) in enumerate(zip(items, values)):
        y = TOP + i * (ROW + GAP)
        left, right = sorted((zero, x(v)))
        rows.append({'label': label, 'tip': tip, 'text': text, 'neg': v < 0, 'y': y, 'ty': y + ROW * 0.7,
                     'x': round(left, 1), 'w': round(max(right - left, 1 if v else 0), 1),
                     'tx': round(max(right, zero) + 6, 1)})
    return {'rows': rows, 'width': WIDTH, 'height': TOP * 2 + len(items) * (ROW + GAP), 'zero': round(zero, 1),
            'label_x': label_width, 'row_h': ROW}
