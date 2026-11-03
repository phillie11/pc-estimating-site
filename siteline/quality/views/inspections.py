"""
Inspections (INS-): start one from a checklist, then go down the checks on a phone, each pass, fail
or N/A with a note and photo, raise a snag from a failed check in one tap, and finish it.
"""
import datetime

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition, available_transitions
from projects.models import Issue, ProjectMember
from projects.views import notify_assignee
from tenancy.models import log_activity

from .. import photos as photo_files
from .. import rules
from ..access import quality_project
from ..forms import FinishForm, InspectionStartForm, ReasonForm, ResultForm
from ..library import ensure_library
from ..models import Inspection, InspectionItem, QualityPhoto
from .common import csv_response, ctx, first_error, page_of

SNAG_DAYS = 7  # a snag raised from a failed check is due a week later; staff can change it on the snag

FILTERS = {
    'open': ('In progress', Q(status=Inspection.IN_PROGRESS)),
    'failed': ('Failed', Q(status=Inspection.FAILED)),
    'passed': ('Passed', Q(status=Inspection.PASSED)),
    'all': ('All', Q()),
}


def inspection_list(request, org_slug, pk):
    project = quality_project(request, pk)
    base = Inspection.objects.filter(project=project)
    counts = base.aggregate(**{k: Count('pk', filter=q) for k, (_, q) in FILTERS.items()})
    status = request.GET.get('status', '')
    if status not in FILTERS:
        status = 'open' if counts['open'] else 'all'
    qs = base.filter(FILTERS[status][1]).select_related('template', 'ball_in_court_user', 'package__subcontractor')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(area__icontains=q) | Q(ref__icontains=q))
    qs = qs.annotate(n_items=Count('items', distinct=True),
                     n_fail=Count('items', filter=Q(items__result=rules.FAIL), distinct=True),
                     n_done=Count('items', filter=~Q(items__result=''), distinct=True)).order_by('-number')
    if request.GET.get('export') == 'csv':
        return csv_response(f'{project.reference}-inspections.csv',
                            ['Ref', 'Checklist', 'Area', 'Whose work', 'Status', 'Checks', 'Failed', 'Inspector',
                             'Inspect by', 'Finished'],
                            [[i.ref, i.title, i.area, str(i.package.subcontractor) if i.package_id else '',
                              i.get_status_display(), i.n_items, i.n_fail,
                              (i.completed_by or i.ball_in_court_user or i.created_by).full_name
                              if (i.completed_by_id or i.ball_in_court_user_id or i.created_by_id) else '',
                              i.due_date.isoformat() if i.due_date else '',
                              timezone.localtime(i.completed_at).strftime('%Y-%m-%d %H:%M') if i.completed_at else '']
                             for i in qs.select_related('completed_by', 'created_by')])
    page = page_of(request, qs)
    return render(request, 'quality/inspection_list.html', ctx(
        request, project, 'inspections', page=page, inspections=page.object_list, counts=counts, status=status,
        q=q, filters=[(k, label, counts[k]) for k, (label, _) in FILTERS.items()], today=timezone.localdate()))


def inspection_create(request, org_slug, pk):
    project = quality_project(request, pk)
    ensure_library(request.org, request.user)
    form = InspectionStartForm(request.POST or None, project=project, user=request.user,
                               initial={'template': request.GET.get('template') or None})
    if request.method == 'POST' and form.is_valid():
        template = form.cleaned_data['template']
        area = form.cleaned_data['area']
        with transaction.atomic():
            ins = form.save(commit=False)
            ins.project = project
            ins.title = f'{template.name}{f", {area}" if area else ""}'[:200]
            ins.save(actor=request.user)
            InspectionItem.objects.bulk_create([
                InspectionItem(organization=ins.organization, inspection=ins, order=i.order, text=i.text)
                for i in template.items.all()])
        if ins.ball_in_court_user_id and ins.ball_in_court_user_id != request.user.pk:
            notify_assignee(request, ins.ball_in_court_user, f'{ins.ref}: inspect {ins.title}', project,
                            ins.get_absolute_url())
            messages.success(request, f'{ins.ref} started and sent to {ins.ball_in_court_user.full_name}. '
                                      'It is on their Needs me until it is finished.')
        else:
            messages.success(request, f'{ins.ref} started. Go down the checks: pass, fail or N/A.')
        log_activity(request, 'started inspection', f'{ins.ref} {ins.title}', project=project,
                     url=ins.get_absolute_url(), internal_only=True)
        return redirect(ins)
    return render(request, 'quality/inspection_form.html', ctx(request, project, 'inspections', form=form))


