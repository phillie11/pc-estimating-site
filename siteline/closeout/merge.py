"""
The O&M manual as one PDF: a cover page, a contents page, then every component's PDFs in manual order,
each with a bookmark. Made by the `closeout.merge_om_pdf` job from a compiled manual (version N), reading
the files out of that version's zip, so the one PDF and the zip hold exactly the same files.

Every file in a manual came from a subcontractor or a person outside, so every one is treated as hostile:

  - only files that cleared the virus check are in the zip (closeout.services.compile_om), and the zip
    itself is only opened once it has cleared too; a file that was left out of the zip is left out here
    and listed;
  - only PDFs are merged. Anything else is listed on the contents page as "supplied separately, see the zip";
  - a PDF that is encrypted, broken, empty or over the size caps is skipped and listed with a plain reason;
    one bad file never stops the manual;
  - pages are copied into a fresh PDF one file at a time, and links, form fields and page actions are removed
    from the copy, so no JavaScript or action rides along. Nothing is fetched or executed, and nothing from
    a source file's own document catalogue (open actions, attachments, outlines) is carried over;
  - caps: MAX_FILE_MB per file, MAX_FILE_PAGES per file, MAX_TOTAL_MB and MAX_TOTAL_PAGES for the manual, and
    MAX_SECONDS for the job. Over a cap, the file is skipped and listed.
"""
import io
import logging
import re
import tempfile
import time
import unicodedata
import zipfile

from django.core.files import File
from django.utils import timezone

import pypdf
from tenancy import notify

from . import pdf
from .models import HandoverItem, OMCompilation
from .services import file_state

logger = logging.getLogger(__name__)

MAX_FILE_MB = 60
MAX_FILE_PAGES = 2000
MAX_TOTAL_MB = 250
MAX_TOTAL_PAGES = 5000
MAX_SECONDS = 20 * 60
MAX_TITLE = 200

ERROR = 'The manual could not be put together as one PDF. Try again, or contact support.'


def request_merge(comp, user):
    """
    Queue the one-PDF version of a compiled manual. Returns True when a job was queued, False when it is
    already being made or is ready (so a double click makes one job). Raises ValueError for a manual that
    has no files list (compiled before this feature: compile it again).
    """
    import jobs
    if not comp.can_merge:
        raise ValueError('Compile this manual again first, then it can be made as one PDF.')
    queued = OMCompilation.objects.filter(pk=comp.pk, status=OMCompilation.READY).exclude(
        merged_status__in=(OMCompilation.MERGE_QUEUED, OMCompilation.MERGE_READY)).update(
        merged_status=OMCompilation.MERGE_QUEUED, merged_error='', merged_by=user)
    if not queued:
        return False
    jobs.enqueue('closeout.merge_om_pdf', {'id': comp.pk}, org=comp.project.organization)
    return True


def gave_up(comp_id):
    OMCompilation.objects.filter(pk=comp_id, merged_status=OMCompilation.MERGE_QUEUED).update(
        merged_status=OMCompilation.MERGE_FAILED, merged_error=ERROR)


def _title(text):
    """A bookmark title: plain words on one line, no control characters."""
    out = ''.join(' ' if unicodedata.category(c)[0] == 'C' else c for c in str(text or ''))
    return ' '.join(out.split())[:MAX_TITLE] or 'Untitled'


def _is_pdf_name(path):
    return re.sub(r'[^a-z0-9.]', '', path.lower()).endswith('.pdf')


def _prepare(data):
    """
    One source PDF as a clean copy: (bytes, page count) or (None, reason). The pages are copied into a
    new document with their links, form fields and page actions removed.
    """
    if b'%PDF-' not in data[:1024]:
        return None, 'it is not a readable PDF'
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return None, 'it is password protected'
        count = len(reader.pages)
        if count == 0:
            return None, 'it has no pages'
        if count > MAX_FILE_PAGES:
            return None, f'it has more than {MAX_FILE_PAGES} pages'
        clean = pypdf.PdfWriter()
        for source in reader.pages:
            page = clean.add_page(source)
            for key in ('/Annots', '/AA'):
                if key in page:
                    del page[key]
        out = io.BytesIO()
        clean.write(out)
        return out.getvalue(), count
    except Exception as exc:  # noqa: BLE001 - a hostile file can fail in any way; it is skipped, never fatal
        logger.info('closeout.merge: skipped a PDF (%s)', type(exc).__name__)
        return None, 'it could not be read'


