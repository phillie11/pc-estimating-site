r"""
Status changes for the three quality registers, registered in QualityConfig.ready(). Views call
core.workflow.apply_transition(record, name, user, note=..., **params); nothing sets status by hand.
`note` goes in the audit trail; what the record keeps (a closing or inspection note) is `remark`.

Inspections (INS-)
  IN_PROGRESS --pass--> PASSED          every check answered, none failed (rules.inspection_outcome)
              --fail--> FAILED          every check answered, at least one failed
              --cancel--> CANCELLED
  PASSED / FAILED --reopen--> IN_PROGRESS   the project manager or an admin, with a reason
  Ball in court: whoever is doing it (the inspector) while in progress; due: the "inspect by" date.

Permits to work (PTW-)
  REQUESTED --approve--> APPROVED       the PM or an admin, confirming every precaution for the type
            --refuse--> REFUSED         the PM or an admin, with a reason
                                        Never the person who asked for it: a PM's own permit goes to
                                        an owner or admin (pending owner decision)
  APPROVED  --close--> CLOSED           anyone on the job: work finished and the area left safe
                                        (hot works: the fire watch done)
  REQUESTED / APPROVED --withdraw--> WITHDRAWN   approved: only before work starts (after that, close it)
  Ball in court: the approver while requested (due the day work starts); whoever asked for it once
  approved (due the day it runs out). Active / expiring / expired are worked out from the clock.

Fire-stopping penetrations (FS-)
  TO_SEAL --seal--> SEALED --sign_off--> SIGNED_OFF     sign-off needs the system and an "after" photo
                                                          (taken since it last failed or was reopened)
  SEALED --fail--> FAILED --reseal--> SEALED            failing needs a note of what's wrong
  SIGNED_OFF --reopen--> TO_SEAL        the PM or an admin, e.g. the seal was disturbed by later work
  seal / reseal: anyone on the job, or the appointed subcontractor whose package it is (the portal,
  quality/portal.py). sign_off: anyone on the job except whoever recorded the seal (sealed_by), while
  the company keeps "Someone else signs off fire-stopping" on (the default; pending owner decision).
  Ball in court: the PM (else an admin; never the person who sealed it while that setting is on) to
  inspect once sealed; whoever recorded it once failed (the PM when that was a subcontractor).
"""
from django.utils import timezone

from core.exceptions import InvalidTransition
from core.workflow import Transition

from . import rules
from .access import can_approve_permit, is_pm_or_admin, user_on_job
from .models import Inspection, Penetration, Permit, QualityPhoto


def _active_staff(user, project):
    from tenancy.models import Membership
    return user is not None and user.is_active and Membership.objects.filter(
        organization_id=project.organization_id, user=user, is_active=True,
        role__in=Membership.INTERNAL_ROLES).exists()


def approver_for(project):
    """Who approves permits and signs off fire-stopping: the project manager, else the first owner or admin."""
    from tenancy.models import Membership
    if _active_staff(project.manager, project):
        return project.manager
    admin = Membership.objects.filter(organization_id=project.organization_id, role__in=Membership.ADMIN_ROLES,
                                      is_active=True, user__is_active=True).select_related('user').order_by('pk').first()
    return admin.user if admin else None


def permit_approver_for(project, requester):
    """
    Who approves a permit: never the person who asked for it (pending owner decision). The project
    manager, else the first owner or admin who isn't the requester; None if there is no one else.
    """
    from tenancy.models import Membership
    requester_id = getattr(requester, 'pk', None)
    if _active_staff(project.manager, project) and project.manager_id != requester_id:
        return project.manager
    admin = Membership.objects.filter(organization_id=project.organization_id, role__in=Membership.ADMIN_ROLES,
                                      is_active=True, user__is_active=True).exclude(user_id=requester_id) \
        .select_related('user').order_by('pk').first()
    return admin.user if admin else None


def on_job(record, user):
    return user_on_job(user, record.project)


def pm_or_admin(record, user):
    return is_pm_or_admin(user, record.project)


