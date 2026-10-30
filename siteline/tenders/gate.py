"""
The bid/no-bid gate, and the other stage moves estimating must explain.

Every route that changes a project's stage (pipeline drag, a card's "Move to", the
project page's stage select, the edit form and creating a project) asks
`intercept(project, old, new)` first, the same way a win goes through the handover:

- into Tendering or Submitted from Lead (or reopening a lost or declined bid) without a
  bid/no-bid decision on file: the gate, mandatory at or above the organisation's value
  threshold and skippable below it;
- into Declined: a decline reason from the taxonomy;
- into Lost: the loss review.

The page that answers it then makes the move itself with `apply_stage()`.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from projects.models import Project
from tenancy.models import log_activity

from . import scoring
from .models import BidReview, TenderSettings

BIDDING = (Project.STAGE_TENDERING, Project.STAGE_SUBMITTED)
# Estimating stages that aren't actively bidding: leaving them for a bidding stage needs a decision
NOT_BIDDING = (Project.STAGE_LEAD, Project.STAGE_LOST, Project.STAGE_DECLINED)


def gate_url(project, to):
    return f"{reverse('bid_gate', args=[project.organization.slug, project.pk])}?to={to}"


def decline_url(project):
    return reverse('bid_decline', args=[project.organization.slug, project.pk])


def lost_url(project):
    return reverse('bid_lost', args=[project.organization.slug, project.pk])


def review_for(project):
    return BidReview.objects.filter(project=project).first()


def intercept(project, old, new):
    """
    Where a move from `old` to `new` must go first, or None if it can go straight through.
    `project` must be saved; `old` is its stage before the move.
    """
    if old == new:
        return None
    if new == Project.STAGE_DECLINED:
        return decline_url(project)
    if new == Project.STAGE_LOST:
        return lost_url(project)
    if new in BIDDING and old in NOT_BIDDING:
        review = review_for(project)
        if review is None or not review.passed_gate or old != Project.STAGE_LEAD:
            # Reopening a lost or declined bid asks again: whatever was decided then has changed
            return gate_url(project, new)
    return None


def needs_intercept_on_create(stage):
    """A new project created straight into a stage that needs a decision starts as a Lead and goes there."""
    return stage in BIDDING + (Project.STAGE_DECLINED, Project.STAGE_LOST)


def gate_value(project, review=None):
    """The value the threshold is tested against: the tender value, else the client's budget."""
    if project.value is not None:
        return project.value
    return review.client_budget if review else None


def gate_required(project, review=None, settings=None):
    settings = settings or TenderSettings.for_org(project.organization)
    value = gate_value(project, review)
    return value is not None and value >= settings.gate_threshold


def apply_stage(request, project, stage):
    """Make the move once its page is answered: log it, and close the sub tenders on a loss or decline."""
    if project.stage == stage:
        return
    project.stage = stage
    project.save()
    log_activity(request, f'moved to {project.get_stage_display()}', project.name, project=project,
                 url=project.get_absolute_url())
    if stage in (Project.STAGE_LOST, Project.STAGE_DECLINED):
        from .services import close_all
        close_all(request, project)


# ---------------------------------------------------------------------------
# Suggested answers, from what is already on file
# ---------------------------------------------------------------------------

def tender_window(project):
    """The bar this bid has on the tender workload Gantt (projects.views.workload), or a guess at it."""
    start = project.tender_start or timezone.localdate()
    finish = project.tender_return_date or start + timedelta(days=28)
    return start, max(start, finish)


def overlapping_bids(project):
    """
    The estimator's other open bids whose bars on the tender workload Gantt overlap this one.
    Uses the same dates as the chart (enquiry received to tender return), so the count matches
    what they would see filtering the Workload page to that estimator. Bids with no return date
    have no bar there and aren't counted.
    """
    if not project.owner_id:
        return None
    start, finish = tender_window(project)
    others = Project.objects.filter(owner_id=project.owner_id, stage__in=Project.PIPELINE_STAGES,
                                    archived_at__isnull=True, tender_due__isnull=False).exclude(pk=project.pk)
    count = 0
    for p in others:
        s, f = p.tender_start, p.tender_return_date
        if s and f and s <= finish and max(s, f) >= start:
            count += 1
    return count


def _decided(project):
    return Project.objects.filter(stage__in=Project.WON_STAGES + (Project.STAGE_LOST,),
                                  archived_at__isnull=True).exclude(pk=project.pk)


def suggest_client(project):
    name = (project.client_name or '').strip()
    if not name:
        return None
    past = Project.objects.filter(client_name__iexact=name, archived_at__isnull=True).exclude(pk=project.pk)
    if past.filter(stage__in=Project.WON_STAGES).exists():
        return 4
    if past.exists():
        return 3
    return 2


def suggest_sector(project):
    if not project.sector:
        return None
    since = timezone.localdate() - timedelta(days=730)
    sector = _decided(project).filter(sector=project.sector)
    if sector.filter(stage__in=Project.WON_STAGES, decided_at__gte=since).exists():
        return 4
    return 2 if sector.exists() else 0


def suggest_value(project):
    """In a value band we have won in before (4), next to one (2), or not (0). Needs three wins to go on."""
    from projects.analytics import VALUE_BANDS, value_band
    if project.value is None:
        return None
    won = [p.value for p in _decided(project).filter(stage__in=Project.WON_STAGES, value__isnull=False)]
    if len(won) < 3:
        return None
    order = [label for _, label in VALUE_BANDS]
    mine = order.index(value_band(project.value))
    bands = {order.index(value_band(v)) for v in won}
    if mine in bands:
        return 4
    return 2 if (mine - 1 in bands or mine + 1 in bands) else 0


def suggestions(project, review=None):
    """{question key: points} for the answers the records can suggest, and the workload count."""
    other = overlapping_bids(project)
    out = {
        'client': suggest_client(project),
        'sector': suggest_sector(project),
        'value': suggest_value(project),
        'programme': None,
        'workload': scoring.workload_points(other) if other is not None else None,
        'terms': scoring.terms_points(review.contract_form, review.amendments, review.bidders) if review else None,
    }
    return {k: v for k, v in out.items() if v is not None}, other
