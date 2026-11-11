"""
The tender reader: a bid's tender documents in, an editable bid brief out.

Staff who work in estimating pick PDFs already in the bid's Documents (current revisions; new ones are uploaded
there first, so every file goes through the upload checks and the virus check). The worker (ai/tasks.py) calls
read(), which:

  - takes only the chosen revisions of this bid, in this company;
  - skips any file the virus check hasn't cleared (or blocked), any that isn't a real PDF, and any over the size
    limit, saying which and why;
  - measures each file in a way that can't be understated: Anthropic's own count of its tokens (the free
    token-counting endpoint, which reads PDFs as the model does), or, if that can't be had, the larger of the
    pages found in the file and one page per 30 KB. A page count read from the file alone can be understated
    (pages kept in compressed object streams), so it is never trusted on its own for the limit or the cost;
  - stops adding files once the run would pass AI_TENDER_MAX_PAGES pages' worth (5,000 tokens a page) or
    AI_TENDER_MAX_MB, and says which were left out. The PDF text reader (documents/textindex.py) only
    serves search, so the PDFs go to the model as documents, which reads both the text and the page
    images, and the budget is held for the counted tokens;
  - sends the PDFs with the project's name, and nothing else from Siteline;
  - checks the answer against the schema again, caps every length, and saves it as BriefItem rows, each naming
    the document and page it came from.

Nothing is copied into the project's own fields: each key date has an Accept button that copies that one value,
recorded with who accepted it. Reading again replaces only the AI lines nobody has edited or accepted.
"""
import base64
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import BidBrief, BriefItem, TENDER_BRIEF

MAX_ITEMS = 90
MAX_TEXT = 2000
MAX_SUMMARY = 3000
BYTES_PER_PAGE_FLOOR = 30 * 1024  # without a token count, a file is taken as at least one page per 30 KB
SEVERITY_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, '': 4}


def choices(project):
    """Current, live PDF revisions on this bid: tender documents first."""
    from projects.models import DocumentRevision
    revs = (DocumentRevision.objects.filter(document__project=project, is_current=True)
            .exclude(status=DocumentRevision.WITHDRAWN).select_related('document')
            .order_by('document__category', 'document__reference', 'document__title'))
    out = [r for r in revs[:300] if (r.original_name or r.file.name or '').lower().endswith('.pdf')]
    out.sort(key=lambda r: r.document.category != 'TENDER')
    return out


def label(rev):
    d = rev.document
    return f'{d.reference + " " if d.reference else ""}{d.title} rev {rev.revision}'[:200]


def _block(data):
    return {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf',
                                           'data': base64.standard_b64encode(data).decode('ascii')}}


