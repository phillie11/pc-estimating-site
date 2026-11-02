"""
Safety tab views. Every project view goes through safety.access.safety_project() (get_project()
plus "staff on the job") before anything else, so archived jobs are read only and externals and
other companies never get in. Files are only handed out through projects.files.serve().

The public QR sign-in page is in safety/public.py.
"""
import csv
import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from core.exceptions import InvalidTransition, RecordConflict
from core.models import AuditEntry
from core.workflow import apply_transition, available_transitions
from diary import photos as photo_files
from levelling.exports import safe
from projects.files import serve
from projects.models import Project
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from . import qr, rules
from .access import can_see_company_register, is_pm_or_admin, safety_project
from .forms import (ActionForm, CDMForm, DutyHolderForm, IncidentForm, ManualSignInForm, PhasePlanForm, PhotoForm,
                    ReportForm)
from .models import (CDMProfile, DutyHolder, Incident, IncidentAction, IncidentPhoto, PhasePlan, SiteAttendance,
                     SiteGate, on_site_now)
from .workflow import close_problems

PHOTO_SIZES = ('thumb', 'display', 'original')
PAGE_SIZE = 50


def _ctx(request, project, sub, **extra):
    return project_context(request, project, 'safety', sub=sub, **extra)


def _date_param(request, name):
    try:
        return datetime.date.fromisoformat(request.GET.get(name, ''))
    except ValueError:
        return None


