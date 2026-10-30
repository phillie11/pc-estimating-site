"""
Bid discipline pages: the bid/no-bid gate, declining to bid, the loss review, estimating
settings and the company-wide coverage board. Staff who work in estimating only.
"""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from projects.access import get_project, visible_projects
from projects.models import Project
from projects.views import use_section
from tenancy.models import log_activity
from tenancy.permissions import admin_required, estimating_required

from . import coverage, gate, scoring
from .forms import BidGateForm, DeclineBidForm, LossReviewForm, TenderSettingsForm
from .models import BidReview, TenderSettings


def _review(project):
    return gate.review_for(project) or BidReview(project=project)


def _plan_blocks(request, project, to):
    """Reopening a lost or declined bid takes a plan slot again."""
    if project.stage in Project.CLOSED_STAGES and to not in Project.CLOSED_STAGES and not Project.plan_has_room(request.org):
        messages.error(request, f'Your {request.org.plan_config["name"]} plan allows '
                                f'{request.org.plan_config["max_active_projects"]} active projects. '
                                'Close some or upgrade before reopening this bid.')
        return True
    return False


def _not_a_bid(request, project, stage, verb):
    """Only an open bid can be declined or lost; one already there can have its reason edited."""
    if project.stage in Project.PIPELINE_STAGES or project.stage == stage:
        return None
    messages.error(request, f'Only an open bid (lead, tendering or submitted) can be {verb}. '
                            f'{project.name} is {project.get_stage_display().lower()}.')
    return redirect(project)


@estimating_required
def bid_gate(request, org_slug, pk):
    """
    The six bid/no-bid questions, a score and Go / Review / No-go. Reached on the way from
    Lead to Tendering (or Submitted), or from the tender page to look at or re-score a bid.
    """
    project = get_project(request, pk)
    if project.section != 'estimating':
        messages.info(request, 'The bid/no-bid check is for bids. This job is with the projects team.')
        return redirect(project)
    to = request.POST.get('to') or request.GET.get('to')
    # Into Tendering or Submitted from a lead or a closed bid, or Tendering on to Submitted (re-gated)
    moving = to in gate.BIDDING and to != project.stage and (
        project.stage in gate.NOT_BIDDING or project.stage == Project.STAGE_TENDERING)
    if not moving:
        to = None
    review = _review(project)
    settings = TenderSettings.for_org(request.org)
    suggested, other_bids = gate.suggestions(project, review)
    action = request.POST.get('action') if request.method == 'POST' else None
    if request.method == 'POST' and request.POST.get('from') != project.stage:
        # The page was opened when the bid was at another stage (another tab, or someone else moved it)
        messages.error(request, f'{project.name} is now at {project.get_stage_display()}, so nothing was saved. '
                                'Check the questions again below.')
        return redirect(request.path + (f'?to={to}' if moving else ''))
    previous = review.decision
    # Answering for a decision to bid (moving, or replacing a skipped check) obeys the No-go rule, whatever the button
    deciding = action == 'go' or (action == 'save' and (moving or previous in ('', BidReview.SKIPPED)))
    form = BidGateForm(request.POST if request.method == 'POST' else None, instance=review,
                       scoring_required=action in ('go', 'save'), suggested=suggested)

    if request.method == 'POST' and form.is_valid():
        review = form.instance  # holds the posted intake, so the threshold uses the client budget given now
        required = gate.gate_required(project, review, settings)
        if action == 'skip' and (required or not moving):
            messages.error(request, 'The bid/no-bid check is required for this bid. Answer the questions.')
        elif deciding and scoring.result(scoring.score(_answers(form))) == scoring.NO_GO \
                and not form.cleaned_data['decision_note'].strip():
            form.add_error('decision_note', 'The score says No-go. Say why we are bidding anyway, or decline it.')
        elif action in ('go', 'skip', 'save') and not (moving and _plan_blocks(request, project, to)):
            review = form.save(commit=False)
            if action == 'skip':
                review.decision = BidReview.SKIPPED
            elif deciding:
                review.decision = BidReview.PROCEED
            if moving:
                # Reopening a declined bid: it is being bid now, so the old decline no longer applies
                review.decline_reason, review.decline_detail = '', ''
            if action != 'skip':
                review.assessed_by, review.assessed_at, review.workload_bids = request.user, timezone.now(), other_bids
            review.save()
            if review.score is not None:
                log_activity(request, 'scored bid/no-bid', f'{project.name}: {review.score} ({review.get_result_display()})',
                             project=project, url=project.get_absolute_url())
            if moving:
                gate.apply_stage(request, project, to)
                verdict = f'Scored {review.score}: {review.get_result_display()}. ' if review.score is not None else ''
                messages.success(request, f'{verdict}{project.name} moved to {project.get_stage_display()}.')
                return redirect('tender', org_slug=org_slug, pk=pk)
            messages.success(request, 'Bid/no-bid saved.')
            return redirect('bid_gate', org_slug=org_slug, pk=pk)

    required = gate.gate_required(project, form.instance if form.is_bound else review, settings)
    live = scoring.score(_answers(form)) if form.is_bound and form.is_valid() else review.score
    return render(request, 'tenders/bid_gate.html', {
        'project': project, 'form': form, 'review': review, 'to': to, 'moving': moving,
        'regate': not moving and previous == BidReview.SKIPPED and required,
        'to_label': dict(Project.STAGE_CHOICES).get(to, ''), 'required': required, 'settings': settings,
        'gate_value': gate.gate_value(project, review), 'other_bids': other_bids,
        'window': gate.tender_window(project), 'live_score': live,
        'live_label': dict(scoring.RESULT_CHOICES).get(scoring.result(live), ''),
        'live_tone': scoring.RESULT_TONES.get(scoring.result(live), 'muted'),
        'go_from': scoring.GO_FROM, 'review_from': scoring.REVIEW_FROM,
        'section': use_section(request, 'estimating'),
    })


