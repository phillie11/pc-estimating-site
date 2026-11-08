"""
Permits and fire-stopping for an appointed subcontractor, in their portal (pending owner decision;
built as the recommended default).

  page                         who                      what
  ---------------------------  -----------------------  -----------------------------------------------
  Your permits                 an appointed sub         permits on their own company's packages on this
                                                        job, and asking for one (it goes to the PM to
                                                        approve, exactly like a request from our staff)
  Fire-stopping                an appointed sub         penetrations on their own company's packages;
                                                        recording one with before and after photos and
                                                        marking it sealed or resealed. It goes to the PM
                                                        to inspect and sign off (never the sub).
  everyone else                staff are sent to the staff page; clients, consultants and anyone not
                               appointed on the job get a 403; another company a 404 (get_project).

A subcontractor sees nothing of another subcontractor's permits or fire-stopping, no internal history,
and never the originals of photos (they keep the camera's location data). Archived jobs are read
only through get_project(). Needs me: a failed seal on their package (sub_items, a HookProvider for
externals registered in QualityConfig.ready()).
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from delivery.access import subcontractor_id
from directory.models import Package
from projects.access import get_project, is_internal
from projects.files import serve
from projects.models import ProjectMember
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from . import photos as photo_files
from . import rules
from .forms import PackageChoice, PenetrationForm, PenetrationPhotoForm, PermitForm
from .models import Penetration, Permit, QualityPhoto, QualitySettings
from .workflow import inspector_for, is_installer, permit_approver_for

PORTAL_SIZES = ('thumb', 'display')  # never the original, which keeps the camera's data


# --- access ---------------------------------------------------------------------------

def sub_project(request, pk, staff_page=None):
    """
    get_project() plus: an appointed subcontractor on a won, live or complete job. Staff are sent to
    their own page (raises _StaffRedirect, handled by the views); everyone else gets a 403.
    """
    project = get_project(request, pk)
    if is_internal(request):
        raise _StaffRedirect(staff_page)
    if subcontractor_id(request) is None or project.section != 'delivery':
        raise PermissionDenied
    return project


class _StaffRedirect(Exception):
    def __init__(self, name):
        super().__init__(name)
        self.name = name


def _for_staff(view):
    """Staff following a portal link land on the staff page for the same job."""
    from functools import wraps

    @wraps(view)
    def wrapped(request, org_slug, pk, *args, **kwargs):
        try:
            return view(request, org_slug, pk, *args, **kwargs)
        except _StaffRedirect as e:
            return redirect(e.name or 'inspection_list', org_slug=org_slug, pk=pk)
    return wrapped


def my_packages(request, project):
    """This subcontractor's appointed packages on the job (the only ones they can record against)."""
    return Package.objects.filter(project=project, subcontractor_id=subcontractor_id(request),
                                  status=Package.APPOINTED).select_related('subcontractor', 'trade')


def my_permits(request, project):
    return Permit.objects.filter(project=project, package__subcontractor_id=subcontractor_id(request))


def my_penetrations(request, project):
    return Penetration.objects.filter(project=project, package__subcontractor_id=subcontractor_id(request))


def tabs_for(request, project):
    """The portal tabs for an appointed subcontractor (no query: who they are is on the request)."""
    if is_internal(request) or subcontractor_id(request) is None or project.section != 'delivery':
        return []
    org, pk = request.org.slug, project.pk
    return [('permits', 'Your permits', reverse('portal_permits', args=[org, pk]), None),
            ('firestopping', 'Fire-stopping', reverse('portal_firestop', args=[org, pk]), None)]


def _ctx(request, project, tab, **extra):
    return project_context(request, project, tab, **extra)


def _choose_package(form, request, project):
    packages = my_packages(request, project)
    form.fields['package'] = PackageChoice(packages, required=True, label='Your package', empty_label=None,
                                           error_messages={'required': 'Choose which of your packages it is for.',
                                                           'invalid_choice': 'Choose one of your own packages.'})
    return packages


# --- permits --------------------------------------------------------------------------

class SubPermitForm(PermitForm):
    """The staff form, less the choice of subcontractor: it is always one of their own packages."""

    def __init__(self, *args, request=None, project=None, **kwargs):
        super().__init__(*args, project=project, org=request.org, **kwargs)
        self.packages = _choose_package(self, request, project)
        self.fields['holder'].help_text = 'Who is in charge of the work on site, if not you.'
        if not self.is_bound:
            self.initial.setdefault('holder', request.user.full_name)
            if len(self.packages) == 1:
                self.initial.setdefault('package', self.packages[0].pk)


