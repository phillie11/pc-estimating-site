"""
AI drafting pages: Settings > AI drafting (switch, budget, who, company profile), the playbooks, the usage page,
the bid brief on a bid, and the button that drafts a handover pack's clarifications. See access.py for who.

No page here calls the model: starting a draft queues an AIRun for the worker (ai/tasks.py).
"""
from datetime import datetime, time

from django.contrib import messages
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exports import writer
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import access, brief as briefs, registry, service, tasks
from .forms import ItemForm, PlaybookForm, ReadForm, SettingsForm, SummaryForm
from .models import AIRun, AISettings, AIUsage, BidBrief, BriefItem, CLARIFICATIONS, Playbook, TENDER_BRIEF


# --- Settings, playbooks and usage (owners and admins) ------------------------------------------------------------

@admin_required
def settings_page(request, org_slug):
    obj = AISettings.for_org(request.org)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        s = form.save(commit=False)
        s.updated_by = request.user
        s.save()
        log_activity(request, 'changed AI drafting settings', str(s), internal_only=True)
        messages.success(request, 'AI drafting settings saved.')
        return redirect('ai_settings', org_slug=org_slug)
    newest = {}
    for p in Playbook.objects.filter(task__in=list(registry.TASKS)).order_by('-version').select_related('created_by'):
        newest.setdefault(p.task, p)
    playbooks = [{'task': t, 'row': newest.get(t.key), 'own': bool(newest.get(t.key) and newest[t.key].text.strip()),
                  'url': reverse('ai_playbook', args=[org_slug, t.key])} for t in registry.TASKS.values()]
    return render(request, 'ai/settings.html', {
        'form': form, 'server_ready': service.server_ready(), 'spent': service.month_spend(request.org),
        'ai': obj, 'playbooks': playbooks})


@admin_required
def playbook_page(request, org_slug, task):
    t = registry.TASKS.get(task)
    if t is None:
        raise Http404
    history = list(Playbook.objects.filter(task=task).select_related('created_by')[:10])
    current = history[0] if history else None
    own = bool(current and current.text.strip())
    form = PlaybookForm(request.POST or None, initial={'text': current.text if own else t.default_playbook})
    if request.method == 'POST':
        reset = bool(request.POST.get('reset'))
        if reset or form.is_valid():
            text = '' if reset else form.cleaned_data['text'].strip()
            if text == t.default_playbook.strip():
                text = ''  # saving the default unchanged keeps following Siteline's default
            if text != (current.text if current else ''):
                Playbook.objects.create(task=task, version=(current.version if current else 0) + 1, text=text,
                                        created_by=request.user)
                log_activity(request, 'changed an AI playbook', t.title, internal_only=True)
            messages.success(request, 'Back to Siteline\'s default playbook.' if not text else 'Playbook saved.')
            return redirect('ai_playbook', org_slug=org_slug, task=task)
    return render(request, 'ai/playbook.html', {'t': t, 'form': form, 'history': history, 'own': own,
                                                'max_chars': registry.PLAYBOOK_MAX_CHARS})


def _month(request):
    """The month asked for (?month=2026-10), else this one: (start, end, 'YYYY-MM')."""
    start = service.month_start()
    raw = request.GET.get('month', '')
    if len(raw) == 7 and raw[:4].isdigit() and raw[5:].isdigit() and raw[4] == '-' and 1 <= int(raw[5:]) <= 12:
        start = timezone.make_aware(datetime.combine(datetime(int(raw[:4]), int(raw[5:]), 1).date(), time.min))
    nxt = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return start, nxt, start.strftime('%Y-%m')


