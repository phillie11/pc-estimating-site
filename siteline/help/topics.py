"""
Help topics: short markdown files in help/content/, one per area of the app, so a customer can tailor
the wording later without touching code.

Each file starts with a front matter block:

    ---
    title: RFIs
    pages: rfi_list rfi_detail rfi_create
    who: staff            (optional: staff, external or all; all is the default)
    ---
    What this page is for, then how to do the main job.

`pages` are URL names. The **?** button in the layout (help_tags.help_for) finds the topic for the page
being viewed. Where two files list the same page, the one written for the reader (staff or external)
wins over one for everyone. Files are read once and re-read when they change on disk.

The markdown is a small, safe subset, rendered here with no extra dependency: paragraphs, `## ` and
`### ` headings, `- ` and `1. ` lists, **bold**, *italic* and [links](/path/). Everything is escaped
first; a link must start with / or https://, and `{org}` in a link becomes the workspace's address.
"""
import html
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

CONTENT = Path(__file__).resolve().parent / 'content'
WHO = ('staff', 'external', 'all')

_lock = threading.Lock()
_cache = {'stamp': None, 'topics': {}}


@dataclass
class Topic:
    slug: str
    title: str
    pages: tuple = ()
    who: str = 'all'
    body: str = ''
    order: int = 100
    links: list = field(default_factory=list)

    def for_reader(self, internal):
        return self.who == 'all' or self.who == ('staff' if internal else 'external')

    def html(self, org_slug='', top=3):
        return render_markdown(self.body, org_slug, top)


def _parse(path):
    text = path.read_text(encoding='utf-8')
    meta, body = {}, text
    if text.startswith('---'):
        head, _, body = text[3:].partition('\n---')
        body = body.lstrip('\n')
        for line in head.strip().splitlines():
            key, _, value = line.partition(':')
            meta[key.strip().lower()] = value.strip()
    who = meta.get('who', 'all').lower()
    return Topic(slug=path.stem, title=meta.get('title') or path.stem.replace('-', ' ').capitalize(),
                 pages=tuple(meta.get('pages', '').split()), who=who if who in WHO else 'all', body=body.strip(),
                 order=int(meta['order']) if meta.get('order', '').isdigit() else 100)


def _stamp():
    files = sorted(CONTENT.glob('*.md'))
    return tuple((f.name, f.stat().st_mtime_ns) for f in files)


def topics():
    """Every topic, by slug. Cheap: a directory listing, then the parsed files from memory."""
    stamp = _stamp()
    if stamp != _cache['stamp']:
        with _lock:
            _cache['topics'] = {t.slug: t for t in (_parse(CONTENT / name) for name, _ in stamp)}
            _cache['stamp'] = stamp
    return _cache['topics']


def get(slug, internal=True):
    t = topics().get(slug)
    return t if t is not None and t.for_reader(internal) else None


def for_page(url_name, internal):
    """The topic for a page (by URL name) for this reader, or None."""
    if not url_name:
        return None
    best = None
    for t in topics().values():
        if url_name in t.pages and t.for_reader(internal):
            if t.who != 'all':
                return t
            best = best or t
    return best


def listed(internal):
    return sorted((t for t in topics().values() if t.for_reader(internal)), key=lambda t: (t.order, t.title.lower()))


# ---------------------------------------------------------------------------
# Markdown, the safe subset
# ---------------------------------------------------------------------------

LINK = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
BOLD = re.compile(r'\*\*(.+?)\*\*')
ITALIC = re.compile(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])')
ORDERED = re.compile(r'^\d+[.)]\s+')


def _inline(text, org_slug):
    out = html.escape(text, quote=True)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2)).replace('{org}', org_slug)
        if not (url.startswith('/') and not url.startswith('//')) and not url.startswith('https://'):
            return label
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    out = LINK.sub(link, out)
    out = BOLD.sub(r'<strong>\1</strong>', out)
    return ITALIC.sub(r'<em>\1</em>', out)


def render_markdown(text, org_slug='', top=3):
    """`top` is the heading level ## becomes: 3 under the panel's h2, 2 on a help page under its h1."""
    blocks, para, items, kind = [], [], [], None

    def flush():
        nonlocal para, items, kind
        if para:
            blocks.append('<p>' + _inline(' '.join(para), org_slug) + '</p>')
        if items:
            tag = 'ol' if kind == 'ol' else 'ul'
            blocks.append(f'<{tag}>' + ''.join(f'<li>{_inline(i, org_slug)}</li>' for i in items) + f'</{tag}>')
        para, items, kind = [], [], None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith('#'):
            flush()
            level = len(line) - len(line.lstrip('#'))
            tag = f'h{top}' if level <= 2 else f'h{top + 1}'
            blocks.append(f'<{tag}>{_inline(line.lstrip("#").strip(), org_slug)}</{tag}>')
            continue
        bullet = line[:2] in ('- ', '* ')
        ordered = ORDERED.match(line)
        if bullet or ordered:
            new_kind = 'ol' if ordered else 'ul'
            if para or (items and kind != new_kind):
                flush()
            kind = new_kind
            items.append(line[2:].strip() if bullet else line[ordered.end():].strip())
            continue
        if items and raw[:1] in (' ', '\t'):  # a wrapped list item
            items[-1] += ' ' + line
            continue
        if items:
            flush()
        para.append(line)
    flush()
    return '\n'.join(blocks)
