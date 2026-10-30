"""
Which documents and revisions a person may see on a project.

Staff see everything. An external collaborator (client, consultant, appointed
subcontractor) sees a revision when either:

  - it was on an issued transmittal to them, or to the company they represent on the
    project; or
  - its document is shared with their role (Document.visibility SHARED, and share_roles
    blank or naming their role), and the revision is past work in progress.

Documents shared before transmittals existed keep working through the second route: the
data migration gave their revisions a shared, published or superseded status. Externals
also always see revisions they uploaded themselves. Tendering subcontractors never get
here (get_project refuses them); they see tender documents on the tender page only.
"""
from collections import defaultdict

from django.db.models import Q

from projects.access import is_internal
from projects.models import Document, DocumentRevision

from .models import Transmittal, TransmittalItem, recipient_q


def _member(request):
    return getattr(request, 'project_member', None)


def transmitted_revision_ids(request, project):
    """Revisions on issued transmittals to this person (or their company) on this project."""
    return TransmittalItem.objects.filter(
        recipient_q(request.user, prefix='transmittal__recipients__', project=project),
        transmittal__project=project, transmittal__status__in=Transmittal.SENT_STATUSES,
        transmittal__deleted_at__isnull=True,
    ).values('revision_id')


def external_revision_q(request, project):
    member = _member(request)
    role = member.role if member else None
    by_role = Q(document__visibility=Document.VIS_SHARED) & ~Q(status=DocumentRevision.WIP) & (
        Q(document__share_roles='') | Q(document__share_roles__contains=f',{role},'))
    return by_role | Q(pk__in=transmitted_revision_ids(request, project)) | Q(uploaded_by=request.user)


def visible_revisions(request, project):
    qs = DocumentRevision.objects.filter(document__project=project)
    if is_internal(request):
        return qs
    return qs.filter(external_revision_q(request, project))


def visible_documents(request, project):
    qs = Document.objects.filter(project=project)
    if is_internal(request):
        return qs
    member = _member(request)
    role = member.role if member else None
    shared_to_role = Q(visibility=Document.VIS_SHARED) & (Q(share_roles='') | Q(share_roles__contains=f',{role},'))
    return qs.filter(shared_to_role | Q(pk__in=visible_revisions(request, project).values('document_id'))
                     | Q(created_by=request.user))


def revisions_for(request, project, doc):
    """This document's revisions the person may see, newest upload first."""
    return visible_revisions(request, project).filter(document=doc).select_related('uploaded_by', 'superseded_by')


def display_revision(revisions):
    """The revision to show on the register: the current one if visible, else the latest the person can see."""
    from projects.models import latest_revision
    revisions = list(revisions)
    cur = next((r for r in revisions if r.is_current), None)
    if cur:
        return cur
    live = [r for r in revisions if r.status != DocumentRevision.WITHDRAWN]
    return latest_revision(live or revisions)


def latest_revisions(request, project, limit=6):
    """The most recently updated documents' revisions this person can see, for the project overview."""
    docs = list(visible_documents(request, project).order_by('-updated_at')[:limit * 3])
    by_doc = defaultdict(list)
    for r in visible_revisions(request, project).filter(document__in=[d.pk for d in docs]).select_related('document'):
        by_doc[r.document_id].append(r)
    out = []
    for d in docs:
        if by_doc.get(d.pk):
            out.append(display_revision(by_doc[d.pk]))
        if len(out) == limit:
            break
    return out