def is_installer(record, user):
    """The appointed subcontractor whose package the record is on: one of that company's people on this job."""
    from directory.models import Package
    from projects.models import ProjectMember
    if user is None or not record.package_id or record.package.status != Package.APPOINTED:
        return False
    return ProjectMember.objects.filter(project_id=record.project_id, user=user, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                        subcontractor_id=record.package.subcontractor_id).exists()


def on_job_or_installer(record, user):
    return on_job(record, user) or is_installer(record, user)


def independent_sign_off(project):
    from .models import QualitySettings
    return QualitySettings.for_org(project.organization).fs_independent_sign_off


# --- inspections -----------------------------------------------------------------------

def _finished(record, user, **params):
    record.completed_at, record.completed_by = timezone.now(), user
    record.ball_in_court_user = None


def _cancelled(record, user, **params):
    record.ball_in_court_user = None


def _reopened(record, user, **params):
    record.completed_at, record.completed_by = None, None
    record.ball_in_court_user = user


I = Inspection
INSPECTION_TRANSITIONS = [
    Transition('pass', source=(I.IN_PROGRESS,), target=I.PASSED, label='finish: passed', allowed=on_job,
               on_apply=_finished),
    Transition('fail', source=(I.IN_PROGRESS,), target=I.FAILED, label='finish: failed', allowed=on_job,
               on_apply=_finished),
    Transition('cancel', source=(I.IN_PROGRESS,), target=I.CANCELLED, label='cancel', allowed=on_job,
               on_apply=_cancelled),
    Transition('reopen', source=(I.PASSED, I.FAILED), target=I.IN_PROGRESS, label='reopen', allowed=pm_or_admin,
               on_apply=_reopened),
]
INSPECTION_AUDIT_FIELDS = ('template', 'area', 'package', 'notes', 'completed_at')


# --- permits ---------------------------------------------------------------------------

def may_approve(record, user):
    """The PM or an owner or admin, and never whoever asked for it (pending owner decision)."""
    if record.created_by_id is not None and record.created_by_id == getattr(user, 'pk', None):
        return False
    return can_approve_permit(user, record.project)


def _approved(record, user, precautions=(), extra='', **params):
    required = rules.precautions_for(record.permit_type)
    if set(required) - set(precautions):
        raise InvalidTransition('Confirm every precaution is in place before approving the permit.')
    problem = rules.validity_problem(record.permit_type, record.starts_at, record.expires_at)
    if problem:
        raise InvalidTransition(problem)
    if record.expires_at <= timezone.now():
        raise InvalidTransition('This permit has already run out. Change the times, or ask for a new permit.')
    record.precautions = required
    record.extra_precautions = extra or record.extra_precautions
    record.approved_by, record.approved_at = user, timezone.now()
    holder = record.created_by if _active_staff(record.created_by, record.project) else user
    record.ball_in_court_user = holder


def _refused(record, user, reason='', **params):
    if not reason.strip():
        raise InvalidTransition('Say why the permit is refused, so it can be put right.')
    record.refusal_reason = reason.strip()
    record.ball_in_court_user = None


def _closed(record, user, remark='', fire_watch_minutes=None, **params):
    record.closed_by, record.closed_at, record.close_note = user, timezone.now(), (remark or '').strip()
    if record.is_hot_works:
        record.fire_watch_minutes = fire_watch_minutes
    record.ball_in_court_user = None


def _withdrawn(record, user, **params):
    # Withdrawing skips the closing checks, so it is only for a permit whose work hasn't started.
    # (record.status is already the target here; approved_at says it was approved.)
    if record.approved_at is not None and timezone.now() >= record.starts_at:
        raise InvalidTransition(f'Work under {record.reference} has started, so it can\'t be withdrawn. '
                                'When the work is finished, close it instead: confirm the area is safe'
                                f'{" and the fire watch is done" if record.is_hot_works else ""}.')
    record.ball_in_court_user = None


