from datetime import datetime, time

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from directory.regions import guess_regions
from directory.regions import labels as region_labels
from projects.access import get_project, is_internal, posted_id
from projects.files import serve
from projects.models import Document
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from . import coverage, gate, services
from .forms import AddendumForm, AnswerForm, DeclineForm, QueryForm, QuoteForm
from .models import Addendum, AddendumFile, AddendumReceipt, BidReview, TenderQuery, TenderSettings


def _my_packages(request, project):
    """The packages this external user's company was invited to price on this project."""
    pm = request.project_member
    if pm is None or pm.subcontractor_id is None:
        return Package.objects.none()
    return Package.objects.filter(project=project, subcontractor_id=pm.subcontractor_id).select_related('trade')


def _project_for_tender(request, pk):
    """Internal staff, or an external whose company has a package on this project."""
    project = get_project(request, pk, allow_bidder=True)
    if not is_internal(request) and not _my_packages(request, project).exists():
        raise PermissionDenied
    return project


def tender(request, org_slug, pk):
    project = _project_for_tender(request, pk)
    if is_internal(request):
        return _internal(request, project)
    return _bidder(request, project)


# ---------------------------------------------------------------------------
# Subcontractor's view
# ---------------------------------------------------------------------------

def _bidder(request, project):
    packages = list(_my_packages(request, project))
    sub_id = request.project_member.subcontractor_id
    Package.objects.filter(pk__in=[p.pk for p in packages], viewed_at__isnull=True).update(viewed_at=timezone.now())
    url = services.tender_url(project)

    if request.method == 'POST':
        action = request.POST.get('action')
        pkg = next((p for p in packages if str(p.pk) == request.POST.get('package')), None)
        if action in ('quote', 'decline') and (pkg is None or not pkg.is_open):
            raise PermissionDenied
        if action == 'quote':
            form = QuoteForm(request.POST, request.FILES, instance=pkg)
            if form.is_valid():
                pkg = form.save(commit=False)
                first = pkg.submitted_at is None
                pkg.status, pkg.submitted_at, pkg.submitted_by = Package.QUOTED, timezone.now(), request.user
                pkg.save()
                verb = 'Quote received' if first else 'Quote revised'
                notify.alert(services.watchers(project), f'{verb}: {pkg.subcontractor} for {pkg.trade or "package"}',
                             _packages_url(project), project=project, actor=request.user, org=request.org)
                log_activity(request, verb.lower(), f'{pkg.subcontractor} · {pkg.trade or ""}', project=project)
                messages.success(request, 'Quote submitted. You can revise it until the contractor makes a decision.')
                return redirect(url)
            return _render_bidder(request, project, packages, quote_forms={pkg.pk: form})
        if action == 'decline':
            form = DeclineForm(request.POST)
            if form.is_valid():
                pkg.status, pkg.decided_at = Package.DECLINED, timezone.now()
                pkg.decline_reason = form.cleaned_data['reason']
                pkg.save(update_fields=['status', 'decided_at', 'decline_reason'])
                notify.alert(services.watchers(project), f'Declined to price: {pkg.subcontractor} for {pkg.trade or "package"}',
                             _packages_url(project), project=project, actor=request.user, org=request.org)
                services.revoke_if_finished(project, pkg.subcontractor)
                messages.success(request, 'Thanks for letting them know.')
                return redirect('dashboard', org_slug=request.org.slug)
        if action == 'ack':
            addendum = get_object_or_404(Addendum, project=project, pk=posted_id(request, 'addendum'))
            AddendumReceipt.objects.get_or_create(addendum=addendum, subcontractor_id=sub_id,
                                                  defaults={'user': request.user})
            return redirect(url + '#addenda')
        if action == 'ask':
            form = QueryForm(request.POST)
            if form.is_valid():
                q = form.save(commit=False)
                q.project, q.asked_by, q.subcontractor_id = project, request.user, sub_id
                q.package = next((p for p in packages if p.is_open), None)
                q.save()
                notify.alert(services.watchers(project), f'Tender query {q.reference} from {q.subcontractor}',
                             url + '#queries', project=project, actor=request.user, org=request.org)
                messages.success(request, f'{q.reference} sent. You will be notified when it is answered.')
                return redirect(url + '#queries')
            return _render_bidder(request, project, packages, query_form=form)
        return redirect(url)
    return _render_bidder(request, project, packages)


