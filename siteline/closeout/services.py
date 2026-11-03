"""
What happens on the Close-out tab. Views call these.

  set_completion        save a completion section; the whole-works date goes to the main contract
  setup_om              copy the company's O&M template onto a job, matching trade packages
  request_om            ask a section's subcontractor for its O&M information (a procurement
                        submittal of type O&M, so it works exactly as submittals do)
  compile_om            build the manual: a zip of every accepted O&M file, by section, with a
                        contents PDF that lists each file and its SHA-256 (pypdf is not allowed
                        yet, so the files are not merged into one PDF)
  build_pack            freeze a golden thread handover pack: one item per file, then checksums
  issue_pack            share a ready pack with the client side

Small jobs are done during the request so people see the result at once; anything bigger goes to
the background worker (closeout.tasks). Files are only read once the virus check has cleared them.
"""
import hashlib
import os
import re
import tempfile
import zipfile

from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

import jobs
from projects.files import scan_status
from projects.models import FileScan
from tenancy import notify

from . import goldenthread, rules
from .models import (CompletionSection, HandoverItem, HandoverPack, OMCompilation, OMDocument, OMRequest, OMSection,
                     OMTemplateSection)

INLINE_MAX_FILES = 60
INLINE_MAX_BYTES = 50 * 1024 * 1024
CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Practical completion
# ---------------------------------------------------------------------------

def whole_works(project):
    """The project's whole-works section, made (unsaved) if it doesn't exist yet."""
    row = CompletionSection.objects.filter(project=project, is_whole_works=True).first()
    if row is None:
        from contracts.models import MainContract
        contract = MainContract.objects.filter(project=project).first()
        row = CompletionSection(project=project, is_whole_works=True, name=CompletionSection.WHOLE_WORKS,
                                pc_date=contract.practical_completion_on if contract else None)
    return row


def sync_contract(section):
    """The whole-works PC date is the contract's "practical completion certified on" (payments reads it)."""
    if not section.is_whole_works:
        return
    from contracts.models import MainContract
    contract = MainContract.objects.filter(project_id=section.project_id).first()
    if contract is not None and contract.practical_completion_on != section.pc_date:
        contract.practical_completion_on = section.pc_date
        contract.save(update_fields=['practical_completion_on', 'updated_at'])


def set_completion(section, user):
    newly_complete = section.pc_date is not None and (
        section.pk is None or CompletionSection.objects.filter(pk=section.pk, pc_date__isnull=True).exists())
    with transaction.atomic():
        section.updated_by = user
        section.save()
        sync_contract(section)
    return newly_complete


# ---------------------------------------------------------------------------
# O&M manual: sections and requests
# ---------------------------------------------------------------------------

def _norm(text):
    return re.sub(r'[^a-z]', '', (text or '').lower())


def setup_om(project, org):
    """Copy the template's sections onto the job, each matched to an appointed package of its trade."""
    from directory.models import Package
    if OMSection.objects.filter(project=project).exists():
        return 0
    packages = list(Package.objects.filter(project=project, status=Package.APPOINTED).select_related('trade'))
    by_trade = {_norm(p.trade.name): p for p in packages if p.trade_id}
    rows = []
    for t in OMTemplateSection.for_org(org):
        rows.append(OMSection(project=project, title=t.title, contents=t.contents, order=t.order,
                              package=by_trade.get(_norm(t.trade)) if t.trade else None))
    OMSection.objects.bulk_create(rows)
    return len(rows)


def request_om(section, user, due_date=None, note=''):
    """Ask the section's subcontractor for its O&M information, through their submittals."""
    from directory.models import Package
    from procurement.models import Submittal, region_for
    from procurement.services import initial_ball
    if section.package_id is None or section.package.status != Package.APPOINTED:
        raise ValueError('Choose the appointed subcontractor who supplies this section first.')
    project = section.project
    due_date = due_date or rules.om_due_default(timezone.localdate(), project.completion_date, region_for(project))
    description = '\n'.join(filter(None, [note.strip(), 'Please include:', *[f'- {x}' for x in section.content_lines]]))
    with transaction.atomic():
        s = Submittal(project=project, package=section.package, kind=Submittal.OM, due_date=due_date,
                      title=f'O&M: {section.title}'[:200], description=description)
        initial_ball(s)
        s.save(actor=user)
        OMRequest.objects.create(section=section, submittal=s)
    from delivery.views import sub_users
    notify.alert(sub_users(project, section.package.subcontractor_id),
                 f'O&M information to send: {s.ref} {section.title}', s.get_absolute_url(), project=project, actor=user)
    return s


