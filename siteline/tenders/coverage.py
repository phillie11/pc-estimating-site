"""
Trade coverage on a tender: per trade, how many subcontractors were invited, opened it,
quoted, declined or are still outstanding, against the number of quotes the company wants.

Internal only. Nothing here may reach a tendering subcontractor: they must never learn who
else is pricing or how many are.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, time

from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

import jobs
from directory.models import Package, Subcontractor
from directory.regions import covers, guess_regions
from projects.models import Project

from .models import TenderSettings

# Bids still collecting subcontract quotes
OPEN_TENDER_STAGES = (Project.STAGE_LEAD, Project.STAGE_TENDERING)
ALERT_DAYS = 3  # "quotes due in 3 days with thin coverage"


@dataclass
class TradeRow:
    trade: object
    packages: list = field(default_factory=list)
    invited: int = 0
    viewed: int = 0
    quoted: int = 0
    declined: int = 0
    outstanding: int = 0
    appointed: bool = False
    target: int = 3
    due: object = None
    today: object = None

    @property
    def thin(self):
        return not self.appointed and self.quoted < self.target

    @property
    def short_by(self):
        return max(self.target - self.quoted, 0)

    @property
    def days_to_due(self):
        return (self.due - self.today).days if self.due and self.today else None

    @property
    def status_label(self):
        if self.appointed:
            return 'Appointed'
        if not self.thin:
            return 'Covered'
        return f'Thin: {self.quoted} of {self.target}'

    @property
    def status_tone(self):
        return 'good' if not self.thin else 'bad'

    @property
    def due_label(self):
        return due_label(self.days_to_due)

    @property
    def trade_name(self):
        return self.trade.name if self.trade else 'No trade'


def due_label(days):
    if days is None:
        return 'No quote date'
    if days < 0:
        return f'{-days} day{"s" if days != -1 else ""} overdue'
    if days == 0:
        return 'Quotes due today'
    return f'{days} day{"s" if days != 1 else ""} to quotes due'


def quotes_due(project, packages=()):
    """The project's subcontractor quotes due date, else the earliest date on an open invitation."""
    if project.quotes_due:
        return project.quotes_due
    dates = [p.quote_due for p in packages if p.quote_due and p.is_open]
    return min(dates) if dates else None


def trade_rows(project, target=None, packages=None, today=None):
    """One row per trade with invitations on this tender, thinnest and soonest first."""
    today = today or timezone.localdate()
    if target is None:
        target = TenderSettings.for_org(project.organization).min_quotes
    if packages is None:
        packages = list(Package.objects.filter(project=project).select_related('subcontractor', 'trade'))
    rows = {}
    for p in packages:
        row = rows.setdefault(p.trade_id, TradeRow(trade=p.trade, target=target, today=today))
        row.packages.append(p)
        row.invited += 1
        row.viewed += bool(p.viewed_at)
        if p.status == Package.DECLINED:
            row.declined += 1
        elif p.submitted_at or p.status in (Package.QUOTED, Package.APPOINTED):
            row.quoted += 1
        elif p.status == Package.ENQUIRY:
            row.outstanding += 1
        row.appointed = row.appointed or p.status == Package.APPOINTED
    for row in rows.values():
        row.due = quotes_due(project, row.packages)
        row.packages.sort(key=lambda p: (p.status != Package.ENQUIRY, p.subcontractor.company_name))
    return sorted(rows.values(), key=lambda r: (not r.thin, r.short_by * -1, r.trade_name))


def outstanding_packages(project):
    """Invitations still waiting on a quote."""
    return Package.objects.filter(project=project, status=Package.ENQUIRY, submitted_at__isnull=True)


# ---------------------------------------------------------------------------
# Bulk chase
# ---------------------------------------------------------------------------

def queue_chase(request, project):
    """
    Queue one reminder per subcontractor still to quote on this tender. Returns how many
    companies will be chased (0 if everyone outstanding was chased today already).
    """
    today = timezone.localdate()
    start = timezone.make_aware(datetime.combine(today, time.min))
    due = outstanding_packages(project).filter(Q(chased_at__isnull=True) | Q(chased_at__lt=start))
    rows = sorted(due.values_list('pk', 'chased_at', 'subcontractor_id'))
    if not rows:
        return 0
    ids = [pk for pk, _, _ in rows]
    companies = len({sub for _, _, sub in rows})
    # The same click twice (or two people at once) is one job; a new invitation, or a package
    # chased since, makes a new one
    state = ','.join(f'{pk}@{chased.isoformat() if chased else "-"}' for pk, chased, _ in rows)
    digest = hashlib.sha256(state.encode()).hexdigest()[:16]
    jobs.enqueue('tenders.chase_outstanding', {'project_id': project.pk, 'package_ids': ids,
                                               'actor_id': request.user.pk, 'day': today.isoformat()},
                 org=project.organization, idempotency_key=f'tenders.chase:{project.pk}:{today}:{digest}')
    return companies