def _render_bidder(request, project, packages, quote_forms=None, query_form=None):
    sub_id = request.project_member.subcontractor_id
    quote_forms = quote_forms or {}
    for p in packages:
        p.form = quote_forms.get(p.pk) or (QuoteForm(instance=p) if p.is_open else None)
    acked = set(AddendumReceipt.objects.filter(addendum__project=project, subcontractor_id=sub_id)
                .values_list('addendum_id', flat=True))
    addenda = list(Addendum.objects.filter(project=project).prefetch_related('files'))
    for a in addenda:
        a.acked = a.pk in acked
    queries = TenderQuery.objects.filter(project=project, subcontractor_id=sub_id) | \
        TenderQuery.objects.filter(project=project, published=True)
    docs = Document.objects.filter(project=project, tender_issue=True).prefetch_related('revisions')
    return render(request, 'tenders/bidder.html', project_context(
        request, project, 'tender', packages=packages, addenda=addenda, docs=docs,
        queries=queries.distinct().order_by('-number'), my_sub_id=sub_id,
        query_form=query_form or QueryForm(), decline_form=DeclineForm(),
        unacked=sum(1 for a in addenda if not a.acked), any_open=any(p.is_open for p in packages)))


# ---------------------------------------------------------------------------
# Contractor's view
# ---------------------------------------------------------------------------

def _packages_url(project):
    from django.urls import reverse
    return reverse('package_list', args=[project.organization.slug, project.pk])


def _internal(request, project):
    url = services.tender_url(project)
    addendum_form, answer_forms = AddendumForm(), {}
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'addendum':
            addendum_form = AddendumForm(request.POST, request.FILES)
            if addendum_form.is_valid():
                a = addendum_form.save(commit=False)
                a.project, a.issued_by = project, request.user
                a.save()
                for f in addendum_form.cleaned_data['files']:
                    AddendumFile.objects.create(addendum=a, file=f, original_name=f.name[:200], size=f.size)
                notify.alert(services.open_bidders(project), f'{a.reference} issued: {a.title}', url + '#addenda',
                             project=project, actor=request.user, org=request.org)
                log_activity(request, 'issued', f'{a.reference} {a.title}', project=project, url=url)
                messages.success(request, f'{a.reference} issued to everyone pricing.')
                return redirect(url + '#addenda')
        elif action == 'answer':
            q = get_object_or_404(TenderQuery, project=project, pk=posted_id(request, 'query'))
            form = AnswerForm(request.POST, instance=q)
            if form.is_valid():
                was_published = TenderQuery.objects.get(pk=q.pk).published
                q = form.save(commit=False)
                q.answered_by, q.answered_at = request.user, timezone.now()
                q.save()
                if q.published and not was_published:
                    notify.alert(services.open_bidders(project), f'Tender query answered for all tenderers: {q.reference}',
                                 url + '#queries', project=project, actor=request.user, org=request.org)
                elif q.asked_by:
                    notify.alert([q.asked_by], f'Your tender query {q.reference} has been answered', url + '#queries',
                                 project=project, actor=request.user, org=request.org)
                messages.success(request, f'{q.reference} answered.')
                return redirect(url + '#queries')
            answer_forms[q.pk] = form
        elif action == 'issue_docs':
            chosen = set(int(i) for i in request.POST.getlist('docs') if i.isdigit())
            docs = Document.objects.filter(project=project)
            newly = [d for d in docs if d.pk in chosen and not d.tender_issue]
            docs.filter(pk__in=chosen).update(tender_issue=True)
            docs.exclude(pk__in=chosen).update(tender_issue=False)
            if newly:
                names = ', '.join(d.reference or d.title for d in newly[:4]) + ('…' if len(newly) > 4 else '')
                notify.alert(services.open_bidders(project), f'New tender documents issued: {names}', url + '#documents',
                             project=project, actor=request.user, org=request.org)
            messages.success(request, 'Tender documents updated.')
            return redirect(url + '#documents')
        elif action == 'chase':
            n = coverage.queue_chase(request, project)
            if n:
                log_activity(request, 'chased outstanding quotes', f'{n} subcontractor{"s" if n != 1 else ""}',
                             project=project, url=url)
                messages.success(request, f'Reminder going to {n} subcontractor{"s" if n != 1 else ""} still to quote. '
                                          'Each is chased at most once a day.')
            else:
                messages.info(request, 'Everyone still to quote has already been chased today.')
            return redirect(url + '#coverage')
        elif action == 'invite_suggested':
            return _invite_suggested(request, project, url)

    packages = list(Package.objects.filter(project=project).select_related('subcontractor', 'trade'))
    open_subs = {p.subcontractor_id for p in packages if p.is_open}
    addenda = list(Addendum.objects.filter(project=project).prefetch_related('files', 'receipts__subcontractor')
                   .annotate(n_acks=Count('receipts')))
    for a in addenda:
        acked = {r.subcontractor_id for r in a.receipts.all()}
        a.outstanding = sorted({p.subcontractor.company_name for p in packages if p.is_open and p.subcontractor_id not in acked})
    # Which pricing companies have actually opened each issued document and addendum file
    from projects.models import FileAccess, ProjectMember
    company = {m.user_id: m.company_name or (m.subcontractor.company_name if m.subcontractor else m.user.full_name)
               for m in ProjectMember.objects.filter(project=project, subcontractor__isnull=False)
               .select_related('subcontractor', 'user')}
    opened = {}
    for fa in FileAccess.objects.filter(project=project, user_id__in=company).values('path', 'user_id'):
        opened.setdefault(fa['path'], set()).add(company[fa['user_id']])
    for a in addenda:
        a.opened_by = sorted(set().union(*[opened.get(f.file.name, set()) for f in a.files.all()]))
    queries = list(TenderQuery.objects.filter(project=project).select_related('subcontractor', 'asked_by'))
    for q in queries:
        q.form = answer_forms.get(q.pk) or AnswerForm(instance=q)
    stats = {
        'invited': len(packages), 'open_subs': len(open_subs),
        'viewed': sum(1 for p in packages if p.viewed_at),
        'quoted': sum(1 for p in packages if p.submitted_at),
        'declined': sum(1 for p in packages if p.status == Package.DECLINED),
        'unanswered': sum(1 for q in queries if not q.answered_at),
    }
    documents = list(Document.objects.filter(project=project).prefetch_related('revisions'))
    for d in documents:
        cur = d.current
        d.opened_by = sorted(opened.get(cur.file.name, set())) if cur else []
    settings = TenderSettings.for_org(request.org)
    rows = coverage.trade_rows(project, target=settings.min_quotes, packages=packages)
    for r in rows:
        r.suggestions = coverage.suggest(project, r.trade) if r.thin else []
    today = timezone.localdate()
    start_today = timezone.make_aware(datetime.combine(today, time.min))
    outstanding = [p for p in packages if p.status == Package.ENQUIRY and not p.submitted_at]
    review = BidReview.objects.filter(project=project).first()
    due = coverage.quotes_due(project, packages)
    return render(request, 'tenders/internal.html', project_context(
        request, project, 'tender', stats=stats, addenda=addenda, queries=queries, addendum_form=addendum_form,
        documents=documents, has_packages=bool(packages), rows=rows, target=settings.min_quotes,
        outstanding=len({p.subcontractor_id for p in outstanding}),
        to_chase=len({p.subcontractor_id for p in outstanding if not p.chased_at or p.chased_at < start_today}),
        last_chased=max((p.chased_at for p in packages if p.chased_at), default=None),
        due_label=coverage.due_label((due - today).days if due else None),
        site_regions=region_labels(guess_regions(project.site_address)), review=review,
        gate_required=gate.gate_required(project, review, settings)))