def _inspection(project, ins_id):
    photos = QualityPhoto.objects.order_by('uploaded_at', 'pk')
    items = InspectionItem.objects.select_related('snag', 'answered_by').prefetch_related(
        Prefetch('photos', queryset=photos))
    return get_object_or_404(
        Inspection.objects.select_related('template', 'package__subcontractor', 'ball_in_court_user', 'created_by',
                                          'completed_by').prefetch_related(Prefetch('items', queryset=items)),
        project=project, pk=ins_id)


def inspection_detail(request, org_slug, pk, ins_id):
    project = quality_project(request, pk)
    ins = _inspection(project, ins_id)
    items = list(ins.items.all())
    results = [i.result for i in items]
    outcome = rules.inspection_outcome(results)
    open_ = ins.status == Inspection.IN_PROGRESS and not project.is_archived
    return render(request, 'quality/inspection_detail.html', ctx(
        request, project, 'inspections', ins=ins, items=items, outcome=outcome, editable=open_,
        done=sum(1 for r in results if r), total=len(items), fails=results.count(rules.FAIL),
        transitions={t.name for t in available_transitions(ins, request.user)},
        finish_form=FinishForm(initial={'notes': ins.notes}), reason_form=ReasonForm()))


@require_POST
def inspection_item(request, org_slug, pk, ins_id, item_id):
    """Answer one check (the button pressed is the result), with an optional note and photo."""
    project = quality_project(request, pk)
    ins = get_object_or_404(Inspection, project=project, pk=ins_id)
    item = get_object_or_404(InspectionItem, inspection=ins, pk=item_id)
    back = ins.get_absolute_url() + f'#check-{item.pk}'
    if ins.status != Inspection.IN_PROGRESS:
        messages.error(request, f'{ins.ref} is {ins.get_status_display().lower()}, so its checks can\'t change. '
                                'The project manager can reopen it.')
        return redirect(back)
    form = ResultForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, first_error(form))
        return redirect(back)
    data = form.cleaned_data
    with transaction.atomic():
        if data['result']:
            item.result = data['result']
        item.note = data['note']
        if data['result'] or data['note'] or data['photo']:
            item.answered_by, item.answered_at = request.user, timezone.now()
        item.save()
        if data['photo']:
            photo_files.add_photo(project, request.user, data['photo'], item=item)
    if item.result == rules.FAIL and not item.snag_id:
        messages.warning(request, f'Failed: {item.text}. Raise a snag so it gets put right.')
    else:
        messages.success(request, f'{item.result_label}: {item.text}' if data['result'] else 'Saved.')
    return redirect(back)


def _sub_user(ins):
    """The subcontractor's person on this job for the inspection's package, for snags."""
    if not ins.package_id:
        return None
    m = ProjectMember.objects.filter(project_id=ins.project_id, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                     subcontractor_id=ins.package.subcontractor_id, user__is_active=True) \
        .select_related('user').order_by('pk').first()
    return m.user if m else None


