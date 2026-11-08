"""
Quote mapping pages: "Map with AI" on a levelling sheet's quote, and the review page for the draft.

Access is the levelling's own (levelling/views.py _load): staff who work in estimating, and read only for the
won job's project manager or QS; clients, consultants, subcontractors and bidders get 403, another company 404.
Starting a mapping or accepting anything also needs the bid still in the pipeline (levelling's _editable),
estimating rights (the levelling refuses any write from anyone else), and, to start, AI drafting available to
this person (service.status). A draft shows only its own subcontractor's quote against our scope lines; no
other bidder's prices are on the page.
"""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from directory.models import Package
from levelling import maths, services as levelling
from levelling import views as lv
from projects.access import posted_id
from projects.views import project_context
from tenancy.models import log_activity

from . import quotemap
from .models import AIRun, QUOTE_MAPPING, QuoteMapDraft, QuoteMapExclusion, QuoteMapLine


def _load(request, pk, trade_id, package_id):
    project, trade = lv._load(request, pk, trade_id)  # the levelling's access rules, archived writes refused
    if not request.membership.can_estimate and request.method not in lv.SAFE:
        raise Http404
    package = get_object_or_404(Package.objects.select_related('subcontractor'), project=project, trade=trade,
                                pk=package_id)
    return project, trade, package


def _review_url(project, trade, package):
    return reverse('ai_quote_map', args=[project.organization.slug, project.pk, trade.pk, package.pk])


@require_POST
def quote_map_start(request, org_slug, pk, trade_id, package_id):
    from .views import _start
    project, trade, package = _load(request, pk, trade_id, package_id)
    back = lv._sheet_url(project, trade) + '#ai-map'
    if not lv._editable(project):
        return lv._closed(request, project, back)
    sheet = levelling.sheet_for(project, trade)
    if package.status in levelling.NOT_PRICING or not quotemap.can_map(package):
        messages.error(request, f'{package.subcontractor} has no PDF, Excel or CSV quote attached to map.')
        return redirect(back)
    if not sheet.pk or not sheet.lines.exists():
        messages.error(request, 'Add or import our scope lines first: the quote is mapped onto them.')
        return redirect(back)
    if _start(request, project, QUOTE_MAPPING, {'sheet': sheet.pk, 'package': package.pk}):
        log_activity(request, 'asked AI to map a quote', f'{package.subcontractor} · {trade}', project=project,
                     url=_review_url(project, trade, package), internal_only=True)
        messages.success(request, f'Reading {package.subcontractor}\'s quote. The mapping appears here in a minute '
                                  'or two, for you to check.')
    return redirect(_review_url(project, trade, package))


def quote_map(request, org_slug, pk, trade_id, package_id):
    project, trade, package = _load(request, pk, trade_id, package_id)
    url = _review_url(project, trade, package)
    editable = lv._editable(project) and request.membership.can_estimate
    sheet = levelling.sheet_for(project, trade)
    draft = None
    if sheet.pk:
        draft = QuoteMapDraft.objects.filter(sheet=sheet, package=package).exclude(
            status=QuoteMapDraft.DISCARDED).select_related('package', 'created_by').first()
    if request.method == 'POST':
        if not editable:
            return lv._closed(request, project, url)
        if draft is None or draft.status != QuoteMapDraft.OPEN:
            messages.error(request, 'That mapping has already been reviewed.')
            return redirect(url)
        return _act(request, project, trade, package, draft, url)
    lines = list(draft.lines.select_related('line', 'decided_by')) if draft else []
    for m in lines:
        m.value = maths.line_value(m.line.qty, m.rate, m.amount, m.included)
    mapped = {m.line_id for m in lines}
    unpriced = [ln for ln in sheet.lines.all() if ln.pk not in mapped] if (draft and sheet.pk) else []
    tie = quotemap.tie(draft, lines) if draft else None

    run = AIRun.objects.filter(project=project, feature=QUOTE_MAPPING, params__package=package.pk).first()
    return render(request, 'ai/quote_map.html', project_context(
        request, project, 'packages', trade=trade, package=package, draft=draft, lines=lines, unpriced=unpriced,
        exclusions=list(draft.exclusions.select_related('decided_by')) if draft else [], tie=tie, run=run,
        editable=editable and draft is not None and draft.status == QuoteMapDraft.OPEN,
        sheet_url=lv._sheet_url(project, trade)))


def _act(request, project, trade, package, draft, url):
    action = request.POST.get('action')
    who = request.user
    if action in ('accept_line', 'reject_line'):
        m = get_object_or_404(QuoteMapLine.objects.select_related('line', 'draft__package'), draft=draft,
                              pk=posted_id(request, 'item'), status=QuoteMapLine.PENDING)
        if action == 'accept_line':
            dropped = []
            quotemap.accept_line(m, who, dropped)
            lv._log_dropped(request, project, package, dropped, url)
            log_activity(request, 'accepted an AI-mapped price', f'{package.subcontractor} · {m.line}'[:255],
                         project=project, url=url, internal_only=True)
            messages.success(request, 'Price put in the levelling.')
        else:
            quotemap.reject(m, who)
            messages.success(request, 'Left out of the levelling.')
        return redirect(url + f'#m-{m.pk}')
    if action in ('accept_exclusion', 'reject_exclusion'):
        x = get_object_or_404(QuoteMapExclusion.objects.select_related('draft__sheet', 'draft__package'),
                              draft=draft, pk=posted_id(request, 'item'), status=QuoteMapExclusion.PENDING)
        if action == 'accept_exclusion':
            quotemap.accept_exclusion(x, who)
            messages.success(request, 'Added to the exclusions register, not priced yet.')
        else:
            quotemap.reject(x, who)
            messages.success(request, 'Left out of the register.')
        return redirect(url + '#exclusions')
    if action == 'accept_all':
        n, e, dropped = quotemap.accept_all(draft, who)
        lv._log_dropped(request, project, package, dropped, url)
        log_activity(request, 'accepted an AI quote mapping', f'{package.subcontractor} · {trade}: {n} prices, '
                     f'{e} exclusions', project=project, url=url, internal_only=True)
        messages.success(request, f'{n} price{"s" if n != 1 else ""} and {e} exclusion{"s" if e != 1 else ""} put in '
                                  'the levelling.')
        return redirect(lv._sheet_url(project, trade) + '#lines')
    if action == 'finish':
        draft.status = QuoteMapDraft.DONE
        draft.save(update_fields=['status'])
        messages.success(request, 'Mapping closed. Anything you didn\'t accept stays out of the levelling.')
        return redirect(lv._sheet_url(project, trade) + '#lines')
    if action == 'discard':
        draft.status = QuoteMapDraft.DISCARDED
        draft.save(update_fields=['status'])
        messages.success(request, 'Mapping discarded. Anything already accepted stays in the levelling.')
        return redirect(lv._sheet_url(project, trade) + '#ai-map')
    return redirect(url)
