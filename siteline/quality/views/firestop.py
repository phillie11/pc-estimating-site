"""
The fire-stopping register (FS-): each penetration with where it is (optionally pinned on a drawing
from the viewer), what passes through, the rating, the system or product, who installed it, before
and after photos and the inspection sign-off. CSV for the golden thread (quality/exports.py).
"""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.exceptions import InvalidTransition, RecordConflict
from core.models import AuditEntry
from core.workflow import apply_transition, available_transitions
from tenancy.models import log_activity

from .. import exports
from .. import photos as photo_files
from .. import pins
from ..access import quality_project
from ..forms import NoteForm, PenetrationForm, PenetrationPhotoForm
from ..models import Penetration, QualityPhoto
from .common import csv_response, ctx, first_error, page_of

FILTERS = {
    'open': ('Not signed off', Q(status__in=(Penetration.TO_SEAL, Penetration.SEALED, Penetration.FAILED))),
    'to_seal': ('To seal', Q(status=Penetration.TO_SEAL)),
    'to_inspect': ('To inspect', Q(status=Penetration.SEALED)),
    'failed': ('Failed', Q(status=Penetration.FAILED)),
    'signed_off': ('Signed off', Q(status=Penetration.SIGNED_OFF)),
    'all': ('All', Q()),
}


def penetration_list(request, org_slug, pk):
    project = quality_project(request, pk)
    base = Penetration.objects.filter(project=project)
    counts = base.aggregate(**{k: Count('pk', filter=q) for k, (_, q) in FILTERS.items()})
    status = request.GET.get('status', '')
    if status not in FILTERS:
        status = 'all'
    qs = base.filter(FILTERS[status][1]).select_related('package__subcontractor', 'drawing', 'inspected_by')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(location__icontains=q) | Q(ref__icontains=q) | Q(system__icontains=q)
                       | Q(title__icontains=q) | Q(installer__icontains=q))
    rating = request.GET.get('rating', '')
    if rating.isdigit() and int(rating) in dict(Penetration.RATING_CHOICES):
        qs = qs.filter(fire_rating=int(rating))
    else:
        rating = ''
    if request.GET.get('export') == 'csv':
        header, rows = exports.register(project, qs)
        return csv_response(f'{project.reference}-fire-stopping-register.csv', header, rows)
    qs = qs.annotate(n_before=Count('photos', filter=Q(photos__stage=QualityPhoto.BEFORE)),
                     n_after=Count('photos', filter=Q(photos__stage=QualityPhoto.AFTER))).order_by('-number')
    page = page_of(request, qs)
    return render(request, 'quality/penetration_list.html', ctx(
        request, project, 'firestopping', page=page, penetrations=page.object_list, counts=counts, status=status,
        q=q, rating=rating, ratings=Penetration.RATING_CHOICES,
        filters=[(k, label, counts[k]) for k, (label, _) in FILTERS.items()], filtered=bool(q or rating or status != 'all')))


