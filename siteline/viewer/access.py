"""
Who may open a revision in the viewer, and which pins they see.

The viewer never widens what someone can already reach:

  - The revision must be one documents.access lets them see (staff: all; externals: issued to
    them, or shared to their role once it has left work in progress).
  - Externals also need it to have been shared (shared_at set) and not withdrawn. Withdrawn
    revisions stay listed for them but can't be downloaded, so they can't be viewed either; a
    revision they uploaded themselves that was never shared stays a download only.
  - Staff can open withdrawn revisions, with a "do not use" banner.

Pins are filtered by the record they point at, using the same rules as the snag and RFI pages:
staff see every pin; an external sees pins for snags assigned to them and RFIs shared with
collaborators. A pin on a document they can't see is never shown, even from the record page.
"""
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404

from documents.access import display_revision, revisions_for, visible_documents, visible_revisions
from projects.access import is_internal
from projects.models import DocumentRevision, Issue
from rfis.models import RFI

from .models import DrawingPin


def is_pdf(rev):
    return (rev.original_name or rev.file.name or '').lower().endswith('.pdf')


def can_view(request, rev):
    """May this person open this (already visible) revision in the viewer?"""
    if not is_pdf(rev):
        return False
    if is_internal(request):
        return True
    return rev.shared_at is not None and rev.status != DocumentRevision.WITHDRAWN


def can_view_external(rev):
    """The external half of can_view, for templates that only have the revision."""
    return is_pdf(rev) and rev.shared_at is not None and rev.status != DocumentRevision.WITHDRAWN


def get_doc_and_revision(request, project, doc_id, rev_id):
    """The document and revision from the URL, or a 404 if this person can't see them at all."""
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    rev = get_object_or_404(revisions_for(request, project, doc), pk=rev_id)
    return doc, rev


def viewable_revisions(request, project, doc):
    revs = list(revisions_for(request, project, doc))
    return [r for r in revs if can_view(request, r)]


def best_revision(request, project, doc):
    """The revision to open a pin on: the current one if they may view it, else the latest they may."""
    revs = viewable_revisions(request, project, doc)
    if not revs:
        raise Http404
    live = [r for r in revs if r.status != DocumentRevision.WITHDRAWN]
    return display_revision(live or revs)


def visible_issues(request, project):
    qs = Issue.objects.filter(project=project)
    return qs if is_internal(request) else qs.filter(assignee=request.user)


def visible_rfis(request, project):
    qs = RFI.objects.filter(project=project)
    return qs if is_internal(request) else qs.filter(visibility=RFI.VIS_SHARED)


def visible_pins(request, project, doc=None):
    qs = DrawingPin.objects.filter(project=project)
    if doc is not None:
        qs = qs.filter(document=doc)
    if not is_internal(request):
        qs = qs.filter(Q(issue__in=visible_issues(request, project).values('pk'))
                       | Q(rfi__in=visible_rfis(request, project).values('pk')))
    # RFI.objects hides soft-deleted RFIs; a pin to one stays hidden for staff too
    qs = qs.filter(Q(issue__isnull=False) | Q(rfi__in=RFI.objects.filter(project=project).values('pk')))
    return qs.select_related('issue', 'rfi', 'revision', 'document')


def pins_for_record(request, project, record):
    """Pins of this snag or RFI on documents this person can see (for the record's own page)."""
    field = 'issue' if isinstance(record, Issue) else 'rfi'
    return visible_pins(request, project).filter(**{field: record}).filter(
        document__in=visible_documents(request, project).values('pk')).order_by('document__reference', 'page')


def can_remove(request, pin):
    return is_internal(request) or pin.created_by_id == request.user.pk


def can_raise_snag(request):
    return is_internal(request)


def visible_revision_ids(request, project):
    return None if is_internal(request) else set(visible_revisions(request, project).values_list('pk', flat=True))
