import csv
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from projects.access import get_project
from projects.models import ProjectInvite, ProjectMember
from projects.views import project_context, send_project_invite
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .forms import ContactForm, PackageForm, SubcontractorForm, TradeForm
from .models import Package, Subcontractor, SubcontractorContact, Trade


@internal_required
def sub_list(request, org_slug):
    qs = Subcontractor.objects.prefetch_related('trades')
    q = request.GET.get('q', '').strip()
    trade = request.GET.get('trade', '')
    status = request.GET.get('status', '')
    insurance = request.GET.get('insurance', '')
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
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="subcontractors.csv"'
        w = csv.writer(resp)
        w.writerow(['Company', 'Trades', 'Status', 'Contact', 'Email', 'Phone', 'Regions', 'Rating',
                    'PL expiry', 'EL expiry', 'Accreditations'])
        for s in qs:
            w.writerow([s.company_name, '; '.join(t.name for t in s.trades.all()), s.get_status_display(),
                        s.contact_name, s.email, s.phone, s.regions, s.rating or '',
                        s.public_liability_expiry or '', s.employers_liability_expiry or '', s.accreditations])
        return resp
    return render(request, 'directory/sub_list.html', {
        'subs': qs, 'trades': Trade.objects.annotate(n=Count('subcontractors')), 'q': q, 'trade': trade,
        'status': status, 'insurance': insurance, 'status_choices': Subcontractor.STATUS_CHOICES,
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
    sub = get_object_or_404(Subcontractor, pk=pk)
    form = SubcontractorForm(request.POST or None, instance=sub)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Saved.')
        return redirect(sub)
    return render(request, 'directory/sub_form.html', {'form': form, 'sub': sub})


@internal_required
def sub_detail(request, org_slug, pk):
    sub = get_object_or_404(Subcontractor.objects.prefetch_related('trades'), pk=pk)
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
            SubcontractorContact.objects.filter(subcontractor=sub, pk=request.POST.get('contact')).delete()
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
            Trade.objects.filter(pk=request.POST.get('trade')).delete()
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
    project = get_project(request, pk)
    form = PackageForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            form = PackageForm(request.POST)
            if form.is_valid():
                p = form.save(commit=False)
                p.project = project
                p.save()
                return redirect('package_list', org_slug=org_slug, pk=pk)
        elif action == 'status':
            pkg = get_object_or_404(Package, project=project, pk=request.POST.get('package'))
            if request.POST.get('status') in dict(Package.STATUS_CHOICES):
                pkg.status = request.POST['status']
            val = request.POST.get('quote_value', '').replace(',', '').strip()
            if val:
                try:
                    pkg.quote_value = val
                    pkg.full_clean(exclude=['project', 'subcontractor', 'trade'])
                except Exception:  # noqa: BLE001
                    messages.error(request, 'Quote value must be a number.')
                    return redirect('package_list', org_slug=org_slug, pk=pk)
            pkg.save()
            if pkg.status == 'APPOINTED':
                log_activity(request, f'appointed {pkg.subcontractor}', pkg.trade or '', project=project)
            return redirect('package_list', org_slug=org_slug, pk=pk)
        elif action == 'delete':
            Package.objects.filter(project=project, pk=request.POST.get('package')).delete()
            return redirect('package_list', org_slug=org_slug, pk=pk)
        elif action == 'portal':
            pkg = get_object_or_404(Package.objects.select_related('subcontractor'), project=project,
                                    pk=request.POST.get('package'))
            sub = pkg.subcontractor
            if not sub.email:
                messages.error(request, f'{sub.company_name} has no email address in the directory.')
            elif ProjectMember.objects.filter(project=project, user__email__iexact=sub.email).exists():
                messages.info(request, f'{sub.company_name} already has portal access.')
            else:
                inv = ProjectInvite.objects.create(project=project, email=sub.email.lower(),
                                                   role=ProjectMember.ROLE_SUBCONTRACTOR, company_name=sub.company_name,
                                                   subcontractor=sub, invited_by=request.user)
                send_project_invite(request, inv)
                messages.success(request, f'Portal invite sent to {sub.email}.')
            return redirect('package_list', org_slug=org_slug, pk=pk)
    packages = project.packages.select_related('subcontractor', 'trade')
    return render(request, 'directory/packages.html', project_context(
        request, project, 'packages', packages=packages, form=form, status_choices=Package.STATUS_CHOICES))
