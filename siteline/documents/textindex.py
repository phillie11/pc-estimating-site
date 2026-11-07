"""
Searching inside PDFs: reading the words out of each PDF revision when it is uploaded, keeping
them on a RevisionText row, and finding documents by them (the register's search box and the
global search, help/search.py).

Every uploaded PDF is treated as hostile:

  - nothing is read until the virus check has cleared the file (as documents.file_facts);
  - files over MAX_FILE_MB are not read at all, and at most MAX_PAGES pages and MAX_CHARS
    characters are kept;
  - the reader gets a deadline (TIME_LIMIT_SECONDS) it must check between pages;
  - files up to INLINE_MB are read straight after the upload commits; bigger ones by the
    `documents.extract_text` background job;
  - any failure just leaves the file unsearchable by its words. It never stops an upload.

A scanned drawing has no text layer: it is marked "no text" and the document page says it can only
be found by its number and title. There is no OCR.

THE READER. Reading PDF text needs a PDF parser, and none is installed yet: pypdf was held back
over security advisories (see the Release 3 wave 2 report). Until one is, EXTRACTOR is None,
`available()` is False, nothing is queued at upload, and the document page says searching inside
files isn't switched on. To switch it on, set EXTRACTOR to a function

    extractor(fh, max_pages, deadline) -> (text, pages_read, truncated)

that reads the open binary file `fh`, stops after `max_pages` pages or when `time.monotonic()`
passes `deadline` (returning what it has, truncated=True), and raises ExtractionFailed for a file it
can't read. Then run `manage.py index_document_text` once to read the PDFs already uploaded.
"""
import logging
import time
import unicodedata
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

import jobs

logger = logging.getLogger(__name__)

MAX_FILE_MB = 50
INLINE_MB = 5
MAX_PAGES = 500
MAX_CHARS = 500_000
TIME_LIMIT_SECONDS = 60
MIN_QUERY = 3          # searching inside files needs at least this many characters
SCAN_WAIT_TRIES = 120  # as documents.file_facts: an hour or two for the virus check

EXTRACTOR = None


class ExtractionFailed(Exception):
    """The reader couldn't read this file (broken, encrypted, or not really a PDF)."""


def available():
    return EXTRACTOR is not None


def is_pdf(rev):
    return (rev.original_name or rev.file.name or '').lower().endswith('.pdf')


def normalise(text):
    """One line of plain words: no control characters, single spaces, at most MAX_CHARS."""
    out = ''.join(ch if unicodedata.category(ch)[0] != 'C' else ' ' for ch in text or '')
    out = ' '.join(out.split())
    return out[:MAX_CHARS], len(out) > MAX_CHARS


def snippet(text, q, width=70):
    """A short piece of `text` around the first match of `q`, for a search result; '' if no match."""
    if not text or not q:
        return ''
    at = text.lower().find(q.lower())
    if at < 0:
        return ''
    start, end = max(0, at - width), min(len(text), at + len(q) + width)
    while start > 0 and text[start - 1] != ' ' and at - start < width + 15:
        start -= 1
    while end < len(text) and text[end] != ' ' and end - at < width + 15 + len(q):
        end += 1
    return ('…' if start > 0 else '') + text[start:end].strip() + ('…' if end < len(text) else '')


# ---------------------------------------------------------------------------
# Reading a revision
# ---------------------------------------------------------------------------

