"""
Subcontractor application status changes. Registered in SubpaymentsConfig.ready(); views call
core.workflow.apply_transition(app, name, user, **params).

  (received, by the sub in the portal or recorded by staff) RECEIVED
  RECEIVED --payment_notice--> NOTIFIED      our payment notice (late ones are recorded but don't count: s110B)
  RECEIVED / NOTIFIED --pay_less--> PAY_LESS only on or before the pay less deadline (s111)
  RECEIVED / NOTIFIED / PAY_LESS --paid--> PAID   when our payments cover the sum due (less any discount)
  PAID --reopen_*--> back                    a payment reversed by an owner or admin
  RECEIVED --withdraw--> WITHDRAWN           the sub or staff, before any notice or payment, while the
                                             payment notice deadline has not passed

Who may: notices, payments and withdrawing are for owners, admins and the job's PM/QS
(subpayments.access); the subcontractor may withdraw their own application. Reopening is owners
and admins (it follows a reversal). Advisory: check your contract.
"""
from django.utils import timezone

from core.access import is_admin
from core.exceptions import InvalidTransition
from core.workflow import Transition, apply_transition

from . import rules
from .models import SubApplication

A = SubApplication


def staff(app, user):
    from .access import user_is_staff_for
    return user_is_staff_for(user, app.project)


def staff_or_own_sub(app, user):
    if staff(app, user):
        return True
    from projects.models import ProjectMember
    return ProjectMember.objects.filter(project_id=app.project_id, user=user, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                        subcontractor_id=app.subcontract.package.subcontractor_id).exists()


def admin(app, user):
    return is_admin(user, app.organization_id)


def is_covered(app, user=None):
    app._paid_gross = None
    return app.paid_gross > 0 and app.outstanding <= 0


def _payment_notice(app, user, amount=None, basis='', given_on=None):
    app.payment_notice_amount = rules.money(amount)
    app.payment_notice_basis = basis
    app.payment_notice_on = given_on or timezone.localdate()
    app.payment_notice_given_by = user
    app.cap_materials()  # a lower sum due can't carry more materials than it
    app.due_date = app.pay_less_by if app.pay_less_by and timezone.localdate() <= app.pay_less_by else app.final_date


def _pay_less(app, user, amount=None, basis='', given_on=None):
    given_on = given_on or timezone.localdate()
    refusal = rules.pay_less_refusal(given_on, app.pay_less_by)
    if refusal:
        raise InvalidTransition(refusal)
    app.pay_less_amount = rules.money(amount)
    app.pay_less_basis = basis
    app.pay_less_on = given_on
    app.pay_less_given_by = user
    app.cap_materials()
    app.due_date = app.final_date


def _paid(app, user, paid_on=None):
    app.paid_in_full_on = paid_on or timezone.localdate()
    app.due_date = None


def _reopened(app, user):
    app.paid_in_full_on = None
    app.due_date = app.final_date


def withdraw_refusal(app):
    if app.pk and app.live_payments().exists():
        return 'Money has been paid against it, so it cannot be withdrawn.'
    if app.payment_notice_on:
        return 'Our payment notice has been given, so it cannot be withdrawn.'
    if rules.stands_as_notified(timezone.localdate(), app.payment_notice_by, app.payment_notice_on):
        return ('No payment notice was given by its deadline, so it stands as the notified sum and cannot be '
                'withdrawn. ' + rules.ADVISORY)
    return ''


def _withdrawn(app, user):
    refusal = withdraw_refusal(app)
    if refusal:
        raise InvalidTransition(refusal)
    app.due_date = None


def settle_if_covered(app, user, note=''):
    """After a notice or a payment: mark it paid when payments already cover the sum due. True if it did."""
    if app.status in A.AWAITING and is_covered(app):
        last = app.live_payments().order_by('-paid_on').first()
        apply_transition(app, 'paid', user, note=note or 'Paid in full',
                         paid_on=last.paid_on if last else None)
        return True
    return False


def awaiting_status(app):
    if app.pay_less_on:
        return A.PAY_LESS
    if app.payment_notice_on:
        return A.NOTIFIED
    return A.RECEIVED


def reopen_after_reversal(app, user, note=''):
    app._paid_gross = None
    if app.status == A.PAID and app.outstanding > 0:
        apply_transition(app, f'reopen_{awaiting_status(app).lower()}', user, note=note)
        return True
    return False


TRANSITIONS = [
    Transition('payment_notice', source=(A.RECEIVED,), target=A.NOTIFIED, label='give our payment notice on',
               allowed=staff, on_apply=_payment_notice),
    Transition('pay_less', source=(A.RECEIVED, A.NOTIFIED), target=A.PAY_LESS, label='give a pay less notice on',
               allowed=staff, on_apply=_pay_less),
    Transition('paid', source=A.AWAITING, target=A.PAID, label='mark as paid', allowed=lambda a, u: staff(a, u)
               or is_covered(a), sets_ball_in_court=None, on_apply=_paid),
    Transition('withdraw', source=(A.RECEIVED,), target=A.WITHDRAWN, label='withdraw', allowed=staff_or_own_sub,
               sets_ball_in_court=None, on_apply=_withdrawn),
    *[Transition(f'reopen_{s.lower()}', source=(A.PAID,), target=s, label='reopen after a reversed payment',
                 allowed=admin, on_apply=_reopened) for s in (A.RECEIVED, A.NOTIFIED, A.PAY_LESS)],
]

AUDIT_FIELDS = ('valuation_date', 'received_on', 'gross_to_date', 'materials_stated', 'materials', 'notes',
                'variations_value', 'retention_percent', 'retention_stage', 'gross_valuation', 'retention',
                'previous_certified', 'amount_due', 'discount_percent', 'vat_rate', 'reverse_charge', 'due_on',
                'payment_notice_by', 'pay_less_by', 'final_date', 'payment_notice_amount', 'payment_notice_basis',
                'payment_notice_on', 'pay_less_amount', 'pay_less_basis', 'pay_less_on', 'paid_in_full_on')
