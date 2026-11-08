"""
Global search: the box in the top bar (keyboard shortcut /).

It finds projects, RFIs, client variations, drawings and documents, subcontractors and people by
reference or title. Each source applies the same rule as the page its results open, so nobody finds
anything they couldn't already open:

  source         staff                                    external (client, consultant, subcontractor)
  -------------  ---------------------------------------  ------------------------------------------------
  projects       visible_projects (their sections;        projects they were invited to (a tendering sub's
                 archived for owners and admins)          tender opens their tender page)
  RFIs           every RFI on those projects               shared RFIs on their projects, not tenders
  variations     every client variation on those          shared ones, only where they are the client or
                                                          a consultant (as variations.views._visible)
  drawings       every document on those projects          documents.access.visible_documents per project
                 (words inside PDFs: every revision)      (words inside PDFs: only revisions they may see)
  subcontractors the supply chain (archived: admins)      never
  people         everyone in the workspace (Team page)    who's who on their projects (no tendering subs)

TenantModel.objects keeps every query inside the current company. To add a source, write a function
(request, q, limit) returning Hits with the same access rule as its list page, and add it to SOURCES.
"""
import re
from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse

from projects.access import can_see_archived, is_internal, visible_projects
from projects.models import Project, ProjectMember

MIN_LENGTH = 2
MAX_LENGTH = 80
PER_SOURCE = 6
REF = re.compile(r'^([a-z]{1,4})-?0*(\d{1,6})$', re.I)


@dataclass
class Hit:
    title: str
    url: str
    ref: str = ''
    detail: str = ''


def clean(q):
    return ' '.join((q or '').split())[:MAX_LENGTH]


def _ref_q(q, prefix_field='ref'):
    """RFI-7, rfi 7, rfi007 and RFI-007 all find RFI-007 (and so on for other refs)."""
    m = REF.match(q.replace(' ', ''))
    if not m:
        return Q(pk__in=[])
    return Q(**{f'{prefix_field}__istartswith': m.group(1) + '-', 'number': int(m.group(2))})


def _open_projects(request):
    """Projects whose records this person can open: tenders excluded for a tendering subcontractor."""
    qs = visible_projects(request, include_archived=can_see_archived(request))
    if not is_internal(request):
        qs = qs.exclude(pk__in=ProjectMember.objects.filter(user=request.user, role=ProjectMember.ROLE_BIDDER)
                        .values('project'))
    return qs


def projects(request, q, limit):
    qs = visible_projects(request, include_archived=can_see_archived(request))
    cond = Q(name__icontains=q)
    m = re.fullmatch(r'(?:p-?)?0*(\d{1,6})', q.replace(' ', ''), re.I)
    if m:
        cond |= Q(number=int(m.group(1)))
    if is_internal(request):
        cond |= Q(client_name__icontains=q)
    rows = qs.filter(cond).select_related(None).only('pk', 'name', 'number', 'stage', 'client_name', 'archived_at',
                                                       'organization').order_by('-pk')[:limit]
    internal = is_internal(request)
    return [Hit(p.name, p.get_absolute_url(), p.reference if internal else '',
                ' · '.join(x for x in ((p.client_name if internal else ''), p.get_stage_display() if internal else '',
                                       'Archived' if p.archived_at else '') if x)) for p in rows]


def rfis(request, q, limit):
    from rfis.models import RFI
    qs = RFI.objects.filter(project__in=_open_projects(request).values('pk'))
    if not is_internal(request):
        qs = qs.filter(visibility=RFI.VIS_SHARED).exclude(status=RFI.STATUS_DRAFT)  # drafts: staff until issued
    rows = qs.filter(Q(title__icontains=q) | Q(ref__iexact=q) | _ref_q(q)) \
        .select_related('project').order_by('-pk')[:limit]
    return [Hit(r.title, r.get_absolute_url(), r.reference, f'{r.project.name} · {r.get_status_display()}')
            for r in rows]


def variations(request, q, limit):
    from variations.models import Variation
    projects_qs = _open_projects(request)
    qs = Variation.objects.all()
    if not is_internal(request):
        # As variations.views._visible: only a client or consultant on the project, and only shared ones
        projects_qs = projects_qs.filter(pk__in=ProjectMember.objects.filter(
            user=request.user, role__in=(ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT)).values('project'))
        qs = qs.filter(visibility=Variation.VIS_SHARED)
    rows = qs.filter(project__in=projects_qs.values('pk')).filter(
        Q(title__icontains=q) | Q(ref__iexact=q) | _ref_q(q)).select_related('project').order_by('-pk')[:limit]
    return [Hit(v.title, v.get_absolute_url(), v.reference, f'{v.project.name} · {v.get_status_display()}')
            for v in rows]


