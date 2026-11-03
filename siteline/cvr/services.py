"""
Everything that changes CVR and daywork data goes through here, so the rules hold whichever
screen (or the demo, or a test) does it.

  settings_for(org)                          the company's commercial reporting settings
  start_period(project, month, actor)        open a month's CVR (in order, never before a signed-off one)
  add_adjustment(period, kind, amount, reason, actor)
  reverse_adjustment(adjustment, actor, reason)
  bring_forward(period, actor)               last month's live adjustments again, as this month's
  terms_for(project, create)                 the job's daywork percentage additions
  new_sheet(project, actor, **fields)        a daywork sheet with the job's percentages
  send_to_change(sheet, actor, change_event) a change event from the sheet (or lines on an existing one)
"""
from django.db import IntegrityError, transaction
from django.utils import timezone

from costs.money import pennies

from . import rules
from .models import CommercialSettings, CVRAdjustment, CVRPeriod, DayworkSheet, DayworkTerms


class CVRError(ValueError):
    """Something the CVR won't do, in words to show the person."""


def settings_for(org, create=True):
    """The company's settings; with create=False an unsaved row of defaults (Needs me, GET pages: no writes)."""
    obj = CommercialSettings.objects.filter(organization=org).first()
    if obj is not None:
        return obj
    if not create:
        return CommercialSettings(organization=org)
    try:
        with transaction.atomic():
            return CommercialSettings.objects.create(organization=org)
    except IntegrityError:  # two first requests at once
        return CommercialSettings.objects.get(organization=org)


# ---------------------------------------------------------------------------
# The monthly CVR
# ---------------------------------------------------------------------------

def first_month(project):
    """The earliest month a CVR can be started for: the start on site, else when the job was set up."""
    start = project.start_date or timezone.localdate(project.created_at)
    return rules.month_start(start)


def startable_months(project, today=None):
    """Months a new CVR can be started for: after the last signed-off one, up to this month, not started yet."""
    today = today or timezone.localdate()
    rows = list(CVRPeriod.with_deleted.filter(project=project).values_list('month', 'status', 'deleted_at'))
    existing = {m for m, _, _ in rows}
    last_signed = max((m for m, status, gone in rows if status == CVRPeriod.SIGNED_OFF and gone is None), default=None)
    earliest = first_month(project)
    if last_signed:
        earliest = max(earliest, rules.add_months(last_signed, 1))
    earliest = max(earliest, rules.add_months(today, -24))  # two years back is plenty to choose from
    return [m for m in reversed(rules.months_between(earliest, today)) if m not in existing]


def start_period(project, month, actor, today=None):
    today = today or timezone.localdate()
    month = rules.month_start(month)
    if month > rules.month_start(today):
        raise CVRError("A CVR can't be started for a month that hasn't begun.")
    deleted = CVRPeriod.with_deleted.filter(project=project, month=month, deleted_at__isnull=False).first()
    if deleted is not None:
        raise CVRError(f'The CVR for {rules.month_label(month)} ({deleted.reference}) was deleted. An admin can '
                       'restore it.')
    if CVRPeriod.objects.filter(project=project, month__gte=month, status=CVRPeriod.SIGNED_OFF).exists():
        raise CVRError(f'A later month is already signed off, so {rules.month_label(month)} is closed. Put any '
                       'correction in the next open month as an adjustment.')
    existing = CVRPeriod.objects.filter(project=project, month=month).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            due = rules.due_on(month, settings_for(project.organization, create=False).cvr_due_day)
            period = CVRPeriod(project=project, month=month, due_date=due)
            period.save(actor=actor)
    except IntegrityError:  # started by someone else a moment ago
        return CVRPeriod.objects.get(project=project, month=month), False
    return period, True


def _open(period):
    if period.status != CVRPeriod.DRAFT:
        raise CVRError(f'{period.reference} is {period.get_status_display().lower()}, so its adjustments are fixed. '
                       + ('Send it back for changes first.' if period.status == CVRPeriod.SUBMITTED else
                          'Put any correction in a later month.'))


def add_adjustment(period, kind, amount, reason, actor):
    _open(period)
    reason = (reason or '').strip()
    if not reason:
        raise CVRError('Say what the adjustment is for.')
    if kind not in dict(CVRAdjustment.KIND_CHOICES):
        raise CVRError('Choose what the adjustment is to.')
    amount = pennies(amount)
    if not amount:
        raise CVRError('Enter an amount other than nil.')
    with transaction.atomic():
        adj = CVRAdjustment(organization_id=period.organization_id, period=period, project_id=period.project_id,
                            kind=kind, amount=amount, reason=reason[:300], created_by=actor)
        adj.save()
        period.save(actor=actor, audit_action='adjusted',
                    audit_note=f'{adj.get_kind_display()}: {amount} ({reason})'[:255])
    return adj


def reverse_adjustment(adj, actor, reason):
    period = adj.period
    _open(period)
    reason = (reason or '').strip()
    if not reason:
        raise CVRError('Say why it is being reversed.')
    if adj.reverses_id is not None:
        raise CVRError('That is itself a reversal. Add a new adjustment instead.')
    try:
        with transaction.atomic():
            rev = CVRAdjustment(organization_id=adj.organization_id, period=period, project_id=adj.project_id,
                                kind=adj.kind, amount=-adj.amount, reason=f'Reversal: {reason}'[:300], reverses=adj,
                                created_by=actor)
            rev.save()
            period.save(actor=actor, audit_action='adjusted',
                        audit_note=f'Reversed {adj.get_kind_display()} {adj.amount}: {reason}'[:255])
    except IntegrityError:
        raise CVRError('That adjustment has already been reversed.')
    return rev


