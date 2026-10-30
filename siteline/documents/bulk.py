"""
Bulk upload: many files in, one preview row each, then commit.

1. add_files() stores each file (so GuardDuty starts scanning it straight away) and reads
   its number, revision and suitability from the file name using the project's pattern.
2. classify() works out what committing each row would do: a new document, a new
   revision (and which revision it supersedes), an older revision filed as superseded, a
   conflict (that revision is already there with different content, or twice in this
   batch), already uploaded (same content), or needs details. It also lists naming
   warnings, which the user must accept per row.
3. commit() adds the chosen rows to the register. A row is held back while its file is
   still being virus-checked, and refused if the check failed, exactly as downloads are.

Files go through Django for now. Follow-up: browser-to-S3 direct upload with presigned
POSTs, so big drawing packs don't pass through the app server.
"""
from django.db import transaction
from django.utils import timezone

from projects.files import scan_status, scanning_enabled, validate_upload
from projects.models import Document, DocumentRevision, FileScan, is_older_revision, latest_revision

from . import naming
from .models import RegisterSettings, UploadBatch, UploadRow, code_sets
from .services import INLINE_PROCESS_LIMIT, facts_from_upload, process_later

SCAN_CLEAN, SCAN_PENDING, SCAN_BLOCKED = 'clean', 'scanning', 'blocked'


def add_files(batch, files):
    """Store the files on the batch. Returns (rows added, [(name, reason) refused])."""
    rs = RegisterSettings.for_project(batch.project)
    inline = len(files) <= INLINE_PROCESS_LIMIT
    added, refused = [], []
    for f in files:
        try:
            validate_upload(f)
        except Exception as err:  # ValidationError: blocked type or too big
            refused.append((f.name, ' '.join(getattr(err, 'messages', [str(err)]))))
            continue
        try:
            parsed = naming.parse_filename(f.name, rs.filename_pattern)
        except naming.PatternError:
            parsed = naming.parse_filename(f.name)
        row = UploadRow(batch=batch, original_name=f.name[:200], size=f.size,
                        reference=parsed['reference'][:80], revision=parsed['revision'][:20],
                        suitability=parsed['suitability'][:4], title=parsed['title'][:200])
        if inline:
            row.sha256, row.page_count = facts_from_upload(f)
            row.processed = True
        row.file = f
        row.save()
        if not inline:
            process_later('row', row)
        added.append(row)
    classify(batch)
    return added, refused


def row_scan(row, org):
    if not scanning_enabled():
        return SCAN_CLEAN
    status = scan_status(row.file, org)
    if status is None:
        return SCAN_PENDING
    return SCAN_CLEAN if status == FileScan.CLEAN else SCAN_BLOCKED


def classify(batch):
    """Work out each waiting row's action, target document and warnings, and save them."""
    project = batch.project
    codes = code_sets(project)
    rs = RegisterSettings.for_project(project)
    rows = list(batch.rows.filter(state=UploadRow.PENDING).order_by('pk'))
    docs = {}
    for d in Document.objects.filter(project=project).exclude(reference='').prefetch_related('revisions'):
        docs.setdefault(d.reference.upper(), d)
    seen = {}
    for row in rows:
        row.document, row.supersedes, row.message = None, None, ''
        warnings = []
        ref = (row.reference or '').strip()
        parts = naming.split_reference(ref)
        if parts:
            ref = naming.assemble(parts)
            row.reference = ref
        if rs.check_naming and ref:
            warnings += naming.check_reference(ref, codes, rs.project_code)[1]
            warnings += naming.validate_suitability(row.suitability, codes)
        row.warnings = '\n'.join(warnings)
        if not ref or not row.revision:
            row.action = UploadRow.INCOMPLETE
            row.message = 'Add the document number and revision.' if not ref and not row.revision else (
                'Add the document number.' if not ref else 'Add the revision.')
            row.save()
            continue
        key = (ref.upper(), row.revision.upper())
        doc = docs.get(ref.upper())
        row.document = doc
        if key in seen:
            row.action = UploadRow.CONFLICT
            row.message = f'{seen[key].original_name} in this upload is also {ref} rev {row.revision}.'
        elif doc is None:
            row.action = UploadRow.NEW_DOC
            row.message = 'New entry on the register.'
        else:
            revs = list(doc.revisions.all())
            same = next((r for r in revs if r.revision.upper() == row.revision.upper()), None)
            current = next((r for r in revs if r.is_current), None) or latest_revision(
                [r for r in revs if r.status != DocumentRevision.WITHDRAWN])
            if same is not None:
                if row.sha256 and same.sha256 and row.sha256 == same.sha256:
                    row.action = UploadRow.DUPLICATE
                    row.message = f'Rev {same.revision} is already on the register with exactly this file.'
                else:
                    row.action = UploadRow.CONFLICT
                    row.message = (f'Rev {same.revision} is already on the register'
                                   f'{" with a different file" if row.sha256 and same.sha256 else ""}. '
                                   'Change the revision, or leave this file out.')
            elif current is not None and is_older_revision(DocumentRevision(revision=row.revision), current):
                row.action = UploadRow.OLDER_REV
                row.supersedes = None
                row.message = f'Older than the current rev {current.revision}: it will be filed as superseded.'
            else:
                row.action = UploadRow.NEW_REV
                row.supersedes = current
                row.message = f'Supersedes rev {current.revision}.' if current else 'First revision of this document.'
        seen.setdefault(key, row)
        row.save()
    return rows