def documents(request, q, limit):
    """
    By number and title, and by the words inside PDF revisions (documents/textindex.py) from three
    characters on. The words only ever come from revisions this person may see, so a match in work in
    progress or a revision never issued to them finds nothing.
    """
    from documents import textindex
    from documents.access import visible_documents, visible_revisions
    from projects.models import Document, DocumentRevision
    cond = Q(title__icontains=q) | Q(reference__icontains=q)
    if is_internal(request):
        qs = Document.objects.filter(project__in=_open_projects(request).values('pk'))
        revs = DocumentRevision.objects.filter(document__project__in=_open_projects(request).values('pk'))
    else:
        # documents.access decides per project, from the person's role there; build one query across them
        members = list(ProjectMember.objects.filter(user=request.user, project__in=_open_projects(request).values('pk'))
                       .select_related('project')[:50])
        if not members:
            return []
        saved = getattr(request, 'project_member', None)
        qs = revs = None
        try:
            for m in members:
                request.project_member = m
                part, rpart = visible_documents(request, m.project), visible_revisions(request, m.project)
                qs = part if qs is None else qs | part
                revs = rpart if revs is None else revs | rpart
        finally:
            request.project_member = saved
    inside = textindex.matching_revisions(textindex.openable(request, revs), q)  # never a withdrawn one for externals
    if len(q) >= textindex.MIN_QUERY:
        cond |= Q(pk__in=inside.values('revision__document_id'))
    rows = list(qs.filter(cond).select_related('project').order_by('-updated_at')[:limit])
    # A line from the file for documents found by their words (one more query, however many hits)
    found = {}
    if rows and len(q) >= textindex.MIN_QUERY:
        plain = q.lower()
        for doc_id, text in inside.filter(revision__document__in=[d.pk for d in rows]) \
                .order_by('-revision__is_current', '-revision__uploaded_at').values_list('revision__document_id', 'text'):
            if doc_id not in found:
                found[doc_id] = textindex.snippet(text, q)
        found = {k: v for k, v in found.items() if v}
        for d in rows:  # a number or title match needs no quote from the file
            if plain in d.title.lower() or plain in (d.reference or '').lower():
                found.pop(d.pk, None)
    return [Hit(d.title, reverse('document_detail', args=[request.org.slug, d.project_id, d.pk]), d.reference,
                ' · '.join(x for x in (d.project.name, d.get_category_display(),
                                       f'Found in the file: {found[d.pk]}' if d.pk in found else '') if x))
            for d in rows]


def subcontractors(request, q, limit):
    if not is_internal(request):
        return []
    from directory.models import Subcontractor
    qs = Subcontractor.objects.all()
    if not request.membership.is_admin:
        qs = qs.filter(archived_at__isnull=True)
    rows = qs.filter(Q(company_name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q)) \
        .prefetch_related('trades').order_by('company_name')[:limit]
    return [Hit(s.company_name, s.get_absolute_url(), '',
                ' · '.join(x for x in (', '.join(t.name for t in s.trades.all()), s.contact_name,
                                       'Archived' if s.archived_at else '') if x)) for s in rows]


def people(request, q, limit):
    from tenancy.models import Membership
    who = Q(user__full_name__icontains=q) | Q(user__email__icontains=q)
    if is_internal(request):
        rows = Membership.objects.filter(who, organization=request.org, is_active=True) \
            .select_related('user').order_by('user__full_name')[:limit]
        team = reverse('team', args=[request.org.slug])
        return [Hit(m.user.full_name or m.user.email, team, '',
                    'External collaborator' if m.role == Membership.ROLE_EXTERNAL else m.get_role_display())
                for m in rows]
    # Externals: who's who on their projects, as the project's Team page shows it (no tendering subs)
    rows = ProjectMember.objects.filter(who, project__in=_open_projects(request).values('pk')) \
        .exclude(role=ProjectMember.ROLE_BIDDER).select_related('user', 'project', 'subcontractor') \
        .order_by('user__full_name', 'project_id')
    out, seen = [], set()
    for m in rows[:limit * 3]:
        if m.user_id in seen:
            continue
        seen.add(m.user_id)
        company = m.subcontractor.company_name if m.subcontractor_id else (m.company_name or '')
        out.append(Hit(m.user.full_name or m.user.email, reverse('project_team', args=[request.org.slug, m.project_id]),
                       '', ' · '.join(x for x in (m.get_role_display(), company, m.project.name) if x)))
        if len(out) == limit:
            break
    return out


SOURCES = [
    ('projects', 'Projects', projects),
    ('rfis', 'RFIs', rfis),
    ('variations', 'Client variations', variations),
    ('documents', 'Drawings and documents', documents),
    ('subcontractors', 'Subcontractors', subcontractors),
    ('people', 'People', people),
]


def run(request, q, limit=PER_SOURCE):
    """[(key, label, [Hit])] for every source with a match. Nothing runs for a query under 2 characters."""
    q = clean(q)
    if len(q) < MIN_LENGTH:
        return []
    out = []
    for key, label, fn in SOURCES:
        hits = fn(request, q, limit)
        if hits:
            out.append((key, label, hits))
    return out