def robust_pages(data, found=None):
    """Pages to assume without a token count: never fewer than one per 30 KB of file."""
    import io

    from documents.services import pdf_page_count
    found = found or pdf_page_count(io.BytesIO(data)) or 0
    return max(found, -(-len(data) // BYTES_PER_PAGE_FLOOR), 1)


def gather(project, revision_ids, user=None):
    """
    ([(n, revision, bytes, pages, tokens)], [notes]) for the chosen revisions that may be sent, in the order
    chosen, within the limits. tokens is Anthropic's count of the file (plus a margin), or the cautious estimate
    from robust_pages when there's no count. Notes say, in plain words, what was left out and why.
    """
    from projects.files import scan_status
    from projects.models import DocumentRevision, FileScan

    from . import service
    max_pages, max_bytes = settings.AI_TENDER_MAX_PAGES, settings.AI_TENDER_MAX_MB * 1024 * 1024
    max_tokens = max_pages * service.TOKENS_PER_PAGE
    revs = {r.pk: r for r in DocumentRevision.objects.filter(
        document__project=project, organization=project.organization, pk__in=revision_ids,
    ).exclude(status=DocumentRevision.WITHDRAWN).select_related('document')}
    docs, notes, tokens_total, bytes_total = [], [], 0, 0
    for pk in revision_ids:
        rev = revs.get(pk)
        if rev is None:
            continue
        name = label(rev)
        if not (rev.original_name or rev.file.name or '').lower().endswith('.pdf'):
            notes.append(f'{name}: not a PDF, so it wasn\'t read.')
            continue
        scan = scan_status(rev.file, rev.organization)
        if scan is None:
            notes.append(f'{name}: the virus check hasn\'t finished, so it wasn\'t read. Try again shortly.')
            continue
        if scan != FileScan.CLEAN:
            notes.append(f'{name}: blocked by the virus check, so it wasn\'t read.')
            continue
        if (rev.size or 0) > max_bytes:
            notes.append(f'{name}: over {settings.AI_TENDER_MAX_MB} MB, so it wasn\'t read.')
            continue
        with rev.file.open('rb') as fh:
            data = fh.read(max_bytes + 1)
        if not data.startswith(b'%PDF') or len(data) > max_bytes:
            notes.append(f'{name}: not a readable PDF, so it wasn\'t read.')
            continue
        pages = robust_pages(data, rev.page_count)
        too_big = (f'{name}: left out, because one reading is limited to about {max_pages} pages and '
                   f'{settings.AI_TENDER_MAX_MB} MB in all. Read it on its own, or with fewer others.')
        if bytes_total + len(data) > max_bytes:
            notes.append(too_big)
            continue
        counted = service.count_tokens([_block(data)], org=project.organization, user=user)
        tokens = counted if counted is not None else pages * service.TOKENS_PER_PAGE
        if tokens_total + tokens > max_tokens:
            notes.append(too_big)
            continue
        tokens_total += tokens
        bytes_total += len(data)
        shown = rev.page_count or (-(-tokens // service.TOKENS_PER_PAGE) if counted is not None else pages)
        docs.append((len(docs) + 1, rev, data, shown, tokens))
    return docs, notes


def build_content(project, docs):
    content = []
    for n, rev, data, pages, _ in docs:
        content.append({'type': 'text', 'text': f'Document {n}: {label(rev)} (about {pages} pages)'})
        content.append(_block(data))
    content.append({'type': 'text', 'text': f'Project: {project.name[:200]}\n'
                                            'Read the documents above and fill in the bid brief.'})
    return content


def _str(v, limit):
    return v.strip()[:limit] if isinstance(v, str) else ''


def _int(v, lo, hi):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _date(v):
    try:
        d = date.fromisoformat(v) if isinstance(v, str) and len(v) == 10 else None
    except ValueError:
        return None
    return d if d and 2000 <= d.year <= 2100 else None


def save(project, data, docs, user=None):
    """Validate the answer and save the brief. Returns the BidBrief."""
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError('no items')
    by_n = {n: (rev, pages) for n, rev, _, pages, _ in docs}
    sections = set(BriefItem.SECTIONS)
    kinds = {k for k, _ in BriefItem.DATE_KINDS}
    severities = {k for k, _ in BriefItem.SEVERITY_CHOICES}
    rec = data.get('recommendation') if data.get('recommendation') in dict(BidBrief.RECOMMENDATION_CHOICES) else ''
    with transaction.atomic():
        brief, _ = BidBrief.objects.select_for_update().get_or_create(project=project,
                                                                      defaults={'organization': project.organization})
        brief.items.filter(ai_drafted=True, edited=False, accepted_at__isnull=True).delete()
        if not brief.edited:
            brief.recommendation = rec
            brief.summary = _str(data.get('summary'), MAX_SUMMARY)
            brief.value_basis = _str(data.get('value_basis'), MAX_TEXT)
        brief.ai_drafted = True
        brief.sources = [{'revision': rev.pk, 'label': label(rev), 'pages': pages} for _, rev, _, pages, _ in docs]
        brief.generated_at, brief.generated_by = timezone.now(), user
        brief.save()
        rows = []
        for i, item in enumerate(data['items'][:MAX_ITEMS]):
            if not isinstance(item, dict) or item.get('section') not in sections:
                continue
            text = _str(item.get('text'), MAX_TEXT)
            if not text:
                continue
            section = item['section']
            doc = by_n.get(_int(item.get('doc'), 1, len(docs)) or 0)
            page = _int(item.get('page'), 1, (doc[1] if doc else 0) + 5) if doc else None
            low, high = _int(item.get('value_low'), 1, 10 ** 11), _int(item.get('value_high'), 1, 10 ** 11)
            severity = item.get('severity') if section == BriefItem.RISK and item.get('severity') in severities else ''
            rows.append(BriefItem(
                organization=project.organization, brief=brief, section=section, label=_str(item.get('label'), 120),
                text=text, ai_drafted=True,
                date=_date(item.get('date')) if section == BriefItem.DATE else None,
                date_kind=item.get('date_kind') if section == BriefItem.DATE and item.get('date_kind') in kinds else '',
                severity=severity, rank=SEVERITY_ORDER[severity],
                value_low=Decimal(low) if low and section == BriefItem.SCOPE else None,
                value_high=Decimal(high) if high and section == BriefItem.SCOPE else None,
                source_revision=doc[0] if doc else None, source_label=label(doc[0]) if doc else '', source_page=page,
                order=BriefItem.SECTIONS.index(section) * 1000 + i))
        BriefItem.objects.bulk_create(rows)
    return brief


def read(project, user, revision_ids, run=None):
    """Read the chosen documents into the bid's brief (the worker calls this). Returns (brief, notes)."""
    from . import service
    service.preflight(project.organization, user)  # before reading or counting anything
    docs, notes = gather(project, revision_ids, user)
    if not docs:
        raise service.CallFailed(' '.join(notes) or 'None of the chosen documents could be read.')
    pages, doc_tokens = sum(d[3] for d in docs), sum(d[4] for d in docs)
    system = service.system_for(project.organization, TENDER_BRIEF)
    # The counted documents, plus a generous estimate for the instructions and labels around them
    tokens = doc_tokens + service.input_estimate(chars=len(system) + 400 * len(docs))
    data = service.call(org=project.organization, user=user, task=TENDER_BRIEF, project=project, run=run,
                        content=build_content(project, docs), input_tokens=tokens, system=system,
                        note=f'{len(docs)} document{"s" if len(docs) != 1 else ""}, about {pages} pages, '
                             f'{doc_tokens:,} tokens held')
    try:
        brief = save(project, data, docs, user)
    except ValueError:
        raise service.CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
    return brief, notes


def accept(item, user):
    """Copy one key date into the project's own field. Returns the field's label, or None if there isn't one."""
    from datetime import datetime, time
    field = item.project_field
    if field is None:
        return None
    project = item.brief.project
    value = item.date
    if field == 'tender_due':
        value = timezone.make_aware(datetime.combine(item.date, time(12, 0)))
    setattr(project, field, value)
    project.save(update_fields=[field, 'updated_at'])
    item.accepted_at, item.accepted_by = timezone.now(), user
    item.save(update_fields=['accepted_at', 'accepted_by'])
    return project._meta.get_field(field).verbose_name
