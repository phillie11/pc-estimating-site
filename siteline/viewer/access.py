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
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404

from documents.access import display_revision, revisions_for, visible_documents, visible_revisions
from projects.access import is_internal
from projects.models import DocumentRevision, Issue
from rfis.models import RFI

from .models import DrawingPin, MarkupLayer


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


# ---------------------------------------------------------------------------
# Mark-up layers
# ---------------------------------------------------------------------------

def visible_layers(request, project, doc, revisions=None):
    """
    Mark-up layers on this document this person may see. Never wider than the drawing itself: the
    layer's own revision must be one they may open in the viewer (so a layer drawn on work in
    progress or a withdrawn revision stays hidden from externals, even when shown over a newer one).

      their own layers                      always (on revisions they may open)
      staff                                 also layers shared with the site team or wider, by anyone
      externals                             also staff layers shared with everyone the drawing is shared with

    "Staff layer" is checked now, not when it was shared: the owner must still be an active staff member
    of this company (an active account and an active owner, admin or staff membership). A layer shared
    with everyone by someone who has since become external or left is shown to staff as shared with the
    site team only, and to no external. Each layer carries owner_staff (bool) for effective_share().
    """
    revs = revisions if revisions is not None else viewable_revisions(request, project, doc)
    qs = MarkupLayer.objects.filter(project=project, document=doc, revision__in=[r.pk for r in revs]) \
        .annotate(owner_staff=_owner_is_staff(project))
    mine = Q(owner=request.user)
    if is_internal(request):
        qs = qs.filter(mine | Q(share__in=(MarkupLayer.TEAM, MarkupLayer.EXTERNAL)))
    else:
        qs = qs.filter(mine | Q(share=MarkupLayer.EXTERNAL, owner_staff=True))
    return qs.select_related('owner', 'revision')


def _owner_is_staff(project):
    from tenancy.models import Membership
    return Exists(Membership.objects.filter(organization_id=project.organization_id, user=OuterRef('owner'),
                                            role__in=Membership.INTERNAL_ROLES, is_active=True,
                                            user__is_active=True))


def effective_share(request, layer):
    """
    Who a layer is really shared with: a layer shared with everyone counts only as shared with the site team
    once its owner is no longer staff (see visible_layers).
    """
    if layer.share != MarkupLayer.EXTERNAL:
        return layer.share
    staff = getattr(layer, 'owner_staff', None)
    if staff is None:  # not loaded through visible_layers (a layer just saved: its owner is the person asking)
        staff = layer.owner_id == request.user.pk and is_internal(request)
    return MarkupLayer.EXTERNAL if staff else MarkupLayer.TEAM


def can_mark_up(request, project, rev):
    """May this person draw a layer of their own on this revision? Not on archived jobs or withdrawn revisions."""
    return not project.is_archived and not rev.is_withdrawn and can_view(request, rev)


def share_choices(request):
    """Who an owner may share their layer with: only staff can share with externals."""
    if is_internal(request):
        return MarkupLayer.SHARE_CHOICES
    return [(MarkupLayer.PRIVATE, 'Only me'), (MarkupLayer.TEAM, 'The site team')]
