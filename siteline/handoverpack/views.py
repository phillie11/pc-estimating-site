"""
Pricing workbook and handover pack pages. Every one comes through access.pack_project() (see the table in
access.py): staff only, and only staff who see the commercial figures on that project.

On a bid they sit under its Pricing tab; on a won job under Commercial > Handover pack.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from projects.access import posted_id
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import access, exports, pack as packs, reader, services
from .forms import ClarificationForm, OpenItemForm, SettingsForm, SiteRulesForm, UploadForm
from .models import HandoverPack, OpenItem, PackClarification, PricingWorkbook


def _url(name, project, *args):
    return reverse(name, args=[project.organization.slug, project.pk, *args])


def _context(request, project, sub, pack=None, **extra):
    nav = [('workbook', 'Pricing workbook', _url('hpack_workbook', project)),
           ('pack', 'Handover pack' if project.section != 'estimating' else 'Handover pack (preview)',
            _url('hpack_pack', project))]
    if pack is not None:
        nav += [('items', 'Open items', _url('hpack_items', project)),
                ('clarifications', 'Clarifications', _url('hpack_clarifications', project)),
                ('rules', 'Site rules', _url('hpack_site_rules', project))]
    extra.update(hpack_nav=nav, hpack_sub=sub, pack=pack, editable=access.can_edit(project),
                 is_bid=project.section == 'estimating')
    if project.section == 'estimating':
        from projects.views import project_context
        return project_context(request, project, 'pricing', **extra)
    from contracts.commercial import commercial_context
    return commercial_context(request, project, 'handover_pack', **extra)


def _pack(project):
    return HandoverPack.objects.filter(project=project).select_related('workbook', 'workbook__uploaded_by',
                                                                       'issued_by').first()


def _need_pack(request, project):
    p = _pack(project)
    if p is None:
        messages.info(request, 'Upload the pricing workbook first: the pack is built from it.')
    return p


def workbook_page(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _pack(project)
    revisions = list(PricingWorkbook.objects.filter(project=project).select_related('uploaded_by')
                     .defer('data').order_by('-revision')[:50])
    latest = pack.workbook if pack else None
    rows = []
    if latest is not None:
        previous = PricingWorkbook.objects.filter(project=project, revision__lt=latest.revision) \
            .order_by('-revision').first()
        rows = services.deltas(latest.result, previous.result if previous else None)
    for i, rev in enumerate(revisions):  # change in total charge against the one before
        before = revisions[i + 1] if i + 1 < len(revisions) else None
        rev.charge_delta = (rev.charge - before.charge) if before else None
    form = UploadForm()
    return render(request, 'handoverpack/workbook.html', _context(
        request, project, 'workbook', pack=pack, latest=latest, revisions=revisions, deltas=rows, form=form,
        can_upload=access.can_upload(project, pack), result=latest.result if latest else None))


@require_POST
def workbook_upload(request, org_slug, pk):
    project = access.pack_project(request, pk)
    back = redirect('hpack_workbook', org_slug=org_slug, pk=pk)
    pack = _pack(project)
    if not access.can_upload(project, pack):
        messages.error(request, 'The pack has been issued, so the workbook can\'t change.' if pack and pack.is_issued
                       else f'{project.name} is {project.get_stage_display().lower()}: no more uploads.')
        return back
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, 'Not uploaded: ' + ' '.join(e for errs in form.errors.values() for e in errs))
        return back
    try:
        wb = services.upload(project, request.user, form.cleaned_data['file'])
    except services.Busy as e:
        messages.error(request, f'Not uploaded: {e}')
        return back
    except reader.WorkbookRefused as e:
        messages.error(request, f'Not uploaded: {e}')
        return back
    except (ArithmeticError, InvalidOperation, ValueError, TypeError, KeyError, IndexError):
        messages.error(request, 'Not uploaded: Siteline couldn\'t make sense of that workbook. Check it is the '
                                'pricing workbook and was saved from Excel.')
        return back
    log_activity(request, 'uploaded the pricing workbook', f'revision {wb.revision}', project=project,
                 url=_url('hpack_workbook', project), internal_only=True)
    if wb.reconciled is False:
        messages.warning(request, f'Revision {wb.revision} uploaded, but its total does not match the pricing '
                                  'summary. See the reconciliation below.')
    elif wb.reconciled is None:
        messages.warning(request, f'Revision {wb.revision} uploaded. It had no values saved by Excel, so it could not be '
                                  'checked against Excel: the figures are Siteline\'s own recalculation.')
    else:
        messages.success(request, f'Revision {wb.revision} uploaded and reconciled to the penny.')
    return redirect(wb.get_absolute_url())


def revision_detail(request, org_slug, pk, rev_id):
    project = access.pack_project(request, pk)
    wb = get_object_or_404(PricingWorkbook.objects.select_related('uploaded_by'), project=project, pk=rev_id)
    previous = PricingWorkbook.objects.filter(project=project, revision__lt=wb.revision).order_by('-revision').first()
    result = wb.result
    return render(request, 'handoverpack/revision.html', _context(
        request, project, 'workbook', pack=_pack(project), wb=wb, previous=previous, result=result,
        deltas=services.deltas(result, previous.result if previous else None)))


def revision_file(request, org_slug, pk, rev_id):
    from projects.files import serve
    project = access.pack_project(request, pk)
    wb = get_object_or_404(PricingWorkbook, project=project, pk=rev_id)
    return serve(request, wb.file, download_name=wb.original_name, project=project,
                 label=f'Pricing workbook revision {wb.revision}')


def pack_page(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _pack(project)
    if pack is None:
        return render(request, 'handoverpack/pack.html', _context(request, project, 'pack', ctx=None))
    wb = pack.shown_workbook  # once issued, the revision it was issued from
    ctx = packs.build(project, pack, wb, request)
    ctx['cannot_issue'] = services.why_not_issue(pack, wb, project)
    ctx['unchecked_allowed'] = wb.reconciled is None and ctx['cannot_issue'] is None
    if request.GET.get('export') == 'xlsx':
        data = exports.build(ctx, request.org.name)
        log_activity(request, 'exported the handover pack', f'revision {wb.revision}', project=project,
                     url=_url('hpack_pack', project), internal_only=True)
        resp = HttpResponse(data, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{project.reference}-handover-pack-INTERNAL.xlsx"'
        resp['Cache-Control'] = 'private, no-store'
        return resp
    if request.GET.get('export'):
        raise Http404
    return render(request, 'handoverpack/pack.html', _context(request, project, 'pack', pack=pack, ctx=ctx))


@require_POST
def pack_issue(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _need_pack(request, project)
    if pack is None or not access.can_edit(project):
        return redirect('hpack_pack', org_slug=org_slug, pk=pk)
    try:
        pack = services.issue(pack, request.user)
    except services.CannotIssue as e:
        messages.error(request, f'Not issued: {e}')
        return redirect('hpack_pack', org_slug=org_slug, pk=pk)
    log_activity(request, 'issued the handover pack', f'revision {pack.issued_workbook.revision}', project=project,
                 url=_url('hpack_pack', project), internal_only=True)
    messages.success(request, 'Handover pack issued to the delivery team.')
    return redirect('hpack_pack', org_slug=org_slug, pk=pk)


def site_rules(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _need_pack(request, project)
    if pack is None:
        return redirect('hpack_workbook', org_slug=org_slug, pk=pk)
    form = SiteRulesForm(request.POST or None, initial={'site_rules': pack.site_rules})
    if request.method == 'POST':
        if not access.can_edit(project):
            raise Http404
        if form.is_valid():
            pack.site_rules = form.cleaned_data['site_rules']
            pack.save(update_fields=['site_rules', 'updated_at'])
            messages.success(request, 'Site rules saved.')
            return redirect(_url('hpack_pack', project) + '#rules')
    return render(request, 'handoverpack/site_rules.html', _context(request, project, 'rules', pack=pack, form=form))


def clarifications(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _need_pack(request, project)
    if pack is None:
        return redirect('hpack_workbook', org_slug=org_slug, pk=pk)
    form = ClarificationForm(request.POST or None, prefix='new')
    if request.method == 'POST':
        if not access.can_edit(project):
            raise Http404
        if form.is_valid():
            c = form.save(commit=False)
            c.pack, c.source, c.edited = pack, PackClarification.FROM_SITELINE, True
            c.order = 100000 + pack.clarifications.count()
            c.save()
            messages.success(request, 'Clarification added.')
            return redirect('hpack_clarifications', org_slug=org_slug, pk=pk)
    from . import clarifications as drafting
    return render(request, 'handoverpack/clarifications.html', _context(
        request, project, 'clarifications', pack=pack, form=form, rows=list(pack.visible_clarifications()),
        drafting=drafting.available()))


def clarification_edit(request, org_slug, pk, cl_id):
    project = access.pack_project(request, pk)
    pack = _pack(project)
    if pack is None:
        raise Http404
    c = get_object_or_404(PackClarification, pack=pack, pk=cl_id, hidden=False)
    form = ClarificationForm(request.POST or None, instance=c)
    if request.method == 'POST':
        if not access.can_edit(project):
            raise Http404
        if request.POST.get('delete'):
            if c.source == PackClarification.FROM_WORKBOOK:  # hidden, so the next upload doesn't bring it back
                c.hidden = c.edited = True
                c.save(update_fields=['hidden', 'edited', 'updated_at'])
            else:
                c.delete()
            messages.success(request, 'Clarification removed.')
            return redirect('hpack_clarifications', org_slug=org_slug, pk=pk)
        if form.is_valid():
            c = form.save(commit=False)
            c.edited = True
            c.save()
            messages.success(request, 'Clarification saved.')
            return redirect('hpack_clarifications', org_slug=org_slug, pk=pk)
    return render(request, 'handoverpack/clarification_form.html', _context(
        request, project, 'clarifications', pack=pack, form=form, row=c))


def open_items(request, org_slug, pk):
    project = access.pack_project(request, pk)
    pack = _need_pack(request, project)
    if pack is None:
        return redirect('hpack_workbook', org_slug=org_slug, pk=pk)
    owners = access.possible_owners(project)
    if request.method == 'POST':
        if not access.can_edit(project):
            raise Http404
        item = get_object_or_404(OpenItem, pack=pack, pk=posted_id(request, 'item'))
        before = (item.owner_id, item.status)
        form = OpenItemForm(request.POST, instance=item, owners=owners, prefix=f'i{item.pk}')
        if form.is_valid():
            item = form.save(commit=False)
            if item.status == OpenItem.DONE and before[1] != OpenItem.DONE:
                item.closed_at, item.closed_by, item.cleared_by_upload = timezone.now(), request.user, False
            elif item.status == OpenItem.OPEN:
                item.closed_at = item.closed_by = None
            item.save()
            if item.owner_id and item.owner_id != before[0] and item.owner_id != request.user.pk:
                from tenancy import notify
                notify.alert([item.owner], f'Handover open item for you: {item.title}', item.get_absolute_url(),
                             project=project, actor=request.user, org=request.org)
            messages.success(request, 'Open item saved.')
            return redirect(item.get_absolute_url())
        messages.error(request, 'Not saved: ' + ' '.join(e for errs in form.errors.values() for e in errs))
        return redirect(item.get_absolute_url())
    rows = sorted(pack.items.select_related('owner', 'closed_by'),
                  key=lambda i: (i.status != OpenItem.OPEN, i.amount is None, -(i.amount or Decimal(0)), i.pk))
    show = request.GET.get('show', 'open')
    if show != 'all':
        rows = [i for i in rows if i.status == OpenItem.OPEN]
    choices = [('', 'Nobody yet')] + [(u.pk, u.full_name or u.email) for u in owners]  # one query for every form
    for i in rows:
        i.form = OpenItemForm(instance=i, owners=owners, prefix=f'i{i.pk}')
        i.form.fields['owner'].choices = choices
    return render(request, 'handoverpack/items.html', _context(
        request, project, 'items', pack=pack, rows=rows, show=show,
        open_total=sum((i.amount or Decimal(0)) for i in rows if i.status == OpenItem.OPEN)))


@admin_required
def settings_page(request, org_slug):
    obj = services.settings_for(request.org)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Pricing workbook settings saved.')
        return redirect('hpack_settings', org_slug=org_slug)
    return render(request, 'handoverpack/settings.html', {'form': form, 'custom': bool(obj.custom_mapping)})
