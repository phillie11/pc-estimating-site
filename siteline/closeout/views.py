"""
The Close-out tab (staff: Overview | Defects | O&M manual | Certificates | Handover pack) and the
client's "Defects and handover" page. Every view fetches the project with get_project() and asks
closeout.access what this person may see. Every file goes out through projects.files.serve(), so
the virus-scan gate and the download log apply.
"""
import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from projects.access import get_project, posted_id
from projects.files import serve
from projects.models import Issue, ProjectMember
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import access, goldenthread, rules, services
from .forms import (CertificateForm, CompletionForm, DefectForm, OMDocumentForm, OMRequestForm, OMSectionForm,
                    PackForm, SectionAddForm, TemplateFormSet)
from .models import (Certificate, CompletionSection, HandoverItem, HandoverPack, OMCompilation, OMDocument, OMSection,
                     OMTemplateSection)

PER_PAGE = 50

try:
    from core.exports import safe as _safe
except ImportError:  # until the shared formula-safe helper is on main
    from levelling.exports import safe as _safe


def _csv(filename, header, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for row in rows:
        w.writerow([_safe('' if v is None else (v if isinstance(v, str) else str(v))) for v in row])
    return resp


def _context(request, project, sub, **extra):
    org, pk = request.org.slug, project.pk
    if access.role(request) == access.STAFF:
        extra['closeout_nav'] = [
            ('overview', 'Overview', reverse('closeout_overview', args=[org, pk])),
            ('defects', 'Defects', reverse('closeout_defects', args=[org, pk])),
            ('om', 'O&M manual', reverse('closeout_om', args=[org, pk])),
            ('certificates', 'Certificates', reverse('closeout_certificates', args=[org, pk])),
            ('packs', 'Handover pack', reverse('closeout_packs', args=[org, pk])),
        ]
        extra['closeout_sub'] = sub
    extra.setdefault('advisory', rules.ADVISORY)
    return project_context(request, project, 'closeout', **extra)


def _contract(project):
    from contracts.models import MainContract
    return MainContract.objects.filter(project=project).first()


def _words(contract):
    from contracts.vocabulary import vocabulary
    return vocabulary(contract.form if contract else 'BESPOKE')


def _sections(project):
    """The whole works first (made, unsaved, if missing), then any sections."""
    whole = services.whole_works(project)
    rest = list(CompletionSection.objects.filter(project=project, is_whole_works=False))
    for s in [whole] + rest:
        s.project = project  # one contract lookup for them all
    return [whole] + rest


def _defects(project):
    return Issue.objects.filter(project=project, issue_type='DEFECT')


# ---------------------------------------------------------------------------
# Overview: practical completion and the defects period
# ---------------------------------------------------------------------------

def overview(request, org_slug, pk):
    project = get_project(request, pk)
    if access.role(request) == access.CLIENT:
        return redirect('closeout_client', org_slug, pk)
    access.require_staff(request)
    contract = _contract(project)
    sections = _sections(project)
    today = timezone.localdate()
    defects = _defects(project)
    open_defects = defects.exclude(status=Issue.STATUS_CLOSED)
    from projects.views import count_together
    n = count_together(open=open_defects, unassigned=open_defects.filter(assignee__isnull=True),
                       ready=open_defects.filter(status=Issue.STATUS_READY), total=defects,
                       certs=Certificate.objects.filter(project=project),
                       packs=HandoverPack.objects.filter(project=project, status=HandoverPack.ISSUED))
    om = services.section_rows(project)
    om_live = [s for s in om if not s.not_needed]
    whole = sections[0]
    steps = [
        ('Record practical completion', bool(whole.pc_date),
         reverse('closeout_section', args=[org_slug, pk, 'whole']),
         f'{whole.pc_date:%d %b %Y}' if whole.pc_date else 'Not yet'),
        ('Ask subcontractors for O&M information', bool(om) and all(s.done or s.submittals for s in om_live),
         reverse('closeout_om', args=[org_slug, pk]),
         f'{sum(1 for s in om_live if s.done)} of {len(om_live)} sections complete' if om else 'Not set up'),
        ('Upload certificates and test results', n['certs'] > 0, reverse('closeout_certificates', args=[org_slug, pk]),
         f"{n['certs']} uploaded"),
        ('Issue the handover pack', n['packs'] > 0, reverse('closeout_packs', args=[org_slug, pk]),
         'Issued' if n['packs'] else 'Not yet'),
        ('Close every defect', n['total'] > 0 and n['open'] == 0 and bool(whole.made_good_on),
         reverse('closeout_defects', args=[org_slug, pk]), f"{n['open']} open"),
    ]
    return render(request, 'closeout/overview.html', _context(
        request, project, 'overview', sections=sections, contract=contract, words=_words(contract), counts=n,
        steps=steps, today=today, reminder_days=rules.REMINDER_DAYS))


def section_edit(request, org_slug, pk, section_id):
    project = get_project(request, pk)
    access.require_staff(request)
    if section_id == 'whole':
        section = services.whole_works(project)
    elif section_id.isdigit() and section_id.isascii():
        section = get_object_or_404(CompletionSection.objects.filter(project=project), pk=int(section_id))
    else:
        raise Http404
    section.project = project
    contract = _contract(project)
    months = contract.defects_period_months if contract else None
    if request.method == 'POST' and request.POST.get('action') == 'delete':
        if section.pk is None or section.is_whole_works:
            raise PermissionDenied
        name = section.name
        section.delete()
        messages.success(request, f'Section "{name}" removed.')
        return redirect('closeout_overview', org_slug, pk)
    form = CompletionForm(request.POST or None, request.FILES or None, instance=section, months_hint=months)
    if request.method == 'POST' and form.is_valid():
        section = form.save(commit=False)
        newly = services.set_completion(section, request.user)
        if newly:
            log_activity(request, 'recorded practical completion', f'{section.name}: {section.pc_date:%d %b %Y}',
                         project=project, internal_only=False,
                         url=reverse('closeout_overview', args=[org_slug, pk]))
        end = section.defects_end
        messages.success(request, f'{section.name} saved.' + (
            f' The defects period ends on {end:%d %b %Y}.' if end and not section.made_good_on else ''))
        return redirect('closeout_overview', org_slug, pk)
    return render(request, 'closeout/section_form.html', _context(
        request, project, 'overview', form=form, section=section, contract=contract, words=_words(contract)))


def section_add(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    form = SectionAddForm(request.POST or None, project=project)
    if request.method == 'POST' and form.is_valid():
        section = form.save(commit=False)
        section.project, section.is_whole_works = project, False
        services.set_completion(section, request.user)
        messages.success(request, f'Section "{section.name}" added.')
        return redirect('closeout_overview', org_slug, pk)
    return render(request, 'closeout/section_add.html', _context(request, project, 'overview', form=form))


def section_certificate(request, org_slug, pk, section_id):
    project = get_project(request, pk)
    access.require_staff(request)
    section = get_object_or_404(CompletionSection.objects.filter(project=project), pk=section_id)
    return serve(request, section.certificate, project=project, label=f'Completion certificate: {section.name}')


# ---------------------------------------------------------------------------
# Defects (staff)
# ---------------------------------------------------------------------------

DEFECT_STATUS = {Issue.STATUS_OPEN: 'Being fixed', Issue.STATUS_READY: 'Fixed: to check', Issue.STATUS_CLOSED: 'Fixed and checked'}


def defect_label(issue):
    if issue.status == Issue.STATUS_OPEN and issue.assignee_id is None:
        return 'Reported', 'warn'
    return DEFECT_STATUS[issue.status], issue.status_tone


def defects(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    base = _defects(project).select_related('assignee', 'created_by')
    client_side = access.client_side_users(project)
    status = request.GET.get('status', 'open')
    who = request.GET.get('who', '')
    q = request.GET.get('q', '').strip()
    qs = base
    if status == 'open':
        qs = qs.exclude(status=Issue.STATUS_CLOSED)
    elif status == 'unassigned':
        qs = qs.exclude(status=Issue.STATUS_CLOSED).filter(assignee__isnull=True)
    elif status in dict(Issue.STATUS_CHOICES):
        qs = qs.filter(status=status)
    else:
        status = 'all'
    if who == 'client':
        qs = qs.filter(created_by__in=client_side)
    elif who == 'us':
        qs = qs.exclude(created_by__in=client_side)
    else:
        who = ''
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(location__icontains=q) | Q(description__icontains=q))
    qs = qs.order_by('status', 'due_date', '-created_at')
    client_ids = set(ProjectMember.objects.filter(project=project, role__in=access.CLIENT_ROLES)
                     .values_list('user_id', flat=True))
    if request.GET.get('export') == 'csv':
        rows = []
        for i in qs:
            rows.append([i.reference, i.title, i.location, defect_label(i)[0],
                         'Client' if i.created_by_id in client_ids else project.organization.name,
                         timezone.localdate(i.created_at), i.assignee.full_name if i.assignee else '', i.due_date,
                         timezone.localdate(i.closed_at) if i.closed_at else ''])
        return _csv(f'{project.reference}-defects.csv',
                    ['Ref', 'Defect', 'Where', 'Status', 'Reported by', 'Reported on', 'Being fixed by', 'Fix by',
                     'Closed on'], rows)
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get('page'))
    for i in page:
        i.label, i.tone = defect_label(i)
        i.from_client = i.created_by_id in client_ids
    whole = services.whole_works(project)
    return render(request, 'closeout/defects.html', _context(
        request, project, 'defects', page=page, total=base.count(), filters={'status': status, 'who': who, 'q': q},
        filtered=bool(q or who or status != 'open'), pc_date=whole.pc_date, today=timezone.localdate()))


def defect_new(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    form = DefectForm(request.POST or None, request.FILES or None, project=project, staff=True)
    if request.method == 'POST' and form.is_valid():
        i = form.save(commit=False)
        i.project, i.created_by, i.issue_type = project, request.user, 'DEFECT'
        i.assignee, i.due_date = form.cleaned_data.get('assignee'), form.cleaned_data.get('due_date')
        i.save()
        url = reverse('issue_detail', args=[org_slug, pk, i.pk])
        if i.assignee:
            notify.alert([i.assignee], f'Defect to fix: {i.reference} {i.title}', url, project=project,
                         actor=request.user)
        log_activity(request, 'reported a defect', f'{i.reference} {i.title}', project=project, url=url)
        messages.success(request, f'{i.reference} added to the defects list.' + (
            f' {i.assignee.full_name} has been told.' if i.assignee else ' Give it to whoever will fix it.'))
        return redirect('closeout_defects', org_slug, pk)
    return render(request, 'closeout/defect_form.html', _context(request, project, 'defects', form=form))


# ---------------------------------------------------------------------------
# O&M manual (staff)
# ---------------------------------------------------------------------------

def om(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'setup':
            n = services.setup_om(project, request.org)
            messages.success(request, f'{n} sections added from your company template. Check who supplies each one, '
                                      'then ask them for their information.')
        elif action == 'compile':
            comp = services.start_compilation(project, request.user)
            log_activity(request, 'compiled the O&M manual', f'version {comp.number}', project=project,
                         url=reverse('closeout_om', args=[org_slug, pk]))
            messages.success(request, f'O&M manual version {comp.number} is ready to download.'
                             if comp.status == OMCompilation.READY else
                             f'O&M manual version {comp.number} is being put together. We will tell you when it is ready.')
        elif action in ('share', 'unshare'):
            comp = get_object_or_404(OMCompilation.objects.filter(project=project, status=OMCompilation.READY),
                                     pk=posted_id(request, 'compilation'))
            comp.shared_with_client = action == 'share'
            comp.save(update_fields=['shared_with_client'])
            if comp.shared_with_client:
                people = [m.user for m in ProjectMember.objects.filter(
                    project=project, role__in=access.CLIENT_ROLES).select_related('user')]
                notify.alert(people, f'O&M manual ready to download: {project.name}',
                             reverse('closeout_client', args=[org_slug, pk]) + '#handover', project=project,
                             actor=request.user)
            messages.success(request, f'Version {comp.number} is now ' + (
                'shared with the client.' if comp.shared_with_client else 'internal only.'))
        else:
            raise PermissionDenied
        return redirect('closeout_om', org_slug, pk)
    sections = services.section_rows(project)
    if request.GET.get('export') == 'csv':
        rows = []
        for s in sections:
            rows.append([s.title, str(s.package.subcontractor) if s.package_id else 'Us', s.state[0],
                         ', '.join(x.ref for x in s.outstanding), ', '.join(x.ref for x in s.accepted), len(s.docs),
                         min((x.due_date for x in s.outstanding if x.due_date), default='')])
        return _csv(f'{project.reference}-om-tracker.csv',
                    ['Section', 'Supplied by', 'State', 'Waiting for', 'Accepted', 'Our own files', 'Due'], rows)
    comps = list(OMCompilation.objects.filter(project=project).select_related('created_by')[:10])
    live = [s for s in sections if not s.not_needed]
    return render(request, 'closeout/om.html', _context(
        request, project, 'om', sections=sections, compilations=comps, done=sum(1 for s in live if s.done),
        live=len(live), outstanding=sum(len(s.outstanding) for s in sections), today=timezone.localdate()))


def om_section(request, org_slug, pk, section_id):
    project = get_project(request, pk)
    access.require_staff(request)
    section = get_object_or_404(OMSection.objects.filter(project=project).select_related('package__subcontractor'),
                                pk=section_id)
    form = OMSectionForm(request.POST or None, instance=section, project=project)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{section.title} saved.')
        return redirect(section)
    return render(request, 'closeout/om_section.html', _context(request, project, 'om', form=form, section=section))


def om_section_new(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    form = OMSectionForm(request.POST or None, instance=OMSection(project=project), project=project)
    if request.method == 'POST' and form.is_valid():
        section = form.save(commit=False)
        last = OMSection.objects.filter(project=project).order_by('-order').first()
        section.project, section.order = project, (last.order + 10) if last else 10
        section.save()
        messages.success(request, f'{section.title} added to the manual.')
        return redirect(section)
    return render(request, 'closeout/om_section.html', _context(request, project, 'om', form=form, section=None))


def om_request(request, org_slug, pk, section_id):
    project = get_project(request, pk)
    access.require_staff(request)
    section = get_object_or_404(OMSection.objects.filter(project=project).select_related('package__subcontractor'),
                                pk=section_id)
    from procurement.models import region_for
    default_due = rules.om_due_default(timezone.localdate(), project.completion_date, region_for(project))
    form = OMRequestForm(request.POST or None, initial={'due_date': default_due})
    if section.package_id is None:
        messages.error(request, 'Choose the subcontractor who supplies this section first.')
        return redirect('closeout_om_section', org_slug, pk, section.pk)
    if request.method == 'POST' and form.is_valid():
        try:
            s = services.request_om(section, request.user, form.cleaned_data['due_date'], form.cleaned_data['note'])
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('closeout_om_section', org_slug, pk, section.pk)
        log_activity(request, 'asked for O&M information', f'{s.ref} {section.title}', project=project,
                     internal_only=True, url=s.get_absolute_url())
        messages.success(request, f'{s.ref} sent to {section.package.subcontractor}. They upload it in their portal; '
                                  'you give it A, B or C as any submittal.')
        return redirect('closeout_om', org_slug, pk)
    return render(request, 'closeout/om_request.html', _context(request, project, 'om', form=form, section=section))


def om_upload(request, org_slug, pk, section_id):
    project = get_project(request, pk)
    access.require_staff(request)
    section = get_object_or_404(OMSection.objects.filter(project=project), pk=section_id)
    form = OMDocumentForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        doc = form.save(commit=False)
        doc.section, doc.uploaded_by = section, request.user
        doc.original_name, doc.size = doc.file.name[:200], doc.file.size
        doc.save()
        messages.success(request, f'{doc.title} added to {section.title}.')
        return redirect(section)
    return render(request, 'closeout/om_upload.html', _context(request, project, 'om', form=form, section=section))


def om_document(request, org_slug, pk, doc_id):
    project = get_project(request, pk)
    access.require_staff(request)
    doc = get_object_or_404(OMDocument.objects.filter(section__project=project), pk=doc_id)
    return serve(request, doc.file, doc.original_name or None, project=project, label=f'O&M: {doc.title}')


def om_download(request, org_slug, pk, comp_id):
    project = get_project(request, pk)
    who = access.role(request)
    qs = OMCompilation.objects.filter(project=project, status=OMCompilation.READY)
    if who == access.CLIENT:
        qs = qs.filter(shared_with_client=True)
    elif who != access.STAFF:
        raise PermissionDenied
    comp = get_object_or_404(qs, pk=comp_id)
    return serve(request, comp.file, f'{project.reference}-OM-manual-v{comp.number}.zip', project=project,
                 label=f'O&M manual v{comp.number}')


@admin_required
def om_template(request, org_slug):
    """The company's O&M manual contents, copied onto each job when its manual is set up."""
    OMTemplateSection.for_org(request.org)
    formset = TemplateFormSet(request.POST or None, queryset=OMTemplateSection.objects.all())
    if request.method == 'POST' and formset.is_valid():
        with transaction.atomic():
            for obj in formset.save(commit=False):
                obj.save()
            for obj in formset.deleted_objects:
                obj.delete()
        messages.success(request, 'O&M manual template saved. New jobs use it; jobs already set up keep theirs.')
        return redirect('closeout_om_template', org_slug)
    return render(request, 'closeout/om_template.html', {'formset': formset})


# ---------------------------------------------------------------------------
# Certificates and test results (staff)
# ---------------------------------------------------------------------------

def certificates(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    form = CertificateForm(request.POST or None, request.FILES or None)
    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            cert = get_object_or_404(Certificate.objects.filter(project=project), pk=posted_id(request, 'certificate'))
            label = cert.label
            cert.delete()
            messages.success(request, f'{label} removed. Handover packs already built keep their copy.')
            return redirect('closeout_certificates', org_slug, pk)
        if form.is_valid():
            cert = form.save(commit=False)
            cert.project, cert.uploaded_by = project, request.user
            cert.original_name, cert.size = cert.file.name[:200], cert.file.size
            cert.save()
            log_activity(request, 'uploaded a certificate', cert.label, project=project,
                         url=reverse('closeout_certificates', args=[org_slug, pk]))
            messages.success(request, f'{cert.label} uploaded. It goes in the next handover pack.')
            return redirect('closeout_certificates', org_slug, pk)
    qs = Certificate.objects.filter(project=project).select_related('uploaded_by')
    total = qs.count()
    kind = request.GET.get('kind', '')
    if kind in dict(Certificate.KIND_CHOICES):
        qs = qs.filter(kind=kind)
    else:
        kind = ''
    if request.GET.get('export') == 'csv':
        return _csv(f'{project.reference}-certificates.csv', ['Type', 'Title', 'Issued by', 'Date on it', 'File',
                                                              'Uploaded on'],
                    [[c.get_kind_display(), c.label, c.issued_by, c.issued_on, c.original_name,
                      timezone.localdate(c.uploaded_at)] for c in qs])
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get('page'))
    return render(request, 'closeout/certificates.html', _context(
        request, project, 'certificates', form=form, page=page, total=total, kind=kind,
        kind_choices=Certificate.KIND_CHOICES))


def certificate_file(request, org_slug, pk, cert_id):
    project = get_project(request, pk)
    access.require_staff(request)
    cert = get_object_or_404(Certificate.objects.filter(project=project), pk=cert_id)
    return serve(request, cert.file, cert.original_name or None, project=project, label=f'Certificate: {cert.label}')


# ---------------------------------------------------------------------------
# Golden thread handover packs
# ---------------------------------------------------------------------------

def packs(request, org_slug, pk):
    project = get_project(request, pk)
    access.require_staff(request)
    form = PackForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        pack = services.build_pack(project, request.user, form.cleaned_data['note'])
        log_activity(request, 'built a handover pack', str(pack), project=project, url=pack.get_absolute_url())
        messages.success(request, f'{pack} built: check it, then issue it to the client.'
                         if pack.status == HandoverPack.READY else
                         f'{pack} is being checked. It will be ready once every file has passed its virus check.')
        return redirect(pack)
    rows = list(HandoverPack.objects.filter(project=project).select_related('created_by', 'issued_by'))
    preview = goldenthread.collect(project)
    by_cat = {}
    for src in preview:
        by_cat[src.category] = by_cat.get(src.category, 0) + 1
    return render(request, 'closeout/packs.html', _context(
        request, project, 'packs', form=form, packs=rows,
        preview=[(label, by_cat.get(code, 0)) for code, label in HandoverItem.CATEGORY_CHOICES],
        preview_total=len(preview), quality=goldenthread.quality_installed()))


def _pack_for(request, project, pack_id):
    who = access.role(request)
    qs = HandoverPack.objects.filter(project=project)
    if who == access.CLIENT:
        qs = qs.filter(status=HandoverPack.ISSUED)
    elif who != access.STAFF:
        raise PermissionDenied
    return get_object_or_404(qs, pk=pack_id)


def pack_detail(request, org_slug, pk, pack_id):
    project = get_project(request, pk)
    access.require_staff(request)
    pack = _pack_for(request, project, pack_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'issue' and pack.status == HandoverPack.READY:
            services.issue_pack(pack, request.user)
            log_activity(request, 'issued a handover pack', str(pack), project=project, internal_only=False,
                         url=pack.get_absolute_url())
            messages.success(request, f'{pack} issued. The client can now download every item and check it.')
        elif action == 'recheck' and pack.status == HandoverPack.BUILDING:
            done = services.checksum_pack(pack)
            messages.success(request, 'Every file is checked.' if done else 'Some files are still waiting for the '
                                                                             'virus check. Try again in a few minutes.')
        else:
            raise PermissionDenied
        return redirect(pack)
    items = list(pack.items.all())
    fmt = request.GET.get('export')
    if fmt == 'csv':
        return _csv(f'{project.reference}-handover-pack-{pack.number}.csv',
                    ['#', 'Category', 'Item', 'Reference', 'Revision', 'File', 'Size (bytes)', 'SHA-256', 'Link'],
                    [[n, it.get_category_display(), it.title, it.reference, it.revision, it.file_name, it.size,
                      it.sha256, request.build_absolute_uri(reverse('closeout_pack_item',
                                                                    args=[org_slug, pk, pack.pk, it.pk]))]
                     for n, it in enumerate(items, 1)])
    return render(request, 'closeout/pack_detail.html', _context(
        request, project, 'packs', pack=pack, items=items, groups=_grouped(items)))


def _grouped(items):
    out = []
    for code, label in HandoverItem.CATEGORY_CHOICES:
        rows = [it for it in items if it.category == code]
        out.append((label, rows))
    return out


def pack_manifest(request, org_slug, pk, pack_id):
    project = get_project(request, pk)
    pack = _pack_for(request, project, pack_id)
    from . import pdf
    data = pdf.pack_manifest(pack, list(pack.items.all()), project.organization.name)
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="{project.reference}-handover-pack-{pack.number}.pdf"'
    resp['Cache-Control'] = 'private, no-store'
    return resp


def pack_item(request, org_slug, pk, pack_id, item_id):
    project = get_project(request, pk)
    pack = _pack_for(request, project, pack_id)
    item = get_object_or_404(pack.items.all(), pk=item_id)
    if item.state != HandoverItem.OK:
        raise Http404
    return serve(request, item.file, item.file_name or None, project=project,
                 label=f'Handover pack {pack.number}: {item.title}'[:250])


# ---------------------------------------------------------------------------
# The client's page: defects and handover
# ---------------------------------------------------------------------------

def client_page(request, org_slug, pk):
    project = get_project(request, pk)
    if access.role(request) == access.STAFF:
        return redirect('closeout_overview', org_slug, pk)
    access.require_client(request)
    form = DefectForm(request.POST or None, request.FILES or None, project=project)
    if request.method == 'POST' and form.is_valid():
        i = form.save(commit=False)
        i.project, i.created_by, i.issue_type = project, request.user, 'DEFECT'
        i.save()
        from delivery.views import site_team
        notify.alert(site_team(project), f'Defect reported by the client: {i.reference} {i.title}',
                     reverse('issue_edit', args=[org_slug, pk, i.pk]), project=project, actor=request.user)
        log_activity(request, 'reported a defect', f'{i.reference} {i.title}', project=project, internal_only=True,
                     url=reverse('issue_detail', args=[org_slug, pk, i.pk]))
        messages.success(request, f'Thank you. {i.reference} has been sent to {project.organization.name}. '
                                  'You can follow it below.')
        return redirect(reverse('closeout_client', args=[org_slug, pk]) + '#defects')
    reported = list(access.client_defects(project).order_by('status', '-created_at'))
    for i in reported:
        i.label, i.tone = _client_label(i)
    sections = [s for s in _sections(project) if s.pk or s.pc_date]
    contract = _contract(project)
    return render(request, 'closeout/client.html', _context(
        request, project, 'closeout', form=form, defects=reported, sections=sections, words=_words(contract),
        manuals=list(OMCompilation.objects.filter(project=project, status=OMCompilation.READY, shared_with_client=True)),
        packs=list(HandoverPack.objects.filter(project=project, status=HandoverPack.ISSUED)
                   .prefetch_related('items')),
        today=timezone.localdate()))


def _client_label(issue):
    if issue.status == Issue.STATUS_CLOSED:
        return 'Fixed and checked', 'good'
    if issue.status == Issue.STATUS_READY:
        return 'Fixed: being checked', 'info'
    if issue.assignee_id:
        return 'Being fixed', 'accent'
    return 'Reported', 'warn'
