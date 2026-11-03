"""
Who may see and do what with meetings on a job.

  staff (anyone who can open the job)    every meeting: plan, minute, add actions, issue, correct
  owners and admins                      also the company's agenda templates
  an external person on the project      minutes that have been issued, of meetings they're listed
                                         at (attended or sent apologies); and their own actions
                                         from issued minutes, which they can update and mark done
  everyone else                          nothing (404 from get_project, or 403 here)

Every view still fetches the project with projects.access.get_project() first, so sections, archived
jobs (read only), tendering subcontractors and other companies are handled as everywhere else.
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q

from projects.access import get_project, is_internal
from projects.models import Project

from .models import Attendee, Meeting, MeetingAction

MEETING_STAGES = Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,)


def meetings_project(request, pk):
    """get_project() plus: meetings are for won, live and completed jobs."""
    project = get_project(request, pk)
    if project.stage not in MEETING_STAGES:
        raise PermissionDenied
    return project


def visible_meetings(request, project):
    qs = Meeting.objects.filter(project=project)
    if is_internal(request):
        return qs
    listed = Attendee.objects.filter(meeting=OuterRef('pk'), user=request.user)
    return qs.filter(Exists(listed), status=Meeting.STATUS_ISSUED)


def my_actions(request, project=None):
    """An external person's own actions, from minutes that have been issued."""
    qs = MeetingAction.objects.filter(owner=request.user, meeting__status=Meeting.STATUS_ISSUED,
                                      meeting__deleted_at__isnull=True)
    return qs.filter(project=project) if project is not None else qs


def can_update_action(request, action):
    if is_internal(request):
        return True
    return action.owner_id == request.user.pk and action.meeting.status == Meeting.STATUS_ISSUED


def meetings_tab(request, project):
    """
    The Meetings tab: staff on won, live and complete jobs (it sits under More); an external person
    only when there are issued minutes they're listed at or an action of theirs (under Waiting on you).
    """
    from django.urls import reverse
    if project.stage not in MEETING_STAGES:
        return None
    tab = ('meetings', 'Meetings', reverse('meeting_list', args=[request.org.slug, project.pk]), None)
    if is_internal(request):
        return tab
    listed = Attendee.objects.filter(meeting=OuterRef('pk'), user=request.user)
    owns = MeetingAction.objects.filter(meeting=OuterRef('pk'), owner=request.user)
    has = Meeting.objects.filter(project=project, status=Meeting.STATUS_ISSUED).filter(
        Q(Exists(listed)) | Q(Exists(owns))).exists()
    return tab if has else None