@_for_staff
def permit_list(request, org_slug, pk):
    project = sub_project(request, pk, 'permit_list')
    warning = QualitySettings.for_org(request.org).expiry_warning_hours
    permits = list(my_permits(request, project).select_related('package__subcontractor', 'approved_by')
                   .order_by('-starts_at', '-pk'))
    for p in permits:
        p._warning_hours = warning
    live = [p for p in permits if p.status in Permit.OPEN_STATUSES]
    done = [p for p in permits if p.status not in Permit.OPEN_STATUSES]
    return render(request, 'quality/portal_permits.html', _ctx(
        request, project, 'permits', live=live, done=done, can_ask=my_packages(request, project).exists()))


@_for_staff
def permit_create(request, org_slug, pk):
    project = sub_project(request, pk, 'permit_create')
    form = SubPermitForm(request.POST or None, request=request, project=project)
    if not form.packages:
        messages.error(request, 'You have no package on this job yet, so you can\'t ask for a permit here. '
                                'Ask the site team.')
        return redirect('portal_permits', org_slug=org_slug, pk=pk)
    if request.method == 'POST' and form.is_valid():
        permit = form.save(commit=False)
        permit.project = project
        permit.ball_in_court_user = permit_approver_for(project, request.user)
        permit.save(actor=request.user)
        approver = permit.ball_in_court_user
        if approver is not None:
            notify.alert([approver], f'{permit.ref}: {permit.type_label} permit to approve ({permit.package.subcontractor})',
                         permit.get_absolute_url(), project=project, actor=request.user, org=request.org)
        log_activity(request, 'asked for a permit to work', f'{permit.ref} {permit.title}', project=project,
                     url=permit.get_absolute_url(), internal_only=True)
        messages.success(request, f'{permit.ref} sent to the site team to approve. Work must not start until it is '
                                  'approved. You\'ll be told when it is.')
        return redirect('portal_permit', org_slug=org_slug, pk=pk, permit_id=permit.pk)
    return render(request, 'quality/portal_permit_form.html', _ctx(request, project, 'permits', form=form,
                                                                   types=rules.PERMIT_TYPES))


@_for_staff
def permit_detail(request, org_slug, pk, permit_id):
    project = sub_project(request, pk, 'permit_list')
    permit = get_object_or_404(my_permits(request, project).select_related(
        'package__subcontractor', 'approved_by', 'closed_by', 'created_by'), pk=permit_id)
    settings = QualitySettings.for_org(request.org)
    permit._warning_hours = settings.expiry_warning_hours
    return render(request, 'quality/portal_permit.html', _ctx(
        request, project, 'permits', permit=permit, precautions=permit.precaution_list(),
        fire_watch_minutes=settings.fire_watch_minutes, advisory=rules.ADVISORY, now=timezone.now()))


# --- fire-stopping --------------------------------------------------------------------

class SubPenetrationForm(PenetrationForm):
    def __init__(self, *args, request=None, project=None, **kwargs):
        super().__init__(*args, project=project, **kwargs)
        self.packages = _choose_package(self, request, project)
        self.fields['installer'].help_text = 'The name of whoever did it.'
        self.fields['after_photo'].help_text = ('Adding this marks it as sealed and sends it to the site team to '
                                                'inspect and sign off.')
        if not self.is_bound:
            self.initial.setdefault('installer', request.user.full_name)
            if len(self.packages) == 1:
                self.initial.setdefault('package', self.packages[0].pk)


@_for_staff
def penetration_list(request, org_slug, pk):
    project = sub_project(request, pk, 'penetration_list')
    rows = list(my_penetrations(request, project).select_related('package__subcontractor').order_by('-number'))
    order = {Penetration.FAILED: 0, Penetration.TO_SEAL: 1, Penetration.SEALED: 2, Penetration.SIGNED_OFF: 3}
    rows.sort(key=lambda p: (order[p.status], -p.number))
    return render(request, 'quality/portal_firestop.html', _ctx(
        request, project, 'firestopping', rows=rows, can_add=my_packages(request, project).exists(),
        failed=sum(1 for p in rows if p.status == Penetration.FAILED)))