def _csv(filename, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for row in rows:
        w.writerow([safe(v) for v in row])
    return resp


def _touch(incident, actor, action, note=''):
    """An audited save on the incident for a change to one of its actions or photos (retried once on a race)."""
    try:
        incident.save(actor=actor, audit_action=action, audit_note=str(note)[:255])
    except RecordConflict:
        incident = Incident.objects.get(pk=incident.pk)
        incident.save(actor=actor, audit_action=action, audit_note=str(note)[:255])
    return incident


def _local(dt):
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M') if dt else ''


# --- the Safety tab's front page ---------------------------------------------------------

def cdm_checks(project, cdm, holders, has_plan, today=None):
    """What's missing from the CDM register, in plain words: [(message, anchor)]."""
    today = today or timezone.localdate()
    roles = {h.role for h in holders}
    gaps = []
    for role in DutyHolder.KEY_ROLES:
        if role not in roles:
            gaps.append((f'No {dict(DutyHolder.ROLE_CHOICES)[role].lower()} recorded.', 'duty-holders'))
    if not has_plan and not cdm.cpp_in_place_on:
        due = rules.cpp_check_due(project.start_date, today)
        gaps.append(('No construction phase plan uploaded' + (f' (needed before the start on site, '
                     f'{project.start_date:%a} {project.start_date.day} {project.start_date:%b})' if due else '') + '.',
                     'plan'))
    if not cdm.notifiable:
        gaps.append(('Not yet checked whether the job is notifiable to HSE (F10).', 'notification'))
    elif cdm.notifiable == 'YES' and not cdm.f10_submitted_on:
        gaps.append(('Notifiable, but no date recorded for the F10 going to HSE.', 'notification'))
    return gaps


def safety_home(request, org_slug, pk):
    project = safety_project(request, pk)
    cdm = CDMProfile.for_project(project)
    holders = list(DutyHolder.objects.filter(project=project))
    has_plan = PhasePlan.objects.filter(project=project).exists()
    counts = Incident.objects.filter(project=project).aggregate(
        open=Count('pk', filter=Q(status__in=Incident.OPEN_STATUSES)),
        riddor=Count('pk', filter=Q(riddor_category__in=rules.REPORTABLE, riddor_reported_on__isnull=True)))
    recent = Incident.objects.filter(project=project).select_related('created_by')[:5]
    return render(request, 'safety/home.html', _ctx(
        request, project, 'home', on_site=on_site_now(project).count(), counts=counts, recent=recent,
        gaps=cdm_checks(project, cdm, holders, has_plan), is_admin=can_see_company_register(request)))


# --- incidents ------------------------------------------------------------------------------

def _filter_incidents(request, qs):
    status = request.GET.get('status', '')
    if status == 'open':
        qs = qs.filter(status__in=Incident.OPEN_STATUSES)
    elif status == 'closed':
        qs = qs.filter(status=Incident.STATUS_CLOSED)
    else:
        status = ''
    kind = request.GET.get('kind', '')
    if kind in dict(Incident.KIND_CHOICES):
        qs = qs.filter(kind=kind)
    else:
        kind = ''
    riddor = bool(request.GET.get('riddor'))
    if riddor:
        qs = qs.filter(riddor_category__in=rules.REPORTABLE)
    start, end = _date_param(request, 'from'), _date_param(request, 'to')
    if start:
        qs = qs.filter(occurred_at__date__gte=start)
    if end:
        qs = qs.filter(occurred_at__date__lte=end)
    q = request.GET.get('q', '').strip()[:100]
    if q:
        qs = qs.filter(Q(ref__icontains=q) | Q(title__icontains=q) | Q(description__icontains=q) |
                       Q(location__icontains=q) | Q(injured_name__icontains=q))
    filters = {'status': status, 'kind': kind, 'riddor': riddor, 'from': start, 'to': end, 'q': q}
    return qs, filters, any(filters.values())


INCIDENT_CSV = ['Ref', 'Job', 'When', 'What kind', 'What happened', 'Where', 'Anyone hurt', 'Person hurt',
                'Their company', 'Went to hospital', 'Days off', 'RIDDOR', 'RIDDOR deadline', 'Reported to HSE on',
                'HSE reference', 'Status', 'Reported by', 'Closed on']


def _incident_row(i):
    return [i.ref, i.project.name, _local(i.occurred_at), i.get_kind_display(), i.description, i.location,
            'Yes' if i.anyone_hurt else 'No', i.injured_name, i.injured_company,
            'Yes' if i.taken_to_hospital else 'No', '' if i.days_off is None else i.days_off,
            i.get_riddor_category_display() if i.riddor_category else '',
            i.riddor_deadline.isoformat() if i.riddor_deadline else '',
            i.riddor_reported_on.isoformat() if i.riddor_reported_on else '', i.riddor_reference,
            i.get_status_display(), i.created_by.full_name if i.created_by else '',
            timezone.localdate(i.closed_at).isoformat() if i.closed_at else '']


def incident_list(request, org_slug, pk):
    project = safety_project(request, pk)
    qs, filters, filtered = _filter_incidents(
        request, Incident.objects.filter(project=project).select_related('created_by', 'project'))
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-incidents.csv', INCIDENT_CSV, (_incident_row(i) for i in qs))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'safety/incident_list.html', _ctx(
        request, project, 'incidents', page=page, incidents=page.object_list, filters=filters, filtered=filtered,
        kinds=Incident.KIND_CHOICES, is_admin=can_see_company_register(request)))


def incident_report(request, org_slug, pk):
    """The under-a-minute phone form."""
    project = safety_project(request, pk)
    form = ReportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        incident = form.save(commit=False)
        incident.project = project
        incident.kind = Incident.KIND_INJURY if incident.anyone_hurt else Incident.KIND_NEAR_MISS
        incident.ball_in_court_user = project.manager or request.user
        incident.due_date = rules.investigation_due(timezone.localdate())
        photo = None
        try:
            with transaction.atomic():
                incident.save(actor=request.user)
                f = form.cleaned_data.get('photo')
                if f:
                    photo = IncidentPhoto(incident=incident, uploaded_by=request.user)
                    photo_files.attach(photo, f)
                    photo.save()
        except BaseException:
            if photo is not None:
                photo_files.delete_files(photo)
            raise
        log_activity(request, 'reported', f'{incident.ref} {incident.title}', project=project,
                     url=incident.get_absolute_url(), internal_only=True)
        notify.alert([incident.ball_in_court_user], f'{"Injury" if incident.anyone_hurt else "Near miss"} reported: '
                     f'{incident.ref} {incident.title}', incident.get_absolute_url(), project=project,
                     actor=request.user)
        messages.success(request, f'Thank you. {incident.ref} is recorded and the project manager has been told.'
                                  + (' Add the injured person\'s details below when you can.'
                                     if incident.anyone_hurt else ''))
        return redirect(incident)
    return render(request, 'safety/report.html', _ctx(request, project, 'incidents', form=form))


