"""
What happens when a submittal moves on. Each function makes the revision and file rows,
applies the transition (procurement.workflow) and moves the procurement line on, in one
transaction, then tells the people who now hold the ball. Views call these; they never set
a submittal's status by hand.
"""
from django.db import transaction

from core.exceptions import InvalidTransition
from django.utils import timezone

from core.workflow import apply_transition
from delivery.views import sub_users
from tenancy import notify

from .models import ScheduleLine, Submittal, SubmittalFile, SubmittalForward, SubmittalRevision
from .workflow import TRANSITION_FOR_CODE, reviewer_for


def _store(revision, files, role, user, forward=None):
    for f in files or ():
        SubmittalFile.objects.create(revision=revision, role=role, file=f, original_name=f.name[:200],
                                     size=getattr(f, 'size', 0) or 0, uploaded_by=user, forward=forward)


def sync_line(line):
    """
    Move a procurement line on from its submittals: to "Submittals in review" once one is in,
    and to "Approved" once every live (not withdrawn) submittal on it is A or B. Never back.
    """
    if line is None:
        return
    live = list(Submittal.objects.filter(line=line).exclude(status=Submittal.WITHDRAWN)
                .values_list('status', flat=True))
    if live and all(s in Submittal.ACCEPTED for s in live):
        line.advance_to(ScheduleLine.APPROVED)
    elif any(s in Submittal.IN_REVIEW for s in live):
        line.advance_to(ScheduleLine.IN_REVIEW)


def submit(submittal, user, files, note=''):
    """
    First submission (rev 00) or a resubmission after a C (rev 01, 02...). The submittal row is
    locked first and re-read, so two people sending at once can't both make the same revision:
    the second gets InvalidTransition (it is no longer waiting for one), or, where the database
    can't lock rows, an IntegrityError from the unique (submittal, number).
    """
    with transaction.atomic():
        list(Submittal.objects.select_for_update().filter(pk=submittal.pk).values_list('pk', flat=True))
        submittal.refresh_from_db()
        if submittal.status not in Submittal.TO_SUBMIT:
            raise InvalidTransition(f'{submittal.reference} has already been sent: it is '
                                    f'{submittal.get_status_display().lower()}.')
        name = 'resubmit' if submittal.status == Submittal.REJECTED else 'submit'
        last = submittal.revisions.order_by('-number').first()
        number = 0 if last is None else last.number + 1
        rev = SubmittalRevision.objects.create(submittal=submittal, number=number, note=note, submitted_by=user)
        _store(rev, files, SubmittalFile.SUBMISSION, user)
        submittal.revision = number
        apply_transition(submittal, name, user, note=note[:300])
        rev.review_due = submittal.due_date
        rev.save(update_fields=['review_due'])
        sync_line(submittal.line)
    who = submittal.subcontractor or submittal.organization.name
    notify.alert([submittal.ball_in_court_user], f'Submittal to review: {submittal.reference} rev {rev.label} ({who})',
                 submittal.get_absolute_url(), project=submittal.project, actor=user)
    return rev


def review(submittal, user, code, comments='', markups=()):
    """Our decision: A approved, B approved with comments, C revise and resubmit."""
    with transaction.atomic():
        rev = submittal.latest
        rev.code, rev.review_comments = code, comments
        rev.reviewed_by, rev.reviewed_at = user, timezone.now()
        rev.save(update_fields=['code', 'review_comments', 'reviewed_by', 'reviewed_at'])
        _store(rev, markups, SubmittalFile.MARKUP, user)
        apply_transition(submittal, TRANSITION_FOR_CODE[code], user, note=comments[:300])
        sync_line(submittal.line)
    if submittal.package_id:
        notify.alert(sub_users(submittal.project, submittal.package.subcontractor_id),
                     f'{submittal.reference} rev {rev.label}: {submittal.get_status_display()}',
                     submittal.get_absolute_url(), project=submittal.project, actor=user)
    return rev


def forward(submittal, user, consultant, note=''):
    """Send the latest revision to the designer for comment before we decide."""
    with transaction.atomic():
        rev = submittal.latest
        fwd = SubmittalForward.objects.create(revision=rev, consultant=consultant, forwarded_by=user, note=note)
        apply_transition(submittal, 'forward', user, note=note[:300], consultant=consultant)
    notify.alert([consultant], f'Submittal for your comment: {submittal.reference} {submittal.title}',
                 submittal.get_absolute_url(), project=submittal.project, actor=user)
    return fwd


def designer_reply(submittal, user, comments, files=()):
    """
    The designer's comments (and mark-ups) go on their own forward and back to whoever sent it,
    who gives the final status. Staff recording comments given elsewhere answer the open forward
    of whoever holds the ball.
    """
    from .access import open_forward
    with transaction.atomic():
        fwd = open_forward(submittal, user) or open_forward(submittal, submittal.ball_in_court_user)
        if fwd is None:
            raise InvalidTransition(f'{submittal.reference} is not waiting for comments.')
        fwd.comments, fwd.replied_at = comments, timezone.now()
        fwd.save(update_fields=['comments', 'replied_at'])
        _store(fwd.revision, files, SubmittalFile.CONSULTANT, user, forward=fwd)
        apply_transition(submittal, 'designer_reply', user, note=comments[:300], forward=fwd)
    notify.alert([submittal.ball_in_court_user or reviewer_for(submittal.project)],
                 f"Designer's comments on {submittal.reference}: give it a status",
                 submittal.get_absolute_url(), project=submittal.project, actor=user)
    return fwd


def withdraw(submittal, user):
    with transaction.atomic():
        apply_transition(submittal, 'withdraw', user)
        sync_line(submittal.line)


def initial_ball(submittal):
    """Who a new submittal waits on: the package's subcontractor, or our reviewer for a direct order."""
    if submittal.package_id:
        submittal.ball_in_court_company = submittal.package.subcontractor
        submittal.ball_in_court_user = None
    else:
        submittal.ball_in_court_company = None
        submittal.ball_in_court_user = reviewer_for(submittal.project)