@_for_staff
def penetration_create(request, org_slug, pk):
    project = sub_project(request, pk, 'penetration_create')
    form = SubPenetrationForm(request.POST or None, request.FILES or None, request=request, project=project)
    if not form.packages:
        messages.error(request, 'You have no package on this job yet, so you can\'t record fire-stopping here. '
                                'Ask the site team.')
        return redirect('portal_firestop', org_slug=org_slug, pk=pk)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            p = form.save(commit=False)
            p.project = project
            p.title = p.default_title()
            p.save(actor=request.user)
            if data.get('before_photo'):
                photo_files.add_photo(project, request.user, data['before_photo'], penetration=p,
                                      stage=QualityPhoto.BEFORE)
            if data.get('after_photo'):
                photo_files.add_photo(project, request.user, data['after_photo'], penetration=p,
                                      stage=QualityPhoto.AFTER)
                apply_transition(p, 'seal', request.user, installed_on=p.installed_on)
        log_activity(request, 'added to the fire-stopping register', f'{p.ref} {p.location}', project=project,
                     url=p.get_absolute_url(), internal_only=True)
        if p.status == Penetration.SEALED:
            _tell_inspector(request, p)
            messages.success(request, f'{p.ref} recorded and sent to the site team to inspect and sign off.')
        else:
            messages.success(request, f'{p.ref} recorded. Add a photo of the finished seal and mark it sealed when '
                                      'it is done.')
        return redirect('portal_firestop_detail', org_slug=org_slug, pk=pk, pen_id=p.pk)
    return render(request, 'quality/portal_firestop_form.html', _ctx(request, project, 'firestopping', form=form))


def _tell_inspector(request, p):
    inspector = p.ball_in_court_user or inspector_for(p.project, request.user)
    if inspector is not None:
        notify.alert([inspector], f'{p.ref}: fire-stopping to inspect ({p.package.subcontractor})',
                     p.get_absolute_url(), project=p.project, actor=request.user, org=request.org)


def _penetration(request, project, pen_id):
    return get_object_or_404(my_penetrations(request, project).select_related('package__subcontractor').prefetch_related(
        Prefetch('photos', queryset=QualityPhoto.objects.order_by('uploaded_at', 'pk'))), pk=pen_id)


@_for_staff
def penetration_detail(request, org_slug, pk, pen_id):
    project = sub_project(request, pk, 'penetration_list')
    p = _penetration(request, project, pen_id)
    photos = list(p.photos.all())
    names = {t.name for t in available_transitions(p, request.user)} if not project.is_archived else set()
    return render(request, 'quality/portal_firestop_detail.html', _ctx(
        request, project, 'firestopping', p=p, transitions=names & {'seal', 'reseal'},
        can_add_photos=not project.is_archived and is_installer(p, request.user),
        before=[x for x in photos if x.stage == QualityPhoto.BEFORE],
        after=[x for x in photos if x.stage == QualityPhoto.AFTER],
        photo_form=PenetrationPhotoForm(initial={'stage': 'BEFORE' if p.status == Penetration.TO_SEAL else 'AFTER'})))


@require_POST
@_for_staff
def penetration_photo(request, org_slug, pk, pen_id):
    project = sub_project(request, pk, 'penetration_list')
    p = _penetration(request, project, pen_id)
    back = redirect('portal_firestop_detail', org_slug=org_slug, pk=pk, pen_id=p.pk)
    # Only while they still hold the work: the package still appointed to their company and them still on the
    # job as its subcontractor (the same test as sealing it). A sub who lost the package keeps read access to
    # what they recorded, never adds to it.
    if project.is_archived or not is_installer(p, request.user):
        raise PermissionDenied('This fire-stopping is no longer on a package appointed to you, so you can\'t add '
                               'photos to it.')
    if p.status in (Penetration.SEALED, Penetration.SIGNED_OFF):
        messages.error(request, f'{p.ref} is with the site team now, so its photos are fixed.' if
                       p.status == Penetration.SEALED else f'{p.ref} is signed off, so its photos are part of the record.')
        return back
    form = PenetrationPhotoForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, next((e[0] for e in form.errors.values() if e), 'Choose a photo to add.'))
        return back
    stage = form.cleaned_data['stage']
    with transaction.atomic():
        photo_files.add_photo(project, request.user, form.cleaned_data['photo'], penetration=p, stage=stage)
        p.save(actor=request.user, audit_action='photo added', audit_note='Before' if stage == 'BEFORE' else 'After')
    messages.success(request, 'Photo added.' if stage == 'BEFORE' else 'Photo added. Mark it sealed when it is done.')
    return back