def _incident(project, incident_id):
    return get_object_or_404(Incident.objects.select_related('created_by', 'ball_in_court_user', 'closed_by'),
                             project=project, pk=incident_id)


def incident_detail(request, org_slug, pk, incident_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    audit = AuditEntry.objects.filter(record_idx__object_id=incident.pk, record_idx__record_type='safety.incident') \
        .select_related('actor').order_by('-at', '-pk')[:30]
    actions_ = list(incident.actions.select_related('owner', 'done_by'))
    transitions = available_transitions(incident, request.user)
    return render(request, 'safety/incident_detail.html', _ctx(
        request, project, 'incidents', incident=incident, actions=actions_, photos=list(incident.photos.all()),
        audit=audit, transitions={t.name: t for t in transitions},
        problems=close_problems(incident) if incident.is_open else [],
        action_form=ActionForm(project=project) if incident.is_open else None,
        can_manage=is_pm_or_admin(request.user, project, request.membership)))


def incident_edit(request, org_slug, pk, incident_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    if not incident.is_open:
        messages.error(request, f'{incident.ref} is closed. Reopen it to make changes.')
        return redirect(incident)
    form = IncidentForm(request.POST or None, instance=incident)
    if request.method == 'POST' and form.is_valid():
        try:
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            messages.success(request, 'Saved.')
            return redirect(incident)
    return render(request, 'safety/incident_form.html', _ctx(request, project, 'incidents', form=form,
                                                             incident=incident))


@require_POST
def incident_transition(request, org_slug, pk, incident_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    name = request.POST.get('transition', '')
    if name not in ('investigate', 'close', 'reopen'):
        raise Http404
    note = request.POST.get('note', '').strip()[:255]
    if name == 'reopen' and not note:
        messages.error(request, 'Say why it is being reopened.')
        return redirect(incident)
    try:
        apply_transition(incident, name, request.user, note=note)
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
        return redirect(incident.get_absolute_url())
    verb = {'investigate': 'started investigating', 'close': 'closed', 'reopen': 'reopened'}[name]
    log_activity(request, verb, f'{incident.ref} {incident.title}', project=project, url=incident.get_absolute_url(),
                 internal_only=True)
    messages.success(request, {'investigate': 'Investigation started. Add actions as you find what needs doing.',
                               'close': f'{incident.ref} closed.',
                               'reopen': f'{incident.ref} reopened.'}[name])
    return redirect(incident)


def incident_action_add(request, org_slug, pk, incident_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    if not incident.is_open:
        messages.error(request, f'{incident.ref} is closed. Reopen it to add actions.')
        return redirect(incident)
    form = ActionForm(request.POST or None, project=project)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            action = form.save(commit=False)
            action.incident, action.created_by = incident, request.user
            action.save()
            _touch(incident, request.user, 'action added', action.description)
        if action.owner_id and action.owner_id != request.user.pk:
            notify.alert([action.owner], f'Safety action for you: {action.description[:120]} ({incident.ref})',
                         incident.get_absolute_url() + '#actions', project=project, actor=request.user)
        messages.success(request, 'Action added.')
        return redirect(incident.get_absolute_url() + '#actions')
    return render(request, 'safety/action_form.html', _ctx(request, project, 'incidents', form=form,
                                                           incident=incident))


@require_POST
def incident_action_update(request, org_slug, pk, incident_id, action_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    action = get_object_or_404(IncidentAction, incident=incident, pk=action_id)
    if not incident.is_open:
        messages.error(request, f'{incident.ref} is closed. Reopen it to change its actions.')
        return redirect(incident)
    what = request.POST.get('do', '')
    with transaction.atomic():
        if what == 'done' and not action.is_done:
            action.done_at, action.done_by = timezone.now(), request.user
            action.save()
            _touch(incident, request.user, 'action done', action.description)
            messages.success(request, 'Marked as done.')
        elif what == 'undo' and action.is_done:
            action.done_at = action.done_by = None
            action.save()
            _touch(incident, request.user, 'action reopened', action.description)
            messages.success(request, 'Marked as not done.')
        elif what == 'remove' and not action.is_done:
            if not (action.created_by_id == request.user.pk or is_pm_or_admin(request.user, project,
                                                                              request.membership)):
                raise PermissionDenied
            text = action.description
            action.delete()
            _touch(incident, request.user, 'action removed', text)
            messages.success(request, 'Action removed.')
    return redirect(incident.get_absolute_url() + '#actions')


def incident_photo_add(request, org_slug, pk, incident_id):
    project = safety_project(request, pk)
    incident = _incident(project, incident_id)
    if not incident.is_open:
        messages.error(request, f'{incident.ref} is closed. Reopen it to add photos.')
        return redirect(incident)
    form = PhotoForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        photo = IncidentPhoto(incident=incident, caption=form.cleaned_data['caption'], uploaded_by=request.user)
        try:
            with transaction.atomic():
                photo_files.attach(photo, form.cleaned_data['photo'])
                photo.save()
                _touch(incident, request.user, 'photo added', photo)
        except BaseException:
            photo_files.delete_files(photo)
            raise
        messages.success(request, 'Photo added.')
        return redirect(incident.get_absolute_url() + '#photos')
    return render(request, 'safety/photo_form.html', _ctx(request, project, 'incidents', form=form,
                                                          incident=incident))


def incident_photo(request, org_slug, pk, incident_id, photo_id, size):
    project = safety_project(request, pk)
    if size not in PHOTO_SIZES:
        raise Http404
    photo = get_object_or_404(IncidentPhoto, incident__project=project, incident_id=incident_id, pk=photo_id)
    field = {'thumb': photo.thumbnail, 'display': photo.display, 'original': photo.image}[size] or photo.image
    return serve(request, field, project=project, label=f'Incident photo {photo.pk} ({size})')


# --- company-wide register (owners and admins) ------------------------------------------------

def company_incidents(request, org_slug):
    if not can_see_company_register(request):
        raise PermissionDenied
    qs = Incident.objects.select_related('project', 'created_by')
    project_id = request.GET.get('project', '')
    if project_id.isdigit():
        qs = qs.filter(project_id=int(project_id))
    else:
        project_id = ''
    qs, filters, filtered = _filter_incidents(request, qs)
    filters['project'] = project_id
    filtered = filtered or bool(project_id)
    if request.GET.get('export') == 'csv':
        return _csv(f'{request.org.slug}-incidents.csv', INCIDENT_CSV, (_incident_row(i) for i in qs))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    year_start = timezone.localdate().replace(month=1, day=1)
    totals = Incident.objects.filter(occurred_at__date__gte=year_start).aggregate(
        near_misses=Count('pk', filter=Q(kind=Incident.KIND_NEAR_MISS)), hurt=Count('pk', filter=Q(anyone_hurt=True)),
        riddor=Count('pk', filter=Q(riddor_category__in=rules.REPORTABLE)), all=Count('pk'),
        open=Count('pk', filter=Q(status__in=Incident.OPEN_STATUSES)))
    # Archived jobs included: owners and admins can open them, and their incidents still count
    projects = Project.objects.filter(pk__in=Incident.objects.values('project_id')).order_by('name')
    return render(request, 'safety/company_incidents.html', {
        'page': page, 'incidents': page.object_list, 'filters': filters, 'filtered': filtered, 'totals': totals,
        'year': year_start.year, 'projects': projects, 'kinds': Incident.KIND_CHOICES})


# --- CDM register -----------------------------------------------------------------------------

def cdm_register(request, org_slug, pk):
    project = safety_project(request, pk)
    cdm = CDMProfile.for_project(project)
    holders = list(DutyHolder.objects.filter(project=project))
    plans = list(PhasePlan.objects.filter(project=project).select_related('uploaded_by'))
    order = {role: n for n, (role, _) in enumerate(DutyHolder.ROLE_CHOICES)}
    holders.sort(key=lambda h: (order.get(h.role, 99), h.pk))
    present = {h.role for h in holders}
    return render(request, 'safety/cdm.html', _ctx(
        request, project, 'cdm', cdm=cdm, holders=holders, plans=plans, current_plan=plans[0] if plans else None,
        missing_roles=[(r, dict(DutyHolder.ROLE_CHOICES)[r]) for r in DutyHolder.KEY_ROLES if r not in present],
        gaps=cdm_checks(project, cdm, holders, bool(plans)), suggestion=cdm.notifiable_suggestion,
        thresholds={'days': rules.NOTIFIABLE_WORKING_DAYS, 'workers': rules.NOTIFIABLE_WORKERS,
                    'person_days': rules.NOTIFIABLE_PERSON_DAYS}, advisory=rules.ADVISORY))


def cdm_edit(request, org_slug, pk):
    project = safety_project(request, pk)
    cdm = CDMProfile.for_project(project)
    form = CDMForm(request.POST or None, instance=cdm)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by, obj.updated_at = request.user, timezone.now()
        obj.save()
        log_activity(request, 'updated the CDM register for', project.name, project=project,
                     url=reverse('cdm_register', args=[org_slug, pk]), internal_only=True)
        messages.success(request, 'CDM register saved.')
        return redirect('cdm_register', org_slug=org_slug, pk=pk)
    return render(request, 'safety/cdm_form.html', _ctx(
        request, project, 'cdm', form=form, thresholds={'days': rules.NOTIFIABLE_WORKING_DAYS,
                                                        'workers': rules.NOTIFIABLE_WORKERS,
                                                        'person_days': rules.NOTIFIABLE_PERSON_DAYS}))


def duty_holder_add(request, org_slug, pk):
    project = safety_project(request, pk)
    role = request.GET.get('role', '')
    initial = {'role': role} if role in dict(DutyHolder.ROLE_CHOICES) else {}
    # Sensible defaults: the client is the job's client, the principal contractor is usually us
    if role == DutyHolder.CLIENT:
        initial['company'] = project.client_name
    elif role == DutyHolder.PC:
        initial['company'] = request.org.name
    form = DutyHolderForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        holder = form.save(commit=False)
        holder.project = project
        holder.save()
        log_activity(request, 'added to the CDM register', str(holder), project=project,
                     url=reverse('cdm_register', args=[org_slug, pk]), internal_only=True)
        messages.success(request, f'Added: {holder}.')
        return redirect(reverse('cdm_register', args=[org_slug, pk]) + '#duty-holders')
    return render(request, 'safety/duty_holder_form.html', _ctx(request, project, 'cdm', form=form, holder=None))


def duty_holder_edit(request, org_slug, pk, holder_id):
    project = safety_project(request, pk)
    holder = get_object_or_404(DutyHolder, project=project, pk=holder_id)
    back = reverse('cdm_register', args=[org_slug, pk]) + '#duty-holders'
    if request.method == 'POST' and request.POST.get('action') == 'delete':
        label = str(holder)
        holder.delete()
        log_activity(request, 'removed from the CDM register', label, project=project, url=back, internal_only=True)
        messages.success(request, f'Removed: {label}.')
        return redirect(back)
    form = DutyHolderForm(request.POST or None, instance=holder)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Saved.')
        return redirect(back)
    return render(request, 'safety/duty_holder_form.html', _ctx(request, project, 'cdm', form=form, holder=holder))


def phase_plan_upload(request, org_slug, pk):
    project = safety_project(request, pk)
    form = PhasePlanForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            # Lock the job's row so two uploads at once can't both take the same version number
            Project.objects.select_for_update().filter(pk=project.pk).first()
            last = PhasePlan.objects.filter(project=project).order_by('-version').values_list('version', flat=True) \
                .first() or 0
            plan = form.save(commit=False)
            f = form.cleaned_data['file']
            plan.project, plan.version, plan.uploaded_by = project, last + 1, request.user
            plan.original_name, plan.size = f.name[-255:], f.size
            plan.save()
        log_activity(request, f'uploaded construction phase plan v{plan.version} for', project.name, project=project,
                     url=reverse('cdm_register', args=[org_slug, pk]), internal_only=True)
        messages.success(request, f'Construction phase plan v{plan.version} uploaded.'
                                  + (' Earlier versions are kept below it.' if plan.version > 1 else ''))
        return redirect(reverse('cdm_register', args=[org_slug, pk]) + '#plan')
    current = PhasePlan.objects.filter(project=project).first()
    return render(request, 'safety/plan_form.html', _ctx(request, project, 'cdm', form=form, current=current))


def phase_plan_file(request, org_slug, pk, plan_id):
    project = safety_project(request, pk)
    plan = get_object_or_404(PhasePlan, project=project, pk=plan_id)
    return serve(request, plan.file, download_name=plan.original_name or None, project=project,
                 label=f'Construction phase plan v{plan.version}')


# --- site sign-in: the site team's side -------------------------------------------------------

def on_site(request, org_slug, pk):
    project = safety_project(request, pk)
    people = list(on_site_now(project).select_related('user', 'signed_in_by'))
    by_company = {}
    for a in people:
        by_company.setdefault(a.company or 'No company given', []).append(a)
    gate = SiteGate.for_project(project)
    return render(request, 'safety/on_site.html', _ctx(
        request, project, 'on_site', people=people, by_company=sorted(by_company.items(), key=lambda kv: kv[0].lower()),
        gate=gate, now=timezone.localtime()))


def manual_sign_in(request, org_slug, pk):
    project = safety_project(request, pk)
    form = ManualSignInForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        a = form.save(commit=False)
        a.project, a.method, a.signed_in_by = project, SiteAttendance.METHOD_MANUAL, request.user
        a.save()
        messages.success(request, f'{a.name} signed in at {timezone.localtime(a.signed_in_at):%H:%M}.')
        if request.POST.get('another'):
            return redirect('safety_manual_sign_in', org_slug=org_slug, pk=pk)
        return redirect('safety_on_site', org_slug=org_slug, pk=pk)
    return render(request, 'safety/manual_sign_in.html', _ctx(request, project, 'on_site', form=form))


@require_POST
def staff_sign_out(request, org_slug, pk, attendance_id):
    project = safety_project(request, pk)
    a = get_object_or_404(SiteAttendance, project=project, pk=attendance_id)
    if a.signed_out_at is None:
        a.signed_out_at, a.signed_out_by = timezone.now(), request.user
        a.save(update_fields=['signed_out_at', 'signed_out_by'])
        messages.success(request, f'{a.name} signed out.')
    nxt = request.POST.get('next', '')
    if nxt == 'log':
        day = timezone.localtime(a.signed_in_at).date()
        return redirect(reverse('safety_sign_in_log', args=[org_slug, pk]) + f'?date={day}')
    return redirect('safety_on_site', org_slug=org_slug, pk=pk)


ATTENDANCE_CSV = ['Name', 'Company', 'Who', 'Signed in', 'Signed out', 'Hours on site', 'How', 'Signed in by',
                  'Signed out by']


def sign_in_log(request, org_slug, pk):
    project = safety_project(request, pk)
    today = timezone.localdate()
    day = _date_param(request, 'date')
    start, end = _date_param(request, 'from'), _date_param(request, 'to')
    if day:
        start = end = day
    if not start and not end:
        start = end = today
    qs = SiteAttendance.objects.filter(project=project).select_related('signed_in_by', 'signed_out_by')
    if start:
        qs = qs.filter(signed_in_at__date__gte=start)
    if end:
        qs = qs.filter(signed_in_at__date__lte=end)
    q = request.GET.get('q', '').strip()[:100]
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(company__icontains=q))
    kind = request.GET.get('kind', '')
    if kind in dict(SiteAttendance.KIND_CHOICES):
        qs = qs.filter(kind=kind)
    else:
        kind = ''
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-site-sign-in.csv', ATTENDANCE_CSV, (
            [a.name, a.company, a.get_kind_display(), _local(a.signed_in_at), _local(a.signed_out_at),
             a.hours_on_site if a.signed_out_at else '', a.get_method_display(),
             a.signed_in_by.full_name if a.signed_in_by else '', a.signed_out_by.full_name if a.signed_out_by else '']
            for a in qs.order_by('signed_in_at', 'pk')))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    since = rules.on_site_since(timezone.localtime())
    return render(request, 'safety/sign_in_log.html', _ctx(
        request, project, 'log', page=page, rows=page.object_list, since=since, today=today,
        filters={'from': start, 'to': end, 'q': q, 'kind': kind}, kinds=SiteAttendance.KIND_CHOICES,
        single_day=start == end, day=start, prev_day=(start - datetime.timedelta(days=1)) if start else None,
        next_day=(start + datetime.timedelta(days=1)) if start and start < today else None))


@never_cache
def roll_call(request, org_slug, pk):
    """The fire roll call: a standalone page that prints cleanly and keeps working offline once opened."""
    project = safety_project(request, pk)
    people = list(on_site_now(project))
    by_company = {}
    for a in people:
        by_company.setdefault(a.company or 'No company given', []).append(a)
    return render(request, 'safety/roll_call.html', {
        'project': project, 'people': people, 'now': timezone.localtime(),
        'by_company': sorted(by_company.items(), key=lambda kv: kv[0].lower()),
        'sw_url': reverse('safety_roll_call_sw', args=[org_slug, pk]),
        'back_url': reverse('safety_on_site', args=[org_slug, pk])})


def roll_call_sw(request, org_slug, pk):
    """
    The roll call's service worker. It lives under the roll call's own URL, so it can only ever
    see that page: network first, so the list is always current when there is a signal, and the
    last copy when there isn't.
    """
    project = safety_project(request, pk)
    body = render_to_string('safety/roll_call_sw.js', {
        'page': reverse('safety_roll_call', args=[org_slug, project.pk]), 'cache': f'roll-call-{project.pk}'},
        request=request)
    resp = HttpResponse(body, content_type='application/javascript; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'
    return resp


def poster(request, org_slug, pk):
    project = safety_project(request, pk)
    gate = SiteGate.for_project(project)
    url = request.build_absolute_uri(gate.public_path())
    if request.GET.get('print'):
        return render(request, 'safety/poster_print.html', {
            'project': project, 'signin_url': url, 'qr_svg': qr.svg(url),
            'back_url': reverse('safety_poster', args=[org_slug, pk])})
    return render(request, 'safety/poster.html', _ctx(
        request, project, 'poster', gate=gate, signin_url=url, qr_svg=qr.svg(url),
        can_replace=is_pm_or_admin(request.user, project, request.membership)))


@require_POST
def poster_replace(request, org_slug, pk):
    project = safety_project(request, pk)
    if not is_pm_or_admin(request.user, project, request.membership):
        raise PermissionDenied
    gate = SiteGate.for_project(project)
    gate.replace_token(request.user)
    log_activity(request, 'replaced the site sign-in QR code for', project.name, project=project,
                 url=reverse('safety_poster', args=[org_slug, pk]), internal_only=True)
    messages.success(request, 'New QR code made. Old posters no longer work: print this one and put it up.')
    return redirect('safety_poster', org_slug=org_slug, pk=pk)
