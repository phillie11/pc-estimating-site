"""
The Procurement tab (staff: Schedule | Submittals) and the submittals pages subcontractors and
designers see under Decisions. Every view fetches the project with get_project() and asks
procurement.access what this person may see.
"""
import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.exceptions import InvalidTransition, RecordConflict
from directory.models import Package
from projects.access import get_project, posted_id
from projects.files import serve
from projects.views import project_context
from tenancy.models import log_activity

from . import access, dates, services
from .forms import (DesignerReplyForm, ForwardForm, ReviewForm, ScheduleLineForm, SettingsForm, SubmitForm,
                    SubmittalForm)
from .models import ProcurementSettings, ScheduleLine, Submittal, SubmittalFile
from .workflow import reviewer_for


def _context(request, project, sub, **extra):
    if access.role(request) == access.STAFF:
        org, pk = request.org.slug, project.pk
        extra['procurement_nav'] = [
            ('schedule', 'Schedule', reverse('procurement_schedule', args=[org, pk])),
            ('submittals', 'Submittals', reverse('submittal_list', args=[org, pk])),
        ]
        extra['procurement_sub'] = sub
        return project_context(request, project, 'procurement', **extra)
    return project_context(request, project, 'submittals', **extra)


def _csv_cell(value):
    """Spreadsheet-safe text: a leading = + - @ would otherwise run as a formula in Excel."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def _csv(filename, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for row in rows:
        w.writerow([_csv_cell(v) for v in row])
    return resp


# ---------------------------------------------------------------------------
# Schedule (staff)
# ---------------------------------------------------------------------------

def schedule(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    today = timezone.localdate()
    lines = list(ScheduleLine.objects.filter(project=project).select_related('package__subcontractor', 'package__trade')
                 .order_by(F('order_by').asc(nulls_last=True), 'title'))
    counts = {code: 0 for code, _ in dates.RAG_CHOICES}
    for line in lines:
        line.rag_now = line.rag_on(today)
        counts[line.rag_now] += 1
    status = request.GET.get('status', '')
    rag = request.GET.get('rag', '')
    q = request.GET.get('q', '').strip()
    long_lead = request.GET.get('long_lead', '')
    shown = lines
    if status == 'open':
        shown = [x for x in shown if not x.is_done]
    elif status in dict(ScheduleLine.STATUS_CHOICES):
        shown = [x for x in shown if x.status == status]
    else:
        status = ''
    if rag in counts:
        shown = [x for x in shown if x.rag_now == rag]
    else:
        rag = ''
    if long_lead:
        shown = [x for x in shown if x.long_lead]
    if q:
        needle = q.lower()
        shown = [x for x in shown if needle in x.title.lower() or needle in (x.who or '').lower()]
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-procurement-schedule.csv',
                    ['Package or item', 'Long lead', 'Package / supplier', 'Status', 'Required on site',
                     'Lead time (weeks)', 'Approval period (weeks)', 'Design information by', 'Submittals due',
                     'Order by', 'RAG', 'Ordered on', 'Notes'],
                    [[x.title, 'Yes' if x.long_lead else '', x.who, x.get_status_display(), x.required_on_site,
                      x.lead_time_weeks, x.approval_weeks, x.design_info_by, x.submittals_due, x.order_by,
                      dates.RAG_LABELS[x.rag_now], x.ordered_on, x.notes] for x in shown])
    filtered = bool(status or rag or q or long_lead)
    return render(request, 'procurement/schedule.html', _context(
        request, project, 'schedule', lines=shown, counts=counts, total=len(lines), filtered=filtered,
        filters={'status': status, 'rag': rag, 'q': q, 'long_lead': long_lead}, today=today,
        status_choices=ScheduleLine.STATUS_CHOICES, rag_choices=dates.RAG_CHOICES, rag_labels=dates.RAG_LABELS,
        amber_days=dates.AMBER_DAYS))


def line_create(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    initial = {}
    package_id = request.GET.get('package', '')
    if package_id.isdigit() and package_id.isascii():
        package = Package.objects.filter(project=project, pk=int(package_id)).select_related('trade').first()
        if package:
            initial = {'package': package, 'title': str(package.trade or ''),
                       'status': ScheduleLine.APPOINTED if package.status == Package.APPOINTED else ScheduleLine.TENDER}
    form = ScheduleLineForm(request.POST or None, project=project, initial=initial)
    if request.method == 'POST' and form.is_valid():
        line = form.save(commit=False)
        line.project, line.created_by = project, request.user
        line.save()
        log_activity(request, 'added to the procurement schedule', line.title, project=project, internal_only=True,
                     url=reverse('procurement_schedule', args=[org_slug, pk]))
        messages.success(request, f'{line.title} added. Order by {line.order_by:%d %b %Y}.' if line.order_by
                         else f'{line.title} added. Add a required-on-site date to work out the order-by date.')
        return redirect('procurement_schedule', org_slug, pk)
    return render(request, 'procurement/line_form.html', _context(request, project, 'schedule', form=form, line=None))


def line_detail(request, org_slug, pk, line_id):
    project = get_project(request, pk)
    access.require_staff(request)
    line = get_object_or_404(ScheduleLine.objects.filter(project=project).select_related('package__subcontractor'),
                             pk=line_id)
    form = ScheduleLineForm(instance=line, project=project)
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'delete':
            if line.submittals.exists():
                messages.error(request, 'This line has submittals, so it can\'t be deleted. Withdraw them or mark '
                                        'the line delivered instead.')
                return redirect(line)
            title = line.title
            line.delete()
            messages.success(request, f'{title} removed from the schedule.')
            return redirect('procurement_schedule', org_slug, pk)
        if action == 'ordered':
            line.advance_to(ScheduleLine.ORDERED)
            messages.success(request, f'{line.title} marked as ordered.')
            return redirect('procurement_schedule', org_slug, pk)
        form = ScheduleLineForm(request.POST, instance=line, project=project)
        if form.is_valid():
            form.save()
            messages.success(request, f'{line.title} saved.')
            return redirect('procurement_schedule', org_slug, pk)
    submittals = line.submittals.select_related('package__subcontractor', 'ball_in_court_user',
                                                'ball_in_court_company')
    return render(request, 'procurement/line_form.html', _context(
        request, project, 'schedule', form=form, line=line, submittals=submittals))


def procurement_settings(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    settings_row = ProcurementSettings.for_project(project)
    form = SettingsForm(request.POST or None, instance=settings_row, org=request.org)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Submittal periods saved. They apply from the next submission or decision.')
        return redirect('submittal_list', org_slug, pk)
    return render(request, 'procurement/settings.html', _context(
        request, project, 'submittals', form=form, default_reviewer=project.manager or project.owner))


# ---------------------------------------------------------------------------
# Submittals
# ---------------------------------------------------------------------------

def _filter_submittals(request, qs):
    status = request.GET.get('status', 'open')
    kind = request.GET.get('kind', '')
    ball = request.GET.get('ball', '')
    if status == 'open':
        qs = qs.filter(status__in=Submittal.OPEN_STATUSES)
    elif status in dict(Submittal.STATUS_CHOICES):
        qs = qs.filter(status=status)
    else:
        status = 'all'
    if kind in dict(Submittal.KIND_CHOICES):
        qs = qs.filter(kind=kind)
    else:
        kind = ''
    if ball == 'mine':
        qs = qs.filter(ball_in_court_user=request.user, status__in=Submittal.OPEN_STATUSES)
    elif ball == 'subs':
        qs = qs.filter(status__in=Submittal.TO_SUBMIT, ball_in_court_company__isnull=False)
    elif ball == 'designer':
        qs = qs.filter(status=Submittal.WITH_CONSULTANT)
    else:
        ball = ''
    overdue = bool(request.GET.get('overdue'))
    if overdue:
        qs = qs.filter(status__in=Submittal.OPEN_STATUSES, due_date__lt=timezone.localdate())
    return qs, {'status': status, 'kind': kind, 'ball': ball, 'overdue': overdue}


def submittal_list(request, org_slug, pk):
    project = get_project(request, pk)
    who = access.role(request)
    qs = access.submittals_for(request, project).select_related(
        'line', 'package__subcontractor', 'package__trade', 'ball_in_court_user', 'ball_in_court_company')
    if who == access.SUB:
        return _sub_submittals(request, project, qs)
    if who == access.CONSULTANT:
        return render(request, 'procurement/consultant_list.html', _context(
            request, project, 'submittals',
            waiting=[s for s in qs if s.status == Submittal.WITH_CONSULTANT and s.ball_in_court_user_id == request.user.pk],
            done=[s for s in qs if not (s.status == Submittal.WITH_CONSULTANT and s.ball_in_court_user_id == request.user.pk)]))
    shown, filters = _filter_submittals(request, qs)
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-submittals.csv',
                    ['Ref', 'Rev', 'Submittal', 'Type', 'Procurement line', 'Subcontractor', 'Status', 'Ball in court',
                     'Due', 'Specification reference'],
                    [[s.ref, s.revision_label, s.title, s.get_kind_display(), s.line or '', s.subcontractor or '',
                      s.get_status_display(), s.ball_label(), s.due_date or '', s.spec_reference] for s in shown])
    settings_row = ProcurementSettings.for_project(project)
    return render(request, 'procurement/submittal_list.html', _context(
        request, project, 'submittals', submittals=shown, filters=filters, total=qs.count(),
        status_choices=Submittal.STATUS_CHOICES, kind_choices=Submittal.KIND_CHOICES, today=timezone.localdate(),
        settings_row=settings_row, reviewer=reviewer_for(project)))


def _sub_submittals(request, project, qs):
    """A subcontractor's view: submittals to make or resubmit (with due dates), what's coming up, and the rest."""
    packages = access.appointed_packages(request, project)
    upcoming = list(ScheduleLine.objects.filter(project=project, package__in=packages).exclude(
        status__in=ScheduleLine.DONE_STATUSES).exclude(submittals__isnull=False).order_by(
        F('submittals_due').asc(nulls_last=True), 'pk'))
    for line in upcoming:
        line.form = SubmitForm(prefix=f'l{line.pk}')
    if request.method == 'POST':
        if request.POST.get('action') != 'start':
            raise PermissionDenied
        line_id = posted_id(request, 'line')
        line = next((x for x in upcoming if x.pk == line_id), None)
        if line is None:
            raise Http404
        line.form = form = SubmitForm(request.POST, request.FILES, prefix=f'l{line.pk}')
        kind = request.POST.get('kind', Submittal.SHOP_DRAWING)
        if form.is_valid():
            with transaction.atomic():
                s = Submittal(project=project, line=line, package=line.package, title=f'{line.title} submittal'[:200],
                              kind=kind if kind in dict(Submittal.KIND_CHOICES) else Submittal.OTHER,
                              due_date=line.submittals_due)
                services.initial_ball(s)
                s.save(actor=request.user)
                services.submit(s, request.user, form.cleaned_data['files'], form.cleaned_data['note'])
            log_activity(request, 'sent a submittal', f'{s.ref} {s.title}', project=project, url=s.get_absolute_url())
            messages.success(request, f'{s.ref} sent to {project.organization.name} for review.')
            return redirect(s)
    to_make = [s for s in qs if s.status in Submittal.TO_SUBMIT]
    rest = [s for s in qs if s.status not in Submittal.TO_SUBMIT]
    return render(request, 'procurement/sub_list.html', _context(
        request, project, 'submittals', to_make=to_make, rest=rest, upcoming=upcoming,
        kind_choices=Submittal.KIND_CHOICES, today=timezone.localdate()))


def submittal_create(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    initial = {}
    line_id = request.GET.get('line', '')
    if line_id.isdigit() and line_id.isascii():
        line = ScheduleLine.objects.filter(project=project, pk=int(line_id)).first()
        if line:
            initial = {'line': line, 'package': line.package, 'title': f'{line.title} shop drawings'[:200],
                       'due_date': line.submittals_due}
    form = SubmittalForm(request.POST or None, project=project, initial=initial)
    if request.method == 'POST' and form.is_valid():
        s = form.save(commit=False)
        s.project = project
        services.initial_ball(s)
        s.save(actor=request.user)
        services.sync_line(s.line)
        if s.package_id:
            from delivery.views import sub_users
            from tenancy import notify
            notify.alert(sub_users(project, s.package.subcontractor_id), f'Submittal to make: {s.ref} {s.title}',
                         s.get_absolute_url(), project=project, actor=request.user)
        log_activity(request, 'asked for a submittal', f'{s.ref} {s.title}', project=project, internal_only=True,
                     url=s.get_absolute_url())
        messages.success(request, f'{s.ref} added. ' + (f'{s.subcontractor} can upload it in their portal.'
                                                        if s.package_id else 'Upload it when it arrives.'))
        return redirect(s)
    return render(request, 'procurement/submittal_form.html', _context(request, project, 'submittals', form=form, s=None))


def submittal_edit(request, org_slug, pk, sub_id):
    project = get_project(request, pk)
    access.require_staff(request)
    s = get_object_or_404(Submittal.objects.filter(project=project), pk=sub_id)
    form = SubmittalForm(request.POST or None, instance=s, project=project)
    if request.method == 'POST' and form.is_valid():
        try:
            s = form.save(commit=False)
            if s.status in Submittal.TO_SUBMIT and 'package' in form.changed_data:
                services.initial_ball(s)  # a different subcontractor now owes it
            s.save(actor=request.user)
        except RecordConflict:
            form.add_error(None, SubmittalForm.CONFLICT_MESSAGE)
        else:
            messages.success(request, f'{s.ref} saved.')
            return redirect(s)
    return render(request, 'procurement/submittal_form.html', _context(request, project, 'submittals', form=form, s=s))


def submittal_detail(request, org_slug, pk, sub_id):
    project = get_project(request, pk)
    who = access.role(request)
    s = get_object_or_404(access.submittals_for(request, project).select_related(
        'line', 'package__subcontractor', 'package__trade', 'ball_in_court_user', 'ball_in_court_company'), pk=sub_id)
    staff = who == access.STAFF
    submit_form = SubmitForm()
    review_form = ReviewForm()
    forward_form = ForwardForm(project=project) if staff else None
    reply_form = DesignerReplyForm()
    is_designer_now = s.status == Submittal.WITH_CONSULTANT and s.ball_in_court_user_id == request.user.pk
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'submit' and access.can_submit(request, s):
                submit_form = SubmitForm(request.POST, request.FILES)
                if submit_form.is_valid():
                    rev = services.submit(s, request.user, submit_form.cleaned_data['files'],
                                          submit_form.cleaned_data['note'])
                    log_activity(request, 'sent a submittal', f'{s.ref} rev {rev.label}', project=project,
                                 url=s.get_absolute_url(), internal_only=staff)
                    messages.success(request, f'{s.ref} rev {rev.label} sent for review. '
                                              f'Review due by {s.due_date:%d %b %Y}.')
                    return redirect(s)
            elif action == 'review' and staff and s.status in Submittal.IN_REVIEW:
                review_form = ReviewForm(request.POST, request.FILES)
                if review_form.is_valid():
                    d = review_form.cleaned_data
                    rev = services.review(s, request.user, d['code'], d['comments'], d['markups'])
                    log_activity(request, f'gave a submittal status {d["code"]}', f'{s.ref} rev {rev.label}',
                                 project=project, url=s.get_absolute_url(), internal_only=True)
                    messages.success(request, f'{s.ref} rev {rev.label}: {s.get_status_display()}.' + (
                        f' The procurement line is now {s.line.get_status_display().lower()}.' if s.line_id else ''))
                    return redirect(s)
            elif action == 'forward' and staff and s.status == Submittal.SUBMITTED:
                forward_form = ForwardForm(request.POST, project=project)
                if forward_form.is_valid():
                    services.forward(s, request.user, forward_form.cleaned_data['consultant'],
                                     forward_form.cleaned_data['note'])
                    messages.success(request, f'{s.ref} sent to {s.ball_in_court_user.full_name} for comment.')
                    return redirect(s)
            elif action == 'designer_reply' and (is_designer_now or (staff and s.status == Submittal.WITH_CONSULTANT)):
                reply_form = DesignerReplyForm(request.POST, request.FILES)
                if reply_form.is_valid():
                    services.designer_reply(s, request.user, reply_form.cleaned_data['comments'],
                                            reply_form.cleaned_data['files'])
                    messages.success(request, f'Comments on {s.ref} sent to {project.organization.name}.')
                    return redirect(s)
            elif action == 'withdraw' and staff and s.is_open:
                services.withdraw(s, request.user)
                messages.success(request, f'{s.ref} withdrawn.')
                return redirect(s)
            else:
                raise PermissionDenied
        except InvalidTransition as e:
            messages.error(request, str(e))
            return redirect(s)
        except RecordConflict:
            messages.error(request, 'Someone else changed this since you opened it. Check it and try again.')
            return redirect(s)
    revisions = list(access.revisions_for(request, s))
    for rev in revisions:
        rev.visible_files = [f for f in rev.files.all() if access.file_visible(request, f)]
    return render(request, 'procurement/submittal_detail.html', _context(
        request, project, 'submittals', s=s, revisions=revisions, who=who, staff=staff,
        submit_form=submit_form, review_form=review_form, forward_form=forward_form, reply_form=reply_form,
        can_submit=access.can_submit(request, s), can_review=staff and s.status in Submittal.IN_REVIEW,
        can_forward=staff and s.status == Submittal.SUBMITTED and forward_form.fields['consultant'].queryset.exists(),
        can_reply=is_designer_now or (staff and s.status == Submittal.WITH_CONSULTANT),
        ball=s.ball_label(external=not staff), settings_row=ProcurementSettings.for_project(project),
        today=timezone.localdate()))


def submittal_file(request, org_slug, pk, sub_id, file_id):
    project = get_project(request, pk)
    s = get_object_or_404(access.submittals_for(request, project), pk=sub_id)
    revisions = access.revisions_for(request, s)
    f = get_object_or_404(SubmittalFile.objects.filter(revision__in=revisions.values('pk')), pk=file_id)
    if not access.file_visible(request, f):
        raise Http404
    return serve(request, f.file, f.original_name or None, project=project,
                 label=f'{s.ref} rev {f.revision.label} {f.original_name}'[:250])

