"""
Mark-up layers: the shapes someone draws over a drawing, checked before they are stored.

A layer's shapes come from the person's browser, so nothing here trusts them. `parse()` takes the
JSON text the viewer posts and either returns a clean copy in the one shape we store, or raises
InvalidMarkup with a plain reason. Nothing in a layer is ever rendered as HTML: the viewer draws
shapes on a canvas and writes text with fillText, and the layer list shows only our own words.

Stored form, version 1 (positions are fractions of the page, 0 to 1 across and down, so a layer
drawn on one revision lands in the same place on another and doesn't depend on zoom):

    {"v": 1, "shapes": [
        {"t": "pen",     "p": 1, "c": "red", "w": 3, "pts": [[0.1, 0.2], [0.11, 0.21], ...]},
        {"t": "line",    "p": 1, "c": "red", "w": 3, "a": [x, y], "b": [x, y]},   # also arrow, rect, cloud
        {"t": "measure", "p": 1, "c": "red", "w": 3, "a": [x, y], "b": [x, y], "text": "3.45 m"},
        {"t": "text",    "p": 1, "c": "red", "s": 18, "at": [x, y], "text": "Check this opening"},
    ]}

t kind, p page (from 1), c colour (one of COLOURS), w line weight (1 to 12, thousandths of the page
width), s text size (6 to 120, thousandths of the page width).
"""
import json
import math
import unicodedata

VERSION = 1
MAX_BYTES = 256 * 1024
MAX_SHAPES = 1000
MAX_STROKE_POINTS = 2000
MAX_POINTS = 20000
MAX_TEXT = 300
MAX_PAGE = 10000
PLACES = 5

COLOURS = ('red', 'blue', 'green', 'orange', 'purple', 'black')
TWO_POINT = ('line', 'arrow', 'rect', 'cloud', 'measure')
KINDS = ('pen', 'text') + TWO_POINT
KIND_LABELS = {'pen': 'pen stroke', 'line': 'line', 'arrow': 'arrow', 'rect': 'box', 'cloud': 'cloud', 'measure': 'measurement',
               'text': 'text'}

_KEYS = {'pen': {'t', 'p', 'c', 'w', 'pts'}, 'text': {'t', 'p', 'c', 's', 'at', 'text'},
         'measure': {'t', 'p', 'c', 'w', 'a', 'b', 'text'}}
for _k in ('line', 'arrow', 'rect', 'cloud'):
    _KEYS[_k] = {'t', 'p', 'c', 'w', 'a', 'b'}


class InvalidMarkup(ValueError):
    """A mark-up that can't be stored: the message is shown to the person as it stands."""


def empty():
    return {'v': VERSION, 'shapes': []}


def _refuse_constant(name):
    raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')


def loads(text):
    """JSON text from the browser, size-capped, refusing NaN and Infinity."""
    if not isinstance(text, str):
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise InvalidMarkup('This mark-up is too big to save. Split it over two layers or remove some pen strokes.')
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')


def _int(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidMarkup(f'A shape has a {what} outside {low} to {high}.')
    return value


def _coord(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidMarkup('A shape has a position that is not a number.')
    # Range first: a huge JSON whole number is a Python int that math.isfinite can't turn into a float
    if value < 0 or value > 1:
        raise InvalidMarkup('A shape is off the page.')
    if not math.isfinite(value):
        raise InvalidMarkup('A shape has a position that is not a number.')
    return round(float(value), PLACES)


def _point(value):
    if not isinstance(value, list) or len(value) != 2:
        raise InvalidMarkup('A shape has a position that is not a point.')
    return [_coord(value[0]), _coord(value[1])]


def clean_text(value, required):
    """Plain text on one line: no control or formatting characters, at most MAX_TEXT long."""
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise InvalidMarkup('Text on a mark-up must be words.')
    out = ''.join(' ' if ch in '\t\n\r' else ch for ch in value
                  if ch in '\t\n\r' or unicodedata.category(ch)[0] != 'C')
    out = ' '.join(out.split())
    if len(out) > MAX_TEXT:
        raise InvalidMarkup(f'Text on a mark-up can be at most {MAX_TEXT} characters.')
    if required and not out:
        raise InvalidMarkup('Type the text for the note.')
    return out


def clean_shape(raw, page_count=None):
    if not isinstance(raw, dict):
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')
    kind = raw.get('t')
    if kind not in KINDS:
        raise InvalidMarkup('That kind of mark-up is not supported.')
    extra = set(raw) - _KEYS[kind]
    if extra:
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')
    pages = min(page_count or MAX_PAGE, MAX_PAGE)
    shape = {'t': kind, 'p': _int(raw.get('p'), 1, pages, 'page')}
    colour = raw.get('c')
    if colour not in COLOURS:
        raise InvalidMarkup('Choose one of the mark-up colours.')
    shape['c'] = colour
    if kind == 'text':
        shape['s'] = _int(raw.get('s'), 6, 120, 'text size')
        shape['at'] = _point(raw.get('at'))
        shape['text'] = clean_text(raw.get('text'), required=True)
        return shape
    shape['w'] = _int(raw.get('w'), 1, 12, 'line weight')
    if kind == 'pen':
        pts = raw.get('pts')
        if not isinstance(pts, list) or not 1 <= len(pts) <= MAX_STROKE_POINTS:
            raise InvalidMarkup(f'A pen stroke must have from 1 to {MAX_STROKE_POINTS} points.')
        shape['pts'] = [_point(p) for p in pts]
        return shape
    shape['a'], shape['b'] = _point(raw.get('a')), _point(raw.get('b'))
    if kind == 'measure':
        shape['text'] = clean_text(raw.get('text'), required=False)
    return shape


def clean_shapes(shapes, page_count=None):
    if not isinstance(shapes, list):
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')
    if len(shapes) > MAX_SHAPES:
        raise InvalidMarkup(f'A layer can hold at most {MAX_SHAPES} shapes. Start another layer on a new revision, '
                            'or remove some.')
    out = [clean_shape(s, page_count) for s in shapes]
    if sum(len(s.get('pts', ())) for s in out) > MAX_POINTS:
        raise InvalidMarkup('This mark-up has too many pen strokes to save. Remove some, or use lines and boxes.')
    return out


def parse(text, page_count=None):
    """The JSON a browser posted for a whole layer, as the clean dict we store. Raises InvalidMarkup."""
    data = loads(text)
    if not isinstance(data, dict) or set(data) - {'v', 'shapes'} or data.get('v') != VERSION:
        raise InvalidMarkup('The mark-up could not be read. Reload the page and try again.')
    return {'v': VERSION, 'shapes': clean_shapes(data.get('shapes'), page_count)}


def parse_shapes(text, page_count=None):
    """A JSON list of shapes to add to a layer (the compare page's clouds). Raises InvalidMarkup."""
    shapes = loads(text)
    if not isinstance(shapes, list) or not shapes:
        raise InvalidMarkup('Draw something first.')
    return clean_shapes(shapes, page_count)


def summary(shapes):
    """'3 clouds, 1 note' style description for the layer list."""
    counts = {}
    for s in shapes:
        counts[s['t']] = counts.get(s['t'], 0) + 1
    if not counts:
        return 'Nothing drawn yet'
    parts = []
    for kind in KINDS:
        n = counts.get(kind)
        if n:
            label = 'note' if kind == 'text' else KIND_LABELS[kind]
            plural = 'boxes' if label == 'box' else f'{label}s'
            parts.append(f'{n} {label if n == 1 else plural}')
    return ', '.join(parts)
