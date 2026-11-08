"""
RFI drafting: a bid's tender documents in, draft RFIs to the client's design team out.

Estimating staff pick PDFs from the bid's Documents on the Bid brief page (the same picker and gates as the
tender reader: virus check, size, Anthropic's token count against AI_TENDER_MAX_PAGES). The worker
(ai/tasks.py) calls draft(), which sends:

  - those PDFs, as documents;
  - optionally, the bid brief's risks and "not in pack" lines (label and text only);
  - the subjects of the RFIs already raised on this bid, so they aren't asked again;
  - the project's name. Nothing else: no names, no prices, nothing from another project or company.

Each answer is checked (category and priority from the RFI lists, document numbers and pages in range, texts
capped, subjects not already raised) and saved as an rfis.RFI with status DRAFT, internal visibility,
ai_drafted set, the source document and page in document_reference, and why it matters in ai_basis (staff
only). A draft reaches nobody outside, whatever its visibility, until staff issue it through the RFI's own
workflow (rfis/workflow.py "issue"); it raises no notification until then.
"""
from .models import RFI_DRAFTING

MAX_RFIS = 40
MAX_SUBJECT = 120
MAX_QUESTION = 2000
MAX_WHY = 1000
MAX_EXISTING = 300


def _norm(text):
    return ' '.join((text or '').casefold().split())


def brief_lines(project):
    """The bid brief's risks and missing items as text, or '' when there's no brief."""
    from .models import BidBrief, BriefItem
    brief = BidBrief.objects.filter(project=project).first()
    if brief is None:
        return ''
    out = []
    for i in brief.items.filter(section__in=(BriefItem.RISK, 'pack_missing'))[:60]:
        label = f'{i.label}: ' if i.label else ''
        out.append(f'- {i.get_section_display()}: {label}{" ".join(i.text.split())[:400]}')
    return ('From our bid brief (for context; ask only what the documents leave open):\n' + '\n'.join(out)) if out else ''


def existing_subjects(project):
    from rfis.models import RFI
    return list(RFI.objects.filter(project=project).exclude(status=RFI.STATUS_VOID)
                .order_by('-number').values_list('title', flat=True)[:MAX_EXISTING])


def draft(project, user, revision_ids, use_brief=False, run=None):
    """Draft RFIs from the chosen documents and save them as internal drafts. Returns (rfis, notes)."""
    from . import brief, service
    service.preflight(project.organization, user)  # switched on, allowed, budget left: before anything is sent
    docs, notes = brief.gather(project, revision_ids, user)
    if not docs:
        raise service.CallFailed(' '.join(notes) or 'None of the chosen documents could be read.')
    extra = [f'Project: {project.name[:200]}']
    if use_brief:
        b = brief_lines(project)
        if b:
            extra.append(b)
    subjects = existing_subjects(project)
    if subjects:
        extra.append('RFIs already raised (don\'t ask these again):\n' + '\n'.join(f'- {s[:150]}' for s in subjects))
    extra.append(f'Categories: {", ".join(_categories())}. Priorities: LOW, NORMAL, HIGH, URGENT.\n'
                 'Draft the RFIs.')
    content = []
    for n, rev, data, pages, _ in docs:
        content.append({'type': 'text', 'text': f'Document {n}: {brief.label(rev)} (about {pages} pages)'})
        content.append(brief._block(data))
    content.append({'type': 'text', 'text': '\n\n'.join(extra)})
    system = service.system_for(project.organization, RFI_DRAFTING)
    doc_tokens = sum(d[4] for d in docs)
    tokens = doc_tokens + service.input_estimate(chars=len(system) + sum(len(e) for e in extra) + 400 * len(docs))
    data = service.call(org=project.organization, user=user, task=RFI_DRAFTING, project=project, run=run,
                        content=content, input_tokens=tokens, system=system,
                        note=f'{len(docs)} document{"s" if len(docs) != 1 else ""}, {doc_tokens:,} tokens held')
    try:
        rfis = save(project, data, docs, user, subjects)
    except ValueError:
        raise service.CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
    return rfis, notes


def _categories():
    from rfis.models import RFI
    return [k for k, _ in RFI.CATEGORY_CHOICES]


def _s(v, limit):
    return v.strip()[:limit] if isinstance(v, str) else ''


def save(project, data, docs, user, subjects=()):
    """Validate the answer and save each RFI as an internal DRAFT. Returns the RFIs saved."""
    from django.db import transaction

    from projects.models import PRIORITY_CHOICES
    from rfis.models import RFI

    from .brief import label
    items = data.get('rfis') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('no rfis')
    by_n = {n: (rev, pages) for n, rev, _, pages, _ in docs}
    categories, priorities = set(_categories()), {k for k, _ in PRIORITY_CHOICES}
    seen = {_norm(s) for s in subjects}
    saved = []
    with transaction.atomic():
        for item in items:
            if len(saved) >= MAX_RFIS or not isinstance(item, dict):
                continue
            subject = ' '.join(_s(item.get('subject'), 400).split())[:MAX_SUBJECT]
            question = _s(item.get('question'), MAX_QUESTION)
            if not subject or not question or _norm(subject) in seen:
                continue
            seen.add(_norm(subject))
            n = item.get('doc') if isinstance(item.get('doc'), int) and not isinstance(item.get('doc'), bool) else 0
            doc = by_n.get(n)
            page = item.get('page') if isinstance(item.get('page'), int) and not isinstance(item.get('page'), bool) \
                else 0
            ref = ''
            if doc:
                ref = label(doc[0]) + (f', page {page}' if 1 <= page <= doc[1] + 5 else '')
            rfi = RFI(project=project, title=subject, question=question,
                      category=item.get('category') if item.get('category') in categories else 'OTHER',
                      priority=item.get('priority') if item.get('priority') in priorities else 'NORMAL',
                      status=RFI.STATUS_DRAFT, visibility=RFI.VIS_INTERNAL, document_reference=ref[:200],
                      ai_drafted=True, ai_basis=_s(item.get('why'), MAX_WHY))
            rfi.save(actor=user)
            saved.append(rfi)
    return saved
