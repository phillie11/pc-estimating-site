"""
What the API serves, declared once: each resource's fields, filters and the rows it reads. The views
(api/views.py), the OpenAPI document (api/openapi.py) and the human docs page all come from RESOURCES,
so they can't disagree.

Rules every resource keeps:

  * A token reads as a member of staff in both estimating and projects: every project that isn't
    archived. Nothing an external collaborator's view adds or hides applies, and archived jobs
    (owners and admins only on screen) are left out.
  * Fields are listed one by one (an allow-list), never "every column", so a field added to a model
    later is not served until someone adds it here. No email addresses, phone numbers, tax
    references (UTR, CIS verification numbers), payment references or injured people's details.
  * Money is a string with two decimal places ("1250.00"), so no figure goes through a float. Dates
    are ISO 8601 (2026-11-07); times are ISO 8601 in UTC.
  * Every queryset is a TenantModel one, run inside the token's organisation (api/views.py), so a row
    from another company can't be reached by changing an id: it is a 404.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Callable, Optional

from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from costs.money import pennies

from .models import (SCOPE_COMMERCIAL, SCOPE_DOCUMENTS, SCOPE_PROJECTS, SCOPE_REGISTERS, SCOPE_SAFETY,
                     SCOPE_SNAGS)


class BadParameter(ValueError):
    """A query-string value the API can't use: answered with a 400 and this message."""


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    name: str
    type: str                     # integer, string, boolean, date, date-time, money, number, object, array
    get: Callable                 # get(obj, ctx) -> value
    description: str = ''
    nullable: bool = False
    enum: tuple = ()
    detail_only: bool = False     # only on the single-record endpoint
    items: Optional[dict] = None  # OpenAPI schema for an array's items or an object's properties


@dataclass(frozen=True)
class Filter:
    name: str
    type: str
    description: str
    apply: Callable               # apply(queryset, value, ctx) -> queryset
    enum: tuple = ()


@dataclass
class Resource:
    name: str                     # URL segment and OpenAPI tag, e.g. "rfis"
    title: str
    scope: str
    description: str
    fields: list
    queryset: Callable            # queryset(ctx) -> the rows this token may read, in a stable order
    filters: list = field(default_factory=list)
    prepare: Optional[Callable] = None  # prepare(rows, ctx, detail): batch any per-row work for one page
    detail_lookup: str = 'pk'
    detail: bool = True
    example_id: str = '12'

    def list_url_name(self):
        return f'api_{self.name.replace("-", "_")}'

    def detail_url_name(self):
        return f'api_{self.name.replace("-", "_")}_detail'

    def serialize(self, obj, ctx, detail=False):
        return {f.name: f.get(obj, ctx) for f in self.fields if detail or not f.detail_only}


class Context:
    """What a resource needs to know about the call: the token, its company, the request, today."""

    def __init__(self, token, request):
        self.token = token
        self.org = token.organization
        self.request = request
        self.today = timezone.localdate()
        self.now = timezone.now()

    def absolute(self, path):
        return self.request.build_absolute_uri(path)


def money(value):
    return None if value is None else str(pennies(value))


def percent(value):
    return None if value is None else str(value)


def iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(dt_timezone.utc).isoformat().replace('+00:00', 'Z')
    return value.isoformat()


def attr(name, conv=None):
    def get(obj, ctx):
        value = obj
        for part in name.split('.'):
            value = getattr(value, part, None) if value is not None else None
        if callable(value) and not isinstance(value, (Decimal, datetime)):
            value = value()
        return conv(value) if conv else value
    return get


def display(name):
    """The label for a choices field (get_<name>_display)."""
    return lambda obj, ctx: getattr(obj, f'get_{name}_display')()


def person(name):
    """A person's display name only: never their email address or phone number."""
    def get(obj, ctx):
        user = getattr(obj, name, None)
        if user is None:
            return None
        return user.full_name or 'Unnamed user'
    return get


def web_url(obj, ctx):
    try:
        return ctx.absolute(obj.get_absolute_url())
    except Exception:  # noqa: BLE001 - a row whose page needs something it doesn't have; the link is optional
        return None


def choices(model, name):
    return tuple(code for code, _ in model._meta.get_field(name).choices)


# --- common fields and filters ------------------------------------------------------------------

def f_id(desc='Siteline\'s id for this record.'):
    return Field('id', 'integer', attr('pk'), desc)


def f_project():
    return Field('project_id', 'integer', attr('project_id'), 'The job it belongs to (see /projects/).')