@admin_required
def usage_page(request, org_slug):
    start, end, key = _month(request)
    rows = AIUsage.objects.filter(created_at__gte=start, created_at__lt=end)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="ai-usage-{key}.csv"'
        resp['Cache-Control'] = 'private, no-store'
        w = writer(resp)
        w.writerow(['When', 'Who', 'Feature', 'Project', 'Model', 'Status', 'Input tokens', 'Cached input tokens',
                    'Cache write tokens', 'Output tokens', 'Cost (GBP)', 'Note'])
        for u in rows.select_related('user', 'project').order_by('created_at')[:5000]:
            w.writerow([timezone.localtime(u.created_at).strftime('%d/%m/%Y %H:%M'),
                        (u.user.full_name or u.user.email) if u.user else '', u.get_feature_display(),
                        u.project.name if u.project else '', u.model, u.get_status_display(), u.input_tokens,
                        u.cache_read_tokens, u.cache_write_tokens, u.output_tokens, f'{u.cost_gbp:.4f}', u.note])
        log_activity(request, 'exported AI usage', key, internal_only=True)
        return resp
    ai = AISettings.for_org(request.org)
    total = rows.aggregate(t=Sum('cost_gbp'), n=Count('pk'))
    by_user = list(rows.values('user__full_name', 'user__email').annotate(cost=Sum('cost_gbp'), calls=Count('pk'))
                   .order_by('-cost'))
    by_feature = list(rows.values('feature').annotate(cost=Sum('cost_gbp'), calls=Count('pk')).order_by('-cost'))
    labels = dict(AIUsage._meta.get_field('feature').choices)
    for f in by_feature:
        f['label'] = labels.get(f['feature'], f['feature'])
    prev = (start - timezone.timedelta(days=1)).strftime('%Y-%m')
    return render(request, 'ai/usage.html', {
        'ai': ai, 'total': total['t'] or 0, 'calls': total['n'], 'by_user': by_user, 'by_feature': by_feature,
        'recent': list(rows.select_related('user', 'project').order_by('-created_at')[:50]),
        'month': start, 'month_key': key, 'prev': prev,
        'next': end.strftime('%Y-%m') if end <= service.month_start() else '',
        'is_current': start == service.month_start(), 'server_ready': service.server_ready()})


# --- Handover pack clarifications -----------------------------------------------------------------------------------

def _open_run(project, feature):
    return AIRun.objects.filter(project=project, feature=feature, status__in=(AIRun.QUEUED, AIRun.RUNNING)).first()


def _start(request, project, feature, params=None):
    usable, reason = service.status(request.org, request.membership)
    if not usable:
        messages.error(request, reason or 'AI drafting isn\'t available to you.')
        return None
    if _open_run(project, feature):
        messages.info(request, 'A draft is already on its way. This page shows when it is done.')
        return None
    run = AIRun.objects.create(project=project, feature=feature, requested_by=request.user, params=params or {})
    tasks.queue(run)
    return run


@require_POST
def draft_clarifications(request, org_slug, pk):
    from handoverpack import access as pack_access
    project = pack_access.pack_project(request, pk)
    back = redirect('hpack_clarifications', org_slug=org_slug, pk=pk)
    pack = getattr(project, 'handover_pack', None)
    if pack is None or not pack_access.can_edit(project):
        raise Http404
    if _start(request, project, CLARIFICATIONS):
        log_activity(request, 'asked AI to draft handover clarifications', project.name, project=project,
                     url=reverse('hpack_clarifications', args=[org_slug, pk]), internal_only=True)
        messages.success(request, 'Drafting clarifications. They appear below, marked AI draft, in a minute or two.')
    return back


# --- Bid brief (tender reader) ------------------------------------------------------------------------------------

def _brief_context(request, project, **extra):
    from projects.views import project_context
    return project_context(request, project, 'brief', **extra)


def brief_page(request, org_slug, pk):
    project = access.brief_project(request, pk)
    brief = BidBrief.objects.filter(project=project).select_related('generated_by').first()
    editable = access.can_edit_brief(project)
    if request.GET.get('export') == 'csv' and brief is not None:
        return _brief_csv(request, project, brief)
    items = list(brief.items.select_related('accepted_by')) if brief else []
    sections = []
    for key, title in BriefItem.SECTION_CHOICES:
        rows = [i for i in items if i.section == key]
        if rows:
            sections.append({'key': key, 'title': title, 'rows': rows})
    usable, reason = service.status(request.org, request.membership)
    revisions = briefs.choices(project) if editable else []
    form = SummaryForm(request.POST or None, instance=brief) if brief else None
    add = ItemForm(prefix='add')
    if request.method == 'POST':
        if not editable:
            raise Http404
        if request.POST.get('action') == 'add':
            add = ItemForm(request.POST, prefix='add')
            if add.is_valid():
                b = brief or BidBrief.objects.create(project=project)
                item = add.save(commit=False)
                item.brief, item.edited, item.order = b, True, BriefItem.SECTIONS.index(item.section) * 1000 + 999
                item.save()
                messages.success(request, 'Line added.')
                return redirect('ai_brief', org_slug=org_slug, pk=pk)
        elif form is not None and form.is_valid():
            b = form.save(commit=False)
            b.edited = True
            b.save()
            messages.success(request, 'Brief saved.')
            return redirect('ai_brief', org_slug=org_slug, pk=pk)
    return render(request, 'ai/brief.html', _brief_context(
        request, project, brief=brief, sections=sections, editable=editable, usable=usable, reason=reason,
        read_form=ReadForm(revisions=revisions) if revisions else None, form=form, add=add,
        run=AIRun.objects.filter(project=project, feature=TENDER_BRIEF).first(),
        max_pages=_settings().AI_TENDER_MAX_PAGES, max_mb=_settings().AI_TENDER_MAX_MB))