def read(rev):
    """
    Read one revision's words into its RevisionText row, if the file is clean and small enough.
    Returns the row, or None when there's nothing to do yet (no reader, not a PDF, or the virus check
    hasn't finished: the caller decides whether to wait).
    """
    from projects.files import scan_status
    from projects.models import FileScan

    from .models import RevisionText

    if not available() or not is_pdf(rev):
        return None
    scan = scan_status(rev.file, rev.organization)
    if scan is None:
        return None
    if scan != FileScan.CLEAN:
        return _store(rev, RevisionText.BLOCKED)  # never opened
    if (rev.size or 0) > MAX_FILE_MB * 1024 * 1024:
        return _store(rev, RevisionText.TOO_BIG)
    try:
        with rev.file.open('rb') as fh:
            raw, pages, truncated = EXTRACTOR(fh, MAX_PAGES, time.monotonic() + TIME_LIMIT_SECONDS)
    except ExtractionFailed:
        return _store(rev, RevisionText.FAILED)
    except Exception:  # noqa: BLE001 - a hostile file must never break an upload or the worker
        logger.warning('documents.textindex: reading revision %s failed', rev.pk, exc_info=True)
        return _store(rev, RevisionText.FAILED)
    text, cut = normalise(raw)
    status = RevisionText.DONE if text else RevisionText.NO_TEXT
    return _store(rev, status, text=text, pages=pages or 0, truncated=bool(truncated or cut))


def _store(rev, status, text='', pages=0, truncated=False):
    from .models import RevisionText
    row, _ = RevisionText.objects.update_or_create(
        revision=rev, defaults={'status': status, 'text': text, 'pages_read': pages, 'truncated': truncated})
    return row


def queue(rev):
    """
    After a PDF revision is saved: read small files as soon as the upload commits, and leave bigger
    ones (or any whose virus check hasn't finished) to the background job. Does nothing without a reader.
    """
    if not available() or not is_pdf(rev):
        return
    from tenancy.context import org_context
    org = rev.organization

    def run():
        with org_context(org):
            try:
                if (rev.size or 0) <= INLINE_MB * 1024 * 1024 and read(rev) is not None:
                    return
            except Exception:  # noqa: BLE001 - never fail the upload that triggered it
                logger.warning('documents.textindex: inline read of revision %s failed', rev.pk, exc_info=True)
            enqueue(rev)

    transaction.on_commit(run)


def enqueue(rev, tries=0, later=False, key='upload'):
    jobs.enqueue('documents.extract_text', {'revision_id': rev.pk, 'tries': tries, 'key': key}, org=rev.organization,
                 run_after=timezone.now() + timedelta(minutes=1) if later else None,
                 idempotency_key=f'extract-text:{key}:{rev.organization_id}:{rev.pk}:{tries}')


# ---------------------------------------------------------------------------
# Finding documents and saying what can be found
# ---------------------------------------------------------------------------

def matching_revisions(revisions, q):
    """RevisionText rows among these revisions (a queryset) whose words contain q."""
    from .models import RevisionText
    if len(q or '') < MIN_QUERY:
        return RevisionText.objects.none()
    return RevisionText.objects.filter(revision__in=revisions, status=RevisionText.DONE, text__icontains=q)


def page_note(rev):
    """
    (sentence, tone) for the document page: whether the words in this revision can be searched.
    Kept plain for externals as well as staff.
    """
    from .models import RevisionText
    if rev is None or not is_pdf(rev):
        return '', ''
    row = getattr(rev, '_text_row', None)
    if row is None:
        row = RevisionText.objects.filter(revision=rev).only('status', 'truncated', 'pages_read').first()
    by_name = 'Search still finds it by its number and title.'
    if row is None:
        if not available():
            return f'Searching the words inside PDFs isn\'t switched on yet. {by_name}', 'muted'
        return f'The words in rev {rev.revision} are still being read for search.', 'info'
    if row.status == RevisionText.DONE:
        extra = f' (the first {row.pages_read} pages)' if row.truncated and row.pages_read else (
            ' (the first part)' if row.truncated else '')
        return f'The words in rev {rev.revision}{extra} can be searched.', 'good'
    if row.status == RevisionText.NO_TEXT:
        return (f'Rev {rev.revision} has no text in it, so it is probably a scan. Its words can\'t be searched. '
                f'{by_name}'), 'warn'
    if row.status == RevisionText.TOO_BIG:
        return f'Rev {rev.revision} is too big to read for search ({MAX_FILE_MB} MB at most). {by_name}', 'warn'
    return f'The words in rev {rev.revision} couldn\'t be read for search. {by_name}', 'warn'
