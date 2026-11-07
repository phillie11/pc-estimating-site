"""
The Meetings tab. Access rules are in meetings/access.py; every view starts with get_project()
(through meetings_project), so other companies get a 404 and archived jobs are read only.
"""
import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from projects.access import is_internal, posted_id, require_internal
from projects.views import project_context
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import services
from .access import SHARED_ACTIONS, can_update_action, meetings_project, my_actions, shared_items, visible_meetings
from .forms import (ActionForm, ActionUpdateForm, AgendaTemplateForm, AttendeesForm, MeetingDetailsForm, MeetingForm,
                    MeetingSettingsForm)
from .models import KIND_CHOICES, AgendaTemplate, Attendee, Meeting, MeetingAction, MeetingSettings

TRANSITION_BUTTONS = {
    'issue': ('Issue the minutes', 'primary'),
    'correct': ('Correct the minutes', ''),
    'cancel': ('Cancel this meeting', 'ghost'),
    'reinstate': ('Reinstate', ''),
}


def _safe(value):
    try:
        from core.exports import safe  # Stream F moves the shared helper here
    except ImportError:
        from levelling.exports import safe
    return safe(value)


def _ctx(request, project, sub, **extra):
    internal = is_internal(request)
    ctx = project_context(request, project, 'meetings', **extra)
    if internal:
        org, pk = request.org.slug, project.pk
        ctx['meetings_nav'] = [('meetings', 'Meetings', reverse('meeting_list', args=[org, pk])),
                               ('actions', 'Actions', reverse('meeting_actions', args=[org, pk]))]
    ctx['meetings_sub'] = sub
    return ctx


def _back(request, default):
    nxt = request.POST.get('next') or request.GET.get('next') or ''
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default


# --- the register (staff) and "your meetings" (externals) --------------------------------

def meeting_list(request, org_slug, pk):
    project = meetings_project(request, pk)
    if not is_internal(request):
        return _external_list(request, project)
    meetings = visible_meetings(request, project).select_related('chair').order_by('-held_on', '-number').annotate(
        n_actions=Count('actions', filter=Q(actions__deleted_at__isnull=True), distinct=True),
        n_open=Count('actions', filter=Q(actions__status=MeetingAction.OPEN, actions__deleted_at__isnull=True),
                     distinct=True))
    kind, status, q = request.GET.get('kind', ''), request.GET.get('status', ''), request.GET.get('q', '').strip()
    if kind in dict(KIND_CHOICES):
        meetings = meetings.filter(kind=kind)
    if status in dict(Meeting.STATUS_CHOICES):
        meetings = meetings.filter(status=status)
    if q:
        meetings = meetings.filter(Q(title__icontains=q) | Q(ref__icontains=q) | Q(location__icontains=q))
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-meetings.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Title', 'Kind', 'Date', 'Time', 'Where', 'Chaired by', 'Status', 'Actions', 'Open actions'])
        for m in meetings:
            w.writerow([_safe(v) for v in (m.ref, m.title, m.get_kind_display(), m.held_on.isoformat(), m.starts_at,
                                           m.location, m.chair.full_name if m.chair else '', m.get_status_display(),
                                           str(m.n_actions), str(m.n_open))])
        return resp
    page = Paginator(meetings, 50).get_page(request.GET.get('page'))
    open_actions = MeetingAction.objects.filter(project=project, status=MeetingAction.OPEN,
                                                meeting__deleted_at__isnull=True)
    counts = {'open': open_actions.count(),
              'overdue': open_actions.filter(due_date__lt=timezone.localdate()).count()}
    return render(request, 'meetings/list.html', _ctx(
        request, project, 'meetings', page=page, meetings=page.object_list, kinds=KIND_CHOICES,
        statuses=Meeting.STATUS_CHOICES, filters={'kind': kind, 'status': status, 'q': q},
        filtered=bool(kind or status or q), counts=counts))


def _external_list(request, project):
    minutes = visible_meetings(request, project).select_related('chair')
    actions = my_actions(request, project).select_related('meeting').order_by('status', 'due_date', 'pk')
    return render(request, 'meetings/external.html', _ctx(
        request, project, 'meetings', minutes=minutes, actions=actions, update_form=ActionUpdateForm()))


# --- planning a meeting ---------------------------------------------------------------

