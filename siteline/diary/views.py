"""
Site diary views. Every project view goes through diary.access.diary_project() (get_project()
plus "staff on the job"), except the photo file view, which also lets a client open photos
marked "share with client". Files are only handed out through projects.files.serve().
"""
import csv
import datetime
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contracts.models import MainContract
from core.exceptions import InvalidTransition, RecordConflict
from core.models import AuditEntry
from core.workflow import apply_transition, available_transitions
from projects.access import get_project, is_internal
from projects.files import serve
from projects.views import project_context
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import photos as photo_files
from . import rules
from .access import can_see_shared_photos, can_use_diary, diary_project, is_pm_or_admin
from .actions import missing_days
from .forms import (DelayForm, DeliveryForm, EntryForm, InstructionForm, LabourForm, PhotoForm, PhotoUploadForm,
                    PlantForm, SettingsForm, UnlockForm, VisitorForm)
from .models import DelayLine, DiaryEntry, DiarySettings, SitePhoto, entry_totals

# kind in the URL -> (form, related name on the entry, button label, what the audit trail calls it)
LINE_KINDS = {
    'labour': (LabourForm, 'labour', 'Add labour', 'labour'),
    'deliveries': (DeliveryForm, 'deliveries', 'Add delivery', 'delivery'),
    'visitors': (VisitorForm, 'visitors', 'Add visitor', 'visitor'),
    'delays': (DelayForm, 'delays', 'Log a delay', 'delay'),
    'plant': (PlantForm, 'plant', 'Add plant', 'plant'),
    'instructions': (InstructionForm, 'instructions', 'Add instruction', 'instruction'),
}
SECTION_ORDER = ('labour', 'deliveries', 'visitors', 'delays', 'plant', 'instructions')
SECTION_TITLES = {'labour': 'Labour on site', 'deliveries': 'Deliveries', 'visitors': 'Visitors',
                  'delays': 'Delays and disruption', 'plant': 'Plant on site', 'instructions': 'Instructions received'}
PHOTO_SIZES = ('thumb', 'display', 'original')


def _ctx(request, project, **extra):
    return project_context(request, project, 'diary', **extra)


def _day(value):
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        raise Http404
    if day > timezone.localdate():
        raise Http404('Diary entries are for today or earlier.')
    return day


def _settings(request, project):
    region = MainContract.objects.filter(project=project).values_list('bank_holiday_region', flat=True).first()
    return DiarySettings.for_org(request.org).saturday_counts, region


def _touch(entry, actor, action, note=''):
    """An audited save on the entry for a change to one of its lines or photos (retried once on a race)."""
    try:
        entry.save(actor=actor, audit_action=action, audit_note=str(note)[:255])
    except RecordConflict:
        entry = DiaryEntry.objects.get(pk=entry.pk)
        entry.save(actor=actor, audit_action=action, audit_note=str(note)[:255])
    return entry


def _entry_for_write(request, project, day):
    """The day's entry, created if it doesn't exist yet. PermissionDenied if it is locked."""
    entry = DiaryEntry.objects.filter(project=project, entry_date=day).first()
    if entry is None:
        try:
            with transaction.atomic():
                entry = DiaryEntry(project=project, entry_date=day)
                entry.save(actor=request.user)
        except IntegrityError:  # someone else started the same day a moment ago
            entry = DiaryEntry.objects.get(project=project, entry_date=day)
    if entry.locked:
        raise PermissionDenied(f'The diary for {day:%a} {day.day} {day:%b} is locked. Ask the project manager to '
                               'unlock it if something needs correcting.')
    return entry


def _locked_refusal(request, project, day, err):
    messages.error(request, str(err))
    return redirect('diary_day', org_slug=request.org.slug, pk=project.pk, day=day.isoformat())


# --- the register -------------------------------------------------------------------

def _date_param(request, name):
    try:
        return datetime.date.fromisoformat(request.GET.get(name, ''))
    except ValueError:
        return None