P = Permit
PERMIT_TRANSITIONS = [
    Transition('approve', source=(rules.REQUESTED,), target=rules.APPROVED, label='approve', allowed=may_approve,
               on_apply=_approved),
    Transition('refuse', source=(rules.REQUESTED,), target=rules.REFUSED, label='refuse', allowed=may_approve,
               on_apply=_refused),
    Transition('close', source=(rules.APPROVED,), target=rules.CLOSED, label='close', allowed=on_job,
               on_apply=_closed),
    Transition('withdraw', source=P.OPEN_STATUSES, target=rules.WITHDRAWN, label='withdraw', allowed=on_job,
               on_apply=_withdrawn),
]
PERMIT_AUDIT_FIELDS = ('permit_type', 'location', 'package', 'holder', 'starts_at', 'expires_at', 'method_notes',
                       'precautions', 'extra_precautions', 'approved_by', 'refusal_reason', 'close_note',
                       'fire_watch_minutes')


# --- fire-stopping ---------------------------------------------------------------------

def inspector_for(project, sealer):
    """Who inspects a seal: the approver, but never the person who sealed it while the company wants that."""
    if independent_sign_off(project):
        return permit_approver_for(project, sealer)
    return approver_for(project)


def may_sign_off(record, user):
    if not on_job(record, user):
        return False
    return rules.own_seal_problem(record.sealed_by_id, getattr(user, 'pk', None),
                                  independent_sign_off(record.project)) is None


def _sealed(record, user, installed_on=None, **params):
    record.installed_on = installed_on or record.installed_on or timezone.localdate()
    record.sealed_by = user
    record.ball_in_court_user = inspector_for(record.project, user)


def _signed_off(record, user, remark='', **params):
    own = rules.own_seal_problem(record.sealed_by_id, user.pk, independent_sign_off(record.project))
    if own:
        raise InvalidTransition(own)
    after = QualityPhoto.objects.filter(penetration=record, stage=QualityPhoto.AFTER)
    if record.inspected_at is not None:
        # Failed or reopened since: only a photo of the seal as it is now counts, not the earlier one
        after = after.filter(uploaded_at__gt=record.inspected_at)
    problem = rules.sign_off_problem(after.count(), record.system)
    if problem and record.inspected_at is not None and (record.system or '').strip():
        problem = ('Add a photo of the seal as it is now ("after") before signing it off. The earlier photos '
                   'show it before it was failed or reopened.')
    if problem:
        raise InvalidTransition(problem)
    record.inspected_by, record.inspected_at, record.inspection_note = user, timezone.now(), (remark or '').strip()
    record.ball_in_court_user = None


def _failed(record, user, remark='', **params):
    if not (remark or '').strip():
        raise InvalidTransition('Say what is wrong with the seal, so it can be put right.')
    record.inspected_by, record.inspected_at, record.inspection_note = user, timezone.now(), remark.strip()
    owner = record.created_by if _active_staff(record.created_by, record.project) else approver_for(record.project)
    record.ball_in_court_user = owner


def _reopened_penetration(record, user, remark='', **params):
    if not (remark or '').strip():
        raise InvalidTransition('Say why it needs sealing again, e.g. disturbed by later work.')
    record.inspection_note = remark.strip()
    record.ball_in_court_user = None


F = Penetration
PENETRATION_TRANSITIONS = [
    Transition('seal', source=(F.TO_SEAL,), target=F.SEALED, label='mark as sealed', allowed=on_job_or_installer,
               on_apply=_sealed),
    Transition('reseal', source=(F.FAILED,), target=F.SEALED, label='mark as resealed', allowed=on_job_or_installer,
               on_apply=_sealed),
    Transition('sign_off', source=(F.SEALED,), target=F.SIGNED_OFF, label='sign off', allowed=may_sign_off,
               on_apply=_signed_off),
    Transition('fail', source=(F.SEALED,), target=F.FAILED, label='fail the inspection', allowed=on_job,
               on_apply=_failed),
    Transition('reopen', source=(F.SIGNED_OFF,), target=F.TO_SEAL, label='reopen', allowed=pm_or_admin,
               on_apply=_reopened_penetration),
]
PENETRATION_AUDIT_FIELDS = ('location', 'element', 'fire_rating', 'service', 'size', 'system', 'detail_reference',
                            'package', 'installer', 'installed_on', 'notes', 'drawing', 'pin_page', 'sealed_by',
                            'inspected_by', 'inspection_note')
