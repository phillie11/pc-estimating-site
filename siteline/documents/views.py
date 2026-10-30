"""
Documents tab: the drawing register, document and revision pages, bulk upload,
transmittals and the project's naming settings. Every view fetches the project with
projects.access.get_project(); externals only ever get what documents.access lets them see.
"""
import csv
from collections import defaultdict

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, RestrictedError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from projects.access import get_project, is_internal, posted_id, require_internal
from projects.files import serve
from projects.models import Document, DocumentRevision, FileAccess
from projects.views import project_context
from tenancy.models import log_activity

from . import bulk, naming, services
from .access import display_revision, revisions_for, visible_documents, visible_revisions
from .forms import (CodeForm, DistributionRuleForm, DocumentDetailsForm, DocumentUploadForm, RegisterSettingsForm,
                    RevisionUploadForm, TransmittalForm)
from .models import (CodeList, DistributionRule, RegisterSettings, Transmittal, UploadBatch, UploadRow, code_lists,
                     customise_codes, pending_acknowledgements)


def docs_context(request, project, section, **extra):
    internal = is_internal(request)
    if internal:
        waiting = Transmittal.objects.filter(project=project, status=Transmittal.STATUS_ISSUED).count()
    else:
        waiting = pending_acknowledgements(request.user, project).count()
    return project_context(request, project, 'documents', docs_section=section, transmittals_waiting=waiting,
                           **extra)


def _url(name, request, project, *args):
    return reverse(name, args=[request.org.slug, project.pk, *args])


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

FILTERS = ('discipline', 'type', 'status', 'suitability', 'category', 'naming', 'q')


def register_rows(request, project, params):
    """[(document, revision to show)] the person may see, filtered by the register's query string."""
    docs = list(visible_documents(request, project).order_by('category', 'reference', 'title'))
    by_doc = defaultdict(list)
    for r in visible_revisions(request, project).select_related('superseded_by'):
        by_doc[r.document_id].append(r)
    rows = []
    for d in docs:
        d._revs = by_doc.get(d.pk, [])
        d._rev_ids = {r.pk for r in d._revs}
        rows.append((d, display_revision(d._revs) if d._revs else None))
    all_rows = rows
    f = {k: params.get(k, '').strip() for k in FILTERS}
    if f['discipline']:
        rows = [(d, r) for d, r in rows if d.discipline.lower() == f['discipline'].lower()]
    if f['type']:
        rows = [(d, r) for d, r in rows if d.doc_type == f['type'].upper()]
    if f['category']:
        rows = [(d, r) for d, r in rows if d.category == f['category']]
    if f['status']:
        rows = [(d, r) for d, r in rows if r and r.status == f['status']]
    if f['suitability']:
        rows = [(d, r) for d, r in rows if r and r.suitability == f['suitability'].upper()]
    if f['naming'] == 'check':
        rows = [(d, r) for d, r in rows if d.naming in (Document.NAMING_UNPARSED, Document.NAMING_ACCEPTED)]
    if f['q']:
        q = f['q'].lower()
        rows = [(d, r) for d, r in rows if q in d.title.lower() or q in d.reference.lower()
                or q in (d.folder or '').lower() or (r and q in r.original_name.lower())]
    return all_rows, rows, f


def _issue_columns(request, project):
    """[(date, {revision_id: code})] from transmittals the person can see, oldest first."""
    by_date = defaultdict(dict)
    trs = services.transmittals_for(request, project).filter(status__in=Transmittal.SENT_STATUSES) \
        .prefetch_related('items')
    for tr in trs:
        day = timezone.localtime(tr.issued_at).date()
        for item in tr.items.all():
            by_date[day][item.revision_id] = item.revision_code
    return sorted(by_date.items())