def _invite_suggested(request, project, url):
    """Invite a suggested subcontractor on the same scope and quote date as the others pricing that trade."""
    trade = get_object_or_404(Trade, pk=posted_id(request, 'trade'))
    sub = get_object_or_404(Subcontractor.objects.filter(archived_at__isnull=True).exclude(email='')
                            .exclude(status=Subcontractor.STATUS_STRUCK_OFF),
                            pk=posted_id(request, 'subcontractor'))
    same = Package.objects.filter(project=project, trade=trade).exclude(scope='').order_by('created_at').first()
    if same is None:
        messages.error(request, f'Invite them from Sub quotes, with the scope for {trade}.')
        return redirect(url + '#coverage')
    due = same.quote_due if same.quote_due and same.quote_due >= timezone.localdate() else project.quotes_due
    if not due or due < timezone.localdate():
        messages.error(request, f'The quote date for {trade} has passed. Invite {sub} from Sub quotes with a new date.')
        return redirect(url + '#coverage')
    if services.invite(request, project, sub, trade, same.scope, due):
        messages.success(request, f'{sub} invited to price {trade}, on the same scope, due {due:%a} {due.day} {due:%b}.')
    else:
        messages.info(request, f'{sub} is already invited for {trade}.')
    return redirect(url + '#coverage')


# ---------------------------------------------------------------------------
# Files (permission-checked; nothing is ever emailed)
# ---------------------------------------------------------------------------

def addendum_file(request, org_slug, pk, file_id):
    project = _project_for_tender(request, pk)
    f = get_object_or_404(AddendumFile, addendum__project=project, pk=file_id)
    return serve(request, f.file, f.original_name, project=project, label=f'{f.addendum.reference}: {f.original_name}')


def tender_document(request, org_slug, pk, doc_id):
    project = _project_for_tender(request, pk)
    doc = get_object_or_404(Document, project=project, pk=doc_id, tender_issue=True)
    rev = doc.current
    if rev is None:
        raise PermissionDenied
    return serve(request, rev.file, rev.original_name, project=project,
                 label=f'{doc.reference or doc.title} rev {rev.revision} (tender issue)')


def quote_file(request, org_slug, pk, package_id):
    project = _project_for_tender(request, pk)
    qs = Package.objects.filter(project=project) if is_internal(request) else _my_packages(request, project)
    pkg = get_object_or_404(qs, pk=package_id)
    return serve(request, pkg.quote_file, project=project, label=f'Quote: {pkg.subcontractor} ({pkg.trade or "package"})')
