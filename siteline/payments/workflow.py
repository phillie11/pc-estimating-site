"""
Application status changes. Registered in PaymentsConfig.ready(); views call
core.workflow.apply_transition(app, name, user, **params).

  DRAFT --submit--> SUBMITTED --payment_notice / _on_behalf--> NOTIFIED ("Certified")
  SUBMITTED / NOTIFIED --pay_less / _on_behalf--> PAY_LESS (only on or before the pay less deadline)
  SUBMITTED / NOTIFIED / PAY_LESS --paid--> PAID (when payments received cover the notified sum)
  SUBMITTED / NOTIFIED / PAY_LESS --settled--> PAID (a notice brings the sum due down to what was already paid)
  PAID --reopen_submitted / reopen_notified / reopen_pay_less--> back (a payment reversed by an owner or admin)
  DRAFT / SUBMITTED --withdraw--> WITHDRAWN (DRAFT, or SUBMITTED with nothing received and the payment
                                             notice deadline not passed without a notice)

Submitting is refused while a later application has been submitted: its "previous amounts" did not
include this one, so the money would be claimed twice. Withdrawing makes it internal again.

Who may:
  submit, withdraw, paid    owners, admins and the job's project manager (payments.access).
  payment_notice, pay_less  the payer's side on the project, in their portal: the client, or the
                            consultant named on the contract as its administrator (payments.access.is_payer).
  *_on_behalf               owners and admins recording a notice the client gave by email or
                            letter (product owner decision, as variations). The view requires a
                            note and the evidence file; the audit entry is noted "Recorded on
                            behalf of client" and the clients are told.

Submitting freezes the figures and the cycle dates, shares the application with the project's
clients and passes the ball to the client (the contract administrator if a client, else the
first client invited; variations.workflow.client_ball). Its due date becomes the payment notice
deadline, then the final date for payment once a notice is given.
"""
from django.utils import timezone

from core.access import is_admin
from core.exceptions import InvalidTransition
from core.models import VIS_INTERNAL, VIS_SHARED
from core.workflow import Transition, apply_transition

from . import rules
from .models import Application


def manages(app, user):
    from .access import user_manages_payments
    return user_manages_payments(user, app.project)


def records_for_client(app, user):
    return is_admin(user, app.organization_id)


def is_project_client(app, user):
    """The payer's side: the job's client, or the consultant the contract names as administrator."""
    from .access import is_payer
    return is_payer(user, app.project)


def payer_ball(app, user=None):
    """Who has the ball while the payer gives their notice: the contract administrator if on the payer's side, else the first client."""
    from .access import administrator_member, payer_members
    admin = administrator_member(app.project)
    if admin is not None:
        return admin.user
    from contracts.models import MainContract
    from projects.models import ProjectMember
    contract = MainContract.objects.filter(project_id=app.project_id).select_related('administrator').first()
    if contract and contract.administrator and contract.administrator.role == ProjectMember.ROLE_CLIENT:
        return contract.administrator.user
    members = sorted(payer_members(app.project), key=lambda m: m.pk)
    return members[0].user if members else None


def is_covered(app, user=None):
    """Money already received covers the notified sum and its VAT (after a notice brought it down)."""
    return app.paid_total > 0 and app.outstanding <= 0


def _contract(app):
    from contracts.models import MainContract
    return MainContract.objects.filter(project_id=app.project_id).first()


def _submitted(app, user):
    later = app.later_submitted
    if later is not None:
        raise InvalidTransition(
            f'{later.reference}, made up to {later.valuation_date:%d %b %Y}, has already been submitted and its '
            'previous amounts do not include this one, so submitting it would claim the same money twice. Put this '
            f'work in your next application instead, or withdraw {later.reference} first if that is still allowed.')
    contract = _contract(app)
    app.recalculate(contract)
    today = timezone.localdate()
    app.set_cycle(app.live_cycle(contract, claim_date=today))
    app.submitted_at = timezone.now()
    app.visibility = VIS_SHARED
    app.due_date = app.payment_notice_by


def _payment_notice(app, user, amount=None, basis='', given_on=None, evidence=None, on_behalf=False):
    app.payment_notice_amount = rules.money(amount)
    app.payment_notice_basis = basis
    app.payment_notice_on = given_on or timezone.localdate()
    app.payment_notice_given_by = user
    app.payment_notice_on_behalf = on_behalf
    if evidence:
        app.payment_notice_evidence = evidence
    app.due_date = app.final_date


def _payment_notice_on_behalf(app, user, **params):
    _payment_notice(app, user, on_behalf=True, **params)


def _pay_less(app, user, amount=None, basis='', given_on=None, evidence=None, on_behalf=False):
    given_on = given_on or timezone.localdate()
    refusal = rules.pay_less_refusal(given_on, app.pay_less_by)
    if refusal:
        raise InvalidTransition(refusal)
    app.pay_less_amount = rules.money(amount)
    app.pay_less_basis = basis
    app.pay_less_on = given_on
    app.pay_less_given_by = user
    app.pay_less_on_behalf = on_behalf
    if evidence:
        app.pay_less_evidence = evidence
    app.due_date = app.final_date