def diary_register(request, org_slug, pk):
    project = diary_project(request, pk)
    entries = DiaryEntry.objects.filter(project=project).select_related('created_by')
    start, end = _date_param(request, 'from'), _date_param(request, 'to')
    if start:
        entries = entries.filter(entry_date__gte=start)
    if end:
        entries = entries.filter(entry_date__lte=end)
    author = request.GET.get('author', '')
    if author.isdigit():
        entries = entries.filter(created_by_id=int(author))
    if request.GET.get('delays'):
        entries = entries.filter(delays__isnull=False).distinct()
    if request.GET.get('incident'):
        entries = entries.filter(hs_incident=True)
    lock = request.GET.get('lock', '')
    rows = list(entries)
    if lock in ('open', 'locked'):
        rows = [e for e in rows if e.locked == (lock == 'locked')]
    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    entry_totals(page.object_list)
    saturday, region = _settings(request, project)
    missing = missing_days(project, timezone.localtime().replace(tzinfo=None), saturday, region) \
        if project.stage == project.STAGE_LIVE and project.start_date else []
    authors = {e.created_by for e in DiaryEntry.objects.filter(project=project).select_related('created_by')
               if e.created_by}
    filters = {'from': start, 'to': end, 'author': author, 'delays': bool(request.GET.get('delays')),
               'incident': bool(request.GET.get('incident')), 'lock': lock}
    return render(request, 'diary/register.html', _ctx(
        request, project, page=page, entries=page.object_list, missing=missing, filters=filters,
        filtered=any([start, end, author, filters['delays'], filters['incident'], lock]),
        authors=sorted(authors, key=lambda u: u.full_name), today=timezone.localdate(),
        is_admin=request.membership.is_admin))


def diary_today(request, org_slug, pk):
    project = diary_project(request, pk)
    return redirect('diary_day', org_slug=org_slug, pk=project.pk, day=timezone.localdate().isoformat())


# --- one day: the phone-first site page ----------------------------------------------

def diary_day(request, org_slug, pk, day):
    project = diary_project(request, pk)
    day = _day(day)
    entry = DiaryEntry.objects.filter(project=project, entry_date=day).first()
    today = timezone.localdate()
    if entry is not None:
        locked = entry.is_locked(today)
        lines = {kind: list(getattr(entry, rel).select_related(*(('created_by', 'rfi', 'change_event')
                                                                  if kind == 'instructions' else ('created_by',))))
                 for kind, (_, rel, _, _) in LINE_KINDS.items()}
        day_photos = list(entry.photos.all())
        audit = AuditEntry.objects.filter(record_idx__object_id=entry.pk,
                                          record_idx__record_type='diary.diaryentry').select_related('actor') \
            .order_by('-at', '-pk')[:30]
    else:
        locked = False
        lines = {kind: [] for kind in LINE_KINDS}
        day_photos = []
        audit = []
    loose = SitePhoto.objects.filter(project=project, taken_on=day, entry__isnull=True)
    can_unlock = entry is not None and is_pm_or_admin(request.user, project, request.membership)
    return render(request, 'diary/day.html', _ctx(
        request, project, day=day, entry=entry, locked=locked, lines=lines, day_photos=day_photos,
        loose_photos=loose, audit=audit, can_unlock=can_unlock, unlock_form=UnlockForm(),
        transitions=available_transitions(entry, request.user) if entry else [],
        is_today=day == today, prev_day=day - rules.ONE_DAY, next_day=day + rules.ONE_DAY if day < today else None,
        sections=[(kind, SECTION_TITLES[kind], lines[kind], LINE_KINDS[kind][2]) for kind in SECTION_ORDER],
        sub_total=sum(line.headcount for line in lines['labour']),
        hours_lost=sum((d.hours_lost for d in lines['delays']), start=Decimal('0'))))