def meeting_create(request, org_slug, pk):
    project = meetings_project(request, pk)
    require_internal(request)
    groups = services.people(project, also=[request.user])
    form = MeetingForm(request.POST or None, groups=groups, initial={
        'kind': request.GET.get('kind') if request.GET.get('kind') in dict(KIND_CHOICES) else 'PROGRESS'})
    if request.method == 'GET':
        form.fields['attendees'].initial = list(services.default_attendees(project, form['kind'].value(), request.user))
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        m = services.create(project, request.user, kind=d['kind'], held_on=d['held_on'], starts_at=d['starts_at'],
                            location=d['location'], attendees=list(d['attendees']), others=d['others'])
        log_activity(request, 'planned', m.title, project=project, url=m.get_absolute_url(), internal_only=True)
        n = m.brought_forward.count()
        messages.success(request, f'{m.ref} set up with the standard agenda'
                                  + (f' and {n} open action{"s" if n != 1 else ""} brought forward.' if n else '.'))
        return redirect(m)
    return render(request, 'meetings/form.html', _ctx(request, project, 'meetings', form=form, groups=groups))


def _meeting(request, project, meeting_id):
    return get_object_or_404(visible_meetings(request, project), pk=meeting_id)


def meeting_detail(request, org_slug, pk, meeting_id):
    project = meetings_project(request, pk)
    meeting = _meeting(request, project, meeting_id)
    internal = is_internal(request)
    if request.method == 'POST':
        require_internal(request)
        return _post(request, project, meeting)
    items = list(shared_items(request, meeting).prefetch_related(
        Prefetch('actions', queryset=MeetingAction.objects.select_related('owner', 'meeting'))))
    raised = meeting.actions.select_related('owner', 'item')
    brought = meeting.brought_forward.select_related('owner', 'meeting', 'item').order_by('meeting__held_on', 'number')
    if not internal:  # externals: only actions from minutes that were issued, never a draft's, never internal ones
        brought = brought.filter(meeting__status=Meeting.STATUS_ISSUED).filter(SHARED_ACTIONS)
        raised = raised.filter(SHARED_ACTIONS)
    raised = list(raised)
    brought = list(brought)
    attendees = list(meeting.attendees.select_related('user'))
    ctx = {'meeting': meeting, 'items': items, 'raised': raised, 'general': [a for a in raised if a.item_id is None],
           'brought': brought, 'attendees': attendees, 'editable': internal and meeting.is_planned,
           'today': timezone.localdate()}
    if internal:
        ctx.update(
            action_form=ActionForm(meeting=meeting, prefix='act') if meeting.is_planned else None,
            internal_headings=sum(1 for i in items if i.internal_only),
            buttons=[(t.name, *TRANSITION_BUTTONS.get(t.name, (t.label, '')))
                     for t in available_transitions(meeting, request.user)],
            recipients=len([1 for p, _, _ in services.recipients(meeting, attendees, raised + brought)
                            if p.pk != request.user.pk]),
            update_form=ActionUpdateForm())
    return render(request, 'meetings/detail.html', _ctx(request, project, 'meetings', **ctx))


