"""
"Needs me" for people outside the company (registered as a HookProvider in MeetingsConfig.ready()).

    items(request) -> [actions.Item]

An external person's open actions from minutes that have been issued, due on the action's due date,
linking to their "Meetings" page in the portal where their own actions are listed and can be marked
done. Actions in minutes not yet issued wait until they are (that's when the owner is told).

Actions under an agenda heading marked internal only never reach anyone here (access.SHARED_ACTIONS).

Staff don't come through here: meetings and actions are on the record base, so the chair's "issue
the minutes" and each owner's open actions reach Needs me through the record index (the ball is in
their court), with no extra query behind the badge. Projects outside portal_projects are dropped by
the HookProvider, so archived jobs never show.
"""
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from projects.access import is_internal

from .access import SHARED_ACTIONS
from .models import Meeting, MeetingAction


def items(request):
    if request.membership is None or is_internal(request):
        return []
    org = request.org.slug
    today = timezone.localdate()
    mine = MeetingAction.objects.filter(owner=request.user, status=MeetingAction.OPEN,
                                        meeting__status=Meeting.STATUS_ISSUED, meeting__deleted_at__isnull=True,
                                        project__archived_at__isnull=True).filter(SHARED_ACTIONS) \
        .select_related('meeting', 'project')
    return [Item(
        kind='meeting_action', ref=a.reference, title=a.title, project=a.project, due=a.due_date,
        url=reverse('meeting_list', args=[org, a.project_id]) + f'#action-{a.pk}',
        status_label=a.status_label, tone=a.status_tone,
        priority=PRIORITY_HIGH if a.due_date and a.due_date < today else PRIORITY_NORMAL,
        action=MeetingAction.NEEDS_ME_ACTION, detail=f'From {a.meeting.title}', kind_label=MeetingAction.NEEDS_ME_LABEL,
        key=f'mtg-action:{a.pk}') for a in mine]
