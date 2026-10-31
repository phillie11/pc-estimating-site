import csv
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from projects.access import get_project, posted_id
from projects.models import ProjectMember
from projects.views import project_context
from tenancy.models import log_activity
from tenancy.permissions import admin_required, internal_required

from .forms import ContactForm, SubcontractorForm, TradeForm
from .models import Package, Subcontractor, SubcontractorContact, Trade
from .regions import NATIONWIDE, REGION_CHOICES, REGION_LABELS


def _subs(request):
    """The supply chain this person may open: archived subcontractors only for owners and admins."""
    qs = Subcontractor.objects.all()
    return qs if request.membership.is_admin else qs.filter(archived_at__isnull=True)


@internal_required
def sub_list(request, org_slug):
    qs = Subcontractor.objects.prefetch_related('trades')
    q = request.GET.get('q', '').strip()
    trade = request.GET.get('trade', '')
    status = request.GET.get('status', '')
    insurance = request.GET.get('insurance', '')
    region = request.GET.get('region', '')
    if status == 'ARCHIVED' and request.membership.is_admin:
        qs = qs.filter(archived_at__isnull=False)
    else:
        qs = qs.filter(archived_at__isnull=True)
    if q:
        qs = qs.filter(Q(company_name__icontains=q) | Q(contact_name__icontains=q) | Q(regions__icontains=q)
                       | Q(email__icontains=q))
    if trade.isdigit():
        qs = qs.filter(trades__id=int(trade))
    if status in dict(Subcontractor.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif not status:
        qs = qs.exclude(status=Subcontractor.STATUS_STRUCK_OFF)
    if insurance:
        soon = timezone.localdate() + timedelta(days=30)
        qs = qs.filter(Q(public_liability_expiry__lte=soon) | Q(employers_liability_expiry__lte=soon))
    qs = qs.distinct()
    if region in REGION_LABELS:
        # Regions are a JSON list, which SQLite can't search inside portably: filter in Python.
        # Nationwide subcontractors count for every region.
        qs = qs.filter(pk__in=[s.pk for s in qs.only('pk', 'region_codes')
                               if region in (s.region_codes or []) or NATIONWIDE in (s.region_codes or [])])
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="subcontractors.csv"'
        w = csv.writer(resp)
        w.writerow(['Company', 'Trades', 'Status', 'Contact', 'Email', 'Phone', 'Regions', 'Region notes', 'Rating',
                    'PL expiry', 'EL expiry', 'Accreditations'])
        for s in qs:
            w.writerow([s.company_name, '; '.join(t.name for t in s.trades.all()), s.get_status_display(),
                        s.contact_name, s.email, s.phone, '; '.join(s.region_labels), s.regions, s.rating or '',
                        s.public_liability_expiry or '', s.employers_liability_expiry or '', s.accreditations])
        return resp
    return render(request, 'directory/sub_list.html', {
        'subs': qs, 'trades': Trade.objects.annotate(n=Count('subcontractors')), 'q': q, 'trade': trade,
        'status': status, 'insurance': insurance, 'region': region, 'region_choices': REGION_CHOICES,
        'status_choices': Subcontractor.STATUS_CHOICES
        + ([('ARCHIVED', 'Archived')] if request.membership.is_admin else []),
    })


@internal_required
def sub_create(request, org_slug):
    form = SubcontractorForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        s = form.save()
        log_activity(request, 'added subcontractor', s.company_name, url=s.get_absolute_url())
        messages.success(request, f'{s.company_name} added.')
        return redirect(s)
    return render(request, 'directory/sub_form.html', {'form': form})


@internal_required
def sub_edit(request, org_slug, pk):
    sub = get_object_or_404(_subs(request), pk=pk)
    form = SubcontractorForm(request.POST or None, instance=sub)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Saved.')
        return redirect(sub)
    return render(request, 'directory/sub_form.html', {'form': form, 'sub': sub, 'has_records': sub.has_records()})


@require_POST
@admin_required
def sub_archive(request, org_slug, pk):
    """
    Archive a subcontractor with quotes, RAMS or variations on file, keeping all of it;
    restore one; or delete one with nothing on file (e.g. added twice by mistake).
    """
    sub = get_object_or_404(Subcontractor, pk=pk)
    action = request.POST.get('action')
    if action == 'restore':
        sub.restore()
        log_activity(request, 'restored subcontractor', sub.company_name, url=sub.get_absolute_url())
        messages.success(request, f'{sub.company_name} restored.')
        return redirect(sub)
    if action == 'delete':
        if sub.has_records():
            messages.error(request, f'{sub.company_name} has quotes, RAMS or variations on file, so it can only be archived.')
            return redirect('sub_edit', org_slug=org_slug, pk=pk)
        name = sub.company_name
        sub.delete()
        log_activity(request, 'deleted subcontractor', name)
        messages.success(request, f'{name} deleted.')
        return redirect('sub_list', org_slug=org_slug)
    if not sub.is_archived:
        sub.archive(request.user)
        log_activity(request, 'archived subcontractor', sub.company_name, url=sub.get_absolute_url())
        messages.success(request, f'{sub.company_name} archived. Their quotes, RAMS and variations are kept.')
    return redirect(f'{reverse("sub_list", args=[org_slug])}?status=ARCHIVED')


@internal_required
def sub_detail(request, org_slug, pk):
    sub = get_object_or_404(_subs(request).prefetch_related('trades'), pk=pk)
    contact_form = ContactForm()
    if request.method == 'POST':
        if request.POST.get('action') == 'contact':
            contact_form = ContactForm(request.POST)
            if contact_form.is_valid():
                c = contact_form.save(commit=False)
                c.subcontractor = sub
                c.save()
                return redirect(sub)
        elif request.POST.get('action') == 'delete_contact':
            SubcontractorContact.objects.filter(subcontractor=sub, pk=posted_id(request, 'contact')).delete()
            return redirect(sub)
    return render(request, 'directory/sub_detail.html', {
        'sub': sub, 'contacts': sub.contacts.all(), 'contact_form': contact_form,
        'packages': sub.packages.select_related('project', 'trade').order_by('-created_at'),
        'portal_members': ProjectMember.objects.filter(subcontractor=sub).select_related('user', 'project'),
    })


@internal_required
def trades(request, org_slug):
    form = TradeForm(request.POST or None)
    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            Trade.objects.filter(pk=posted_id(request, 'trade')).delete()
            return redirect('trades', org_slug=org_slug)
        if form.is_valid():
            name = form.cleaned_data['name'].strip()
            if not Trade.objects.filter(name__iexact=name).exists():
                Trade.objects.create(name=name)
            return redirect('trades', org_slug=org_slug)
    return render(request, 'directory/trades.html', {'trades': Trade.objects.annotate(n=Count('subcontractors')),
                                                     'form': form})


# ---------------------------------------------------------------------------
# Packages on a project
# ---------------------------------------------------------------------------

@internal_required
def package_list(request, org_slug, pk):
    """Who is pricing each trade, their quotes side by side, and who gets appointed."""
    from tenders import services
    from tenders.forms import TenderInviteForm
    project = get_project(request, pk)
    form = TenderInviteForm(project=project)
    back = redirect('package_list', org_slug=org_slug, pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'invite':
            form = TenderInviteForm(request.POST, project=project)
            if form.is_valid():
                d = form.cleaned_data
                sent = [s for s in d['subcontractors']
                        if services.invite(request, project, s, d['trade'], d['scope'], d['quote_due'])]
                skipped = len(d['subcontractors']) - len(sent)
                messages.success(request, f'Invited {len(sent)} to price {d["trade"]}.'
                                 + (f' {skipped} already invited for this trade.' if skipped else ''))
                return back
        else:
            pkg = get_object_or_404(Package.objects.select_related('subcontractor', 'trade', 'project'),
                                    project=project, pk=posted_id(request, 'package'))
            if action == 'appoint' and pkg.status in (Package.ENQUIRY, Package.QUOTED):
                services.appoint(request, pkg)
                messages.success(request, f'{pkg.subcontractor} appointed for {pkg.trade or "the package"}. '
                                          'They now have the project portal; other bidders were told.')
            elif action == 'unsuccessful' and pkg.is_open:
                services.mark_unsuccessful(request, pkg)
                messages.success(request, f'{pkg.subcontractor} told they were unsuccessful.')
            elif action == 'remind' and pkg.status == Package.ENQUIRY:
                from tenancy import notify
                notify.alert(services.sub_members(project, [pkg.subcontractor_id]),
                             f'Reminder: your quote for {pkg.trade or "the package"} on {project.name} is due '
                             f'{pkg.quote_due:%a} {pkg.quote_due.day} {pkg.quote_due:%b}' if pkg.quote_due
                             else f'Reminder: your quote for {pkg.trade or "the package"} on {project.name} is awaited',
                             services.tender_url(project), project=project, actor=request.user, org=request.org)
                messages.success(request, f'Reminder sent to {pkg.subcontractor}.')
            elif action == 'remove' and Package.objects.filter(pk=pkg.pk, commitment__isnull=False).exists():
                # Cost control (costs.Commitment) holds an order against it; the ledger can't lose its source
                messages.error(request, 'This package has an order in Costs; adjust the order instead.')
            elif action == 'remove':
                sub = pkg.subcontractor
                pkg.delete()
                services.revoke_if_finished(project, sub)
                messages.success(request, f'{sub} removed from this package.')
            elif action == 'note':
                pkg.notes = request.POST.get('notes', '')[:300]
                pkg.save(update_fields=['notes'])
            return back

    packages = Package.objects.filter(project=project).select_related('subcontractor', 'trade') \
        .annotate(has_order=Count('commitment'))
    groups = {}
    for p in packages:
        groups.setdefault(p.trade_id, {'trade': p.trade, 'rows': []})['rows'].append(p)
    for g in groups.values():
        quoted = [p.quote_value for p in g['rows'] if p.quote_value and p.status != Package.DECLINED]
        g['lowest'] = min(quoted) if quoted else None
        g['appointed'] = next((p for p in g['rows'] if p.status == Package.APPOINTED), None)
        g['rows'].sort(key=lambda p: (p.quote_value is None, p.quote_value or 0, p.subcontractor.company_name))
        for p in g['rows']:
            p.over_lowest = round((p.quote_value - g['lowest']) / g['lowest'] * 100, 1) \
                if g['lowest'] and p.quote_value and p.quote_value != g['lowest'] else None
    ordered = sorted(groups.values(), key=lambda g: g['trade'].name if g['trade'] else '')
    return render(request, 'directory/packages.html', project_context(
        request, project, 'packages', groups=ordered, form=form, today=timezone.localdate()))
