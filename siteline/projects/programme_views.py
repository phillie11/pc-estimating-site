"""
The Programme tab's sub-pages (Release 3): the look-ahead and MS Project XML import. The chart
itself is projects.views.programme.

  page              who                                         what
  ----------------  ------------------------------------------  --------------------------------------
  look-ahead        staff: every line                           2/3/4 weeks, filter by trade or sub,
                    client/consultant: shared lines              print, CSV (staff)
                    subcontractor: their own shared lines only
  import            staff                                       upload, preview, apply or discard

Every view fetches the project with projects.access.get_project() first (sections, external
roles, archived projects read only).
"""
import csv

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from tenancy.models import log_activity

from . import lookahead as la
from . import mspdi
from . import programme_import as importer
from .access import get_project, is_internal, require_internal
from .forms import ProgrammeUploadForm
from .models import Activity, ProgrammeImport
from .views import project_context


def _csv_safe(value):
    try:
        from core.exports import safe  # Stream F moves the shared helper here
    except ImportError:
        from levelling.exports import safe
    return safe(value)


def programme_nav(request, project, sub):
    org, pk = request.org.slug, project.pk
    nav = [('chart', 'Chart', reverse('programme', args=[org, pk])),
           ('lookahead', 'Look-ahead', reverse('programme_lookahead', args=[org, pk]))]
    if is_internal(request):
        nav.append(('import', 'Import from MS Project', reverse('programme_import', args=[org, pk])))
    return {'programme_nav': nav, 'programme_sub': sub}


def _ctx(request, project, sub, **extra):
    return project_context(request, project, 'programme', **programme_nav(request, project, sub), **extra)


def _sub_of(request):
    from delivery.access import subcontractor_id
    return subcontractor_id(request)


def visible_lines(request, project):
    """Programme lines this person may see on the look-ahead."""
    qs = Activity.objects.filter(project=project).select_related('subcontractor', 'trade', 'after')
    if is_internal(request):
        return qs
    qs = qs.filter(is_shared=True)
    pm = getattr(request, 'project_member', None)
    if pm is not None and pm.role == pm.ROLE_SUBCONTRACTOR:
        sub = _sub_of(request)
        return qs.filter(subcontractor_id=sub) if sub else qs.none()
    return qs


def hide_who(request, activities):
    """
    A subcontractor sees the shared programme, but not who else is on the job: on any line that isn't
    theirs, "who" (typed in, or MS Project resource names) and the subcontractor are blanked for this page.
    The rows are never saved.
    """
    pm = getattr(request, 'project_member', None)
    if is_internal(request) or pm is None or pm.role != pm.ROLE_SUBCONTRACTOR:
        return activities
    mine = _sub_of(request)
    for a in activities:
        if mine is None or a.subcontractor_id != mine:
            a.responsible, a.subcontractor = '', None
    return activities


def lookahead(request, org_slug, pk):
    project = get_project(request, pk)
    internal = is_internal(request)
    today = timezone.localdate()
    weeks = la.weeks_param(request.GET.get('weeks'))
    start, end = la.window(today, weeks)
    all_lines = list(visible_lines(request, project))
    rows = la.lines(all_lines, start, end, today=today)
    # Filter choices come from the lines in the look-ahead, so every option finds something
    trades = sorted({ln.activity.trade for ln in rows if ln.activity.trade_id}, key=lambda t: t.name)
    subs = sorted({ln.activity.subcontractor for ln in rows if ln.activity.subcontractor_id},
                  key=lambda s: s.company_name)
    trade, sub = request.GET.get('trade', ''), request.GET.get('sub', '')
    if trade.isdigit():
        rows = [ln for ln in rows if ln.activity.trade_id == int(trade)]
    if sub.isdigit() and (internal or _sub_of(request) is None):
        rows = [ln for ln in rows if ln.activity.subcontractor_id == int(sub)]
    filtered = trade.isdigit() or sub.isdigit()
    if request.GET.get('export') == 'csv' and internal:
        return _lookahead_csv(project, rows, start, end)
    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    return render(request, 'projects/lookahead.html', _ctx(
        request, project, 'lookahead', rows=page.object_list, page=page, start=start, end=end, weeks=weeks,
        week_choices=la.WEEK_CHOICES, grid=la.weeks_of(start, end), trades=trades, subs=subs,
        filters={'trade': trade, 'sub': sub}, filtered=filtered, today=today, has_programme=bool(all_lines),
        is_sub=_sub_of(request) is not None and not internal, printed=timezone.localtime()))


def _lookahead_csv(project, rows, start, end):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-lookahead-{start:%Y%m%d}.csv"'
    w = csv.writer(resp)
    w.writerow(['Activity', 'WBS', 'Who', 'Subcontractor', 'Trade', 'Start', 'Finish', '% done', 'Status'])
    for ln in rows:
        a = ln.activity
        w.writerow([_csv_safe(v) for v in (
            a.name, a.wbs, a.responsible, a.subcontractor.company_name if a.subcontractor_id else '',
            a.trade.name if a.trade_id else '', a.start.isoformat(), a.finish.isoformat(), str(a.progress),
            'Behind' if ln.behind else ('Milestone' if a.is_milestone else 'Planned'))])
    return resp


# --- MS Project XML import ------------------------------------------------------------

def programme_import(request, org_slug, pk):
    project = get_project(request, pk)
    require_internal(request)
    form = ProgrammeUploadForm()
    if request.method == 'POST':
        form = ProgrammeUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                imp = importer.stage(project, form.cleaned_data['file'], request.user)
            except mspdi.NotMspdi as e:
                form.add_error('file', str(e))
            else:
                return redirect('programme_import_preview', org_slug=org_slug, pk=pk, import_id=imp.pk)
    history = ProgrammeImport.objects.filter(project=project).select_related('uploaded_by', 'applied_by')[:20]
    imported = Activity.objects.filter(project=project, source_uid__isnull=False).count()
    return render(request, 'projects/programme_import.html', _ctx(
        request, project, 'import', form=form, history=history, imported=imported))


def programme_import_preview(request, org_slug, pk, import_id):
    project = get_project(request, pk)
    require_internal(request)
    imp = get_object_or_404(ProgrammeImport, project=project, pk=import_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        if imp.status != ProgrammeImport.PREVIEW:
            messages.error(request, 'This file has already been applied or discarded. Upload it again to re-import.')
            return redirect('programme_import', org_slug=org_slug, pk=pk)
        if action == 'discard':
            importer.discard(imp)
            messages.success(request, f'Discarded {imp.file_name}. Nothing on the programme changed.')
            return redirect('programme_import', org_slug=org_slug, pk=pk)
        if action == 'apply':
            try:
                result = importer.apply(imp, request.user, remove_missing=bool(request.POST.get('remove_missing')))
            except importer.Overtaken as e:
                importer.discard(imp)
                messages.error(request, str(e))
                return redirect('programme_import', org_slug=org_slug, pk=pk)
            log_activity(request, 'imported the programme from', imp.file_name, project=project,
                         url=reverse('programme', args=[org_slug, pk]), internal_only=True)
            messages.success(request, f"Programme updated from {imp.file_name}: {result['created']} added, "
                                      f"{result['updated']} changed, {result['removed']} removed.")
            return redirect('programme', org_slug=org_slug, pk=pk)
        messages.error(request, 'Choose "Apply to the programme" or "Discard".')
    plan = importer.compare(imp) if imp.status == ProgrammeImport.PREVIEW else None
    return render(request, 'projects/programme_import_preview.html', _ctx(
        request, project, 'import', imp=imp, plan=plan))
