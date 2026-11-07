from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def pct(value, places=1):
    """0.1023 -> '10.2%'. Blank for None."""
    if value in (None, ''):
        return '—'
    try:
        v = Decimal(value) * 100
    except (InvalidOperation, TypeError, ValueError):
        return value
    q = Decimal(1).scaleb(-int(places))
    return f'{v.quantize(q, ROUND_HALF_UP)}%'


@register.filter
def qty(value):
    """A quantity without trailing zeros: 186, 0.4, 12.5."""
    if value in (None, ''):
        return ''
    try:
        v = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f'{v.quantize(Decimal("0.001"), ROUND_HALF_UP).normalize():f}'