def _post(request, project, meeting):
    org, pk = request.org.slug, project.pk
    here = reverse('meeting_detail', args=[org, pk, meeting.pk])
    what = request.POST.get('action', '')
    if what in TRANSITION_BUTTONS:
        try:
            if what == 'issue':
                sent = services.issue(meeting, request.user)
                log_activity(request, 'issued the minutes of', meeting.title, project=project, url=here,
                             internal_only=True)
                told = (f' {sent} {"person was" if sent == 1 else "people were"} emailed a link to them, and each '
                        'action is on its owner\'s Needs me.') if sent else ' Nobody listed has a login, so no one was emailed.'
                messages.success(request, f'{meeting.ref} minutes issued.{told}')
            else:
                apply_transition(meeting, what, request.user, note=request.POST.get('note', '')[:255])
                messages.success(request, {
                    'correct': f'{meeting.ref} is open for corrections. Issue the minutes again to send the corrected '
                               'version to everyone.',
                    'cancel': f'{meeting.ref} cancelled. Its actions stay open and carry forward to the next meeting.',
                    'reinstate': f'{meeting.ref} reinstated.'}[what])
        except InvalidTransition as e:
            messages.error(request, str(e))
        return redirect(here)
    if not meeting.is_planned:
        messages.error(request, 'These minutes have been issued. Choose "Correct the minutes" to change them.')
        return redirect(here)
    if what == 'minutes':
        posted = request.POST.get('version', '')
        if posted.isdigit() and int(posted) != meeting.version:
            messages.error(request, 'Someone else saved these minutes since you opened the page. Your changes '
                                    'weren\'t saved: reload, check theirs, then add yours again.')
            return redirect(here + '#minutes')
        internal = {int(x) for x in request.POST.getlist('internal') if x.isdigit()}
        for item in meeting.items.all():
            text = request.POST.get(f'item-{item.pk}')
            changed = []
            if text is not None and text != item.minutes:
                item.minutes = text[:20000]
                changed.append('minutes')
            # The tick boxes are only on the form when it lists them (older pages post none: leave them)
            if request.POST.get('internal_listed') and item.internal_only != (item.pk in internal):
                item.internal_only = item.pk in internal
                changed.append('internal_only')
            if changed:
                item.save(update_fields=changed)
        notes = request.POST.get('notes')
        present = {int(x) for x in request.POST.getlist('present') if x.isdigit()}
        for att in meeting.attendees.all():
            if att.present != (att.pk in present):
                att.present = att.pk in present
                att.save(update_fields=['present'])
        if notes is not None and notes != meeting.notes:
            meeting.notes = notes[:20000]
        try:
            meeting.save(actor=request.user, audit_action='minutes written')
        except RecordConflict:
            messages.error(request, 'Someone else changed this meeting at the same time. Check the minutes and save again.')
            return redirect(here)
        messages.success(request, f'{meeting.ref} minutes saved. Nothing is sent until you issue them.')
        return redirect(here + '#minutes')
    if what == 'add_action':
        form = ActionForm(request.POST, meeting=meeting, prefix='act', staff_ids=services._staff_ids(meeting))
        if form.is_valid():
            a = form.save(commit=False)
            a.meeting, a.project = meeting, project
            a.save(actor=request.user)
            messages.success(request, f'Action {a.reference} added. {a.who} gets it on Needs me once the minutes '
                                      'are issued.' if a.owner_id else f'Action {a.reference} added.')
            return redirect(here + f'#action-{a.pk}')
        messages.error(request, 'The action wasn\'t added: ' + '; '.join(
            f'{form.fields[f].label if f in form.fields else "Action"}: {e[0]}' for f, e in form.errors.items()))
        return redirect(here + '#add-action')
    if what == 'remove_action':
        a = get_object_or_404(MeetingAction, meeting=meeting, pk=posted_id(request, 'action_id'))
        try:
            a.soft_delete(request.user, note='Removed before the minutes were issued')
        except PermissionDenied:
            messages.error(request, f'Only whoever added {a.reference}, or an admin, can remove it.')
            return redirect(here + '#actions')
        messages.success(request, f'{a.reference} removed.')
        return redirect(here + '#actions')
    messages.error(request, 'Nothing was changed. Use one of the buttons on the page.')
    return redirect(here)


def meeting_edit(request, org_slug, pk, meeting_id):
    project = meetings_project(request, pk)
    require_internal(request)
    meeting = _meeting(request, project, meeting_id)
    if not meeting.is_planned:  # issued (or cancelled) minutes change only through "Correct the minutes"
        messages.error(request, 'These minutes have been issued. Choose "Correct the minutes" to change them.'
                       if meeting.is_issued else 'This meeting is cancelled. Reinstate it to change it.')
        return redirect(meeting)
    current = [a.user for a in meeting.attendees.select_related('user') if a.user_id]
    groups = services.people(project, also=current + [request.user])
    form = MeetingDetailsForm(request.POST or None, instance=meeting, project=project)
    people = AttendeesForm(request.POST or None, groups=groups, prefix='p', initial={
        'attendees': [u.pk for u in current],
        'others': '\n'.join(f'{a.name}, {a.company}' if a.company else a.name
                            for a in meeting.attendees.filter(user__isnull=True))})
    if request.method == 'POST':
        if form.is_valid() and people.is_valid():
            try:
                form.instance.save(actor=request.user)
            except RecordConflict:
                form.add_error(None, form.CONFLICT_MESSAGE)
            else:
                _sync_attendees(meeting, people.cleaned_data.get('attendees') or [], people.cleaned_data.get('others', ''))
                messages.success(request, 'Meeting details saved.')
                return redirect(meeting)
    return render(request, 'meetings/edit.html', _ctx(request, project, 'meetings', meeting=meeting, form=form,
                                                      people=people))


def _sync_attendees(meeting, users, others):
    wanted = {u.pk for u in users}
    for att in meeting.attendees.filter(user__isnull=False):
        if att.user_id not in wanted:
            att.delete()
    have = set(meeting.attendees.filter(user__isnull=False).values_list('user_id', flat=True))
    for u in users:
        if u.pk not in have:
            Attendee.objects.create(meeting=meeting, user=u)
    old = {(a.name, a.company): a for a in meeting.attendees.filter(user__isnull=True)}
    new = services.parse_others(others)
    for key, att in old.items():
        if key not in new:
            att.delete()
    for name, company in new:
        if (name, company) not in old:
            Attendee.objects.create(meeting=meeting, name=name, company=company)