def f_project_ref():
    return Field('project_ref', 'string', lambda o, c: o.project.reference, "The job's reference, e.g. P-0012.")


def f_web():
    return Field('web_url', 'string', web_url, 'The page in Siteline (needs a staff login).', nullable=True)


# The earliest time a filter takes. Anything earlier means "everything", and dates near year 1 overflow
# once they are moved into UTC or back a month.
EARLIEST = datetime(1900, 1, 1, tzinfo=dt_timezone.utc)
MAX_ID_DIGITS = 18  # ids are 64-bit at most; a longer number is no record's id (and int() refuses 4,300+ digits)


def _int(value, name):
    if not value.isdigit() or not value.isascii() or len(value) > MAX_ID_DIGITS:
        raise BadParameter(f'{name} must be a whole number.')
    return int(value)


def _project_filter(qs, value, ctx, lookup='project'):
    from projects.models import Project
    pk = _int(value, 'project')
    # A job this token can't see (another company's, or archived) is a 404, as its own endpoint is
    if not visible_projects(ctx).filter(pk=pk).exists():
        raise Http404
    return qs.filter(**{lookup: pk}) if lookup else qs.filter(pk=pk)


def project_filter(lookup='project'):
    return Filter('project', 'integer', 'Only this job\'s records (a project id).',
                  lambda qs, v, c: _project_filter(qs, v, c, lookup))


def _since(value, name):
    message = f'{name} must be a date (2026-11-01) or a date and time (2026-11-01T09:00:00Z).'
    try:
        # parse_* return None for text not shaped like a date and raise ValueError for one that is shaped
        # right but doesn't exist (2026-02-30): both are the caller's mistake, never a server error
        dt = parse_datetime(value)
        if dt is None:
            d = parse_date(value)
            if d is None:
                raise BadParameter(message)
            dt = datetime(d.year, d.month, d.day)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, dt_timezone.utc)
        dt = dt.astimezone(dt_timezone.utc)  # 9999-12-31T23:59-01:00 is past the last date there is
    except (ValueError, OverflowError):
        raise BadParameter(message)
    return max(dt, EARLIEST)


def updated_since(field_name='updated_at'):
    return Filter('updated_since', 'date-time', 'Only records changed at or after this time (UTC unless it says).',
                  lambda qs, v, c: qs.filter(**{f'{field_name}__gte': _since(v, 'updated_since')}))


def status_filter(model, name='status'):
    allowed = choices(model, name)

    def apply(qs, value, ctx):
        wanted = [v.strip().upper() for v in value.split(',') if v.strip()]
        bad = [v for v in wanted if v not in allowed]
        if bad:
            raise BadParameter(f'{name} must be one or more of {", ".join(allowed)}.')
        return qs.filter(**{f'{name}__in': wanted})
    return Filter(name, 'string', f'Only these {name} codes, comma separated.', apply, enum=allowed)


def visible_projects(ctx):
    """Every job a member of staff in both sections can open: all of them except archived ones."""
    from projects.models import Project
    return Project.objects.filter(archived_at__isnull=True)


def on_visible(qs, lookup='project'):
    return qs.filter(**{f'{lookup}__archived_at__isnull': True})


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _projects_qs(ctx):
    return visible_projects(ctx).select_related('manager').order_by('pk')


def _project_stage(qs, value, ctx):
    from projects.models import Project
    allowed = choices(Project, 'stage')
    wanted = [v.strip().upper() for v in value.split(',') if v.strip()]
    if any(v not in allowed for v in wanted):
        raise BadParameter(f'stage must be one or more of {", ".join(allowed)}.')
    return qs.filter(stage__in=wanted)


def _projects():
    from projects.models import Project
    return Resource(
        name='projects', title='Projects', scope=SCOPE_PROJECTS,
        description='Every bid and job in the company except archived ones.',
        queryset=_projects_qs,
        filters=[Filter('stage', 'string', 'Only these stages, comma separated.', _project_stage,
                        enum=choices(Project, 'stage')),
                 updated_since()],
        fields=[
            f_id(), Field('ref', 'string', attr('reference'), 'Job reference, e.g. P-0012.'),
            Field('name', 'string', attr('name')),
            Field('client_name', 'string', attr('client_name')),
            Field('site_address', 'string', attr('site_address')),
            Field('stage', 'string', attr('stage'), 'Stage code.', enum=choices(Project, 'stage')),
            Field('stage_label', 'string', display('stage'), 'Stage, in words.'),
            Field('sector', 'string', attr('sector'), enum=('',) + choices(Project, 'sector')),
            Field('value', 'money', attr('value', money), 'Tender or contract value, excluding VAT.', nullable=True),
            Field('start_date', 'date', attr('start_date', iso), nullable=True),
            Field('completion_date', 'date', attr('completion_date', iso), nullable=True),
            Field('project_manager', 'string', person('manager'), nullable=True),
            Field('created_at', 'date-time', attr('created_at', iso)),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            f_web(),
        ])


# ---------------------------------------------------------------------------
# RFIs, variations and change events
# ---------------------------------------------------------------------------

def _overdue(obj, ctx):
    """As the RFI's own page (RFI.is_overdue) and the overdue report: still waiting for a response, past its date."""
    from rfis.models import RFI
    return bool(obj.status == RFI.STATUS_OPEN and obj.due_date and obj.due_date < ctx.today)


def _rfis():
    from rfis.models import RFI
    return Resource(
        name='rfis', title='RFIs', scope=SCOPE_REGISTERS,
        description='Requests for information on every job, internal and shared, as staff see them.',
        queryset=lambda ctx: on_visible(RFI.objects.all()).select_related('project', 'ball_in_court_user')
        .order_by('pk'),
        filters=[project_filter(), status_filter(RFI), updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. RFI-004.'),
            Field('title', 'string', attr('title'), 'Subject.'),
            Field('status', 'string', attr('status'), enum=choices(RFI, 'status')),
            Field('status_label', 'string', display('status')),
            Field('shared', 'boolean', attr('is_shared'), 'Shared with the client side and subcontractors.'),
            Field('category', 'string', attr('category'), enum=choices(RFI, 'category')),
            Field('priority', 'string', attr('priority')),
            Field('due_date', 'date', attr('due_date', iso), 'Response required by.', nullable=True),
            Field('overdue', 'boolean', _overdue, 'Still open and past its due date.'),
            Field('with', 'string', person('ball_in_court_user'), 'Who it is waiting on.', nullable=True),
            Field('cost_impact', 'string', attr('cost_impact'), enum=choices(RFI, 'cost_impact')),
            Field('programme_impact_days', 'integer', attr('programme_impact_days'), nullable=True),
            Field('raised_at', 'date-time', attr('created_at', iso)),
            Field('responded_at', 'date-time', attr('responded_at', iso), nullable=True),
            Field('closed_at', 'date-time', attr('closed_at', iso), nullable=True),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            Field('question', 'string', attr('question'), detail_only=True),
            Field('response', 'string', attr('response'), detail_only=True),
            f_web(),
        ])


def _variations():
    from variations.models import Variation
    return Resource(
        name='variations', title='Client variations', scope=SCOPE_REGISTERS,
        description='Client variations (VO-001), drafts included, as staff see them. Values exclude VAT.',
        queryset=lambda ctx: on_visible(Variation.objects.all()).select_related('project').order_by('pk'),
        filters=[project_filter(), status_filter(Variation), updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. VO-002.'),
            Field('title', 'string', attr('title')),
            Field('status', 'string', attr('status'), enum=choices(Variation, 'status')),
            Field('status_label', 'string', display('status')),
            Field('cause', 'string', attr('cause'), enum=choices(Variation, 'cause')),
            Field('quoted_value', 'money', attr('value', money), nullable=True),
            Field('approved_value', 'money', attr('approved_value', money), nullable=True),
            Field('time_impact_days', 'integer', attr('time_impact_days'), nullable=True),
            Field('respond_by', 'date', attr('due_date', iso), nullable=True),
            Field('client_reference', 'string', attr('client_reference')),
            Field('submitted_at', 'date-time', attr('submitted_at', iso), nullable=True),
            Field('decided_at', 'date-time', attr('decided_at', iso), nullable=True),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            Field('description', 'string', attr('description'), detail_only=True),
            f_web(),
        ])


def _change_events():
    from changes.models import ChangeEvent
    return Resource(
        name='change-events', title='Change events', scope=SCOPE_REGISTERS,
        description='The change register (CE-001): estimated cost and revenue from its lines, excluding VAT.',
        queryset=lambda ctx: on_visible(ChangeEvent.objects.all()).select_related('project')
        .prefetch_related('lines').order_by('pk'),
        filters=[project_filter(), status_filter(ChangeEvent), updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. CE-003.'),
            Field('title', 'string', attr('title')),
            Field('status', 'string', attr('status'), enum=choices(ChangeEvent, 'status')),
            Field('status_label', 'string', display('status')),
            Field('origin', 'string', attr('origin'), enum=choices(ChangeEvent, 'origin')),
            Field('cost_total', 'money', attr('cost_total', money), 'What it costs us.'),
            Field('revenue_total', 'money', attr('revenue_total', money), 'What we recover from the client.'),
            Field('margin', 'money', attr('margin', money)),
            Field('time_impact_days', 'integer', attr('time_impact_days'), nullable=True),
            Field('aware_on', 'date', attr('aware_on', iso), nullable=True),
            Field('notified_on', 'date', attr('notified_on', iso), nullable=True),
            Field('quotation_submitted_on', 'date', attr('quotation_submitted_on', iso), nullable=True),
            Field('reply_received_on', 'date', attr('reply_received_on', iso), nullable=True),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            Field('description', 'string', attr('description'), detail_only=True),
            f_web(),
        ])


# ---------------------------------------------------------------------------
# Drawings and documents
# ---------------------------------------------------------------------------

REVISION_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'}, 'revision': {'type': 'string'}, 'status': {'type': 'string'},
        'suitability': {'type': 'string'}, 'current': {'type': 'boolean'}, 'file_name': {'type': 'string'},
        'size': {'type': 'integer'}, 'sha256': {'type': 'string'}, 'uploaded_at': {'type': 'string',
                                                                                     'format': 'date-time'},
        'download_url': {'type': 'string', 'description': 'Needs the same token. Every download is logged.'},
    },
}


def revision_json(rev, ctx):
    return {
        'id': rev.pk, 'revision': rev.revision, 'status': rev.status, 'suitability': rev.suitability,
        'current': rev.is_current, 'file_name': rev.original_name, 'size': rev.size, 'sha256': rev.sha256,
        'uploaded_at': iso(rev.uploaded_at),
        'download_url': ctx.absolute(reverse('api_document_download', args=[rev.document_id, rev.pk])),
    }


def _current_revision(doc, ctx):
    revs = list(doc.revisions.all())  # prefetched
    cur = next((r for r in revs if r.is_current), None)
    if cur is None and revs:
        cur = max(revs, key=lambda r: (r.uploaded_at, r.pk))
    return revision_json(cur, ctx) if cur else None


def _all_revisions(doc, ctx):
    return [revision_json(r, ctx) for r in sorted(doc.revisions.all(), key=lambda r: (r.uploaded_at, r.pk))]


def _doc_category(qs, value, ctx):
    from projects.models import Document
    allowed = choices(Document, 'category')
    wanted = [v.strip().upper() for v in value.split(',') if v.strip()]
    if any(v not in allowed for v in wanted):
        raise BadParameter(f'category must be one or more of {", ".join(allowed)}.')
    return qs.filter(category__in=wanted)


def _documents():
    from projects.models import Document
    return Resource(
        name='documents', title='Drawings and documents', scope=SCOPE_DOCUMENTS,
        description='The drawing and document register: details and revisions, never the file itself. Each '
                    'revision has a download link that needs the same token and is logged like any download.',
        queryset=lambda ctx: on_visible(Document.objects.all()).select_related('project')
        .prefetch_related('revisions').order_by('pk'),
        filters=[project_filter(), Filter('category', 'string', 'Only these categories, comma separated.',
                                          _doc_category, enum=choices(Document, 'category')),
                 updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('reference', 'string', attr('reference'), 'Drawing or document number.'),
            Field('title', 'string', attr('title')),
            Field('category', 'string', attr('category'), enum=choices(Document, 'category')),
            Field('shared', 'boolean', lambda o, c: o.visibility == Document.VIS_SHARED,
                  'Shared with collaborators (otherwise internal).'),
            Field('discipline', 'string', attr('discipline')),
            Field('folder', 'string', attr('folder')),
            Field('scale', 'string', attr('scale')),
            Field('sheet_size', 'string', attr('sheet_size')),
            Field('current_revision', 'object', _current_revision, nullable=True, items=REVISION_SCHEMA),
            Field('revisions', 'array', _all_revisions, 'Every revision, oldest first.', detail_only=True,
                  items=REVISION_SCHEMA),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            f_web(),
        ])


# ---------------------------------------------------------------------------
# Snags and defects
# ---------------------------------------------------------------------------

def _issue_type(qs, value, ctx):
    from projects.models import Issue
    allowed = choices(Issue, 'issue_type')
    wanted = [v.strip().upper() for v in value.split(',') if v.strip()]
    if any(v not in allowed for v in wanted):
        raise BadParameter(f'type must be one or more of {", ".join(allowed)}.')
    return qs.filter(issue_type__in=wanted)


def _issue_url(obj, ctx):
    return ctx.absolute(reverse('issue_detail', args=[ctx.org.slug, obj.project_id, obj.pk]))


def _snags():
    from projects.models import Issue
    return Resource(
        name='snags', title='Snags and defects', scope=SCOPE_SNAGS,
        description='Snags, defects (raised after practical completion) and site observations.',
        queryset=lambda ctx: on_visible(Issue.objects.all()).select_related('project', 'assignee').order_by('pk'),
        filters=[project_filter(), Filter('type', 'string', 'Only these types, comma separated (SNAG, DEFECT...).',
                                          _issue_type, enum=choices(Issue, 'issue_type')),
                 status_filter(Issue)],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. IS-012.'),
            Field('title', 'string', attr('title')),
            Field('type', 'string', attr('issue_type'), enum=choices(Issue, 'issue_type')),
            Field('status', 'string', attr('status'), enum=choices(Issue, 'status')),
            Field('status_label', 'string', display('status')),
            Field('priority', 'string', attr('priority')),
            Field('location', 'string', attr('location')),
            Field('assigned_to', 'string', person('assignee'), nullable=True),
            Field('due_date', 'date', attr('due_date', iso), nullable=True),
            Field('raised_at', 'date-time', attr('created_at', iso)),
            Field('closed_at', 'date-time', attr('closed_at', iso), nullable=True),
            Field('description', 'string', attr('description'), detail_only=True),
            Field('web_url', 'string', _issue_url),
        ])


# ---------------------------------------------------------------------------
# Money: cost report totals, applications, sub payments and the CVR
# ---------------------------------------------------------------------------

def cost_stage_projects(ctx):
    from costs.access import COST_STAGES
    return visible_projects(ctx).filter(stage__in=COST_STAGES)


def cost_totals(projects):
    """
    {project_id: {...}} for a page of jobs in a fixed number of queries, by the cost report's own
    rules (costs.report): budget, committed and actual from the ledger, forecast final cost from
    costs.report.forecast_costs, the final account from costs.report.final_accounts, the margin
    from costs.money.margin and the warning from costs.report.is_below. api/tests hold each figure to
    costs.report.cost_report() to the penny.
    """
    from django.db.models import Sum

    from costs.access import settings_for
    from costs.models import LedgerEntry
    from costs.money import margin
    from costs.report import final_accounts, forecast_costs, is_below
    projects = list(projects)
    ids = [p.pk for p in projects]
    sums = {}
    for row in LedgerEntry.objects.filter(project_id__in=ids).values('project_id', 'kind').annotate(s=Sum('amount')):
        sums.setdefault(row['project_id'], {})[row['kind']] = row['s']
    costs = forecast_costs(ids)
    accounts = final_accounts(projects)
    threshold = settings_for(projects[0].organization).margin_threshold if projects else None
    out = {}
    zero = Decimal('0.00')
    for p in projects:
        t = sums.get(p.pk, {})
        final_cost = costs.get(p.pk, zero)
        has_costs = p.pk in costs
        amount, pct = margin(accounts[p.pk], final_cost)
        out[p.pk] = {
            'budget': pennies(t.get('BUDGET') or 0), 'committed': pennies(t.get('COMMITMENT') or 0),
            'actual': pennies(t.get('ACTUAL') or 0), 'final_cost': final_cost, 'final_account': pennies(accounts[p.pk]),
            'margin': amount, 'margin_pct': pct, 'threshold': threshold, 'has_costs': has_costs,
            'below': has_costs and is_below(accounts[p.pk], final_cost, threshold),
        }
    return out


def _prepare_costs(rows, ctx, detail):
    totals = cost_totals(rows)
    for p in rows:
        p._api_costs = totals[p.pk]
    if detail:
        from costs.report import cost_report
        for p in rows:
            p._api_report = cost_report(p)


def _cost(name, conv=money):
    return lambda o, c: conv(o._api_costs[name])


def _cost_lines(project, ctx):
    rep = project._api_report
    return [{'code': r.code.code, 'name': r.code.name, 'budget': money(r.budget), 'committed': money(r.committed),
             'actual': money(r.actual), 'forecast_final_cost': money(r.forecast), 'variance': money(r.variance)}
            for r in rep['rows']]


COST_LINE_SCHEMA = {'type': 'object', 'properties': {
    'code': {'type': 'string'}, 'name': {'type': 'string'},
    **{k: {'type': 'string', 'format': 'decimal'} for k in ('budget', 'committed', 'actual', 'forecast_final_cost',
                                                            'variance')}}}


def _cost_reports():
    return Resource(
        name='cost-reports', title='Cost report totals', scope=SCOPE_COMMERCIAL,
        description='One row per won, live or complete job: the totals on its cost report, worked out by the '
                    'same rules as Commercial → Costs. The single-job endpoint adds a line per cost code. '
                    'Excluding VAT.',
        queryset=lambda ctx: cost_stage_projects(ctx).order_by('pk'), prepare=_prepare_costs,
        filters=[Filter('project', 'integer', "Only this job's row (a project id).",
                        lambda qs, v, c: _project_filter(qs, v, c, lookup=None))],
        example_id='4',
        fields=[
            Field('project_id', 'integer', attr('pk')),
            Field('project_ref', 'string', attr('reference')),
            Field('project_name', 'string', attr('name')),
            Field('budget', 'money', _cost('budget')),
            Field('committed', 'money', _cost('committed'), 'Orders placed, with adjustments.'),
            Field('actual', 'money', _cost('actual'), 'Actual cost to date.'),
            Field('forecast_final_cost', 'money', _cost('final_cost')),
            Field('forecast_final_account', 'money', _cost('final_account'),
                  'Contract sum plus approved client variations.'),
            Field('forecast_margin', 'money', _cost('margin')),
            Field('forecast_margin_percent', 'number', _cost('margin_pct', percent), 'To 0.1%.', nullable=True),
            Field('below_margin_warning', 'boolean', lambda o, c: o._api_costs['below'],
                  "Under the company's margin warning (or costs with no final account)."),
            Field('lines', 'array', _cost_lines, 'One per cost code with any entry or forecast.', detail_only=True,
                  items=COST_LINE_SCHEMA),
        ])


def _prepare_applications(rows, ctx, detail):
    from payments.summary import prime_paid_totals
    prime_paid_totals(rows)


def _applications():
    from payments.models import Application
    return Resource(
        name='applications', title='Applications for payment', scope=SCOPE_COMMERCIAL,
        description='Our applications to the client, drafts included. Figures as on the application: amounts '
                    'exclude VAT unless they say so. paid_total is what was received, including VAT.',
        queryset=lambda ctx: on_visible(Application.objects.all()).select_related('project').order_by('pk'),
        prepare=_prepare_applications,
        filters=[project_filter(), status_filter(Application), updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. AP-003.'),
            Field('title', 'string', attr('title')),
            Field('status', 'string', attr('status'), enum=choices(Application, 'status')),
            Field('status_label', 'string', display('status')),
            Field('valuation_date', 'date', attr('valuation_date', iso)),
            Field('gross_valuation', 'money', attr('gross_valuation', money)),
            Field('retention', 'money', attr('retention', money), 'Retention held.'),
            Field('net_valuation', 'money', attr('net_valuation', money)),
            Field('previous_certified', 'money', attr('previous_certified', money)),
            Field('amount_due', 'money', attr('amount_due', money), 'Applied for this time.'),
            Field('vat', 'money', attr('vat', money)),
            Field('total', 'money', attr('total', money), 'Including VAT.'),
            Field('notified_sum', 'money', attr('notified_sum', money),
                  'The sum due as the notices stand (excluding VAT).'),
            Field('certified', 'boolean', attr('is_certified')),
            Field('paid_total', 'money', attr('paid_total', money), 'Received, including VAT, less reversals.'),
            Field('paid_ex_vat', 'money', attr('paid_ex_vat', money)),
            Field('outstanding', 'money', attr('outstanding', money), 'Still to be paid, including VAT.'),
            Field('due_on', 'date', attr('due_on', iso), nullable=True),
            Field('payment_notice_by', 'date', attr('payment_notice_by', iso), nullable=True),
            Field('pay_less_by', 'date', attr('pay_less_by', iso), nullable=True),
            Field('final_date', 'date', attr('final_date', iso), 'Final date for payment.', nullable=True),
            Field('overdue', 'boolean', attr('is_overdue'), 'Past its final date for payment with money owed.'),
            Field('submitted_at', 'date-time', attr('submitted_at', iso), nullable=True),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            f_web(),
        ])


def _sub_payments():
    from subpayments.models import SubPayment
    return Resource(
        name='sub-payments', title='Subcontractor payments', scope=SCOPE_COMMERCIAL,
        description='Payments we have recorded to subcontractors, reversed ones included (reversed is true). '
                    'Never bank details, tax references or payment references.',
        queryset=lambda ctx: on_visible(SubPayment.objects.all(), 'application__project')
        .select_related('application__project', 'application__subcontract__package__subcontractor',
                        'application__subcontract__package__trade').order_by('pk'),
        filters=[project_filter('application__project'),
                 Filter('paid_since', 'date', 'Only payments made on or after this date.',
                        lambda qs, v, c: qs.filter(paid_on__gte=_date(v, 'paid_since')))],
        fields=[
            f_id(),
            Field('project_id', 'integer', attr('application.project_id')),
            Field('project_ref', 'string', lambda o, c: o.application.project.reference),
            Field('application_ref', 'string', attr('application.reference'), 'e.g. SA-002.'),
            Field('subcontractor', 'string', lambda o, c: o.application.subcontract.package.subcontractor.company_name),
            Field('trade', 'string', lambda o, c: str(o.application.subcontract.package.trade or ''), nullable=True),
            Field('paid_on', 'date', attr('paid_on', iso)),
            Field('gross', 'money', attr('gross', money), 'Gross paid, excluding VAT, before CIS.'),
            Field('materials', 'money', attr('materials', money)),
            Field('labour', 'money', attr('labour', money)),
            Field('cis_status', 'string', attr('cis_status')),
            Field('cis_rate', 'number', attr('cis_rate', str), 'CIS deduction rate, %.'),
            Field('cis_deduction', 'money', attr('cis_deduction', money)),
            Field('vat_rate', 'number', attr('vat_rate', str)),
            Field('reverse_charge', 'boolean', attr('reverse_charge'), 'Domestic reverse charge applied.'),
            Field('vat', 'money', attr('vat', money)),
            Field('net', 'money', attr('net', money), 'Net paid to the subcontractor.'),
            Field('reversed', 'boolean', lambda o, c: o.reversed_at is not None),
            Field('reversed_at', 'date-time', attr('reversed_at', iso), nullable=True),
        ])


def _date(value, name):
    try:
        d = parse_date(value)
    except ValueError:  # shaped like a date but not one: 2026-02-30
        d = None
    if d is None:
        raise BadParameter(f'{name} must be a date, e.g. 2026-11-01.')
    return d


def cvr_month(ctx):
    from cvr import rules
    raw = ctx.request.GET.get('month', '').strip()
    if not raw:
        return rules.month_start(ctx.today)
    try:
        y, m = raw.split('-')
        month = datetime(int(y), int(m), 1).date()
    except (ValueError, TypeError):
        raise BadParameter('month must be a year and month, e.g. 2026-10.')
    if month > rules.month_start(ctx.today):
        raise BadParameter('month can\'t be in the future.')
    if month < EARLIEST.date():
        # The CVR compares with the month before, and there is none before January of year 1
        raise BadParameter(f'month must be {EARLIEST:%Y-%m} or later.')
    return month


def _prepare_cvr(rows, ctx, detail):
    """month_figures_pair for this page of jobs: a submitted or signed-off CVR's frozen figures, else live."""
    from cvr import figures, rules
    from cvr.models import CVRPeriod
    month = cvr_month(ctx)
    prev = rules.add_months(month, -1)
    ids = [p.pk for p in rows]
    periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=ids, month=month)}
    prev_periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=ids, month=prev)}
    now, before = figures.month_figures_pair(rows, month, periods, prev, prev_periods)
    for p in rows:
        p._api_cvr = (month, periods.get(p.pk), now[p.pk], before.get(p.pk))


def _cvr_get(name, conv=money):
    def get(o, c):
        return conv(o._api_cvr[2].derived[name])
    return get


def _cvr_move(o, c):
    from cvr import rules
    _, _, f, prev = o._api_cvr
    return money(rules.movement(f.forecast_margin, prev.forecast_margin if prev and prev.has_activity else None))