def diary_edit(request, org_slug, pk, day):
    project = diary_project(request, pk)
    day = _day(day)
    entry = DiaryEntry.objects.filter(project=project, entry_date=day).first()
    if entry is not None and entry.locked:
        return _locked_refusal(request, project, day, 'This day is locked. Ask the project manager to unlock it.')
    form = EntryForm(request.POST or None, instance=entry or DiaryEntry(project=project, entry_date=day))
    if request.method == 'POST' and form.is_valid():
        try:
            if entry is None:
                entry = _entry_for_write(request, project, day)
                form = EntryForm(request.POST, instance=entry)
                form.is_valid()
            form.save(commit=False).save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        except PermissionDenied as e:
            return _locked_refusal(request, project, day, e)
        else:
            messages.success(request, 'Saved.')
            return redirect(entry)
    return render(request, 'diary/entry_form.html', _ctx(request, project, day=day, form=form, entry=entry))


def line_add(request, org_slug, pk, day, kind):
    project = diary_project(request, pk)
    day = _day(day)
    if kind not in LINE_KINDS:
        raise Http404
    form_class, rel, label, noun = LINE_KINDS[kind]
    entry = DiaryEntry.objects.filter(project=project, entry_date=day).first()
    if entry is not None and entry.locked:
        return _locked_refusal(request, project, day, 'This day is locked. Ask the project manager to unlock it.')
    form = form_class(request.POST or None, project=project)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                entry = _entry_for_write(request, project, day)
                line = form.save(commit=False)
                line.entry, line.created_by = entry, request.user
                line.save()
                _touch(entry, request.user, f'{noun} added', line)
        except PermissionDenied as e:
            return _locked_refusal(request, project, day, e)
        messages.success(request, f'{noun.capitalize()} added: {line}.')
        if request.POST.get('another'):
            return redirect('diary_line_add', org_slug=org_slug, pk=pk, day=day.isoformat(), kind=kind)
        return redirect('diary_day', org_slug=org_slug, pk=pk, day=day.isoformat())
    return render(request, 'diary/line_form.html', _ctx(request, project, day=day, form=form, kind=kind, label=label,
                                                        entry=entry))


@require_POST
def line_remove(request, org_slug, pk, day, kind, line_id):
    project = diary_project(request, pk)
    day = _day(day)
    if kind not in LINE_KINDS:
        raise Http404
    _, rel, _, noun = LINE_KINDS[kind]
    entry = get_object_or_404(DiaryEntry, project=project, entry_date=day)
    line = get_object_or_404(getattr(entry, rel), pk=line_id)
    if entry.locked:
        return _locked_refusal(request, project, day, 'This day is locked. Ask the project manager to unlock it.')
    with transaction.atomic():
        text = str(line)
        line.delete()
        _touch(entry, request.user, f'{noun} removed', text)
    messages.success(request, f'Removed: {text}.')
    return redirect(entry)


@require_POST
def diary_lock(request, org_slug, pk, day):
    """Unlock a locked day for correction (project manager or admin, with a reason), or lock it again."""
    project = diary_project(request, pk)
    day = _day(day)
    entry = get_object_or_404(DiaryEntry, project=project, entry_date=day)
    action = request.POST.get('action')
    try:
        if action == 'unlock':
            form = UnlockForm(request.POST)
            if not form.is_valid():
                messages.error(request, 'Say why it needs changing before you unlock it.')
                return redirect(entry)
            reason = form.cleaned_data['reason']
            apply_transition(entry, 'unlock', request.user, note=reason[:255], reason=reason)
            log_activity(request, 'unlocked the site diary for', f'{day:%a} {day.day} {day:%b}', project=project,
                         url=entry.get_absolute_url(), internal_only=True)
            messages.success(request, f'Unlocked until the end of {entry.unlocked_until:%A}. The reason is in the '
                                      'audit trail.')
        elif action == 'relock':
            apply_transition(entry, 'relock', request.user)
            messages.success(request, 'Locked again.')
        else:
            raise Http404
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
    return redirect(entry)


def diary_pdf(request, org_slug, pk, day):
    project = diary_project(request, pk)
    day = _day(day)
    entry = get_object_or_404(DiaryEntry, project=project, entry_date=day)
    from .pdf import day_sheet
    data = day_sheet(entry, request.org.name)
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-site-diary-{day.isoformat()}.pdf"'
    return resp


# --- delays --------------------------------------------------------------------------