# --- actions --------------------------------------------------------------------------

def action_update(request, org_slug, pk, action_id):
    project = meetings_project(request, pk)
    if request.method != 'POST':
        return redirect('meeting_actions', org_slug=org_slug, pk=pk)
    qs = MeetingAction.objects.filter(project=project).select_related('meeting')
    if not is_internal(request):
        qs = my_actions(request, project)
    action = get_object_or_404(qs, pk=action_id)
    if not can_update_action(request, action):
        raise PermissionDenied
    form = ActionUpdateForm(request.POST)
    default = reverse('meeting_actions' if is_internal(request) else 'meeting_list', args=[org_slug, pk])
    if form.is_valid():
        try:
            services.close_action(action, request.user, form.cleaned_data['status'], form.cleaned_data['update'])
        except InvalidTransition as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f'{action.reference} saved: {action.get_status_display().lower()}.'
                             + ('' if is_internal(request) else ' The site team can see it now.'))
    else:
        messages.error(request, 'Choose a status for the action.')
    return redirect(_back(request, default) + f'#action-{action.pk}')


def action_register(request, org_slug, pk):
    project = meetings_project(request, pk)
    if not is_internal(request):
        return redirect('meeting_list', org_slug=org_slug, pk=pk)
    qs = MeetingAction.objects.filter(project=project, meeting__deleted_at__isnull=True) \
        .select_related('owner', 'meeting').order_by('due_date', 'meeting__held_on', 'number')
    show, owner, q = request.GET.get('show', 'open'), request.GET.get('owner', ''), request.GET.get('q', '').strip()
    today = timezone.localdate()
    if show == 'open':
        qs = qs.filter(status=MeetingAction.OPEN)
    elif show == 'overdue':
        qs = qs.filter(status=MeetingAction.OPEN, due_date__lt=today)
    elif show == 'closed':
        qs = qs.exclude(status=MeetingAction.OPEN)
    else:
        show = 'all'
    owners = sorted({a.owner for a in MeetingAction.objects.filter(project=project, owner__isnull=False)
                     .select_related('owner')}, key=lambda u: u.full_name)
    if owner.isdigit():
        qs = qs.filter(owner_id=int(owner))
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(owner_name__icontains=q) | Q(update__icontains=q))
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-meeting-actions.csv"'
        w = csv.writer(resp)
        w.writerow(['Ref', 'Action', 'Who', 'Due', 'Status', 'Latest update', 'Meeting', 'Meeting date'])
        for a in qs:
            w.writerow([_safe(v) for v in (a.reference, a.title, a.who, a.due_date.isoformat() if a.due_date else '',
                                           a.status_label, a.update, a.meeting.title, a.meeting.held_on.isoformat())])
        return resp
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'meetings/actions.html', _ctx(
        request, project, 'actions', page=page, actions=page.object_list, owners=owners,
        filters={'show': show, 'owner': owner, 'q': q}, filtered=bool(owner or q) or show != 'open',
        show_choices=[('open', 'Open'), ('overdue', 'Overdue'), ('closed', 'Done or dropped'), ('all', 'All')],
        update_form=ActionUpdateForm()))


# --- company agenda templates (owners and admins) ---------------------------------------

@admin_required
def agenda_settings(request, org_slug):
    templates = AgendaTemplate.for_org(request.org)
    forms_ = {kind: AgendaTemplateForm(request.POST if request.POST.get('kind') == kind else None, instance=t,
                                       prefix=kind.lower())
              for kind, t in sorted(templates.items())}
    settings_form = MeetingSettingsForm(request.POST if request.POST.get('kind') == 'settings' else None,
                                        instance=MeetingSettings.for_org(request.org), prefix='settings')
    if request.method == 'POST' and request.POST.get('kind') == 'settings':
        if settings_form.is_valid():
            settings_form.save()
            messages.success(request, 'Saved. Meetings set up or changed from now on use the new due date for minutes.')
            return redirect('meeting_agendas', org_slug=org_slug)
    elif request.method == 'POST':
        f = forms_.get(request.POST.get('kind'))
        if f is not None and f.is_valid():
            f.save()
            messages.success(request, f'{f.instance.name} agenda saved. New meetings use it; existing ones keep theirs.')
            return redirect('meeting_agendas', org_slug=org_slug)
    return render(request, 'meetings/agendas.html', {'forms': [(k, dict(KIND_CHOICES)[k], f) for k, f in forms_.items()],
                                                     'settings_form': settings_form})