def run_chase(project, package_ids, actor=None, today=None):
    """
    The job's work (tenders/tasks.py). Each package is claimed with a conditional update, so a
    package is chased at most once a day however often the job runs. Then each company gets one
    notification covering every trade it still owes a quote for. Returns the companies told.
    """
    from tenancy import notify

    from .services import sub_members, tender_url
    today = today or timezone.localdate()
    now = timezone.now()
    start = timezone.make_aware(datetime.combine(today, time.min))
    claimed = []
    for pkg in outstanding_packages(project).filter(pk__in=package_ids).select_related('subcontractor', 'trade'):
        n = Package.objects.filter(pk=pkg.pk, status=Package.ENQUIRY, submitted_at__isnull=True) \
            .filter(Q(chased_at__isnull=True) | Q(chased_at__lt=start)) \
            .update(chased_at=now, chase_count=F('chase_count') + 1)
        if n:
            claimed.append(pkg)
    by_sub = {}
    for pkg in claimed:
        by_sub.setdefault(pkg.subcontractor_id, []).append(pkg)
    due = project.quotes_due
    url = tender_url(project)
    told = 0
    for sub_id, pkgs in by_sub.items():
        trades = ', '.join(sorted({str(p.trade or 'the package') for p in pkgs}))
        dates = [p.quote_due for p in pkgs if p.quote_due]
        when = min(dates) if dates else due
        title = (f'Reminder: your quote for {trades} on {project.name} is due {when:%a} {when.day} {when:%b}'
                 if when else f'Reminder: your quote for {trades} on {project.name} is awaited')
        users = sub_members(project, [sub_id])
        if users:
            notify.alert(users, title, url, project=project, actor=actor, org=project.organization)
            told += 1
            continue
        # Not signed up yet: send the pending portal invitation's link again
        from projects.models import ProjectInvite
        inv = ProjectInvite.objects.filter(project=project, subcontractor_id=sub_id,
                                           status=ProjectInvite.STATUS_PENDING).first()
        if inv is not None:
            notify.send(inv.email, title, 'tender_chase', {
                'project': project, 'trades': trades, 'due': when, 'actor': actor,
                'link': notify.absolute(reverse('accept_project_invite', args=[inv.token])),
            }, org=project.organization)
            told += 1
    return told


# ---------------------------------------------------------------------------
# Suggest more subcontractors
# ---------------------------------------------------------------------------

INSURANCE_RANK = {'OK': 0, 'EXPIRING': 1, 'UNKNOWN': 2, 'EXPIRED': 3}
REGION_RANK = {'yes': 0, 'unknown': 1, 'no': 2}


def suggest(project, trade, limit=6):
    """
    Approved subcontractors in the directory for this trade who weren't invited to price it here,
    insured first, then those whose regions seem to cover the site, then by rating. Region
    matching reads the site address loosely (directory.regions.guess_regions).
    """
    if trade is None:
        return []
    invited = Package.objects.filter(project=project, trade=trade).values_list('subcontractor_id', flat=True)
    subs = list(Subcontractor.objects.filter(status=Subcontractor.STATUS_APPROVED, archived_at__isnull=True,
                                             trades=trade).exclude(email='').exclude(pk__in=invited).distinct())
    wanted = guess_regions(project.site_address)
    for s in subs:
        s.region_fit = covers(s.region_codes, wanted)
    subs.sort(key=lambda s: (INSURANCE_RANK[s.insurance_status], REGION_RANK[s.region_fit], -(s.rating or 0),
                             s.company_name))
    return subs[:limit]


# ---------------------------------------------------------------------------
# Company-wide, and the alerts other parts of the product pick up
# ---------------------------------------------------------------------------

def open_tenders(projects, today=None):
    """[(project, rows)] for every open tender with invitations, soonest quotes due first."""
    today = today or timezone.localdate()
    projects = list(projects.filter(stage__in=OPEN_TENDER_STAGES, archived_at__isnull=True))
    if not projects:
        return []
    target = TenderSettings.for_org(projects[0].organization).min_quotes
    packages = {}
    for p in Package.objects.filter(project__in=[p.pk for p in projects]).select_related('subcontractor', 'trade'):
        packages.setdefault(p.project_id, []).append(p)
    out = []
    for project in projects:
        if project.pk not in packages:
            continue
        rows = trade_rows(project, target=target, packages=packages[project.pk], today=today)
        project.cover_due = quotes_due(project, packages[project.pk])
        project.cover_days = (project.cover_due - today).days if project.cover_due else None
        project.cover_due_label = due_label(project.cover_days)
        project.thin_rows = [r for r in rows if r.thin]
        out.append((project, rows))
    out.sort(key=lambda pr: (pr[0].cover_days is None, pr[0].cover_days if pr[0].cover_days is not None else 0,
                             pr[0].name))
    return out


def alerts(projects, today=None, days=ALERT_DAYS):
    """
    Open tenders whose subcontract quotes are due within `days` (today included) and that have
    at least one thin trade, as plain dicts for the Needs-me list and the daily digest.
    """
    today = today or timezone.localdate()
    out = []
    for project, rows in open_tenders(projects, today=today):
        thin = [r for r in rows if r.thin]
        if not thin or project.cover_days is None or not 0 <= project.cover_days <= days:
            continue
        names = ', '.join(f'{r.trade_name} ({r.quoted} of {r.target})' for r in thin)
        out.append({
            'kind': 'tenders.thin_coverage',
            'project': project,
            'title': f'Thin quote cover on {project.name}',
            'detail': f'{project.cover_due_label}. Short on {names}.',
            'url': reverse('tender', args=[project.organization.slug, project.pk]) + '#coverage',
            'due': project.cover_due,
            'tone': 'bad' if project.cover_days <= 1 else 'warn',
            'owner': project.owner,
            'trades': [r.trade_name for r in thin],
        })
    return out