def merge_om(comp, now=time.monotonic):
    """Build the one-PDF manual for a ready compilation. Safe to run twice: a ready one is left as it is."""
    if comp.merged_status == OMCompilation.MERGE_READY:
        return comp
    project = comp.project
    org = project.organization
    if not comp.can_merge:
        raise ValueError('not a compiled manual')
    if file_state(comp.file, org) != HandoverItem.OK:
        raise RuntimeError('the zip has not cleared the virus check yet')  # the job retries later
    deadline = now() + MAX_SECONDS
    total_bytes = total_pages = 0
    parts = []        # [(section n, title, [(document title, kind, value, prepared bytes or None, pages)])]
    with comp.file.open('rb') as fh, zipfile.ZipFile(fh) as zf:
        sizes = {i.filename: i.file_size for i in zf.infolist()}
        for n, section_title, files in comp.manifest:
            rows = []
            for path, title, _size, _sha in files:
                if not _is_pdf_name(path):
                    rows.append((title, 'separate', path, None, 0))
                    continue
                reason = None
                if path not in sizes:
                    reason = 'it is missing from the zip'
                elif sizes[path] > MAX_FILE_MB * 1024 * 1024:
                    reason = f'it is over {MAX_FILE_MB} MB'
                elif total_bytes + sizes[path] > MAX_TOTAL_MB * 1024 * 1024:
                    reason = 'the manual is already at its size limit'
                elif now() >= deadline:
                    reason = 'there was not enough time to read it'
                if reason is None:
                    clean, result = _prepare(zf.read(path))
                    if clean is None:
                        reason = result
                    elif total_pages + result > MAX_TOTAL_PAGES:
                        reason = 'the manual is already at its page limit'
                if reason is not None:
                    rows.append((title, 'skipped', reason, None, 0))
                    continue
                total_bytes += sizes[path]
                total_pages += result
                rows.append((title, 'page', 0, clean, result))
            parts.append((n, section_title, rows))
    skipped = [[section, title, value] for _n, section, rows in parts for title, kind, value, _d, _p in rows
               if kind == 'skipped']

    # Front matter: its length depends on the page numbers it prints, so settle it in a few passes
    offset = 2
    for _ in range(5):
        position, entries = offset + 1, []
        for n, section, rows in parts:
            shown = []
            for title, kind, value, data, pages in rows:
                if kind == 'page':
                    shown.append((title, 'page', position))
                    position += pages
                else:
                    shown.append((title, kind, value))
            entries.append((n, section, shown))
        front = pdf.om_front_matter(project, org, comp.number, entries, comp.left_out)
        front_pages = len(pypdf.PdfReader(io.BytesIO(front)).pages)
        if front_pages == offset:
            break
        offset = front_pages
    else:
        raise RuntimeError('the contents pages would not settle')

    writer = pypdf.PdfWriter()
    writer.append(pypdf.PdfReader(io.BytesIO(front)), import_outline=False)
    writer.add_outline_item('Cover', 0)
    writer.add_outline_item('Contents', 1 if front_pages > 1 else 0)
    for n, section, rows in parts:
        item = None
        for title, kind, value, data, pages in rows:
            if kind != 'page':
                continue
            start = len(writer.pages)
            writer.append(pypdf.PdfReader(io.BytesIO(data)), import_outline=False)
            if item is None:
                item = writer.add_outline_item(_title(f'{n:02d} {section}'), start)
            writer.add_outline_item(_title(title), start, parent=item)
    writer.add_metadata({'/Title': _title(f'O&M manual - {project.name}'), '/Author': _title(org.name),
                         '/Subject': _title(f'{project.reference} version {comp.number}')})
    name = f'{project.reference}-OM-manual-v{comp.number}.pdf'
    with tempfile.TemporaryFile() as tmp:
        writer.write(tmp)
        size = tmp.tell()
        tmp.seek(0)
        comp.merged_file.save(name, File(tmp, name=name), save=False)
    comp.merged_status, comp.merged_size, comp.merged_pages = OMCompilation.MERGE_READY, size, len(writer.pages)
    comp.merged_skipped, comp.merged_error, comp.merged_at = skipped, '', timezone.now()
    comp.save()
    if comp.merged_by_id:
        from django.urls import reverse
        notify.alert([comp.merged_by], f'O&M manual version {comp.number} is ready as one PDF: {project.name}',
                     reverse('closeout_om', args=[org.slug, project.pk]), project=project, email=False)
    return comp
