"""
Issuing transmittals, acknowledging them, the "usual recipients" quick path, and the
checksum / page count worked out for each uploaded file.
"""
import hashlib
import logging

from django.db import transaction
from django.utils import timezone

import jobs
from core.workflow import apply_transition
from projects.models import DocumentRevision, ProjectMember
from tenancy import notify
from tenancy.models import log_activity

from .models import DistributionRule, Transmittal, TransmittalItem, TransmittalRecipient, recipient_q

logger = logging.getLogger(__name__)

# Up to this many files in one bulk upload get their checksum and page count during the
# request; bigger batches leave it to the background worker.
INLINE_PROCESS_LIMIT = 10
INLINE_PAGE_COUNT_MB = 25


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def recipient_choices(project):
    """(users, companies) a transmittal can go to: the project's people (not tenderers) and their companies."""
    from directory.models import Subcontractor
    from projects.access import assignable_users
    members = ProjectMember.objects.filter(project=project).exclude(role=ProjectMember.ROLE_BIDDER)
    bidders = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_BIDDER).values('user_id')
    users = assignable_users(project).exclude(pk__in=bidders)
    companies = Subcontractor.objects.filter(pk__in=members.filter(subcontractor__isnull=False)
                                             .values('subcontractor_id')).order_by('company_name')
    return users, companies


def usual_recipients(project, documents):
    """
    Who a document normally goes to: the project's distribution rules for its discipline
    (and the "every discipline" rules), else whoever the last transmittal of it went to,
    else every client, consultant and appointed subcontractor (what happened before
    transmittals). Returns (users, companies).
    """
    disciplines = {(d.discipline or '').strip().lower() for d in documents}
    rules = [r for r in DistributionRule.objects.filter(project=project).select_related('user', 'subcontractor')
             if not r.discipline or r.discipline.strip().lower() in disciplines]
    if rules:
        return _dedupe([r.user for r in rules if r.user], [r.subcontractor for r in rules if r.subcontractor], project)
    last = Transmittal.objects.filter(project=project, status__in=Transmittal.SENT_STATUSES,
                                      items__revision__document__in=[d.pk for d in documents]) \
        .order_by('-issued_at', '-number').first()
    if last is not None:
        rows = list(last.recipients.select_related('user', 'subcontractor'))
        return _dedupe([r.user for r in rows if r.user], [r.subcontractor for r in rows if r.subcontractor], project)
    members = ProjectMember.objects.filter(project=project, role__in=[
        ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT, ProjectMember.ROLE_SUBCONTRACTOR]).select_related('user')
    return _dedupe([m.user for m in members], [], project)


def eligible(project, users, companies):
    """
    The people and companies who can still receive this project's documents: active staff of
    the company, or active collaborators still on the project (not as a tenderer); companies with
    someone still on the project. Rules and old transmittals can name people who have since left.
    """
    allowed_users, allowed_companies = recipient_choices(project)
    user_ids = set(allowed_users.filter(pk__in=[u.pk for u in users if u]).values_list('pk', flat=True))
    company_ids = set(allowed_companies.filter(pk__in=[c.pk for c in companies if c]).values_list('pk', flat=True))
    return [u for u in users if u and u.pk in user_ids], [c for c in companies if c and c.pk in company_ids]


def _dedupe(users, companies, project=None):
    seen_u, seen_c = set(), set()
    users = [u for u in users if u.is_active and not (u.pk in seen_u or seen_u.add(u.pk))]
    companies = [c for c in companies if not (c.pk in seen_c or seen_c.add(c.pk))]
    return eligible(project, users, companies) if project is not None else (users, companies)


def describe(users, companies):
    names = [u.full_name or u.email for u in users] + [c.company_name for c in companies]
    if not names:
        return 'nobody yet'
    return ', '.join(names[:4]) + (f' and {len(names) - 4} more' if len(names) > 4 else '')


def set_recipients(transmittal, users, companies):
    """Replace a draft's recipients."""
    transmittal.recipients.all().delete()
    for u in users:
        TransmittalRecipient.objects.create(transmittal=transmittal, user=u, name=(u.full_name or u.email)[:200])
    for c in companies:
        TransmittalRecipient.objects.create(transmittal=transmittal, subcontractor=c, name=c.company_name[:200])


def set_items(transmittal, revisions):
    transmittal.items.exclude(revision__in=[r.pk for r in revisions]).delete()
    have = set(transmittal.items.values_list('revision_id', flat=True))
    for r in revisions:
        if r.pk not in have:
            item = TransmittalItem(transmittal=transmittal, revision=r)
            item.snapshot()
            item.save()


