r"""
Submittal status changes, registered in ProcurementConfig.ready(). Views go through
procurement.services, which create the revision and files and then call
core.workflow.apply_transition(submittal, name, user) in one transaction.

  REQUIRED --submit--> SUBMITTED --forward--> WITH_CONSULTANT --designer_reply--> SUBMITTED
                          |  \                     |
                          |   +-- approve (A) / approve_comments (B) / reject (C) --+
                          v                                                         v
               APPROVED / APPROVED_COMMENTS                             REJECTED --resubmit--> SUBMITTED (rev 01, 02...)

  withdraw: staff, from any open status.

Ball in court
  REQUIRED, REJECTED   the package's subcontractor (ball_in_court_company); with no package, our reviewer
  SUBMITTED            our reviewer (ProcurementSettings.reviewer, else the project manager)
  WITH_CONSULTANT      the designer it was forwarded to
  A, B, withdrawn      nobody

Due dates
  submit / resubmit    today + our review period (working days, default 10, per job)
  forward              unchanged: the designer comments inside our review period
  reject (C)           today + the resubmission period (working days, default 10, per job)
"""
from django.utils import timezone

from core.access import record_staff
from core.workflow import Transition

from .models import ProcurementSettings, Submittal, region_for

S = Submittal


def _active_staff(user, project):
    from tenancy.models import Membership
    return user is not None and user.is_active and Membership.objects.filter(
        organization_id=project.organization_id, user=user, is_active=True,
        role__in=Membership.INTERNAL_ROLES).exists()


def reviewer_for(project):
    """
    Who reviews submittals on this job: the chosen reviewer, else the project manager, else the
    company's first owner or admin. Never the bid lead. None only if the company has nobody active.
    """
    from tenancy.models import Membership
    for user in (ProcurementSettings.for_project(project).reviewer, project.manager):
        if _active_staff(user, project):
            return user
    admin = Membership.objects.filter(organization_id=project.organization_id, role__in=Membership.ADMIN_ROLES,
                                      is_active=True, user__is_active=True).select_related('user').order_by('pk').first()
    return admin.user if admin else None


def _working_days_from_today(project, days):
    from .dates import add_working_days
    return add_working_days(timezone.localdate(), days, region_for(project))


def may_submit(record, user):
    """Staff, or a subcontractor user of the appointed package's company on this project."""
    if record_staff(record, user):
        return True
    from directory.models import Package
    from projects.models import ProjectMember
    if not record.package_id or record.package.status != Package.APPOINTED:
        return False
    return ProjectMember.objects.filter(project_id=record.project_id, user=user, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                        subcontractor_id=record.package.subcontractor_id).exists()


def may_reply_as_designer(record, user):
    return record.ball_in_court_user_id == getattr(user, 'pk', None) or record_staff(record, user)


def _to_reviewer(record, user, **params):
    record.ball_in_court_user = reviewer_for(record.project)
    record.ball_in_court_company = None
    record.due_date = _working_days_from_today(record.project, ProcurementSettings.for_project(record.project).review_days)


def _to_designer(record, user, consultant=None, **params):
    record.ball_in_court_user = consultant
    record.ball_in_court_company = None


def _back_to_reviewer(record, user, forward=None, **params):
    """Back to whoever forwarded it, else the job's reviewer."""
    sender = forward.forwarded_by if forward is not None and forward.forwarded_by_id else None
    record.ball_in_court_user = sender if _active_staff(sender, record.project) else reviewer_for(record.project)


def _closed(record, user, **params):
    record.ball_in_court_user = None
    record.ball_in_court_company = None
    record.due_date = None


def _to_subcontractor(record, user, **params):
    """Back to whoever submits it: the package's subcontractor, or our reviewer for a direct order."""
    if record.package_id:
        record.ball_in_court_company = record.package.subcontractor
        record.ball_in_court_user = None
    else:
        record.ball_in_court_company = None
        record.ball_in_court_user = reviewer_for(record.project)
    record.due_date = _working_days_from_today(record.project,
                                               ProcurementSettings.for_project(record.project).resubmit_days)


REVIEW_SOURCES = (S.SUBMITTED, S.WITH_CONSULTANT)

TRANSITIONS = [
    Transition('submit', source=(S.REQUIRED,), target=S.SUBMITTED, label='submit', allowed=may_submit,
               on_apply=_to_reviewer),
    Transition('resubmit', source=(S.REJECTED,), target=S.SUBMITTED, label='resubmit', allowed=may_submit,
               on_apply=_to_reviewer),
    Transition('forward', source=(S.SUBMITTED,), target=S.WITH_CONSULTANT, label='forward to the designer',
               allowed=record_staff, on_apply=_to_designer),
    Transition('designer_reply', source=(S.WITH_CONSULTANT,), target=S.SUBMITTED, label='send comments back',
               allowed=may_reply_as_designer, on_apply=_back_to_reviewer),
    Transition('approve', source=REVIEW_SOURCES, target=S.APPROVED, label='approve (A)', allowed=record_staff,
               on_apply=_closed),
    Transition('approve_comments', source=REVIEW_SOURCES, target=S.APPROVED_COMMENTS,
               label='approve with comments (B)', allowed=record_staff, on_apply=_closed),
    Transition('reject', source=REVIEW_SOURCES, target=S.REJECTED, label='reject: revise and resubmit (C)',
               allowed=record_staff, on_apply=_to_subcontractor),
    Transition('withdraw', source=S.OPEN_STATUSES, target=S.WITHDRAWN, label='withdraw', allowed=record_staff,
               on_apply=_closed),
]

TRANSITION_FOR_CODE = {'A': 'approve', 'B': 'approve_comments', 'C': 'reject'}

AUDIT_FIELDS = ('kind', 'line', 'package', 'spec_reference', 'description', 'revision')