def delays(request, org_slug, pk):
    project = diary_project(request, pk)
    rows = DelayLine.objects.filter(entry__project=project, entry__deleted_at__isnull=True) \
        .select_related('entry', 'created_by').order_by('-entry__entry_date', 'pk')
    start, end = _date_param(request, 'from'), _date_param(request, 'to')
    if start:
        rows = rows.filter(entry__entry_date__gte=start)
    if end:
        rows = rows.filter(entry__entry_date__lte=end)
    cause = request.GET.get('cause', '')
    if cause in dict(DelayLine.CAUSE_CHOICES):
        rows = rows.filter(cause=cause)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-delays.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Diary ref', 'Cause', 'Hours lost', 'What happened', 'Trades affected', 'Logged by',
                    'Logged at'])
        for d in rows:
            w.writerow([d.entry.entry_date.isoformat(), d.entry.ref, d.get_cause_display(), d.hours_lost,
                        _csv_safe(d.description), _csv_safe(d.trades_affected),
                        d.created_by.full_name if d.created_by else '', timezone.localtime(d.created_at)
                        .strftime('%Y-%m-%d %H:%M')])
        return resp
    by_cause = rows.values('cause').annotate(h=Sum('hours_lost')).order_by('-h')
    labels = dict(DelayLine.CAUSE_CHOICES)
    return render(request, 'diary/delays.html', _ctx(
        request, project, rows=rows, total=rows.aggregate(h=Sum('hours_lost'))['h'] or Decimal('0'),
        by_cause=[(labels.get(r['cause'], r['cause']), r['h']) for r in by_cause],
        causes=DelayLine.CAUSE_CHOICES, filters={'from': start, 'to': end, 'cause': cause},
        filtered=any([start, end, cause])))


def _csv_safe(text):
    """Stop a spreadsheet treating typed text as a formula."""
    text = str(text or '')
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


# --- photos --------------------------------------------------------------------------

def _areas(project):
    return sorted({a for a in SitePhoto.objects.filter(project=project).exclude(area='')
                   .values_list('area', flat=True)}, key=str.lower)


def gallery(request, org_slug, pk):
    project = diary_project(request, pk)
    qs = SitePhoto.objects.filter(project=project).select_related('entry', 'uploaded_by')
    start, end = _date_param(request, 'from'), _date_param(request, 'to')
    on = _date_param(request, 'date')
    if on:
        qs = qs.filter(taken_on=on)
    if start:
        qs = qs.filter(taken_on__gte=start)
    if end:
        qs = qs.filter(taken_on__lte=end)
    area = request.GET.get('area', '')
    if area:
        qs = qs.filter(area=area)
    if request.GET.get('shared'):
        qs = qs.filter(shared_with_client=True)
    page = Paginator(qs, 48).get_page(request.GET.get('page'))
    filters = {'from': start, 'to': end, 'date': on, 'area': area, 'shared': bool(request.GET.get('shared'))}
    return render(request, 'diary/gallery.html', _ctx(
        request, project, page=page, photos=page.object_list, areas=_areas(project), filters=filters,
        filtered=any([start, end, on, area, filters['shared']]), today=timezone.localdate()))


def photo_upload(request, org_slug, pk):
    project = diary_project(request, pk)
    day = _date_param(request, 'date') or timezone.localdate()
    form = PhotoUploadForm(request.POST or None, request.FILES or None, areas=_areas(project),
                           initial={'taken_on': min(day, timezone.localdate()), 'attach': True})
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        day = data['taken_on']
        try:
            with transaction.atomic():
                entry = _entry_for_write(request, project, day) if data['attach'] else None
                saved = []
                for f in data['files']:
                    photo = SitePhoto(project=project, entry=entry, caption=data['caption'], area=data['area'],
                                      taken_on=day, uploaded_by=request.user)
                    photo_files.attach(photo, f)
                    photo.save()
                    saved.append(photo)
                if entry is not None:
                    _touch(entry, request.user, 'photos added',
                           f'{len(saved)} photo{"s" if len(saved) != 1 else ""}'
                           f'{": " + data["caption"] if data["caption"] else ""}')
        except PermissionDenied as e:
            form.add_error(None, f'{e} You can still add the photos to the job without the diary: untick the box.')
        else:
            log_activity(request, f'added {len(saved)} site photo{"s" if len(saved) != 1 else ""}',
                         data['caption'] or data['area'] or f'{day:%a} {day.day} {day:%b}', project=project,
                         url=reverse('diary_gallery', args=[org_slug, pk]) + f'?date={day.isoformat()}',
                         internal_only=True)
            messages.success(request, f'{len(saved)} photo{"s" if len(saved) != 1 else ""} added.')
            if entry is not None:
                return redirect(entry)
            return redirect(reverse('diary_gallery', args=[org_slug, pk]) + f'?date={day.isoformat()}')
    return render(request, 'diary/photo_upload.html', _ctx(request, project, form=form, day=day,
                                                           areas=_areas(project)))