# ---------------------------------------------------------------------------
# Issue and acknowledge
# ---------------------------------------------------------------------------

class IssueRefused(ValueError):
    pass


def issue(actor, transmittal):
    """
    Issue a draft: share its revisions with the recipients, tell them, and queue the issue
    sheet. Work-in-progress revisions become shared (externals only ever see what has been
    shared with them). The ball is with the recipients until they acknowledge.
    """
    items = list(transmittal.items.select_related('revision__document'))
    recipients = list(transmittal.recipients.select_related('user', 'subcontractor'))
    if not items:
        raise IssueRefused('Add at least one document before issuing.')
    if not recipients:
        raise IssueRefused('Add at least one recipient before issuing.')
    users, companies = eligible(transmittal.project, [r.user for r in recipients if r.user_id],
                                [r.subcontractor for r in recipients if r.subcontractor_id])
    keep = {('u', u.pk) for u in users} | {('c', c.pk) for c in companies}
    leavers = [r for r in recipients if (('u', r.user_id) if r.user_id else ('c', r.subcontractor_id)) not in keep]
    if len(leavers) == len(recipients):
        raise IssueRefused('Everyone it is addressed to is no longer on this project. Choose new recipients.')
    withdrawn = [i for i in items if i.revision.status == DocumentRevision.WITHDRAWN]
    if withdrawn:
        raise IssueRefused(f'{withdrawn[0].revision.document.label} rev {withdrawn[0].revision.revision} '
                           'has been withdrawn, so it can\'t be issued.')
    with transaction.atomic():
        for r in leavers:  # left the project since the draft was made: it doesn't go to them
            r.delete()
        recipients = [r for r in recipients if r not in leavers]
        for item in items:
            item.snapshot()
            item.save()
            if item.revision.status == DocumentRevision.WIP:
                item.revision.set_status(DocumentRevision.SHARED)
            item.revision.mark_shared()
        _set_ball(transmittal)
        apply_transition(transmittal, 'issue', actor)
        jobs.enqueue('documents.transmittal_sheet', {'transmittal_id': transmittal.pk}, org=transmittal.organization,
                     idempotency_key=f'transmittal-sheet:{transmittal.organization_id}:{transmittal.pk}')
        people = [p for r in recipients for p in r.people()]
        notify.alert(people, f'{transmittal.reference} {transmittal.get_purpose_display().lower()}: {transmittal.title}',
                     transmittal.get_absolute_url(), project=transmittal.project, actor=actor, org=transmittal.organization)
        external = any(r.subcontractor_id or not _is_staff(r.user, transmittal) for r in recipients)
        log_activity(actor, 'issued', f'{transmittal.reference} {transmittal.title}', project=transmittal.project,
                     url=transmittal.get_absolute_url(), internal_only=not external)
    return transmittal


def _is_staff(user, record):
    from core.access import is_staff
    return user is not None and is_staff(user, record.organization_id)


def _set_ball(transmittal):
    """Ball in court: the first recipient still to acknowledge (person and/or company); nobody once all have."""
    pending = [r for r in transmittal.recipients.all().order_by('pk') if r.acknowledged_at is None]
    transmittal.ball_in_court_user = next((r.user for r in pending if r.user_id), None)
    transmittal.ball_in_court_company = next((r.subcontractor for r in pending if r.subcontractor_id), None)


def acknowledge(actor, transmittal, recipient=None, note=''):
    """
    A recipient confirming they have received the transmittal (for themselves and any
    company they represent on it), or staff recording receipt for one recipient row.
    Returns how many recipient rows were acknowledged.
    """
    now = timezone.now()
    with transaction.atomic():
        locked = Transmittal.objects.select_for_update().filter(pk=transmittal.pk).first()
        if locked is None or locked.status != Transmittal.STATUS_ISSUED:
            return 0  # drafts, cancelled and fully received transmittals take no receipts
        if recipient is not None:
            rows = transmittal.recipients.filter(pk=recipient.pk, acknowledged_at__isnull=True)
        else:
            rows = transmittal.recipients.filter(recipient_q(actor), acknowledged_at__isnull=True)
        rows = list(rows)
        for r in rows:
            r.acknowledged_at, r.acknowledged_by, r.acknowledged_note = now, actor, note[:200]
            r.save(update_fields=['acknowledged_at', 'acknowledged_by', 'acknowledged_note'])
        if not rows:
            return 0
        transmittal.refresh_from_db()
        _set_ball(transmittal)
        who = ', '.join(r.name for r in rows)
        if transmittal.recipients.filter(acknowledged_at__isnull=True).exists():
            transmittal.save(actor=actor, audit_action='acknowledged', audit_note=f'Received by {who}')
        else:
            apply_transition(transmittal, 'complete', actor, note=f'Received by {who}')
            if transmittal.issued_by_id:
                notify.alert([transmittal.issued_by], f'{transmittal.reference} received by everyone',
                             transmittal.get_absolute_url(), project=transmittal.project, actor=actor,
                             org=transmittal.organization, email=False)
        log_activity(actor, 'acknowledged', transmittal.reference, project=transmittal.project,
                     url=transmittal.get_absolute_url(), internal_only=True)
    return len(rows)