def _settings():
    from django.conf import settings
    return settings


def _brief_csv(request, project, brief):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-bid-brief.csv"'
    resp['Cache-Control'] = 'private, no-store'
    w = writer(resp)
    w.writerow(['Section', 'Item', 'Text', 'Date', 'Rating', 'Low (GBP)', 'High (GBP)', 'Source', 'AI drafted',
                'Changed by hand'])
    w.writerow(['Recommendation', brief.get_recommendation_display(), brief.summary, '', '', '', '', '',
                'yes' if brief.ai_drafted else 'no', 'yes' if brief.edited else 'no'])
    if brief.value_basis:
        w.writerow(['Basis of the value range', '', brief.value_basis, '', '', '', '', '', '', ''])
    for i in brief.items.all():
        w.writerow([i.get_section_display(), i.label, i.text, i.date.strftime('%d/%m/%Y') if i.date else '',
                    i.get_severity_display(), i.value_low or '', i.value_high or '', i.source_text,
                    'yes' if i.ai_drafted else 'no', 'yes' if i.edited else 'no'])
    log_activity(request, 'exported the bid brief', project.name, project=project, internal_only=True)
    return resp


@require_POST
def brief_read(request, org_slug, pk):
    project = access.brief_project(request, pk)
    if not access.can_edit_brief(project):
        raise Http404
    form = ReadForm(request.POST, revisions=briefs.choices(project))
    if not form.is_valid():
        messages.error(request, 'Choose at least one document to read.')
        return redirect('ai_brief', org_slug=org_slug, pk=pk)
    ids = form.cleaned_data['revisions'][:50]
    if _start(request, project, TENDER_BRIEF, {'revisions': ids}):
        log_activity(request, 'asked AI to read the tender documents', project.name, project=project,
                     url=reverse('ai_brief', args=[org_slug, pk]), internal_only=True)
        messages.success(request, 'Reading the documents. The brief appears here in a few minutes.')
    return redirect('ai_brief', org_slug=org_slug, pk=pk)


def brief_item(request, org_slug, pk, item_id):
    project = access.brief_project(request, pk)
    item = get_object_or_404(BriefItem, brief__project=project, pk=item_id)
    form = ItemForm(request.POST or None, instance=item)
    if request.method == 'POST':
        if not access.can_edit_brief(project):
            raise Http404
        if request.POST.get('delete'):
            item.delete()
            messages.success(request, 'Line removed.')
            return redirect('ai_brief', org_slug=org_slug, pk=pk)
        if form.is_valid():
            item = form.save(commit=False)
            item.edited = True
            item.save()
            messages.success(request, 'Line saved.')
            return redirect(reverse('ai_brief', args=[org_slug, pk]) + f'#item-{item.pk}')
    return render(request, 'ai/brief_item.html', _brief_context(request, project, item=item, form=form,
                                                                editable=access.can_edit_brief(project)))


@require_POST
def brief_accept(request, org_slug, pk, item_id):
    project = access.brief_project(request, pk)
    if not access.can_edit_brief(project):
        raise Http404
    item = get_object_or_404(BriefItem.objects.select_related('brief__project'), brief__project=project, pk=item_id)
    field = briefs.accept(item, request.user)
    if field is None:
        messages.error(request, 'That line has no date the project keeps.')
    else:
        log_activity(request, f'accepted a bid brief date as the {field}', project.name, project=project,
                     url=reverse('ai_brief', args=[org_slug, pk]), internal_only=True)
        messages.success(request, f'{field.capitalize()} set to {item.date:%d/%m/%Y}.')
    return redirect(reverse('ai_brief', args=[org_slug, pk]) + f'#item-{item.pk}')
