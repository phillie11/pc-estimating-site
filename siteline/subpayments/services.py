"""
Everything that changes money on a subcontract, each in one transaction.

    receive_application(subcontract, actor, valuation_date, gross_to_date, materials, notes, by_sub, received_on,
                        override_reason, form_key)
    release_retention(subcontract, actor, stage_date)          a retention release, through the same flow
    adjust_materials(app, actor, amount, reason)
    record_payment(app, actor, gross, paid_on, materials, reference, form_key)
                                                                posts an ACTUAL to costs.ledger (gross ex VAT, before CIS)
    reverse_payment(payment, actor, reason)                     reverses the ledger entry too

The subcontract row is locked (select_for_update) while an application is made against it, so two
at once can't both pass the date checks or count the same previous amounts.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from costs import ledger

from . import rules
from .models import Subcontract, SubApplication, SubPayment, tax_for
from .workflow import reopen_after_reversal, settle_if_covered

ZERO = Decimal('0.00')


class SubPayError(ValueError):
    """Something the rules don't allow; show it as a message."""


class AlreadyRecorded(SubPayError):
    def __init__(self, record):
        self.record = record
        super().__init__('That was already recorded from this form, so it was not added again.')


class OverCap(SubPayError):
    """Gross to date above three times the order value: staff may go ahead with a reason."""


def _check_open(subcontract):
    if subcontract.project.archived_at is not None:
        raise SubPayError('This job is archived, so it is read only. Restore it to make changes.')


def _lock(subcontract):
    return Subcontract.objects.select_for_update().select_related('project', 'package__subcontractor') \
        .get(pk=subcontract.pk)


def clash(subcontract, valuation_date, exclude=None):
    """Why an application can't be made up to this date on this subcontract, or ''."""
    qs = SubApplication.objects.filter(subcontract=subcontract).exclude(status=SubApplication.WITHDRAWN)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    same = qs.filter(valuation_date=valuation_date).first()
    if same:
        return f'{same.reference} is already made up to {valuation_date:%d %b %Y}.'
    later = qs.filter(valuation_date__gt=valuation_date).order_by('-valuation_date').first()
    if later:
        return (f'{later.reference} is made up to {later.valuation_date:%d %b %Y}, which is later. A new application '
                'has to be made up to a later date.')
    return ''


def date_refusal(subcontract, valuation_date, today=None):
    """Why this valuation date isn't one of the subcontract's (this cycle's or the one before), or ''."""
    today = today or timezone.localdate()
    if valuation_date > today:
        return 'The valuation date is in the future. Apply on or after it.'
    allowed = subcontract.allowed_dates(today)
    if valuation_date in allowed:
        return ''
    if not allowed:
        return 'The subcontract has no valuation date yet. Ask the contractor to check its payment terms.'
    names = ' or '.join(f'{d.day} {d:%b %Y}' for d in allowed)
    return f'Use a valuation date from the subcontract: {names}.'


def cap_refusal(subcontract, gross_to_date):
    cap = subcontract.gross_cap
    if cap is not None and rules.money(gross_to_date) > cap:
        return (f'The gross value to date is more than three times the order value ({cap:,} is the limit). Check '
                'the figure. If it is right, staff can record it with a reason.')
    return ''


def _team_ball(project):
    return project.manager