def quick_issue(actor, project, revisions, purpose=Transmittal.PURPOSE_INFORMATION, title=''):
    """
    "Issue to usual recipients": one transmittal for these revisions to the people they
    normally go to, issued straight away. Returns it, or None when there is nobody to send to.
    """
    docs = [r.document for r in revisions]
    users, companies = usual_recipients(project, docs)
    if not users and not companies:
        return None
    if not title:
        first = revisions[0]
        title = f'{first.document.label} rev {first.revision}'
        if len(revisions) > 1:
            title = f'{len(revisions)} documents'
    with transaction.atomic():
        tr = Transmittal(project=project, title=title[:200], purpose=purpose)
        tr.save(actor=actor)
        set_items(tr, revisions)
        set_recipients(tr, users, companies)
        issue(actor, tr)
    return tr


def transmittals_for(request, project):
    """Transmittals this person may see: all for staff; for externals, only issued ones addressed to them."""
    from projects.access import is_internal
    qs = Transmittal.objects.filter(project=project)
    if is_internal(request):
        return qs
    rows = TransmittalRecipient.objects.filter(recipient_q(request.user, project=project),
                                               transmittal__project=project)
    return qs.filter(pk__in=rows.values('transmittal_id'), status__in=Transmittal.SENT_STATUSES)


# ---------------------------------------------------------------------------
# Checksums and page counts
# ---------------------------------------------------------------------------

def file_facts(fh, name):
    """(sha256 hex, page count or None) for an open file, read in chunks."""
    h = hashlib.sha256()
    if hasattr(fh, 'seek'):
        fh.seek(0)
    for chunk in iter(lambda: fh.read(1024 * 1024), b''):
        h.update(chunk)
    pages = None
    if name.lower().endswith('.pdf'):
        pages = pdf_page_count(fh)
    if hasattr(fh, 'seek'):
        fh.seek(0)
    return h.hexdigest(), pages


def pdf_page_count(fh):
    try:
        from pypdf import PdfReader
        fh.seek(0)
        return len(PdfReader(fh, strict=False).pages)
    except Exception:  # noqa: BLE001 - a broken or encrypted PDF just has no page count
        return None


def facts_from_upload(uploaded):
    """Checksum (always) and page count (if small enough) for a file still in the request."""
    h = hashlib.sha256()
    for chunk in uploaded.chunks():
        h.update(chunk)
    pages = None
    if uploaded.name.lower().endswith('.pdf') and uploaded.size <= INLINE_PAGE_COUNT_MB * 1024 * 1024:
        pages = pdf_page_count(uploaded)
    uploaded.seek(0)
    return h.hexdigest(), pages


def process_later(kind, obj):
    """Queue the checksum / page count for a stored revision or bulk-upload row."""
    jobs.enqueue('documents.file_facts', {'kind': kind, 'id': obj.pk}, org=obj.organization,
                 idempotency_key=f'file-facts:{kind}:{obj.organization_id}:{obj.pk}')


def apply_upload_facts(rev, uploaded):
    """Fill sha256/page_count on an unsaved revision from its upload; queue anything left for the worker."""
    rev.sha256, rev.page_count = facts_from_upload(uploaded)
    return rev.page_count is None and uploaded.name.lower().endswith('.pdf')


def revisions_by_q(project, ids):
    return list(DocumentRevision.objects.filter(document__project=project, pk__in=ids)
                .select_related('document').order_by('document__reference', 'document__title'))


def doc_transmittals(request, project, doc):
    return transmittals_for(request, project).filter(items__revision__document=doc).distinct() \
        .prefetch_related('items')