def penetration_create(request, org_slug, pk):
    project = quality_project(request, pk)
    from viewer.pins import pending_pin
    from viewer.pins import viewer_url as viewer_page
    pending = pending_pin(request, project)  # opened from the drawing viewer to pin a new one
    form = PenetrationForm(request.POST or None, request.FILES or None, project=project,
                           initial={'location': pending.location} if pending else None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            p = form.save(commit=False)
            p.project = project
            p.title = p.default_title()
            if pending:
                pins.apply_pending(p, pending)
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
        sealed = p.status == Penetration.SEALED
        messages.success(request, f'{p.ref} added{" and marked sealed, ready to inspect" if sealed else ""}.'
                         + ('' if sealed else ' Add a photo of the finished seal when it is done.'))
        if pending:
            return redirect(f'{viewer_page(request, project, pending.doc, pending.rev)}?pin=fs-{p.pk}')
        return redirect(p)
    return render(request, 'quality/penetration_form.html', ctx(request, project, 'firestopping', form=form,
                                                                penetration=None, pending_pin=pending))


def penetration_edit(request, org_slug, pk, pen_id):
    project = quality_project(request, pk)
    p = get_object_or_404(Penetration, project=project, pk=pen_id)
    form = PenetrationForm(request.POST or None, instance=p, project=project)
    if request.method == 'POST' and form.is_valid():
        try:
            obj = form.save(commit=False)
            obj.title = obj.default_title()
            obj.save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            messages.success(request, f'{p.ref} saved.')
            return redirect(p)
    return render(request, 'quality/penetration_form.html', ctx(request, project, 'firestopping', form=form,
                                                                penetration=p, pending_pin=None))


def penetration_detail(request, org_slug, pk, pen_id):
    project = quality_project(request, pk)
    p = get_object_or_404(
        Penetration.objects.select_related('package__subcontractor', 'drawing', 'drawing_revision', 'inspected_by',
                                           'created_by', 'ball_in_court_user')
        .prefetch_related(Prefetch('photos', queryset=QualityPhoto.objects.select_related('uploaded_by'))),
        project=project, pk=pen_id)
    photos = list(p.photos.all())
    audit = AuditEntry.objects.filter(record_idx__object_id=p.pk, record_idx__record_type='quality.penetration') \
        .select_related('actor').order_by('-at', '-pk')[:20]
    audit = list(audit)
    for a in audit:
        a.label = a.action.replace('_', ' ').capitalize()
    names = {t.name for t in available_transitions(p, request.user)} if not project.is_archived else set()
    return render(request, 'quality/penetration_detail.html', ctx(
        request, project, 'firestopping', p=p, transitions=names, audit=audit,
        before=[x for x in photos if x.stage == QualityPhoto.BEFORE],
        after=[x for x in photos if x.stage == QualityPhoto.AFTER],
        photo_form=PenetrationPhotoForm(initial={'stage': 'BEFORE' if p.status == Penetration.TO_SEAL else 'AFTER'}),
        note_form=NoteForm(), viewer_link=pins.viewer_url(request, p)))


@require_POST
def penetration_photo(request, org_slug, pk, pen_id):
    project = quality_project(request, pk)
    p = get_object_or_404(Penetration, project=project, pk=pen_id)
    if p.status == Penetration.SIGNED_OFF:
        messages.error(request, f'{p.ref} is signed off, so its photos are part of the record. Reopen it to add more.')
        return redirect(p)
    form = PenetrationPhotoForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, first_error(form))
        return redirect(p)
    stage = form.cleaned_data['stage']
    with transaction.atomic():
        photo_files.add_photo(project, request.user, form.cleaned_data['photo'], penetration=p, stage=stage)
        p.save(actor=request.user, audit_action='photo added', audit_note='Before' if stage == 'BEFORE' else 'After')
    messages.success(request, 'Photo added.' if stage == 'BEFORE' or p.status != Penetration.TO_SEAL else
                     'Photo added. Mark it sealed when the work is finished.')
    return redirect(p)


ACTION_MESSAGES = {
    'seal': '{ref} marked sealed. It is on the project manager\'s Needs me to inspect and sign off.',
    'reseal': '{ref} marked resealed. It is ready to inspect again.',
    'sign_off': '{ref} signed off.',
    'fail': '{ref} failed inspection. It is back with whoever recorded it to get resealed.',
    'reopen': '{ref} reopened. It needs sealing and signing off again.',
}


@require_POST
def penetration_action(request, org_slug, pk, pen_id):
    project = quality_project(request, pk)
    p = get_object_or_404(Penetration, project=project, pk=pen_id)
    action = request.POST.get('action')
    if action not in ACTION_MESSAGES:
        messages.error(request, 'That action is not available.')
        return redirect(p)
    form = NoteForm(request.POST)
    note = form.cleaned_data['note'] if form.is_valid() else ''
    try:
        apply_transition(p, action, request.user, note=note[:255], remark=note)
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
        return redirect('penetration_detail', org_slug=org_slug, pk=pk, pen_id=pen_id)
    messages.success(request, ACTION_MESSAGES[action].format(ref=p.ref))
    log_activity(request, {'seal': 'marked sealed', 'reseal': 'marked resealed', 'sign_off': 'signed off',
                           'fail': 'failed the inspection of', 'reopen': 'reopened'}[action],
                 f'{p.ref} {p.location}', project=project, url=p.get_absolute_url(), internal_only=True)
    return redirect(p)
