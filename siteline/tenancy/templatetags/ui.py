from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def money(value, symbol='£'):
    """£12,500 (no pence) — fit-out values are quoted in whole pounds."""
    if value in (None, ''):
        return '—'
    try:
        v = Decimal(value)
    except (InvalidOperation, TypeError):
        return value
    sign = '-' if v < 0 else ''
    return f'{sign}{symbol}{abs(v):,.0f}'


@register.filter
def money_short(value, symbol='£'):
    """£1.2m / £450k for tight spaces."""
    if value in (None, ''):
        return '—'
    v = float(value)
    if abs(v) >= 1_000_000:
        return f'{symbol}{v / 1_000_000:.1f}m'
    if abs(v) >= 1_000:
        return f'{symbol}{v / 1_000:.0f}k'
    return f'{symbol}{v:.0f}'


# Text colours for anything drawn on the brand colour (--accent-ink); dark is the app's --ink
INK_LIGHT, INK_DARK = '#FFFFFF', '#15191F'


def relative_luminance(hex_colour):
    """WCAG 2 relative luminance of '#RRGGBB', from 0 (black) to 1 (white). ValueError if it isn't one."""
    h = (hex_colour or '').strip().lstrip('#')
    if len(h) != 6:
        raise ValueError(f'Not a #RRGGBB colour: {hex_colour!r}')
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    """WCAG contrast ratio between two colours, 1 to 21."""
    la, lb = relative_luminance(a), relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@register.filter
def readable_ink(background):
    """White or near-black, whichever reads better on `background` (a company's brand colour)."""
    try:
        light, dark = contrast_ratio(background, INK_LIGHT), contrast_ratio(background, INK_DARK)
    except ValueError:
        return INK_LIGHT
    return INK_LIGHT if light >= dark else INK_DARK


@register.simple_tag
def badge(text, tone='muted'):
    return format_html('<span class="badge {}">{}</span>', tone, text)


@register.filter
def filesize(n):
    n = n or 0
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


@register.filter
def field_type(field):
    return field.field.widget.__class__.__name__


@register.simple_tag(takes_context=True)
def qs(context, **kwargs):
    """Rebuild the current query string with some keys replaced."""
    q = context['request'].GET.copy()
    for k, v in kwargs.items():
        if v in (None, ''):
            q.pop(k, None)
        else:
            q[k] = v
    return '?' + q.urlencode() if q else '?'


ICONS = {
    'home': '<path d="M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z"/>',
    'pipeline': '<path d="M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v6h-4z"/>',
    'projects': '<path d="M3 7h18v13H3zM8 7V4h8v3"/>',
    'rfi': '<path d="M12 21a9 9 0 1 0-9-9c0 1.6.4 3.1 1.2 4.4L3 21l4.6-1.2A9 9 0 0 0 12 21z"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01"/>',
    'directory': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    'team': '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    'billing': '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
    'activity': '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    'chart': '<path d="M3 3v18h18"/><path d="M8 17v-5M13 17V8M18 17v-9"/>',
    'programme': '<path d="M3 5h8M7 10h10M5 15h6M13 20h8"/>',
    'bell': '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    'tender': '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'menu': '<path d="M3 6h18M3 12h18M3 18h18"/>',
    'download': '<path d="M12 3v12m0 0-4-4m4 4 4-4M4 21h16"/>',
}


@register.simple_tag
def icon(name):
    # Allowlist: only the constant SVG paths in ICONS above are ever output; an unknown name (the only
    # input a template controls) gives an empty <svg>. No user data reaches mark_safe, so B308/B703 are
    # reviewed and safe here.
    paths = ICONS.get(name, '')
    return mark_safe(  # nosec B308 B703
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )
