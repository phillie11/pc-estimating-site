"""
The figures behind each company report. Every money figure is read through the function its own
module uses on screen, never worked out again here, so a report always agrees with the job's own
page (reports/tests/test_reconcile.py holds them to it):

  portfolio_margin   cvr.figures.month_figures_pair, as the company CVR: a submitted or signed-off
                     CVR's fixed figures, otherwise live ones from payments, the cost ledger and the
                     contract. Live jobs (won, or live on site), not archived.
  cash_position      cvr.cashflow.build for each live job, this month and the next two, as the job's
                     Cashflow page shows them.
  overdue            open RFIs past their response date; NEC change event clocks that have run out
                     (changes.clocks, advisory); our payment notices to subcontractors past their date
                     and client payments past their final date (payments and subpayments, advisory);
                     approved permits to work past their expiry (quality); open defects past their date.
  safety             incidents by the month they happened and by kind; how many are RIDDOR reportable
                     (safety.rules.is_reportable, advisory).
  subcontractors     per subcontractor with an appointed package: submittals made by their due date,
                     snags and defects per package, and the average days to close one.

Each function takes `today` (and `now` where times matter) so tests can fix the date.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Min

ZERO = Decimal('0.00')
CASH_MONTHS = 3
SAFETY_MONTHS = 12
SECONDS_A_DAY = 86400
LIST_LIMIT = 50  # rows shown per overdue section on screen; the CSV has them all


def live_jobs():
    """Won and live jobs that aren't archived: the jobs the margin, cash and overdue reports cover."""
    from projects.models import Project
    return Project.objects.filter(archived_at__isnull=True, stage__in=Project.DELIVERY_STAGES) \
        .select_related('manager').order_by('name', 'pk')


# ---------------------------------------------------------------------------
# Portfolio margin
# ---------------------------------------------------------------------------

def portfolio_margin(month, projects=None):
    """
    {'rows': [...], 'total': {...}, 'month': month} for each live job: its CVR forecast margin for the
    month and how far it has moved since the month before (None with no figures last month).
    """
    from cvr import figures, rules
    from cvr.models import CVRPeriod
    projects = list(live_jobs() if projects is None else projects)
    ids = [p.pk for p in projects]
    prev_month = rules.add_months(month, -1)
    periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=ids, month=month)}
    prev_periods = {p.project_id: p for p in CVRPeriod.objects.filter(project__in=ids, month=prev_month)}
    now, before = figures.month_figures_pair(projects, month, periods, prev_month, prev_periods)
    rows = []
    for p in projects:
        f = now[p.pk]
        prev = before.get(p.pk)
        moved = rules.movement(f.forecast_margin, prev.forecast_margin if prev and prev.has_activity else None)
        rows.append({'project': p, 'period': periods.get(p.pk), 'f': f, 'move': moved,
                     'has_figures': f.has_activity or p.pk in periods})
    keys = ('value', 'cost', 'margin', 'final_account', 'final_cost', 'forecast_margin')
    # A job with no application, cost, forecast or CVR yet would add its whole contract sum as margin: left out
    counted = [r for r in rows if r['has_figures']]
    total = {k: sum((r['f'].derived[k] for r in counted), ZERO) for k in keys}
    total['margin_pct'] = rules.percent_of(total['margin'], total['value'])
    total['forecast_margin_pct'] = rules.percent_of(total['forecast_margin'], total['final_account'])
    moves = [r['move'] for r in counted if r['move'] is not None]
    total['move'] = sum(moves, ZERO) if moves else None
    return {'rows': rows, 'total': total, 'month': month, 'counted': len(counted)}


# ---------------------------------------------------------------------------
# Cash position
# ---------------------------------------------------------------------------

def cash_position(today, projects=None):
    """
    Money in, out and net for this month and the next two on every live job, each straight from
    cvr.cashflow.build(project, today), with the totals across jobs. Excluding VAT.
    """
    from cvr import cashflow, rules
    projects = list(live_jobs() if projects is None else projects)
    first = rules.month_start(today)
    months = [rules.add_months(first, i) for i in range(CASH_MONTHS)]
    rows = []
    totals = [{'month': m, 'in': ZERO, 'out': ZERO, 'net': ZERO} for m in months]
    for p in projects:
        data = cashflow.build(p, today)
        by_month = {m.month: m for m in data['months']}
        cells = []
        for i, m in enumerate(months):
            row = by_month.get(m)
            money_in = row.money_in if row else ZERO
            money_out = row.money_out if row else ZERO
            cells.append({'month': m, 'in': money_in, 'out': money_out, 'net': money_in - money_out})
            totals[i]['in'] += money_in
            totals[i]['out'] += money_out
            totals[i]['net'] += money_in - money_out
        net = sum((c['net'] for c in cells), ZERO)
        rows.append({'project': p, 'cells': cells, 'net': net,
                     'has_money': any(c['in'] or c['out'] for c in cells)})
    grand = {'in': sum((t['in'] for t in totals), ZERO), 'out': sum((t['out'] for t in totals), ZERO)}
    grand['net'] = grand['in'] - grand['out']
    return {'months': months, 'rows': rows, 'totals': totals, 'grand': grand}


# ---------------------------------------------------------------------------
# Overdue across jobs
# ---------------------------------------------------------------------------

@dataclass
class Late:
    kind: str
    ref: str
    title: str
    project: object
    due: object          # date, or None for a prompt with no fixed date
    days_late: int
    url: str
    detail: str = ''


@dataclass
class Section:
    key: str
    title: str
    what: str            # one line: what counts
    items: list = field(default_factory=list)
    advisory: bool = False

    @property
    def count(self):
        return len(self.items)

    @property
    def shown(self):
        return self.items[:LIST_LIMIT]

    @property
    def more(self):
        return max(0, self.count - LIST_LIMIT)


def _late(day, today):
    return (today - day).days


def overdue(today, now):
    from changes.clocks import TONE_LATE, clocks_for
    from changes.models import ChangeEvent
    from contracts.models import MainContract
    from payments.models import Application
    from payments.summary import prime_paid_totals
    from projects.models import Issue
    from quality import rules as q_rules
    from quality.models import Permit
    from rfis.models import RFI
    from subpayments.models import SubApplication

    jobs = live_jobs()
    sections = []

    rfis = Section('rfis', 'RFIs waiting for a response', 'Open RFIs past their "response required by" date.')
    for r in RFI.objects.filter(project__in=jobs, status=RFI.STATUS_OPEN, due_date__lt=today) \
            .select_related('project').order_by('due_date', 'pk'):
        rfis.items.append(Late('RFI', r.reference, r.title, r.project, r.due_date, _late(r.due_date, today),
                               r.get_absolute_url()))
    sections.append(rfis)

    clocks = Section('clocks', 'NEC clocks run out', 'Compensation event clocks on NEC jobs whose deadline has '
                     'passed with nothing done: notify, quote, or chase the reply.', advisory=True)
    nec = {c.project_id: c for c in MainContract.objects.filter(project__in=jobs) if c.is_nec}
    for ce in ChangeEvent.objects.filter(project_id__in=list(nec), status__in=ChangeEvent.LIVE_STATUSES) \
            .select_related('project').order_by('pk'):
        for c in clocks_for(ce, nec[ce.project_id], today):
            if c.tone == TONE_LATE and c.done_on is None and c.deadline is not None:
                clocks.items.append(Late('Change event', ce.reference, ce.title, ce.project, c.deadline,
                                         _late(c.deadline, today), ce.get_absolute_url(), f'{c.label} ({c.clause})'))
    clocks.items.sort(key=lambda i: (i.due, i.ref))
    sections.append(clocks)

    notices = Section('notices', 'Payment notices we owe subcontractors',
                      'Subcontractor applications with no payment notice from us by its date.', advisory=True)
    for sa in SubApplication.objects.filter(project__in=jobs, status=SubApplication.RECEIVED,
                                            payment_notice_on__isnull=True, payment_notice_by__lt=today) \
            .select_related('project', 'subcontract__package__subcontractor').order_by('payment_notice_by', 'pk'):
        notices.items.append(Late('Sub application', sa.reference, sa.subcontract.package.subcontractor.company_name,
                                  sa.project, sa.payment_notice_by, _late(sa.payment_notice_by, today),
                                  sa.get_absolute_url(), sa.title))
    sections.append(notices)

    paid = Section('payments', 'Client payments overdue',
                   'Our applications past their final date for payment with money still owed.', advisory=True)
    apps = prime_paid_totals(list(Application.objects.filter(project__in=jobs, status__in=Application.AWAITING_PAYMENT,
                                                             final_date__lt=today).select_related('project')
                                  .order_by('final_date', 'pk')))
    for a in apps:
        if a.outstanding > 0:
            paid.items.append(Late('Application', a.reference, a.title, a.project, a.final_date,
                                   _late(a.final_date, today), a.get_absolute_url()))
    sections.append(paid)

    permits = Section('permits', 'Permits to work past their expiry',
                      'Approved permits whose time has run out and that nobody has closed.')
    for pt in Permit.objects.filter(project__in=jobs, status=q_rules.APPROVED, expires_at__lte=now) \
            .select_related('project').order_by('expires_at', 'pk'):
        day = pt.expires_at.date()
        permits.items.append(Late('Permit', pt.reference, pt.title, pt.project, day, max(_late(day, today), 0),
                                  pt.get_absolute_url(), pt.get_permit_type_display()))
    sections.append(permits)

    defects = Section('defects', 'Defects past their date', 'Open defects (raised after practical completion) '
                      'past the date they were due to be put right.')
    for d in Issue.objects.filter(project__in=jobs, issue_type='DEFECT', due_date__lt=today) \
            .exclude(status=Issue.STATUS_CLOSED).select_related('project').order_by('due_date', 'pk'):
        defects.items.append(Late('Defect', d.reference, d.title, d.project, d.due_date, _late(d.due_date, today),
                                  _issue_url(d)))
    sections.append(defects)
    return sections


def _issue_url(issue):
    from django.urls import reverse
    return reverse('issue_detail', args=[issue.organization.slug, issue.project_id, issue.pk])


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def safety(today, months=SAFETY_MONTHS):
    """
    Incidents in each of the last `months` months (this one included), by kind, and the RIDDOR
    reportable ones. Every job counts, archived ones too: it is the company's safety record.
    """
    from django.utils import timezone

    from cvr import rules as month_rules
    from safety import rules
    from safety.models import Incident
    first = month_rules.add_months(month_rules.month_start(today), -(months - 1))
    month_list = [month_rules.add_months(first, i) for i in range(months)]
    kinds = list(Incident.KIND_CHOICES)
    counts = {m: {k: 0 for k, _ in kinds} for m in month_list}
    riddor = {m: 0 for m in month_list}
    start = timezone.make_aware(datetime(first.year, first.month, 1))
    for kind, occurred_at, category in Incident.objects.filter(occurred_at__gte=start) \
            .values_list('kind', 'occurred_at', 'riddor_category'):
        m = month_rules.month_start(timezone.localdate(occurred_at))
        if m not in counts:
            continue  # later than today (a mistyped date)
        counts[m][kind] = counts[m].get(kind, 0) + 1
        if rules.is_reportable(category):
            riddor[m] += 1
    rows = [{'month': m, 'by_kind': [counts[m][k] for k, _ in kinds], 'total': sum(counts[m].values()),
             'riddor': riddor[m]} for m in month_list]
    kind_totals = [sum(counts[m][k] for m in month_list) for k, _ in kinds]
    return {'rows': rows, 'kinds': kinds, 'kind_totals': kind_totals, 'total': sum(r['total'] for r in rows),
            'riddor': sum(riddor.values()), 'first': first}


# ---------------------------------------------------------------------------
# Subcontractor performance
# ---------------------------------------------------------------------------

def _one_dp(value):
    return Decimal(value).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)


def subcontractors(today):
    """
    One row per subcontractor appointed on any job that isn't archived:

      packages          appointed packages
      submittals_due    submittals (not withdrawn) that were made, or whose due date has passed
      on_time           of those, first made on or before the due date (none set: counted as on time once made)
      snags             snags and defects on those jobs given to the subcontractor's people
      per_package       snags / packages, to 0.1
      closed, avg_days  snags closed, and the average days from raised to closed, to 0.1
    """
    from django.utils import timezone

    from directory.models import Package
    from procurement.models import Submittal
    from projects.models import Issue, ProjectMember

    pkgs = list(Package.objects.filter(status=Package.APPOINTED, project__archived_at__isnull=True)
                .select_related('subcontractor').values_list('pk', 'project_id', 'subcontractor_id',
                                                             'subcontractor__company_name'))
    if not pkgs:
        return []
    by_sub = {}
    for pk, project_id, sub_id, name in pkgs:
        row = by_sub.setdefault(sub_id, {'subcontractor_id': sub_id, 'name': name, 'packages': 0, 'jobs': set(),
                                         'submittals_due': 0, 'on_time': 0, 'snags': 0, 'closed': 0,
                                         '_days': []})
        row['packages'] += 1
        row['jobs'].add(project_id)
    pkg_sub = {pk: sub_id for pk, _, sub_id, _ in pkgs}

    for package_id, due, first in Submittal.objects.filter(package_id__in=list(pkg_sub)) \
            .exclude(status=Submittal.WITHDRAWN).annotate(first=Min('revisions__submitted_at')) \
            .values_list('package_id', 'due_date', 'first'):
        row = by_sub[pkg_sub[package_id]]
        made_on = timezone.localdate(first) if first else None
        if made_on is None and (due is None or due >= today):
            continue  # not made yet and not yet due
        row['submittals_due'] += 1
        if made_on is not None and (due is None or made_on <= due):
            row['on_time'] += 1

    # Their people on each job they're appointed on: snags given to those people on that job are theirs
    people = defaultdict(set)  # (project_id, user_id) -> {sub_id}
    for project_id, user_id, sub_id in ProjectMember.objects.filter(
            role=ProjectMember.ROLE_SUBCONTRACTOR, subcontractor_id__in=list(by_sub)) \
            .values_list('project_id', 'user_id', 'subcontractor_id'):
        if project_id in by_sub[sub_id]['jobs']:
            people[(project_id, user_id)].add(sub_id)
    if people:
        projects = {p for p, _ in people}
        users = {u for _, u in people}
        for project_id, user_id, status, created, closed in Issue.objects.filter(
                project_id__in=projects, assignee_id__in=users, issue_type__in=('SNAG', 'DEFECT')) \
                .values_list('project_id', 'assignee_id', 'status', 'created_at', 'closed_at'):
            for sub_id in people.get((project_id, user_id), ()):
                row = by_sub[sub_id]
                row['snags'] += 1
                if status == Issue.STATUS_CLOSED and closed:
                    row['closed'] += 1
                    row['_days'].append(max(int((closed - created).total_seconds()), 0))  # whole seconds

    out = []
    for row in by_sub.values():
        days = row.pop('_days')
        row['jobs'] = len(row['jobs'])
        row['on_time_pct'] = _one_dp(Decimal(row['on_time']) * 100 / row['submittals_due']) \
            if row['submittals_due'] else None
        row['per_package'] = _one_dp(Decimal(row['snags']) / row['packages'])
        row['avg_days'] = _one_dp(Decimal(sum(days)) / (len(days) * SECONDS_A_DAY)) if days else None
        out.append(row)
    out.sort(key=lambda r: (r['name'].lower(), r['subcontractor_id']))
    return out