def document_list(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    form = DocumentUploadForm(project=project, external=not internal)
    if request.method == 'POST':
        form = DocumentUploadForm(request.POST, request.FILES, project=project, external=not internal)
        if form.is_valid():
            return _upload_new(request, project, form)
    all_rows, rows, f = register_rows(request, project, request.GET)
    export = request.GET.get('export')
    if export == 'csv':
        return _register_csv(project, rows)
    if export == 'pdf':
        from .pdf import register_sheet
        resp = HttpResponse(register_sheet(project, rows, _issue_columns(request, project)),
                            content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-drawing-register.pdf"'
        return resp
    types = dict(code_lists(project)['doc_type'])
    present_types = sorted({d.doc_type for d, _ in all_rows if d.doc_type})
    return render(request, 'documents/register.html', docs_context(
        request, project, 'register', rows=rows, total=len(all_rows), form=form, f=f,
        filtered=any(f.values()),
        disciplines=sorted({d.discipline for d, _ in all_rows if d.discipline}, key=str.lower),
        types=[(t, types.get(t, '')) for t in present_types],
        suitabilities=sorted({r.suitability for _, r in all_rows if r and r.suitability}),
        statuses=DocumentRevision.STATUS_CHOICES, categories=Document.CATEGORY_CHOICES,
        to_check=sum(1 for d, _ in all_rows if d.naming in (Document.NAMING_UNPARSED, Document.NAMING_ACCEPTED))))


def _register_csv(project, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-drawing-register.csv"'
    w = csv.writer(resp)
    w.writerow(['Number', 'Title', 'Category', 'Discipline', 'Project', 'Originator', 'Volume', 'Level', 'Type', 'Role',
                'Number part', 'Revision', 'Suitability', 'Status', 'Date', 'Pages', 'Sheet size', 'Scale', 'Folder',
                'Naming'])
    for d, r in rows:
        w.writerow([d.reference, d.title, d.get_category_display(), d.discipline, d.project_code, d.originator, d.volume,
                    d.level, d.doc_type, d.role, d.number, r.revision if r else '', r.suitability if r else '',
                    r.get_status_display() if r else '', r.uploaded_at.date() if r else '',
                    r.page_count if r and r.page_count else '', d.sheet_size, d.scale, d.folder,
                    d.get_naming_display()])
    return resp


def _upload_new(request, project, form):
    internal = is_internal(request)
    doc = form.save(commit=False)
    doc.project, doc.created_by = project, request.user
    form.apply_number(doc)
    if internal:
        doc.set_share_roles(form.cleaned_data.get('shared_with'))
    else:
        doc.visibility = Document.VIS_SHARED
    doc.save()
    f = form.cleaned_data['file']
    rev = DocumentRevision(file=f, original_name=f.name[:200], size=f.size, revision=form.cleaned_data['revision'],
                           suitability=form.cleaned_data.get('suitability', ''),
                           status=form.cleaned_data.get('status', ''), notes=form.cleaned_data['notes'],
                           uploaded_by=request.user)
    later = services.apply_upload_facts(rev, f)
    doc.add_revision(rev)
    if later:
        services.process_later('revision', rev)
    url = _url('document_detail', request, project, doc.pk)
    log_activity(request, 'uploaded', f'{doc.label} rev {rev.revision}', project=project, url=url,
                 internal_only=doc.visibility == Document.VIS_INTERNAL)
    messages.success(request, f'{doc.title} uploaded.')
    if internal and form.cleaned_data.get('issue_now'):
        _quick_issue(request, project, [rev], form.cleaned_data.get('purpose'))
    return redirect('document_list', org_slug=request.org.slug, pk=project.pk)


def _quick_issue(request, project, revisions, purpose=None):
    tr = services.quick_issue(request.user, project, revisions, purpose=purpose or Transmittal.PURPOSE_INFORMATION)
    if tr is None:
        messages.warning(request, 'Not issued: nobody is on this project to send it to yet. Add people on the Team tab '
                                  'or set usual recipients under Documents → Settings.')
    else:
        messages.success(request, f'{tr.reference} issued to {", ".join(r.name for r in tr.recipients.all())}.')
    return tr


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------

def document_detail(request, org_slug, pk, doc_id):
    project = get_project(request, pk)
    internal = is_internal(request)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    can_revise = internal or doc.created_by_id == request.user.pk
    form = RevisionUploadForm(project=project, external=not internal)
    details = DocumentDetailsForm(instance=doc, project=project) if internal else None
    action = request.POST.get('action') if request.method == 'POST' else None
    if action in ('visibility', 'delete', 'withdraw', 'details', 'issue') and not internal:
        raise PermissionDenied
    if action == 'visibility':
        doc.visibility = Document.VIS_SHARED if doc.visibility == Document.VIS_INTERNAL else Document.VIS_INTERNAL
        doc.save(update_fields=['visibility', 'updated_at'])
        return redirect(request.path)
    if action == 'delete':
        try:
            with transaction.atomic():
                doc.delete()
        except (RestrictedError, ProtectedError):
            messages.error(request, 'This document has been issued on a transmittal, so it stays on the register as a '
                                    'record. Withdraw the revision instead.')
            return redirect(request.path)
        messages.success(request, 'Document deleted.')
        return redirect('document_list', org_slug=org_slug, pk=pk)
    if action == 'details':
        details = DocumentDetailsForm(request.POST, instance=doc, project=project)
        if details.is_valid():
            details.save()
            messages.success(request, 'Details saved.')
            return redirect(request.path)
    elif action == 'withdraw':
        rev = get_object_or_404(DocumentRevision, document=doc, pk=posted_id(request, 'revision'))
        doc.withdraw(rev, note=request.POST.get('note', '').strip()[:150])
        log_activity(request, 'withdrew', f'{doc.label} rev {rev.revision}', project=project, url=request.path,
                     internal_only=doc.visibility == Document.VIS_INTERNAL)
        messages.success(request, f'Revision {rev.revision} withdrawn.')
        return redirect(request.path)
    elif action == 'issue':
        rev = get_object_or_404(DocumentRevision, document=doc, pk=posted_id(request, 'revision'))
        _quick_issue(request, project, [rev], request.POST.get('purpose'))
        return redirect(request.path)
    elif action == 'make_current':
        if not can_revise:
            raise PermissionDenied
        rev = get_object_or_404(revisions_for(request, project, doc), pk=posted_id(request, 'revision'))
        doc.set_current(rev)
        doc.save(update_fields=['updated_at'])
        log_activity(request, 'made current', f'{doc.title} rev {rev.revision}', project=project,
                     url=request.path, internal_only=doc.visibility == Document.VIS_INTERNAL)
        messages.success(request, f'Revision {rev.revision} is now current.')
        return redirect(request.path)
    elif request.method == 'POST':
        if not can_revise:
            raise PermissionDenied
        form = RevisionUploadForm(request.POST, request.FILES, project=project, external=not internal)
        if form.is_valid():
            return _upload_revision(request, project, doc, form)
    revisions = list(revisions_for(request, project, doc))
    current = display_revision(revisions) if revisions else None
    downloads = None
    if internal:
        names = {r.file.name: r.revision for r in revisions}
        downloads = list(FileAccess.objects.filter(path__in=names).select_related('user')[:50])
        for d in downloads:
            d.revision = names.get(d.path)
    usual = services.describe(*services.usual_recipients(project, [doc])) if internal else ''
    return render(request, 'documents/document_detail.html', docs_context(
        request, project, 'register', doc=doc, revisions=revisions, current=current, form=form, details=details,
        downloads=downloads, can_revise=can_revise, usual=usual, purposes=Transmittal.PURPOSE_CHOICES,
        transmittals=services.doc_transmittals(request, project, doc), parts=_parts_display(doc)))


def _parts_display(doc):
    return [(naming.FIELD_LABELS[f], getattr(doc, f)) for f in naming.FIELDS] if doc.number else []


def _upload_revision(request, project, doc, form):
    internal = is_internal(request)
    rev = form.save(commit=False)
    rev.uploaded_by = request.user
    rev.original_name, rev.size = rev.file.name[:200], rev.file.size
    rev.suitability = form.cleaned_data.get('suitability', '')
    rev.status = form.cleaned_data.get('status', '')
    later = services.apply_upload_facts(rev, form.cleaned_data['file'])
    doc.add_revision(rev, make_current=form.cleaned_data['make_current'])
    if later:
        services.process_later('revision', rev)
    doc.save(update_fields=['updated_at'])
    log_activity(request, 'uploaded new revision of', f'{doc.title} rev {rev.revision}', project=project,
                 url=request.path, internal_only=doc.visibility == Document.VIS_INTERNAL)
    if rev.is_current:
        messages.success(request, f'Revision {rev.revision} uploaded.')
    else:
        messages.info(request, f'Revision {rev.revision} uploaded. It is older than rev {doc.current.revision}, '
                               'which stays current; use "Make current" to change that.')
    if internal and form.cleaned_data.get('issue_now'):
        _quick_issue(request, project, [rev], form.cleaned_data.get('purpose'))
    return redirect(request.path)


def revision_detail(request, org_slug, pk, doc_id, rev_id):
    project = get_project(request, pk)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    rev = get_object_or_404(revisions_for(request, project, doc), pk=rev_id)
    trs = services.transmittals_for(request, project).filter(items__revision=rev).distinct()
    return render(request, 'documents/revision_detail.html', docs_context(
        request, project, 'register', doc=doc, rev=rev, current=rev.current_revision, transmittals=trs))


def document_download(request, org_slug, pk, doc_id, rev_id):
    project = get_project(request, pk)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    rev = get_object_or_404(revisions_for(request, project, doc), pk=rev_id)
    return serve(request, rev.file, rev.original_name, project=project,
                 label=f'{doc.reference or doc.title} rev {rev.revision}')


# ---------------------------------------------------------------------------
# Bulk upload
# ---------------------------------------------------------------------------

def bulk_upload(request, org_slug, pk):
    project = get_project(request, pk)
    require_internal(request)
    if request.method == 'POST':
        files = request.FILES.getlist('files')
        if not files:
            messages.error(request, 'Choose one or more files first.')
            return redirect(request.path)
        batch = UploadBatch.objects.create(project=project, created_by=request.user)
        added, refused = bulk.add_files(batch, files)
        for name, why in refused:
            messages.error(request, f'{name}: {why}')
        if not added:
            batch.delete()
            return redirect(request.path)
        if len(files) > services.INLINE_PROCESS_LIMIT:
            messages.info(request, 'Page counts and checksums are being worked out in the background; '
                                   'refresh in a minute to spot exact duplicates.')
        return redirect(batch)
    batches = UploadBatch.objects.filter(project=project, status=UploadBatch.OPEN).select_related('created_by')
    rs = RegisterSettings.for_project(project)
    return render(request, 'documents/bulk_upload.html', docs_context(
        request, project, 'bulk', batches=batches, pattern=rs.filename_pattern or naming.DEFAULT_PATTERN,
        accept=_accept()))


def _accept():
    from .forms import ACCEPT
    return ACCEPT


def bulk_upload_batch(request, org_slug, pk, batch_id):
    project = get_project(request, pk)
    require_internal(request)
    batch = get_object_or_404(UploadBatch, project=project, pk=batch_id)
    if request.method == 'POST' and batch.status == UploadBatch.OPEN:
        action = request.POST.get('action')
        if action == 'cancel':
            bulk.cancel(batch)
            messages.success(request, 'Upload cancelled. Files not added to the register were removed.')
            return redirect('bulk_upload', org_slug=org_slug, pk=pk)
        if action == 'add':
            added, refused = bulk.add_files(batch, request.FILES.getlist('files'))
            for name, why in refused:
                messages.error(request, f'{name}: {why}')
            return redirect(batch)
        _save_row_edits(request, batch)
        bulk.classify(batch)
        if action == 'commit':
            ids = [r.pk for r in batch.rows.filter(state=UploadRow.PENDING) if request.POST.get(f'include_{r.pk}')]
            visibility = Document.VIS_SHARED if request.POST.get('visibility') == Document.VIS_SHARED \
                else Document.VIS_INTERNAL
            added, held = bulk.commit(batch, ids, request.user, visibility=visibility)
            if added:
                log_activity(request, 'uploaded', f'{len(added)} documents', project=project,
                             url=_url('document_list', request, project), internal_only=visibility != Document.VIS_SHARED)
                messages.success(request, f'{len(added)} added to the register.')
            for row, why in held.items():
                messages.warning(request, f'{row.original_name}: {why}')
            if added and request.POST.get('then') == 'transmit':
                query = '&'.join(f'rev={r.pk}' for r in added)
                return redirect(_url('transmittal_create', request, project) + '?' + query)
            if added and request.POST.get('then') == 'issue':
                _quick_issue(request, project, added, request.POST.get('purpose'))
        else:
            messages.success(request, 'Changes checked.')
        return redirect(batch)
    rows = list(batch.rows.select_related('document', 'supersedes', 'committed_revision__document'))
    org = request.org
    for r in rows:
        r.scan = bulk.row_scan(r, org) if r.state == UploadRow.PENDING else bulk.SCAN_CLEAN
        r.ready = bulk.ready_to_commit(r, org)[0] if r.state == UploadRow.PENDING else False
    pending = [r for r in rows if r.state == UploadRow.PENDING]
    return render(request, 'documents/bulk_batch.html', docs_context(
        request, project, 'bulk', batch=batch, rows=rows, pending=pending,
        scanning=any(r.scan == bulk.SCAN_PENDING for r in pending),
        suitabilities=code_lists(project)['suitability'], purposes=Transmittal.PURPOSE_CHOICES,
        suit_codes={c for c, _ in code_lists(project)['suitability']},
        categories=Document.CATEGORY_CHOICES, accept=_accept()))


def _save_row_edits(request, batch):
    for row in batch.rows.filter(state=UploadRow.PENDING):
        p = f'_{row.pk}'
        if f'ref{p}' not in request.POST:
            continue
        row.reference = request.POST.get(f'ref{p}', '').strip()[:80]
        row.revision = naming.clean_code(request.POST.get(f'rev{p}', ''))[:20]
        row.suitability = naming.clean_code(request.POST.get(f'suit{p}', ''))[:4]
        row.title = request.POST.get(f'title{p}', '').strip()[:200]
        cat = request.POST.get(f'cat{p}', '')
        if cat in dict(Document.CATEGORY_CHOICES):
            row.category = cat
        row.accept_warnings = bool(request.POST.get(f'accept{p}'))
        row.save()


# ---------------------------------------------------------------------------
# Transmittals
# ---------------------------------------------------------------------------

def transmittal_list(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    qs = services.transmittals_for(request, project).select_related('issued_by', 'created_by') \
        .prefetch_related('recipients', 'items')
    status = request.GET.get('status', 'open' if internal else 'all')
    if status == 'open':
        qs = qs.filter(status__in=[Transmittal.STATUS_DRAFT, Transmittal.STATUS_ISSUED])
    elif status == 'mine':
        qs = qs.filter(pk__in=pending_acknowledgements(request.user, project).values('pk'))
    elif status in dict(Transmittal.STATUS_CHOICES):
        qs = qs.filter(status=status)
    waiting_ids = set(pending_acknowledgements(request.user, project).values_list('pk', flat=True))
    return render(request, 'documents/transmittal_list.html', docs_context(
        request, project, 'transmittals', transmittals=qs, status=status, waiting_ids=waiting_ids,
        today=timezone.localdate()))


def transmittal_create(request, org_slug, pk, tr_id=None):
    project = get_project(request, pk)
    require_internal(request)
    tr = None
    if tr_id is not None:
        tr = get_object_or_404(Transmittal, project=project, pk=tr_id)
        if tr.status != Transmittal.STATUS_DRAFT:
            messages.error(request, f'{tr.reference} has been issued, so it can\'t be changed.')
            return redirect(tr)
    form = TransmittalForm(request.POST or None, instance=tr, project=project)
    if request.method == 'GET':
        if tr is None:
            ids = [int(i) for i in request.GET.getlist('rev') if i.isdigit()]
            revs = services.revisions_by_q(project, ids)
            users, companies = services.usual_recipients(project, [r.document for r in revs]) if revs else ([], [])
            form.initial.update(revisions=[r.pk for r in revs], users=[u.pk for u in users],
                                companies=[c.pk for c in companies])
            if revs:
                form.initial['title'] = (f'{revs[0].document.label} rev {revs[0].revision}' if len(revs) == 1
                                         else f'{len(revs)} documents')
        else:
            form.initial.update(revisions=list(tr.items.values_list('revision_id', flat=True)),
                                users=list(tr.recipients.filter(user__isnull=False).values_list('user_id', flat=True)),
                                companies=list(tr.recipients.filter(subcontractor__isnull=False)
                                               .values_list('subcontractor_id', flat=True)))
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                tr = form.save(commit=False)
                tr.project = project
                tr.save(actor=request.user)
                services.set_items(tr, list(form.cleaned_data['revisions']))
                services.set_recipients(tr, list(form.cleaned_data['users']), list(form.cleaned_data['companies']))
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            if request.POST.get('then') == 'issue':
                try:
                    services.issue(request.user, tr)
                except services.IssueRefused as err:
                    messages.error(request, f'Saved as a draft, not issued: {err}')
                    return redirect(tr)
                messages.success(request, f'{tr.reference} issued.')
            else:
                messages.success(request, f'{tr.reference} saved as a draft. Issue it when it\'s ready.')
            return redirect(tr)
    return render(request, 'documents/transmittal_form.html', docs_context(
        request, project, 'transmittals', form=form, transmittal=tr))


def transmittal_detail(request, org_slug, pk, tr_id):
    project = get_project(request, pk)
    internal = is_internal(request)
    tr = get_object_or_404(services.transmittals_for(request, project), pk=tr_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'acknowledge':
                n = services.acknowledge(request.user, tr)
                messages.success(request, 'Thanks: receipt recorded.' if n else 'Already acknowledged.')
            elif action == 'record_receipt' and internal:
                rec = get_object_or_404(tr.recipients, pk=posted_id(request, 'recipient'))
                services.acknowledge(request.user, tr, recipient=rec, note=request.POST.get('note', '').strip()[:200]
                                     or 'Recorded by staff')
                messages.success(request, f'Receipt recorded for {rec.name}.')
            elif action == 'issue' and internal:
                services.issue(request.user, tr)
                messages.success(request, f'{tr.reference} issued.')
            elif action == 'cancel' and internal:
                apply_transition(tr, 'cancel', request.user)
                messages.success(request, f'{tr.reference} cancelled.')
            else:
                raise PermissionDenied
        except (services.IssueRefused, InvalidTransition) as err:
            messages.error(request, str(err))
        except RecordConflict:
            messages.error(request, 'Someone else changed this transmittal just now. Try again.')
        return redirect(request.path)
    items = list(tr.items.select_related('revision__document', 'revision__superseded_by'))
    recipients = list(tr.recipients.select_related('user', 'subcontractor', 'acknowledged_by'))
    mine = tr.status == Transmittal.STATUS_ISSUED and pending_acknowledgements(request.user, project) \
        .filter(pk=tr.pk).exists()
    return render(request, 'documents/transmittal_detail.html', docs_context(
        request, project, 'transmittals', transmittal=tr, items=items, recipients=recipients, can_acknowledge=mine,
        pending=[r for r in recipients if r.acknowledged_at is None]))


def transmittal_sheet(request, org_slug, pk, tr_id):
    project = get_project(request, pk)
    tr = get_object_or_404(services.transmittals_for(request, project), pk=tr_id)
    return serve(request, tr.sheet, f'{tr.reference}.pdf', project=project, label=f'{tr.reference} issue sheet')


# ---------------------------------------------------------------------------
# Settings: naming, code lists, usual recipients
# ---------------------------------------------------------------------------

def register_settings(request, org_slug, pk):
    project = get_project(request, pk)
    require_internal(request)
    rs = RegisterSettings.for_project(project)
    form = RegisterSettingsForm(instance=rs)
    code_form = CodeForm()
    rule_form = DistributionRuleForm(project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'settings':
            form = RegisterSettingsForm(request.POST, instance=rs)
            if form.is_valid():
                form.save()
                messages.success(request, 'Naming settings saved.')
                return redirect(request.path)
        elif action == 'add_code':
            code_form = CodeForm(request.POST)
            if code_form.is_valid():
                field = code_form.cleaned_data['field']
                customise_codes(project, field)
                try:
                    with transaction.atomic():
                        CodeList.objects.create(project=project, field=field, code=code_form.cleaned_data['code'],
                                                description=code_form.cleaned_data['description'])
                except IntegrityError:
                    messages.info(request, f'{code_form.cleaned_data["code"]} is already in the list.')
                else:
                    messages.success(request, f'{code_form.cleaned_data["code"]} added.')
                return redirect(request.path + '#codes')
        elif action == 'remove_code':
            field = request.POST.get('field', '')
            if field in naming.CODE_FIELDS:
                customise_codes(project, field)
                CodeList.objects.filter(project=project, field=field, code=request.POST.get('code', '')).delete()
            return redirect(request.path + '#codes')
        elif action == 'reset_codes':
            CodeList.objects.filter(project=project, field=request.POST.get('field', '')).delete()
            messages.success(request, 'Back to the UK default list.')
            return redirect(request.path + '#codes')
        elif action == 'add_rule':
            rule_form = DistributionRuleForm(request.POST, project=project)
            if rule_form.is_valid():
                rule = rule_form.save(commit=False)
                rule.project = project
                rule.save()
                messages.success(request, 'Usual recipient added.')
                return redirect(request.path + '#recipients')
        elif action == 'remove_rule':
            DistributionRule.objects.filter(project=project, pk=posted_id(request, 'rule')).delete()
            return redirect(request.path + '#recipients')
    own = set(CodeList.objects.filter(project=project).values_list('field', flat=True).distinct())
    lists = [(f, naming.FIELD_LABELS[f], rows, f in own) for f, rows in code_lists(project).items()]
    rules = DistributionRule.objects.filter(project=project).select_related('user', 'subcontractor')
    return render(request, 'documents/settings.html', docs_context(
        request, project, 'settings', form=form, code_form=code_form, rule_form=rule_form, lists=lists, rules=rules,
        default_pattern=naming.DEFAULT_PATTERN))
