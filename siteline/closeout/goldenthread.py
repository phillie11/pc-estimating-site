"""
What goes in a golden thread handover pack, gathered from the rest of the app.

  Completion certificates   each completion section's uploaded certificate
  Drawings                  every drawing's current revision, unless withdrawn
  Fire-stopping register    from the `quality` app, only if it is installed (see quality_items)
  O&M manual                the latest compiled manual, accepted O&M submissions and our own O&M files
  Certificates              certificates and test results uploaded on the Close-out tab

Each entry is a Source: what it is, and the stored file. The pack links to that exact stored file
and records its SHA-256, so what was handed over can be checked later.

Building Safety Act wording on screen is advisory: whether a building is higher-risk, and what
its golden thread must hold, is for the client and their advisers.
"""
import csv
import io
from dataclasses import dataclass

from django.apps import apps

from .models import Certificate, CompletionSection, HandoverItem, OMCompilation, OMDocument, OMRequest


@dataclass
class Source:
    category: str
    title: str
    file: object = None            # a FieldFile already stored
    reference: str = ''
    revision: str = ''
    file_name: str = ''
    size: int = 0
    sha256: str = ''               # already known (drawings work theirs out on upload)
    generated: tuple = None        # (file name, bytes) for a file made now, such as a register CSV


def drawings(project):
    """
    Current revisions of the drawings the client side has been given: a document shared with the
    client role (the documents app's own rule: visibility SHARED, share roles blank or naming the
    client, and the revision shared at some point), or a revision on an issued transmittal to
    someone on the job as client or consultant. Internal-only and never-issued (WIP) drawings stay
    out, because issuing a pack lets the client download every item in it.
    """
    from django.db.models import Q

    from documents.models import Transmittal, TransmittalItem
    from projects.models import Document, DocumentRevision, ProjectMember

    from .access import CLIENT_ROLES
    client_side = ProjectMember.objects.filter(project=project, role__in=CLIENT_ROLES).values('user_id')
    transmitted = TransmittalItem.objects.filter(
        transmittal__project=project, transmittal__status__in=Transmittal.SENT_STATUSES,
        transmittal__deleted_at__isnull=True, transmittal__recipients__user__in=client_side).values('revision_id')
    shared = Q(document__visibility=Document.VIS_SHARED, shared_at__isnull=False) & (
        Q(document__share_roles='') | Q(document__share_roles__contains=f',{ProjectMember.ROLE_CLIENT},'))
    revs = DocumentRevision.objects.filter(document__project=project, document__category='DRAWING', is_current=True) \
        .filter(shared | Q(pk__in=transmitted)) \
        .exclude(status=DocumentRevision.WITHDRAWN).select_related('document').order_by('document__reference', 'pk')
    return [Source(HandoverItem.DRAWING, r.document.title, r.file, reference=r.document.reference,
                   revision=r.revision, file_name=r.original_name, size=r.size, sha256=r.sha256)
            for r in revs if r.file]


def om(project):
    from procurement.models import Submittal, SubmittalFile
    out = []
    latest = OMCompilation.objects.filter(project=project, status=OMCompilation.READY).order_by('-number').first()
    if latest and latest.file:
        out.append(Source(HandoverItem.OM, f'O&M manual, version {latest.number} (all sections, with index)',
                          latest.file, file_name=latest.file.name.rsplit('/', 1)[-1], size=latest.size,
                          sha256=latest.sha256))
    requests = OMRequest.objects.filter(section__project=project, section__not_needed=False,
                                        submittal__status__in=Submittal.ACCEPTED) \
        .select_related('section', 'submittal').order_by('section__order', 'section__pk', 'pk')
    for req in requests:
        s = req.submittal
        files = SubmittalFile.objects.filter(revision__submittal=s, revision__number=s.revision,
                                             role=SubmittalFile.SUBMISSION).order_by('pk')
        for f in files:
            out.append(Source(HandoverItem.OM, f'{req.section.title}: {f.original_name or s.title}', f.file,
                              reference=s.ref, revision=s.revision_label, file_name=f.original_name, size=f.size))
    for d in OMDocument.objects.filter(section__project=project, section__not_needed=False) \
            .select_related('section').order_by('section__order', 'section__pk', 'pk'):
        out.append(Source(HandoverItem.OM, f'{d.section.title}: {d.title}', d.file, file_name=d.original_name,
                          size=d.size))
    return out


def certificates(project):
    return [Source(HandoverItem.CERTIFICATE, c.label, c.file, reference=c.issued_by,
                   file_name=c.original_name, size=c.size)
            for c in Certificate.objects.filter(project=project)]


def completion(project):
    return [Source(HandoverItem.COMPLETION, f'Practical completion certificate: {s.name}', s.certificate,
                   revision=s.pc_date.strftime('%d %b %Y') if s.pc_date else '',
                   file_name=s.certificate.name.rsplit('/', 1)[-1])
            for s in CompletionSection.objects.filter(project=project) if s.certificate]


# ---------------------------------------------------------------------------
# Fire stopping, from the quality app when it is installed
# ---------------------------------------------------------------------------

def quality_installed():
    return apps.is_installed('quality')


def _quality_hook(module, name):
    """quality.<module>.<name>, or None when that module or function isn't there."""
    import importlib
    try:
        found = importlib.import_module(f'quality.{module}')
    except ImportError:
        return None
    return getattr(found, name, None)


def quality_items(project):
    """
    The fire-stopping register, only if the `quality` app is installed (it is not on every
    installation). Two explicit, optional entry points; nothing is guessed from model names:

      quality.handover.golden_thread_items(project)  dicts with title, file (a stored FieldFile)
                                                     and optionally reference, revision and
                                                     file_name; used as they are
      quality.exports.register(project)              (header, rows) of the register; frozen in
                                                     the pack as a formula-safe CSV snapshot

    Without the app, or without either function, this is empty and the pack page says so.
    """
    if not quality_installed():
        return []
    hook = _quality_hook('handover', 'golden_thread_items')
    if hook is not None:
        return [Source(HandoverItem.FIRE_STOPPING, str(row.get('title', '')), row.get('file'),
                       reference=str(row.get('reference', '')), revision=str(row.get('revision', '')),
                       file_name=str(row.get('file_name', '')) or getattr(row.get('file'), 'name', '').rsplit('/', 1)[-1])
                for row in hook(project) or () if row.get('file')]
    register = _quality_hook('exports', 'register')
    if register is None:
        return []
    header, rows = register(project)
    rows = list(rows)
    return [_register_csv(header, rows)] if rows else []  # nothing recorded: no empty register in the pack


def _register_csv(header, rows):
    try:
        from core.exports import safe
    except ImportError:  # until the shared helper is on main
        from levelling.exports import safe

    def cell(v):
        return safe(v if isinstance(v, str) else ('' if v is None else str(v)))
    rows = list(rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([cell(h) for h in header])
    for row in rows:
        w.writerow([cell(v) for v in row])
    name = 'fire-stopping-register.csv'
    return Source(HandoverItem.FIRE_STOPPING, f'Fire-stopping register ({len(rows)} rows)',
                  generated=(name, buf.getvalue().encode('utf-8-sig')), file_name=name)


def collect(project):
    """Everything that goes in a pack, in manifest order."""
    return completion(project) + drawings(project) + quality_items(project) + om(project) + certificates(project)