@require_POST
def inspection_snag(request, org_slug, pk, ins_id, item_id):
    """Raise a snag from a failed check in one tap: the check, the note, the area and the first photo go with it."""
    project = quality_project(request, pk)
    ins = get_object_or_404(Inspection.objects.select_related('package__subcontractor'), project=project, pk=ins_id)
    item = get_object_or_404(InspectionItem.objects.prefetch_related('photos'), inspection=ins, pk=item_id)
    back = ins.get_absolute_url() + f'#check-{item.pk}'
    if item.result != rules.FAIL:
        messages.error(request, 'Only a failed check can raise a snag. Mark it as failed first.')
        return redirect(back)
    if item.snag_id:
        messages.info(request, f'{item.snag.reference} is already raised for this check.')
        return redirect(back)
    assignee = _sub_user(ins) or ins.ball_in_court_user or request.user
    with transaction.atomic():
        item = InspectionItem.objects.select_for_update().get(pk=item.pk)
        if item.snag_id:  # someone else pressed it a moment ago
            return redirect(back)
        snag = Issue(project=project, title=f'Failed check: {item.text}'[:200], issue_type='QUALITY',
                     description='\n\n'.join(x for x in (item.note, f'From inspection {ins.ref} {ins.title}.') if x),
                     location=ins.area[:200], assignee=assignee, created_by=request.user,
                     due_date=timezone.localdate() + datetime.timedelta(days=SNAG_DAYS))
        photo = next(iter(item.photos.all()), None)
        if photo is not None and photo.display:
            try:
                with photo.display.open('rb') as fh:
                    snag.photo.save(f'{ins.ref}-check-{item.pk}.jpg', ContentFile(fh.read()), save=False)
            except OSError:
                pass  # the snag still stands without the photo; the check keeps it
        snag.save()
        item.snag = snag
        item.save(update_fields=['snag'])
    path = reverse('issue_detail', args=[org_slug, pk, snag.pk])
    if assignee.pk != request.user.pk:
        notify_assignee(request, assignee, f'{snag.reference}: {snag.title}', project, path)
    log_activity(request, 'raised', f'{snag.reference} {snag.title}', project=project, url=path)
    who = 'you' if assignee.pk == request.user.pk else assignee.full_name or 'them'
    messages.success(request, f'{snag.reference} raised and assigned to {who}, due '
                              f'{snag.due_date:%a} {snag.due_date.day} {snag.due_date:%b}.')
    return redirect(back)


@require_POST
def inspection_action(request, org_slug, pk, ins_id):
    project = quality_project(request, pk)
    ins = get_object_or_404(Inspection, project=project, pk=ins_id)
    action = request.POST.get('action')
    try:
        if action == 'finish':
            form = FinishForm(request.POST)
            form.is_valid()
            results = list(ins.items.values_list('result', flat=True))
            outcome = rules.inspection_outcome(results)
            if outcome is None:
                left = sum(1 for r in results if not r)
                messages.error(request, f'{left} check{"s" if left != 1 else ""} still to answer. Mark each one '
                                        'pass, fail or N/A, then finish.')
                return redirect(ins)
            ins.notes = form.cleaned_data.get('notes', '') or ins.notes
            summary = (f'{results.count(rules.PASS)} pass, {results.count(rules.FAIL)} fail, '
                       f'{results.count(rules.NA)} N/A')
            apply_transition(ins, 'pass' if outcome == 'PASSED' else 'fail', request.user, note=summary)
            if outcome == 'PASSED':
                messages.success(request, f'{ins.ref} passed ({summary}).')
            else:
                messages.warning(request, f'{ins.ref} failed ({summary}). Snags raised from it are on the Snags tab.')
        elif action in ('reopen', 'cancel'):
            form = ReasonForm(request.POST)
            if action == 'reopen' and not form.is_valid():
                messages.error(request, 'Say why it is being reopened, so it is in the record.')
                return redirect(ins)
            note = form.cleaned_data.get('reason', '') if form.is_valid() else ''
            apply_transition(ins, action, request.user, note=note[:255])
            messages.success(request, f'{ins.ref} reopened. Change the checks, then finish it again.'
                             if action == 'reopen' else f'{ins.ref} cancelled. It stays on the register.')
        else:
            messages.error(request, 'That action is not available.')
    except (InvalidTransition, RecordConflict) as e:
        messages.error(request, str(e))
    return redirect(ins)