@transaction.atomic
def receive_application(subcontract, actor, valuation_date, gross_to_date, materials=ZERO, notes='', by_sub=False,
                        received_on=None, kind=SubApplication.INTERIM, title='', override_reason='', form_key=None,
                        check_date=True):
    subcontract = _lock(subcontract)
    _check_open(subcontract)
    if form_key:
        first = SubApplication.objects.filter(subcontract=subcontract, form_key=form_key).first()
        if first is not None:
            raise AlreadyRecorded(first)
    received_on = received_on or timezone.localdate()
    received_on = max(received_on, valuation_date)  # it can't have been received before the date it is made up to
    if check_date:
        problem = date_refusal(subcontract, valuation_date)
        if problem:
            raise SubPayError(problem)
    problem = clash(subcontract, valuation_date)
    if problem:
        raise SubPayError(problem)
    over = cap_refusal(subcontract, gross_to_date)
    if over and not (override_reason or '').strip():
        raise OverCap(over)
    app = SubApplication(project=subcontract.project, subcontract=subcontract, kind=kind,
                         valuation_date=valuation_date, received_on=received_on,
                         gross_to_date=rules.money(gross_to_date), materials_stated=rules.money(materials),
                         notes=notes, applied_by_sub=by_sub, form_key=form_key or None)
    app.title = title or f'{subcontract.subcontractor.company_name}: valuation to {valuation_date.day} {valuation_date:%b %Y}'
    app.calculate()
    app.cap_materials()
    app.ball_in_court_user = _team_ball(subcontract.project)
    app.due_date = app.payment_notice_by
    note = 'Applied through the portal' if by_sub else 'Recorded by staff'
    if over:
        note += f'. Over three times the order value, accepted: {override_reason.strip()}'
    app.save(actor=actor, audit_note=note[:500])
    return app


@transaction.atomic
def release_retention(subcontract, actor, stage_date):
    """A retention release, as an application at the release date with the gross value unchanged."""
    subcontract = _lock(subcontract)
    last = SubApplication.objects.filter(subcontract=subcontract, status__in=SubApplication.COUNTED) \
        .order_by('-valuation_date').first()
    if last is None:
        raise SubPayError('There is no application on this subcontract yet, so no retention is held.')
    if stage_date > timezone.localdate():
        raise SubPayError('The release date is in the future.')
    stage = subcontract.stage_at(stage_date)
    if stage == rules.STAGE_BEFORE_PC:
        raise SubPayError('Practical completion has not been reached on that date, so no retention is due for '
                          'release. Set the practical completion date on the subcontract or the main contract.')
    held = rules.retention_held(last.gross_valuation, subcontract.retention_percent,
                                subcontract.retention_released_at_pc_percent, stage)
    if held >= last.retention:
        raise SubPayError('No more retention is due for release at that date.')
    what = 'end of defects' if stage == rules.STAGE_AFTER_DEFECTS else 'practical completion'
    return receive_application(subcontract, actor, stage_date, last.gross_to_date, ZERO,
                               notes=f'Retention released at {what}.', kind=SubApplication.RELEASE,
                               title=f'{subcontract.subcontractor.company_name}: retention release at {what}',
                               override_reason='retention release', check_date=False)


@transaction.atomic
def adjust_materials(app, actor, amount, reason):
    app = SubApplication.objects.select_for_update().get(pk=app.pk)
    amount = rules.money(amount)
    if app.status not in SubApplication.AWAITING:
        raise SubPayError('Materials can only be changed while the application is awaiting payment.')
    if amount < app.materials_paid:
        raise SubPayError('That is less than the materials already counted in payments made.')
    if amount > max(app.payable, ZERO):
        raise SubPayError(f'Materials can be no more than the sum we pay ({app.payable:,}).')
    app.materials = amount
    app.save(actor=actor, audit_action='materials_adjusted', audit_note=(reason or '')[:300])
    return app


def live_actuals(commitment):
    """Hand-entered actual costs against the order that haven't been reversed."""
    from costs.models import ActualCost
    return [a for a in ActualCost.objects.filter(commitment=commitment)
            if ledger.source_total(a, ledger.ACTUAL) != 0]


def earlier_tax_month(paid_on, today=None):
    """True when a payment date is in a tax month before the current one (its CIS return may be filed)."""
    return rules.tax_month(paid_on).start < rules.tax_month(today or timezone.localdate()).start