def _pay_less_on_behalf(app, user, **params):
    _pay_less(app, user, on_behalf=True, **params)


def _paid(app, user, paid_on=None):
    app.paid_in_full_on = paid_on or timezone.localdate()
    app.due_date = None


def _settled(app, user, paid_on=None):
    last = app.payments.filter(reversed_at__isnull=True).order_by('-received_on').first()
    _paid(app, user, paid_on or (last.received_on if last else None))


def _withdrawn(app, user):
    refusal = app.withdraw_refusal
    if refusal:
        raise InvalidTransition(refusal)
    app.visibility = VIS_INTERNAL  # the payer's side no longer sees it, in the register or the index


def _reopened(app, user):
    app.paid_in_full_on = None
    app.due_date = app.final_date


def settle_if_covered(app, user, note=''):
    """After a notice: if money already received covers the new sum due, mark it paid. Returns True if it did."""
    app._paid_total = None
    if app.status in Application.AWAITING_PAYMENT and is_covered(app):
        apply_transition(app, 'settled', user, note=note or 'Covered by payments already received')
        return True
    return False


def reopen_after_reversal(app, user, note=''):
    """A reversed payment left money outstanding on a paid application: back to awaiting payment."""
    app._paid_total = None
    if app.status == Application.STATUS_PAID and app.outstanding > 0:
        target = {Application.STATUS_PAY_LESS: 'reopen_pay_less', Application.STATUS_NOTIFIED: 'reopen_notified',
                  Application.STATUS_SUBMITTED: 'reopen_submitted'}[app.awaiting_status]
        apply_transition(app, target, user, note=note)
        return True
    return False


ON_BEHALF_NOTE = 'Recorded on behalf of client'
AWAITING = Application.AWAITING_PAYMENT

TRANSITIONS = [
    Transition('submit', source=(Application.STATUS_DRAFT,), target=Application.STATUS_SUBMITTED, label='submit',
               allowed=manages, sets_ball_in_court=payer_ball, on_apply=_submitted),
    Transition('payment_notice', source=(Application.STATUS_SUBMITTED,), target=Application.STATUS_NOTIFIED,
               label='give a payment notice on', allowed=is_project_client, on_apply=_payment_notice),
    Transition('payment_notice_on_behalf', source=(Application.STATUS_SUBMITTED,), target=Application.STATUS_NOTIFIED,
               label="record the client's payment notice on", allowed=records_for_client,
               on_apply=_payment_notice_on_behalf),
    Transition('pay_less', source=(Application.STATUS_SUBMITTED, Application.STATUS_NOTIFIED),
               target=Application.STATUS_PAY_LESS, label='give a pay less notice on', allowed=is_project_client,
               on_apply=_pay_less),
    Transition('pay_less_on_behalf', source=(Application.STATUS_SUBMITTED, Application.STATUS_NOTIFIED),
               target=Application.STATUS_PAY_LESS, label="record the client's pay less notice on",
               allowed=records_for_client, on_apply=_pay_less_on_behalf),
    Transition('paid', source=AWAITING, target=Application.STATUS_PAID, label='mark as paid', allowed=manages,
               sets_ball_in_court=None, on_apply=_paid),
    # Made by the system after a notice, whoever gave it: only when payments received already cover the sum due
    Transition('settled', source=AWAITING, target=Application.STATUS_PAID, label='mark as paid', allowed=is_covered,
               sets_ball_in_court=None, on_apply=_settled),
    Transition('withdraw', source=(Application.STATUS_DRAFT, Application.STATUS_SUBMITTED),
               target=Application.STATUS_WITHDRAWN, label='withdraw', allowed=manages, sets_ball_in_court=None,
               on_apply=_withdrawn),
    *[Transition(f'reopen_{status.lower()}', source=(Application.STATUS_PAID,), target=status,
                 label='reopen after a reversed payment', allowed=records_for_client, sets_ball_in_court=None,
                 on_apply=_reopened)
      for status in (Application.STATUS_SUBMITTED, Application.STATUS_NOTIFIED, Application.STATUS_PAY_LESS)],
]

AUDIT_FIELDS = ('valuation_date', 'work_done', 'variations_value', 'materials_on_site', 'materials_off_site', 'notes',
                'retention_percent', 'retention_stage', 'reverse_charge', 'gross_valuation', 'retention',
                'previous_certified', 'amount_due', 'vat', 'due_on', 'payment_notice_by', 'pay_less_by', 'final_date',
                'payment_notice_amount', 'payment_notice_basis', 'payment_notice_on', 'payment_notice_on_behalf',
                'payment_notice_evidence', 'pay_less_amount', 'pay_less_basis', 'pay_less_on', 'pay_less_on_behalf',
                'pay_less_evidence', 'paid_in_full_on')
