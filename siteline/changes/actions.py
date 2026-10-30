"""
What the change chain is waiting on this person for, for the "Needs me" registry.

    from changes.actions import items
    items(request) -> list of dicts, most urgent first:
        {
          'kind': 'early_warning' | 'change_event',
          'ref': 'CE-004', 'title': str, 'url': str,
          'project': Project, 'project_name': str,
          'status': 'Notified',            # status label
          'due': date | None,              # the nearest deadline, if any
          'tone': 'bad' | 'warn' | 'info' | 'muted',   # as status_badge tones
          'reason': str,                   # one line: why it needs them (an advisory clock, a meeting...)
        }

Internal staff only (externals always get []). An item is someone's when they are its owner
(ball in court); with no owner set it falls to the project manager. Clock reasons come from
changes.clocks and are advisory: check your contract.
"""
from datetime import date

from django.db.models import Q
from django.utils import timezone

from contracts.models import MainContract
from projects.access import is_internal, visible_projects

from .clocks import TONE_LATE, TONE_SOON, clocks_for
from .models import ChangeEvent, EarlyWarning

_RANK = {TONE_LATE: 0, TONE_SOON: 1, 'info': 2, 'muted': 3}


def _mine(request):
    return Q(ball_in_court_user=request.user) | Q(ball_in_court_user__isnull=True, project__manager=request.user)


def items(request):
    if request.membership is None or not is_internal(request):
        return []
    today = timezone.localdate()
    projects = visible_projects(request)
    out = []
    for ew in EarlyWarning.objects.filter(_mine(request), project__in=projects, status=EarlyWarning.STATUS_OPEN) \
            .select_related('project'):
        due = min([d for d in (ew.meeting_date, ew.due_date) if d], default=None)
        tone = 'muted' if due is None else TONE_LATE if due < today else TONE_SOON if (due - today).days <= 7 else 'info'
        reason = 'Open early warning'
        if ew.meeting_date and ew.meeting_date >= today:
            reason = f'Risk reduction meeting {ew.meeting_date.day} {ew.meeting_date:%b}'
        elif due and due < today:
            reason = 'Review date passed'
        out.append(_item('early_warning', ew, due, tone, reason))
    ces = ChangeEvent.objects.filter(_mine(request), project__in=projects, status__in=ChangeEvent.LIVE_STATUSES) \
        .select_related('project')
    contracts = {c.project_id: c for c in MainContract.objects.filter(project__in={ce.project_id for ce in ces})}
    for ce in ces:
        attention = [c for c in clocks_for(ce, contracts.get(ce.project_id), today) if c.needs_attention]
        if attention:
            worst = min(attention, key=lambda c: (_RANK.get(c.tone, 9), c.deadline or today))
            out.append(_item('change_event', ce, worst.deadline, worst.tone, f'{worst.label}: {worst.message}'))
        elif ce.status == ChangeEvent.STATUS_AGREED:
            out.append(_item('change_event', ce, ce.due_date, 'info', 'Agreed: carry it out and mark it implemented'))
        else:
            out.append(_item('change_event', ce, ce.due_date, 'muted', f'{ce.get_status_display()}: keep it moving'))
    out.sort(key=lambda i: (_RANK.get(i['tone'], 9), i['due'] or date.max, i['ref']))
    return out


def _item(kind, record, due, tone, reason):
    return {'kind': kind, 'ref': record.reference, 'title': record.title, 'url': record.get_absolute_url(),
            'project': record.project, 'project_name': record.project.name, 'status': record.get_status_display(),
            'due': due, 'tone': tone, 'reason': reason}