def photo_edit(request, org_slug, pk, photo_id):
    project = diary_project(request, pk)
    photo = get_object_or_404(SitePhoto.objects.select_related('entry', 'uploaded_by', 'shared_by'), project=project,
                              pk=photo_id)
    entry_locked = photo.entry is not None and photo.entry.locked
    form = PhotoForm(request.POST or None, instance=photo)
    if request.method == 'POST' and request.POST.get('action') == 'delete':
        if entry_locked:
            messages.error(request, 'This photo is part of a locked diary day, so it stays. Ask the project manager '
                                    'to unlock the day if it was added by mistake.')
            return redirect('diary_photo', org_slug=org_slug, pk=pk, photo_id=photo.pk)
        if not (photo.uploaded_by_id == request.user.pk or is_pm_or_admin(request.user, project, request.membership)):
            raise PermissionDenied
        with transaction.atomic():
            entry = photo.entry
            label = str(photo)
            for f in (photo.image, photo.display, photo.thumbnail):
                if f:
                    f.delete(save=False)
            photo.delete()
            if entry is not None:
                _touch(entry, request.user, 'photo removed', label)
        messages.success(request, 'Photo deleted.')
        return redirect('diary_gallery', org_slug=org_slug, pk=pk)
    if request.method == 'POST' and form.is_valid():
        was_shared = SitePhoto.objects.filter(pk=photo.pk).values_list('shared_with_client', flat=True).first()
        p = form.save(commit=False)
        if p.shared_with_client and not was_shared:
            p.shared_at, p.shared_by = timezone.now(), request.user
        elif not p.shared_with_client:
            p.shared_at, p.shared_by = None, None
        p.save()
        if p.entry is not None and was_shared != p.shared_with_client:
            _touch(p.entry, request.user, 'photo shared' if p.shared_with_client else 'photo unshared', str(p))
        messages.success(request, 'Shared with the client.' if p.shared_with_client and not was_shared else 'Saved.')
        return redirect('diary_photo', org_slug=org_slug, pk=pk, photo_id=photo.pk)
    return render(request, 'diary/photo_detail.html', _ctx(request, project, photo=photo, form=form,
                                                           entry_locked=entry_locked, areas=_areas(project)))


def photo_file(request, org_slug, pk, photo_id, size):
    """Staff on the job: every size. A client: thumbnail and display copies of photos shared with the client."""
    project = get_project(request, pk)
    if size not in PHOTO_SIZES:
        raise Http404
    photo = get_object_or_404(SitePhoto, project=project, pk=photo_id)
    if is_internal(request):
        if not can_use_diary(request, project):
            raise PermissionDenied
    elif not (can_see_shared_photos(request) and photo.shared_with_client and size != 'original'):
        raise Http404
    field = {'thumb': photo.thumbnail, 'display': photo.display, 'original': photo.image}[size] or photo.image
    return serve(request, field, project=project, label=f'Site photo {photo.pk} ({size})')


# --- company settings ------------------------------------------------------------------

@admin_required
def diary_settings(request, org_slug):
    obj = DiarySettings.for_org(request.org)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Diary settings saved.')
        return redirect('diary_settings', org_slug=org_slug)
    return render(request, 'diary/settings.html', {'form': form})
