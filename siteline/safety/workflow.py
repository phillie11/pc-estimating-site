"""
Incident status changes, registered in SafetyConfig.ready().

  REPORTED      --investigate (PM or admin)-------------------> INVESTIGATING  (ball: who starts it)
  REPORTED or
  INVESTIGATING --close (PM or admin; findings written, no open
                  actions, RIDDOR reported if reportable, and
                  answered if a worker was off > 7 days)-------> CLOSED         (ball: nobody)
  CLOSED        --reopen (PM or admin, with a reason)----------> INVESTIGATING  (ball: who reopens it)

Reporting puts the ball with the job's project manager (or whoever reported it, if the job has
no manager; for a subcontractor's report from the portal, the first owner or admin) and sets the
due date to the company's investigation target (SafetySettings.investigation_days, default
rules.INVESTIGATE_WITHIN_DAYS), so open incidents appear on that person's Needs me through the
record index.
"""
from django.utils import timezone

from core.exceptions import InvalidTransition
from core.workflow import Transition

from . import rules
from .models import Incident as I


def may_manage(incident, user):
    from .access import is_pm_or_admin
    return is_pm_or_admin(user, incident.project)


def close_problems(incident):
    """What stops an incident being closed, in plain words (empty list: it can be closed)."""
    problems = []
    if not (incident.findings or '').strip():
        problems.append('Write up the findings and lessons learned.')
    if not incident.riddor_category and incident.riddor_suggestion == rules.RIDDOR_OVER_7:
        # The record itself says a worker was off for more than 7 days: that is reportable, so don't let it
        # close with the RIDDOR question unanswered (advisory, like every RIDDOR rule)
        problems.append('A worker was off for more than 7 days, so this may need reporting under RIDDOR. '
                        'Answer the RIDDOR question (or correct the days off).')
    if incident.riddor_outstanding:
        problems.append('Record when it was reported to HSE under RIDDOR (or change the RIDDOR answer).')
    if incident.actions.filter(done_at__isnull=True).exists():
        problems.append('Finish the open actions, or remove any that are no longer needed.')
    return problems


def _closing(incident, user, **params):
    problems = close_problems(incident)
    if problems:
        raise InvalidTransition(' '.join(problems))
    incident.closed_at, incident.closed_by, incident.due_date = timezone.now(), user, None


def _reopening(incident, user, **params):
    incident.closed_at, incident.closed_by = None, None


def _actor(incident, user):
    return user


TRANSITIONS = [
    Transition('investigate', source=(I.STATUS_REPORTED,), target=I.STATUS_INVESTIGATING,
               label='start the investigation', allowed=may_manage, sets_ball_in_court=_actor),
    Transition('close', source=(I.STATUS_REPORTED, I.STATUS_INVESTIGATING), target=I.STATUS_CLOSED,
               label='close', allowed=may_manage, sets_ball_in_court=None, on_apply=_closing),
    Transition('reopen', source=(I.STATUS_CLOSED,), target=I.STATUS_INVESTIGATING, label='reopen',
               allowed=may_manage, sets_ball_in_court=_actor, on_apply=_reopening),
]

AUDIT_FIELDS = ('kind', 'description', 'location', 'occurred_at', 'anyone_hurt', 'injured_name', 'injured_company',
                'injured_is_worker', 'injury_details', 'first_aid_given', 'taken_to_hospital', 'days_off',
                'riddor_category', 'riddor_reported_on', 'riddor_reference', 'immediate_action', 'root_cause',
                'findings', 'closed_at')
