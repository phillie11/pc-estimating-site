"""
Everything that changes money on a subcontract, each in one transaction.

    receive_application(subcontract, actor, valuation_date, gross_to_date, materials, notes, by_sub, received_on)
    release_retention(subcontract, actor, stage_date)          a retention release, through the same flow
    adjust_materials(app, actor, amount, reason)
    record_payment(app, actor, gross, paid_on, materials, reference, form_key)
                                                                posts an ACTUAL to costs.ledger (gross ex VAT, before CIS)
    reverse_payment(payment, actor, reason)                     reverses the ledger entry too
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from costs import ledger

from . import rules
from .models import SubApplication, SubPayment, tax_for
from .workflow import reopen_after_reversal, settle_if_covered

ZERO = Decimal('0.00')


class SubPayError(ValueError):
    """Something the rules don't allow; show it as a message."""


class AlreadyRecorded(SubPayError):
    def __init__(self, record):
        self.record = record
        super().__init__('That payment was already recorded from this form, so it was not added again.')


def _check_open(subcontract):
    if subcontract.project.archived_at is not None:
        raise SubPayError('This job is archived, so it is read only. Restore it to make changes.')


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


def _team_ball(project):
    return project.manager


@transaction.atomic
def receive_application(subcontract, actor, valuation_date, gross_to_date, materials=ZERO, notes='', by_sub=False,
                        received_on=None, kind=SubApplication.INTERIM, title=''):
    _check_open(subcontract)
    problem = clash(subcontract, valuation_date)
    if problem:
        raise SubPayError(problem)
    app = SubApplication(project=subcontract.project, subcontract=subcontract, kind=kind,
                         valuation_date=valuation_date, received_on=received_on or timezone.localdate(),
                         gross_to_date=rules.money(gross_to_date), materials_stated=rules.money(materials),
                         notes=notes, applied_by_sub=by_sub)
    app.title = title or f'{subcontract.subcontractor.company_name}: valuation to {valuation_date.day} {valuation_date:%b %Y}'
    app.calculate()
    if app.materials > max(app.amount_due, ZERO):
        app.materials = max(app.amount_due, ZERO)
    app.ball_in_court_user = _team_ball(subcontract.project)
    app.due_date = app.payment_notice_by
    app.save(actor=actor, audit_note='Applied through the portal' if by_sub else 'Recorded by staff')
    return app


def release_retention(subcontract, actor, stage_date):
    """A retention release, as an application at the release date with the gross value unchanged."""
    last = SubApplication.objects.filter(subcontract=subcontract, status__in=SubApplication.COUNTED) \
        .order_by('-valuation_date').first()
    if last is None:
        raise SubPayError('There is no application on this subcontract yet, so no retention is held.')
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
                               title=f'{subcontract.subcontractor.company_name}: retention release at {what}')


@transaction.atomic
def adjust_materials(app, actor, amount, reason):
    amount = rules.money(amount)
    if app.status not in SubApplication.AWAITING:
        raise SubPayError('Materials can only be changed while the application is awaiting payment.')
    if amount < app.materials_paid:
        raise SubPayError('That is less than the materials already counted in payments made.')
    app.materials = amount
    app.save(actor=actor, audit_action='materials_adjusted', audit_note=(reason or '')[:300])
    return app


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
    mats = app.materials_left if materials is None else rules.money(materials)
    mats = min(mats, gross, app.materials_left)
    tax = tax_for(app.subcontract.subcontractor)
    adv = rules.advice(gross, mats, tax.cis_status, app.vat_rate, app.reverse_charge)
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