def section_rows(project):
    """Each O&M section with its requests, our own files and a plain-words state, for the O&M page."""
    from procurement.models import Submittal
    sections = list(OMSection.objects.filter(project=project).select_related('package__subcontractor', 'package__trade')
                    .prefetch_related('documents'))
    reqs = {}
    for r in OMRequest.objects.filter(section__project=project).select_related(
            'submittal', 'submittal__ball_in_court_company').order_by('pk'):
        reqs.setdefault(r.section_id, []).append(r.submittal)
    for sec in sections:
        sec.submittals = reqs.get(sec.pk, [])
        live = [s for s in sec.submittals if s.status != Submittal.WITHDRAWN]
        sec.docs = list(sec.documents.all())
        sec.accepted = [s for s in live if s.status in Submittal.ACCEPTED]
        sec.outstanding = [s for s in live if s.status not in Submittal.ACCEPTED]
        if sec.not_needed:
            sec.state = ('Not needed', 'muted')
        elif sec.outstanding:
            overdue = any(s.is_overdue for s in sec.outstanding)
            sec.state = ('Overdue', 'bad') if overdue else ('Waiting for information', 'warn')
        elif sec.accepted or sec.docs:
            sec.state = ('Complete', 'good')
        else:
            sec.state = ('Not asked for yet', 'muted')
        sec.done = sec.state[0] in ('Complete', 'Not needed')
    return sections


# ---------------------------------------------------------------------------
# Reading stored files safely
# ---------------------------------------------------------------------------

def file_state(fieldfile, org):
    """HandoverItem.OK when the file may be read, else WAITING, BLOCKED or MISSING."""
    if not fieldfile:
        return HandoverItem.MISSING
    try:
        status = scan_status(fieldfile, org)
    except Exception:  # noqa: BLE001 - a missing object in cloud storage is an Http404 here
        return HandoverItem.MISSING
    if status is None:
        return HandoverItem.WAITING
    if status != FileScan.CLEAN:
        return HandoverItem.BLOCKED
    try:
        if not fieldfile.storage.exists(fieldfile.name):
            return HandoverItem.MISSING
    except Exception:  # noqa: BLE001
        return HandoverItem.MISSING
    return HandoverItem.OK


def sha256_of(fieldfile):
    h = hashlib.sha256()
    with fieldfile.open('rb') as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def _small(count, size):
    return count <= INLINE_MAX_FILES and size <= INLINE_MAX_BYTES


# ---------------------------------------------------------------------------
# O&M compilation
# ---------------------------------------------------------------------------

def om_files(project):
    """[(section, [(title, FieldFile, original name, size)])] for every section in the manual."""
    from procurement.models import Submittal, SubmittalFile
    out = []
    sections = list(OMSection.objects.filter(project=project, not_needed=False))
    accepted = {}
    for r in OMRequest.objects.filter(section__in=sections, submittal__status__in=Submittal.ACCEPTED) \
            .select_related('submittal'):
        s = r.submittal
        for f in SubmittalFile.objects.filter(revision__submittal=s, revision__number=s.revision,
                                              role=SubmittalFile.SUBMISSION).order_by('pk'):
            accepted.setdefault(r.section_id, []).append((f'{s.ref} {f.original_name}', f.file, f.original_name, f.size))
    own = {}
    for d in OMDocument.objects.filter(section__in=sections).order_by('pk'):
        own.setdefault(d.section_id, []).append((d.title, d.file, d.original_name, d.size))
    for sec in sections:
        out.append((sec, accepted.get(sec.pk, []) + own.get(sec.pk, [])))
    return out


def start_compilation(project, user):
    number = (OMCompilation.objects.filter(project=project).aggregate(m=Max('number'))['m'] or 0) + 1
    comp = OMCompilation.objects.create(project=project, number=number, created_by=user)
    files = om_files(project)
    count = sum(len(f) for _, f in files)
    size = sum(x[3] or 0 for _, f in files for x in f)
    if _small(count, size):
        compile_om(comp)
    else:
        jobs.enqueue('closeout.compile_om', {'id': comp.pk}, org=project.organization,
                     idempotency_key=f'closeout-om-{comp.pk}')
    return comp


def _safe_name(text, limit=80):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', text or '').strip().strip('.')
    return re.sub(r'\s+', ' ', text)[:limit] or 'file'