def _answers(form):
    return {k: form.cleaned_data.get(f'q_{k}') for k in scoring.QUESTION_KEYS}


@estimating_required
def bid_decline(request, org_slug, pk):
    """Decline to bid, with a reason from the list. Every route to Declined comes here."""
    project = get_project(request, pk)
    if refused := _not_a_bid(request, project, Project.STAGE_DECLINED, 'declined'):
        return refused
    review = _review(project)
    form = DeclineBidForm(request.POST if request.method == 'POST' else None, instance=review)
    if request.method == 'POST' and form.is_valid():
        review = form.save(commit=False)
        review.decision = BidReview.DECLINED
        review.save()
        if not project.outcome_notes.strip():
            project.outcome_notes = f'Declined to bid. {review.decline_summary}'
        if project.stage == Project.STAGE_DECLINED:
            project.save()
            messages.success(request, 'Reason saved.')
        else:
            gate.apply_stage(request, project, Project.STAGE_DECLINED)
            messages.success(request, f'{project.name} declined: {review.get_decline_reason_display().lower()}. '
                                      'Anyone still pricing it has been told.')
        return redirect(project)
    return render(request, 'tenders/bid_decline.html', {
        'project': project, 'form': form, 'section': use_section(request, 'estimating'),
        'open_packages': project.packages.filter(status__in=('ENQUIRY', 'QUOTED')).count(),
    })


@estimating_required
def bid_lost(request, org_slug, pk):
    """The loss review: our price, the winner's if known, the gap, why, and lessons. Every route to Lost comes here."""
    project = get_project(request, pk)
    if refused := _not_a_bid(request, project, Project.STAGE_LOST, 'marked lost'):
        return refused
    review = _review(project)
    initial = {} if review.our_price is not None else {'our_price': project.value}
    form = LossReviewForm(request.POST if request.method == 'POST' else None, instance=review, initial=initial)
    if request.method == 'POST' and form.is_valid():
        review = form.save(commit=False)
        review.loss_reviewed_by, review.loss_reviewed_at = request.user, timezone.now()
        review.save()
        if not project.outcome_notes.strip():
            gap = review.gap_pct
            project.outcome_notes = f'Lost: {review.get_loss_reason_display().lower()}.' + (
                f' {abs(gap)}% {"above" if gap > 0 else "below"} the winning price.' if gap else '')
        if project.stage == Project.STAGE_LOST:
            project.save()
            messages.success(request, 'Loss review saved.')
        else:
            gate.apply_stage(request, project, Project.STAGE_LOST)
            messages.success(request, f'{project.name} marked lost. Anyone still pricing it has been told.')
        return redirect(project)
    return render(request, 'tenders/bid_lost.html', {
        'project': project, 'form': form, 'review': review, 'section': use_section(request, 'estimating'),
        'open_packages': project.packages.filter(status__in=('ENQUIRY', 'QUOTED')).count(),
    })


@admin_required
def tender_settings(request, org_slug):
    current = TenderSettings.for_org(request.org)
    form = TenderSettingsForm(request.POST if request.method == 'POST' else None, instance=current)
    if request.method == 'POST' and form.is_valid():
        form.save()
        log_activity(request, 'changed estimating settings', str(form.instance))
        messages.success(request, 'Estimating settings saved.')
        return redirect('tender_settings', org_slug=org_slug)
    return render(request, 'tenders/settings.html', {'form': form, 'section': use_section(request, 'estimating')})


@estimating_required
def coverage_board(request, org_slug):
    """Every open tender's trades on one page, thin ones highlighted, soonest quotes due first."""
    projects = visible_projects(request)
    leads = sorted({p.owner for p in projects.filter(stage__in=coverage.OPEN_TENDER_STAGES) if p.owner},
                   key=lambda u: u.full_name)
    lead = request.GET.get('lead', '')
    if lead.isdigit():
        projects = projects.filter(owner_id=int(lead))
    show = 'thin' if request.GET.get('show') == 'thin' else 'all'
    tenders = coverage.open_tenders(projects, today=timezone.localdate())
    if show == 'thin':
        tenders = [(p, [r for r in rows if r.thin]) for p, rows in tenders if p.thin_rows]
    return render(request, 'tenders/coverage.html', {
        'tenders': tenders, 'show': show, 'lead': lead, 'leads': leads,
        'target': TenderSettings.for_org(request.org).min_quotes,
        'thin_total': sum(len(p.thin_rows) for p, _ in tenders),
        'section': use_section(request, 'estimating'),
    })