@require_POST
@_for_staff
def penetration_action(request, org_slug, pk, pen_id):
    project = sub_project(request, pk, 'penetration_list')
    p = _penetration(request, project, pen_id)
    action = request.POST.get('action')
    back = redirect('portal_firestop_detail', org_slug=org_slug, pk=pk, pen_id=p.pk)
    if action not in ('seal', 'reseal'):
        raise Http404
    if not any(x.stage == QualityPhoto.AFTER for x in p.photos.all()):
        messages.error(request, 'Add a photo of the finished seal first. The site team needs it to sign it off.')
        return back
    try:
        apply_transition(p, action, request.user, installed_on=p.installed_on)
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
        return back
    _tell_inspector(request, p)
    log_activity(request, 'marked sealed' if action == 'seal' else 'marked resealed', f'{p.ref} {p.location}',
                 project=project, url=p.get_absolute_url(), internal_only=True)
    messages.success(request, f'{p.ref} marked {"sealed" if action == "seal" else "resealed"}. The site team '
                              'inspects it and signs it off.')
    return back


@_for_staff
def photo_file(request, org_slug, pk, photo_id, size):
    project = sub_project(request, pk, 'penetration_list')
    if size not in PORTAL_SIZES:
        raise Http404
    photo = get_object_or_404(QualityPhoto, project=project, pk=photo_id,
                              penetration__in=my_penetrations(request, project).values('pk'))
    field = {'thumb': photo.thumbnail, 'display': photo.display}[size] or photo.display or photo.thumbnail
    if not field:
        raise Http404  # never fall back to the original
    return serve(request, field, project=project, label=f'Quality photo {photo.pk} ({size})')


# --- Needs me for people outside the company -----------------------------------------

FAILED_SEAL, TO_SIGN = 'fs', 'dw'


def portal_rows(request):
    """
    [(kind, pk, ref, title, due, detail, project)] waiting on this external person from the wave 2 portal
    pages: FAILED_SEAL, a failed seal on their own company's package (an appointed sub); TO_SIGN, a daywork
    sheet they were asked to sign in their portal (client side, cvr/portal.py). One UNION query per request,
    shared by both Needs me providers because the badge runs on every page (plus one for the projects,
    only when there is something to list).
    """
    from tenancy.context import request_memo

    def fetch():
        from django.db.models import CharField, DateField, F, Value
        from django.db.models.functions import Cast

        from cvr.models import DayworkSheet
        from cvr.portal import CLIENT_ROLES
        from projects.models import Project
        cols = ('k', 'pk', 'project_id', 'r_ref', 'r_title', 'r_due', 'r_detail')
        as_sub = ProjectMember.objects.filter(project=OuterRef('project_id'), user=request.user,
                                              role=ProjectMember.ROLE_SUBCONTRACTOR,
                                              subcontractor_id=OuterRef('package__subcontractor_id'))
        seals = Penetration.objects.filter(status=Penetration.FAILED, package__status=Package.APPOINTED,
                                           project__archived_at__isnull=True).filter(Exists(as_sub)).annotate(
            k=Value(FAILED_SEAL, output_field=CharField()), r_ref=F('ref'), r_title=F('title'),
            r_due=Cast(Value(None), output_field=DateField()), r_detail=F('location')).values_list(*cols).order_by()
        as_client = ProjectMember.objects.filter(project=OuterRef('project_id'), user=request.user,
                                                 role__in=CLIENT_ROLES)
        sheets = DayworkSheet.objects.filter(sign_requested_from=request.user, status=DayworkSheet.DRAFT,
                                             project__archived_at__isnull=True).filter(Exists(as_client)).annotate(
            k=Value(TO_SIGN, output_field=CharField()), r_ref=F('ref'), r_title=F('title'),
            r_due=Cast(F('due_date'), output_field=DateField()), r_detail=F('location')).values_list(*cols).order_by()
        rows = list(seals.union(sheets, all=True))
        if not rows:
            return []
        projects = {p.pk: p for p in Project.objects.filter(pk__in={r[2] for r in rows})}
        return [(k, pk, ref, title, due, detail, projects[pid]) for k, pk, pid, ref, title, due, detail in rows
                if pid in projects]
    return request_memo(('portal_needs_me', request.user.pk), fetch)


def sub_items(request):
    """Failed seals on a subcontractor's own packages, on jobs they are on: reseal, then mark it resealed."""
    if request.membership is None or is_internal(request):
        return []
    from actions.registry import PRIORITY_HIGH, Item
    slug = request.org.slug
    return [Item(kind='firestopping', title=title, ref=ref, project=project, due=None,
                 url=reverse('portal_firestop_detail', args=[slug, project.pk, pk]),
                 action='Put the seal right, add a photo, then mark it resealed', status_label='Failed inspection',
                 tone='bad', priority=PRIORITY_HIGH, detail=detail, kind_label='Fire-stopping', key=f'fs-sub:{pk}')
            for kind, pk, ref, title, due, detail, project in portal_rows(request) if kind == FAILED_SEAL]