def compile_om(comp):
    """Build the zip. Safe to run twice: a ready compilation is left as it is."""
    from . import pdf
    if comp.status == OMCompilation.READY:
        return comp
    project = comp.project
    org = project.organization
    index_sections, left_out = [], []
    with tempfile.TemporaryFile() as tmp:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for n, (sec, files) in enumerate(om_files(project), 1):
                folder = f'{n:02d} {_safe_name(sec.title, 60)}'
                listed = []
                for m, (title, fieldfile, original, size) in enumerate(files, 1):
                    state = file_state(fieldfile, org)
                    if state != HandoverItem.OK:
                        left_out.append((sec.title, original or title, dict(HandoverItem.STATE_CHOICES)[state]))
                        continue
                    base, ext = os.path.splitext(original or os.path.basename(fieldfile.name))
                    # The extension is typed data too: letters and digits only, so no "\..\" can ride on it
                    ext = re.sub(r'[^a-z0-9]', '', ext.lower())[:9]
                    path = f'{folder}/{n:02d}-{m:02d} {_safe_name(base)}{"." + ext if ext else ""}'
                    h = hashlib.sha256()
                    with fieldfile.open('rb') as src, zf.open(path, 'w', force_zip64=True) as dst:
                        for chunk in iter(lambda: src.read(CHUNK), b''):
                            h.update(chunk)
                            dst.write(chunk)
                    listed.append((path, title, size, h.hexdigest()))
                index_sections.append((n, sec.title, sec.content_lines, listed))
            index = pdf.om_index(project, org.name, comp.number, index_sections, left_out)
            zf.writestr('00 Contents and index.pdf', index)
        tmp.seek(0)
        h = hashlib.sha256()
        for chunk in iter(lambda: tmp.read(CHUNK), b''):
            h.update(chunk)
        size = tmp.tell()
        tmp.seek(0)
        name = f'{project.reference}-OM-manual-v{comp.number}.zip'
        comp.file.save(name, File(tmp, name=name), save=False)
    comp.status, comp.size, comp.sha256 = OMCompilation.READY, size, h.hexdigest()
    comp.file_count = sum(len(x[3]) for x in index_sections)
    comp.left_out = [list(x) for x in left_out]
    comp.ready_at = timezone.now()
    comp.save()
    if comp.created_by_id:
        notify.alert([comp.created_by], f'O&M manual version {comp.number} is ready: {project.name}',
                     _om_url(project), project=project, email=False)
    return comp


def _om_url(project):
    from django.urls import reverse
    return reverse('closeout_om', args=[project.organization.slug, project.pk])


# ---------------------------------------------------------------------------
# Golden thread handover pack
# ---------------------------------------------------------------------------

def build_pack(project, user, note=''):
    """Freeze what goes in the pack now; work out the checksums now or in the background."""
    sources = goldenthread.collect(project)
    with transaction.atomic():
        number = (HandoverPack.objects.filter(project=project).aggregate(m=Max('number'))['m'] or 0) + 1
        pack = HandoverPack.objects.create(project=project, number=number, created_by=user, note=note)
        items = []
        for i, src in enumerate(sources):
            item = HandoverItem(pack=pack, category=src.category, title=src.title[:255], reference=src.reference[:120],
                                revision=src.revision[:20], file_name=(src.file_name or '')[:200], size=src.size or 0,
                                sha256=src.sha256 or '', order=i)
            if src.generated:
                name, content = src.generated
                item.file.save(name, ContentFile(content), save=False)
                item.size = len(content)
                item.sha256 = hashlib.sha256(content).hexdigest()
                item.state = HandoverItem.OK
            else:
                item.file.name = src.file.name
            items.append(item)
        HandoverItem.objects.bulk_create(items)
    need = [it for it in items if it.state != HandoverItem.OK]
    if _small(len(need), sum(it.size for it in need)):
        checksum_pack(pack)
    else:
        jobs.enqueue('closeout.pack_checksums', {'id': pack.pk}, org=project.organization)
    return pack


def checksum_pack(pack):
    """
    Work out each item's checksum once the virus check has cleared its file. Returns True when the
    pack is finished (READY), False while some files are still waiting for the virus check.
    """
    org = pack.project.organization
    waiting = False
    for item in pack.items.exclude(state=HandoverItem.OK):
        state = file_state(item.file, org)
        if state == HandoverItem.OK:
            if not item.sha256:
                item.sha256 = sha256_of(item.file)
            if not item.size:
                try:
                    item.size = item.file.size
                except Exception:  # noqa: BLE001
                    pass
        waiting = waiting or state == HandoverItem.WAITING
        item.state = state
        item.save(update_fields=['state', 'sha256', 'size'])
    if waiting:
        return False
    pack.manifest_sha256 = manifest_checksum(pack)
    pack.status, pack.ready_at = HandoverPack.READY, timezone.now()
    pack.save(update_fields=['manifest_sha256', 'status', 'ready_at'])
    return True


def manifest_lines(pack):
    """One line per item: category, title, ref, rev, file name, size, SHA-256 (tab separated)."""
    return ['\t'.join([it.category, it.title, it.reference, it.revision, it.file_name, str(it.size), it.sha256])
            for it in pack.items.all()]


def manifest_checksum(pack):
    return hashlib.sha256('\n'.join(manifest_lines(pack)).encode('utf-8')).hexdigest()


def issue_pack(pack, user):
    from django.urls import reverse
    from projects.models import ProjectMember
    from .access import CLIENT_ROLES
    if pack.status != HandoverPack.READY:
        raise ValueError('Only a pack that is ready can be issued.')
    pack.status, pack.issued_at, pack.issued_by = HandoverPack.ISSUED, timezone.now(), user
    pack.save(update_fields=['status', 'issued_at', 'issued_by'])
    project = pack.project
    people = [m.user for m in ProjectMember.objects.filter(project=project, role__in=CLIENT_ROLES).select_related('user')]
    notify.alert(people, f'Handover pack {pack.number} issued: {project.name}',
                 reverse('closeout_client', args=[project.organization.slug, project.pk]) + '#handover',
                 project=project, actor=user)
    return pack
