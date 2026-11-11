"""
The PDF text reader behind documents.textindex.EXTRACTOR (pypdf, BSD licence).

    extract(fh, max_pages, deadline) -> (text, pages_read, truncated)

Every PDF is hostile. This module only ever asks pypdf for the words on each page. It never touches
anything that could act on the reader:

  - no JavaScript, open actions, links or form actions are read or run (pypdf never runs them, and
    nothing here asks for them);
  - no attachments are extracted and no outside address is fetched;
  - encrypted files are refused rather than opened (even with a blank password);
  - pypdf's own size limits are lowered for the length of one read (pypdf.apply_configuration is per
    thread, so it never changes another request), and a page whose content stream is over
    MAX_CONTENT_BYTES, encoded or decoded, is skipped rather than read;
  - the deadline is checked before every page and before every content operation on a page;
  - pypdf's warnings go to the log (logger "pypdf") and never to the screen.

It raises ExtractionFailed for a file that is encrypted, broken or not a PDF, and nothing else: any
other error from pypdf is turned into ExtractionFailed too.
"""
import logging
import time
import warnings

logger = logging.getLogger(__name__)

MAX_CONTENT_BYTES = 4 * 1024 * 1024   # a page whose drawing instructions are bigger than this is skipped
MAX_TEXT_CHARS = 1_000_000            # stop reading once this much text is held (textindex keeps fewer)

try:
    import pypdf
    from pypdf.errors import PdfReadWarning
    warnings.filterwarnings('ignore', category=PdfReadWarning)
except ImportError:  # pragma: no cover - requirements.txt pins it
    pypdf = None


def installed():
    return pypdf is not None


class _Stop(Exception):
    """Raised from inside pypdf's page loop when the deadline passes."""


def _limits():
    mb = MAX_CONTENT_BYTES
    return dict(
        maximum_declared_stream_length=mb * 2, array_based_stream_maximum_output_length=mb,
        zlib_maximum_output_length=mb, lzw_maximum_output_length=mb, run_length_maximum_output_length=mb,
        brotli_maximum_output_length=mb, jbig2_maximum_output_length=mb, image_maximum_buffer_size=mb,
        stream_decoding_work_maximum_length=mb * 4, xmp_maximum_input_length=64 * 1024,
        jbig2dec_binary=None,  # never start another program for a hostile file
        page_tree_maximum_entries=20_000, xform_maximum_invocations_per_extraction=500)


def _too_big(page):
    """True when the page's content stream(s) are over MAX_CONTENT_BYTES as stored (before decoding)."""
    contents = page.get('/Contents')
    contents = contents.get_object() if contents is not None else None
    parts = list(contents) if isinstance(contents, list) else [contents]
    total = 0
    for part in parts:
        part = part.get_object() if part is not None else None
        total += len(getattr(part, '_data', b'') or b'')
    return total > MAX_CONTENT_BYTES


def extract(fh, max_pages, deadline):
    """See the module docstring. `fh` is an open binary file; the caller closes it."""
    from .textindex import ExtractionFailed
    if pypdf is None:
        raise ExtractionFailed('no PDF reader installed')
    try:
        return _extract(fh, max_pages, deadline)
    except ExtractionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - nothing from a hostile file may escape as anything else
        logger.info('documents.pdftext: could not read a PDF (%s)', type(exc).__name__)
        raise ExtractionFailed(f'could not be read: {type(exc).__name__}') from None


def _extract(fh, max_pages, deadline):
    from .textindex import ExtractionFailed
    chunks, size, read, truncated = [], 0, 0, False

    def check_time(*_args):
        if time.monotonic() >= deadline:
            raise _Stop

    with pypdf.apply_configuration(**_limits()):
        reader = pypdf.PdfReader(fh, strict=False)
        if reader.is_encrypted:
            raise ExtractionFailed('encrypted')
        total = len(reader.pages)
        for index in range(total):
            if index >= max_pages or size >= MAX_TEXT_CHARS:
                truncated = True
                break
            if time.monotonic() >= deadline:
                truncated = True
                break
            try:
                page = reader.pages[index]
                if _too_big(page):
                    logger.info('documents.pdftext: skipped page %s, its content is too big', index + 1)
                    read += 1
                    continue
                text = page.extract_text(visitor_operand_before=check_time)
            except _Stop:
                truncated = True
                break
            except Exception as exc:  # noqa: BLE001 - one bad page doesn't lose the others
                logger.info('documents.pdftext: skipped page %s (%s)', index + 1, type(exc).__name__)
                read += 1
                continue
            read += 1
            if text:
                chunks.append(text)
                size += len(text)
    return '\n'.join(chunks), read, truncated