def ready_to_commit(row, org):
    """(True, '') or (False, why not) for one waiting row."""
    if row.state != UploadRow.PENDING:
        return False, 'Already dealt with.'
    if row.action not in UploadRow.COMMITTABLE:
        return False, row.message or row.get_action_display()
    if row.warnings and not row.accept_warnings:
        return False, 'Tick "accept" to use this number as it is, or correct it.'
    scan = row_scan(row, org)
    if scan == SCAN_PENDING:
        return False, 'Still being virus-checked. Check again in a minute.'
    if scan == SCAN_BLOCKED:
        return False, 'Blocked: this file failed the virus check.'
    return True, ''


def commit(batch, row_ids, user, visibility=Document.VIS_INTERNAL, category='DRAWING'):
    """
    Add the chosen rows to the register. Returns (revisions added, {row: reason} held back).
    Rows that can't go yet stay in the batch, so the user can fix them and commit again.
    """
    org = batch.organization
    added, held = [], {}
    with transaction.atomic():
        # Two people pressing commit (or commit and cancel) at once: the second waits here, then
        # sees only the rows still waiting, so nothing is added twice or added after a cancel
        locked = UploadBatch.objects.select_for_update().filter(pk=batch.pk).first()
        if locked is None or locked.status != UploadBatch.OPEN:
            return added, held
        rows = list(UploadRow.objects.select_for_update().filter(batch=batch, pk__in=row_ids,
                                                                 state=UploadRow.PENDING).order_by('pk'))
        for row in rows:
            ok, why = ready_to_commit(row, org)
            if not ok:
                held[row] = why
                continue
            doc = row.document
            if doc is None:
                doc = Document.objects.filter(project=batch.project, reference__iexact=row.reference).first()
            if doc is None:
                doc = Document(project=batch.project, title=(row.title or row.reference)[:200], created_by=user,
                               category=row.category or category, visibility=visibility)
                doc.set_number(row.reference, row.warning_list, accepted=row.accept_warnings)
                doc.save()
            rev = DocumentRevision(revision=row.revision, suitability=row.suitability, original_name=row.original_name,
                                   size=row.size, sha256=row.sha256, page_count=row.page_count, uploaded_by=user)
            rev.file.name = row.file.name  # the stored (and scanned) object itself, not a copy
            doc.add_revision(rev)
            Document.objects.filter(pk=doc.pk).update(updated_at=timezone.now())
            row.state, row.committed_revision, row.document = UploadRow.COMMITTED, rev, doc
            row.save(update_fields=['state', 'committed_revision', 'document'])
            if not row.processed:
                process_later('revision', rev)
            added.append(rev)
        if not UploadRow.objects.filter(batch=batch, state=UploadRow.PENDING).exists():
            batch.status, batch.committed_at = UploadBatch.DONE, timezone.now()
            batch.save(update_fields=['status', 'committed_at'])
    classify(batch)
    return added, held


def cancel(batch):
    """Throw away the rows not committed, and their stored files (once the cancel has committed)."""
    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().filter(pk=batch.pk).first()
        if locked is None or locked.status != UploadBatch.OPEN:
            return
        for row in UploadRow.objects.select_for_update().filter(batch=batch, state=UploadRow.PENDING):
            if row.file:
                storage, name = row.file.storage, row.file.name
                transaction.on_commit(lambda storage=storage, name=name: storage.delete(name))
            row.state = UploadRow.SKIPPED
            row.save(update_fields=['state'])
        batch.status = UploadBatch.CANCELLED
        batch.save(update_fields=['status'])