def _cvr():
    from cvr.models import CVRPeriod
    return Resource(
        name='cvr', title='CVR summary', scope=SCOPE_COMMERCIAL,
        description='One row per won, live or complete job for a month (?month=2026-10, default this month): '
                    'the CVR\'s frozen figures once submitted or signed off, otherwise worked out live from '
                    'applications, the cost ledger and the contract, as the company CVR page does.',
        queryset=lambda ctx: cost_stage_projects(ctx).order_by('pk'), prepare=_prepare_cvr,
        filters=[Filter('month', 'string', 'The month, e.g. 2026-10. Defaults to this month.',
                        lambda qs, v, c: qs),
                 Filter('project', 'integer', "Only this job's row (a project id).",
                        lambda qs, v, c: _project_filter(qs, v, c, lookup=None))],
        example_id='4',
        fields=[
            Field('project_id', 'integer', attr('pk')),
            Field('project_ref', 'string', attr('reference')),
            Field('project_name', 'string', attr('name')),
            Field('month', 'string', lambda o, c: f'{o._api_cvr[0]:%Y-%m}'),
            Field('cvr_ref', 'string', lambda o, c: o._api_cvr[1].reference if o._api_cvr[1] else None,
                  nullable=True),
            Field('cvr_status', 'string', lambda o, c: o._api_cvr[1].status if o._api_cvr[1] else 'NONE',
                  'NONE when no CVR has been started for the month.',
                  enum=('NONE',) + choices(CVRPeriod, 'status')),
            Field('figures_fixed', 'boolean', lambda o, c: o._api_cvr[2].frozen,
                  'True once the CVR was submitted: the figures are the ones it was submitted with.'),
            Field('has_activity', 'boolean', lambda o, c: o._api_cvr[2].has_activity,
                  'Any application, cost or forecast by the month\'s end.'),
            Field('value', 'money', _cvr_get('value')),
            Field('cost', 'money', _cvr_get('cost')),
            Field('margin', 'money', _cvr_get('margin')),
            Field('margin_percent', 'number', _cvr_get('margin_pct', percent), nullable=True),
            Field('forecast_final_account', 'money', _cvr_get('final_account')),
            Field('forecast_final_cost', 'money', _cvr_get('final_cost')),
            Field('forecast_margin', 'money', _cvr_get('forecast_margin')),
            Field('forecast_margin_percent', 'number', _cvr_get('forecast_margin_pct', percent), nullable=True),
            Field('forecast_margin_movement', 'money', _cvr_move, 'Change since the month before.', nullable=True),
        ])


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def _riddor_due(obj, ctx):
    from safety import rules
    if not obj.riddor_category or obj.riddor_reported_on:
        return None
    return iso(rules.riddor_deadline(obj.riddor_category, timezone.localdate(obj.occurred_at)))


def _incidents():
    from safety import rules
    from safety.models import Incident
    return Resource(
        name='incidents', title='Incidents', scope=SCOPE_SAFETY,
        # No free text: the description (and the title, which is its first line) is written on site and often
        # names the people involved, so neither leaves Siteline through the API. The record's web link opens it
        # for someone allowed to read it on screen.
        description='Incidents and near misses on every job (owners and admins see these on screen). Never the '
                    'name, employer or injury of anyone hurt, and never the free-text title or description, which '
                    'can name people: follow web_url to read those. RIDDOR dates are advisory: check with your '
                    'safety adviser.',
        queryset=lambda ctx: on_visible(Incident.objects.all()).select_related('project').order_by('pk'),
        filters=[project_filter(), status_filter(Incident), updated_since()],
        fields=[
            f_id(), f_project(), f_project_ref(),
            Field('ref', 'string', attr('reference'), 'e.g. INC-002.'),
            Field('status', 'string', attr('status'), enum=choices(Incident, 'status')),
            Field('status_label', 'string', display('status')),
            Field('kind', 'string', attr('kind'), enum=choices(Incident, 'kind')),
            Field('kind_label', 'string', display('kind')),
            Field('location', 'string', attr('location')),
            Field('occurred_at', 'date-time', attr('occurred_at', iso)),
            Field('anyone_hurt', 'boolean', attr('anyone_hurt')),
            Field('first_aid_given', 'boolean', attr('first_aid_given')),
            Field('taken_to_hospital', 'boolean', attr('taken_to_hospital')),
            Field('days_off', 'integer', attr('days_off'), nullable=True),
            Field('riddor_category', 'string', attr('riddor_category'), enum=('',) + tuple(c for c, _ in
                                                                                        rules.RIDDOR_CHOICES if c)),
            Field('riddor_reportable', 'boolean', lambda o, c: rules.is_reportable(o.riddor_category)),
            Field('riddor_report_by', 'date', _riddor_due, 'Advisory: the last day to report to HSE.',
                  nullable=True),
            Field('riddor_reported_on', 'date', attr('riddor_reported_on', iso), nullable=True),
            Field('closed_at', 'date-time', attr('closed_at', iso), nullable=True),
            Field('updated_at', 'date-time', attr('updated_at', iso)),
            f_web(),
        ])


def build():
    return [_projects(), _rfis(), _variations(), _change_events(), _documents(), _snags(), _cost_reports(),
            _applications(), _sub_payments(), _cvr(), _incidents()]


_RESOURCES = None


def resources():
    global _RESOURCES
    if _RESOURCES is None:
        _RESOURCES = build()
    return _RESOURCES


def by_name(name):
    for r in resources():
        if r.name == name:
            return r
    raise KeyError(name)