def live_adjustments(period):
    """The month's adjustments that still count: not reversed, and not reversals."""
    rows = list(CVRAdjustment.objects.filter(period=period).select_related('created_by'))
    reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
    return [r for r in rows if r.reverses_id is None and r.pk not in reversed_ids]


def previous_period(period):
    return CVRPeriod.objects.filter(project_id=period.project_id, month__lt=period.month).order_by('-month').first()


def bring_forward(period, actor):
    """
    Last month's accruals and adjustments that still count, added again to this month with the
    same reasons (most accruals carry until the invoice arrives). Returns how many were added.
    """
    _open(period)
    prev = previous_period(period)
    if prev is None:
        raise CVRError('There is no earlier month to bring adjustments forward from.')
    rows = live_adjustments(prev)
    if not rows:
        raise CVRError(f'{prev.label} has no adjustments to bring forward.')
    with transaction.atomic():
        for r in rows:
            reason = r.reason if r.reason.startswith('Brought forward') else f'Brought forward from {prev.label}: {r.reason}'
            CVRAdjustment(organization_id=period.organization_id, period=period, project_id=period.project_id,
                          kind=r.kind, amount=r.amount, reason=reason[:300], created_by=actor).save()
        period.save(actor=actor, audit_action='adjusted', audit_note=f'{len(rows)} brought forward from {prev.label}')
    return len(rows)


# ---------------------------------------------------------------------------
# Dayworks
# ---------------------------------------------------------------------------

def terms_for(project, create=False):
    """The job's daywork percentages: saved ones, else (unsaved, or saved when create) the company defaults."""
    terms = DayworkTerms.objects.filter(project=project).first()
    if terms is not None:
        return terms
    s = settings_for(project.organization)
    terms = DayworkTerms(organization_id=project.organization_id, project=project, labour_pct=s.labour_pct,
                         materials_pct=s.materials_pct, plant_pct=s.plant_pct)
    if create:
        try:
            with transaction.atomic():
                terms.save()
        except IntegrityError:
            terms = DayworkTerms.objects.get(project=project)
    return terms


def new_sheet(project, actor, **fields):
    terms = terms_for(project)
    # The writer holds the ball until it is on the change register (cvr/actions.py reads it from the index)
    sheet = DayworkSheet(project=project, labour_pct=terms.labour_pct, materials_pct=terms.materials_pct,
                         plant_pct=terms.plant_pct, ball_in_court_user=actor, **fields)
    sheet.save(actor=actor)
    return sheet


def refresh_terms(sheet):
    """While a sheet is being written, it follows the job's current percentages."""
    if sheet.is_editable:
        terms = terms_for(sheet.project)
        sheet.labour_pct, sheet.materials_pct, sheet.plant_pct = terms.labour_pct, terms.materials_pct, terms.plant_pct


def change_description(sheet, price):
    signed = (f'Signed on the day by {sheet.signed_by_name} ({sheet.get_signed_by_role_display().lower()}) on '
              f'{timezone.localtime(sheet.signed_at):%d %b %Y}.' if sheet.signed_at else
              'Not signed by the client side.')
    parts = [sheet.description.strip(), f'Daywork on {sheet.work_date:%d %b %Y}'
             + (f' at {sheet.location}' if sheet.location else '') + '.',
             f'Instructed by: {sheet.instructed_by}.' if sheet.instructed_by else '', signed,
             f'Priced at the daywork rates: prime cost {price["prime"]:,.2f} plus percentage additions '
             f'{price["additions"]:,.2f}, total {price["total"]:,.2f}.']
    return '\n'.join(p for p in parts if p)


def send_to_change(sheet, actor, change_event=None):
    """
    Put the sheet on the change register in one step: a new change event (origin: instruction) or the
    one chosen, with a revenue line at the priced total and a cost line at the prime cost, linked so
    the change event shows it came from the sheet. Returns (change_event, created).
    """
    from changes.models import ChangeEvent, ChangeEventLine
    from core.models import RecordLink, link
    from core.workflow import apply_transition

    with transaction.atomic():
        sheet = DayworkSheet.objects.select_for_update().get(pk=sheet.pk)
        if sheet.status not in (DayworkSheet.DRAFT, DayworkSheet.SIGNED):
            raise CVRError(f'{sheet.reference} is already {sheet.get_status_display().lower()}.')
        lines = list(sheet.lines.all())
        if not lines:
            raise CVRError('Add at least one line of labour, plant or materials first.')
        refresh_terms(sheet)
        price = sheet.price(lines)
        created = change_event is None
        if created:
            change_event = ChangeEvent(project=sheet.project, title=f'Daywork: {sheet.title}'[:200], origin='INSTRUCTION',
                                       description=change_description(sheet, price), ball_in_court_user=actor,
                                       aware_on=sheet.work_date)
            change_event.save(actor=actor)
        elif change_event.project_id != sheet.project_id or change_event.status not in ChangeEvent.LIVE_STATUSES:
            raise CVRError('Choose a live change event on this job.')
        for kind, amount, words in ((ChangeEventLine.KIND_REVENUE, price['total'], 'at daywork rates'),
                                    (ChangeEventLine.KIND_COST, price['prime'], 'prime cost')):
            ChangeEventLine.objects.create(change_event=change_event, kind=kind, amount=amount,
                                           description=f'Daywork {sheet.reference} {words}'[:200])
        change_event.save(actor=actor, audit_action='priced',
                          audit_note=f'Daywork {sheet.reference}: revenue {price["total"]}, cost {price["prime"]}')
        link(change_event, sheet, RecordLink.DERIVED_FROM, actor=actor)
        apply_transition(sheet, 'send_to_change', actor, note=f'Became part of {change_event.reference}',
                         change_event=change_event)
    return change_event, created

