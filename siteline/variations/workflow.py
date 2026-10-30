"""
Client variation status changes. Registered in VariationsConfig.ready(); views call
core.workflow.apply_transition(vo, name, user, **params).

  DRAFT --submit--> SUBMITTED --approve--> APPROVED
                              --reject---> REJECTED
  DRAFT / SUBMITTED --withdraw--> WITHDRAWN

Who may:
  submit, withdraw   staff of the organisation.
  approve, reject    the client on the project (a ProjectMember with the client role), in their
                     portal. Owners and admins may also record a decision the client gave by email
                     or verbally; the view makes them say how in the notes. Nobody else: not
                     project staff, consultants or subcontractors.

Submitting shares the variation with the project's clients and consultants (drafts are
internal) and passes the ball to the client: the contract's administrator when that is a
client on the project, otherwise the first client invited. Deciding or withdrawing clears it.
"""
from django.utils import timezone

from core.access import is_admin, is_staff, record_staff
from core.models import VIS_SHARED
from core.workflow import Transition

from .models import Variation


def is_project_client(vo, user):
    """The user is a client on the variation's project, with an active membership of the organisation."""
    from projects.models import ProjectMember
    from tenancy.models import Membership
    if not getattr(user, 'is_authenticated', False):
        return False
    return (ProjectMember.objects.filter(project_id=vo.project_id, user=user, role=ProjectMember.ROLE_CLIENT).exists()
            and Membership.objects.filter(organization_id=vo.organization_id, user=user, is_active=True).exists()
            and not is_staff(user, vo.organization_id))


def records_client_decision(vo, user):
    """An owner or admin recording a decision the client gave outside the portal (notes required by the view)."""
    return is_admin(user, vo.organization_id)


def may_decide(vo, user):
    return is_project_client(vo, user) or records_client_decision(vo, user)


def client_ball(vo, user=None):
    """Who has the ball while the client decides: their contract administrator if a client, else the first client."""
    from contracts.models import MainContract
    from projects.models import ProjectMember
    contract = MainContract.objects.filter(project_id=vo.project_id).select_related('administrator').first()
    if contract and contract.administrator and contract.administrator.role == ProjectMember.ROLE_CLIENT:
        return contract.administrator.user
    first = ProjectMember.objects.filter(project_id=vo.project_id, role=ProjectMember.ROLE_CLIENT) \
        .select_related('user').order_by('pk').first()
    return first.user if first else None


def _submitted(vo, user):
    vo.submitted_at = timezone.now()
    vo.visibility = VIS_SHARED


def _decided(vo, user, approved_value=None, client_reference='', decision_notes=''):
    if vo.status == Variation.STATUS_APPROVED:
        vo.approved_value = approved_value if approved_value is not None else vo.value
    else:
        vo.approved_value = None
    vo.client_reference = client_reference or vo.client_reference
    vo.decision_notes = decision_notes
    vo.decided_at, vo.decided_by = timezone.now(), user


TRANSITIONS = [
    Transition('submit', source=(Variation.STATUS_DRAFT,), target=Variation.STATUS_SUBMITTED, label='submit',
               allowed=record_staff, sets_ball_in_court=client_ball, on_apply=_submitted),
    Transition('approve', source=(Variation.STATUS_SUBMITTED,), target=Variation.STATUS_APPROVED, label='approve',
               allowed=may_decide, sets_ball_in_court=None, on_apply=_decided),
    Transition('reject', source=(Variation.STATUS_SUBMITTED,), target=Variation.STATUS_REJECTED, label='reject',
               allowed=may_decide, sets_ball_in_court=None, on_apply=_decided),
    Transition('withdraw', source=(Variation.STATUS_DRAFT, Variation.STATUS_SUBMITTED), target=Variation.STATUS_WITHDRAWN,
               label='withdraw', allowed=record_staff, sets_ball_in_court=None),
]

# Audited on top of the RecordBase fields (title, status, visibility, due date, ball in court...)
AUDIT_FIELDS = ('description', 'cause', 'value', 'approved_value', 'time_impact_days', 'eot_days_requested',
                'eot_days_granted', 'quotation_due', 'proceed_at_risk', 'client_reference', 'rfi', 'attachment',
                'decision_notes')