@transaction.atomic
def record_payment(app, actor, gross, paid_on, materials=None, reference='', form_key=None):
    app = SubApplication.objects.select_for_update().get(pk=app.pk)
    if form_key:
        first = SubPayment.objects.filter(application=app, form_key=form_key).first()
        if first is not None:
            raise AlreadyRecorded(first)
    _check_open(app.subcontract)
    if app.status not in SubApplication.AWAITING:
        raise SubPayError('This application is not awaiting payment.')
    if paid_on > timezone.localdate():
        raise SubPayError('The payment date is in the future.')
    if paid_on < app.received_on:
        raise SubPayError(f'The payment date is before the application was received ({app.received_on:%d %b %Y}).')
    gross = rules.money(gross)
    if gross <= 0:
        raise SubPayError('Enter the gross amount paid.')
    if gross > app.outstanding:
        raise SubPayError(f'That is more than the {app.outstanding:,} still to pay (excluding VAT, before CIS). '
                          'Overpayments are not supported.')
    commitment = app.subcontract.commitment
    if commitment is None:
        raise SubPayError('This package has no order in Costs yet, so the payment has no cost code to post to. '
                          'Commit the order under Commercial -> Costs -> Orders first.')
    hand = live_actuals(commitment)
    if hand:
        refs = ', '.join(a.reference or a.supplier for a in hand[:3])
        raise SubPayError(f'{commitment.ref} has actual costs entered by hand ({refs}). Reverse them under Costs -> '
                          'Actual costs first, so the subcontract is not counted twice.')
    tax = tax_for(app.subcontract.subcontractor)
    adv = app.part_advice(gross, tax, materials)
    p = SubPayment(application=app, paid_on=paid_on, gross=adv.gross, materials=adv.materials, labour=adv.labour,
                   cis_status=tax.cis_status, cis_rate=adv.cis_rate, cis_deduction=adv.cis,
                   verification_number=tax.verification_number, vat_rate=adv.vat_rate,
                   reverse_charge=adv.reverse_charge, vat=adv.vat, net=adv.net, reference=reference,
                   form_key=form_key or None, recorded_by=actor)
    try:
        with transaction.atomic():
            p.save()
    except IntegrityError:
        raise AlreadyRecorded(SubPayment.objects.get(application=app, form_key=form_key))
    try:
        entry = ledger.post(app.project, commitment.cost_code, ledger.ACTUAL, adv.gross, source=p, actor=actor,
                            date=paid_on, note=f'{app.reference} {app.subcontract.subcontractor.company_name} payment')
    except ledger.LedgerError as e:
        raise SubPayError(str(e))
    p.ledger_entry = entry
    p.save(update_fields=['ledger_entry'])
    app._paid_gross = None
    if not settle_if_covered(app, actor, note=f'Paid {adv.gross:,} on {paid_on:%d %b %Y}'):
        app.save(actor=actor, audit_action='payment_recorded', audit_note=f'Paid {adv.gross:,} on {paid_on:%d %b %Y}')
    return p


@transaction.atomic
def reverse_payment(payment, actor, reason):
    payment = SubPayment.objects.select_for_update().get(pk=payment.pk)
    reason = (reason or '').strip()
    if payment.is_reversed:
        raise SubPayError('This payment has already been reversed.')
    if not reason:
        raise SubPayError('Say why the payment is being reversed.')
    app = payment.application
    _check_open(app.subcontract)
    rev = None
    if payment.ledger_entry_id:
        try:
            rev = ledger.reverse(payment.ledger_entry, actor, reason)
        except ledger.LedgerError as e:
            raise SubPayError(str(e))
    payment.reversed_at, payment.reversed_by, payment.reversal_reason = timezone.now(), actor, reason[:300]
    payment.reversal_entry = rev
    payment.save(update_fields=['reversed_at', 'reversed_by', 'reversal_reason', 'reversal_entry'])
    note = f'Payment of {payment.gross:,} on {payment.paid_on:%d %b %Y} reversed: {reason}'[:500]
    if not reopen_after_reversal(app, actor, note=note):
        app.save(actor=actor, audit_action='payment_reversed', audit_note=note)
    return payment
