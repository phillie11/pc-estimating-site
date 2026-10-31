"""
Applications for payment. Staff (owners, admins and the job's PM) work under Commercial ->
Applications; the client sees submitted ones under Decisions -> Waiting on you -> Payments.
Every view goes through get_project() and payments.access.visible_applications().
"""
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from contracts.commercial import commercial_context, commercial_position
from contracts.models import MainContract
from contracts.vocabulary import vocabulary_for
from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from projects.access import get_project
from projects.files import serve
from projects.models import ProjectMember
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from . import rules
from .access import is_client, manages_payments, visible_applications
from .forms import ApplicationForm, LineFormSet, NoticeForm, PaymentForm
from .models import Application
from .pdf import application_pdf
from .workflow import ON_BEHALF_NOTE, is_project_client, records_for_client

ZERO = Decimal('0.00')


def _contract(project):
    return MainContract.objects.filter(project=project).select_related('administrator__user').first()


def _ctx(request, project, **extra):
    """Staff: the Commercial tab's Applications sub-page. Client: the Payments register under Decisions."""
    if manages_payments(request, project):
        return commercial_context(request, project, 'applications', manages=True, **extra)
    return project_context(request, project, 'payments', vocab=vocabulary_for(project), manages=False, **extra)


def _manager_only(request, project):
    if not manages_payments(request, project):
        raise PermissionDenied


FILTERS = [('', 'All'), ('open', 'Awaiting payment'), ('overdue', 'Overdue'), ('draft', 'Draft'), ('paid', 'Paid')]


def application_list(request, org_slug, pk):
    project = get_project(request, pk)
    apps = list(visible_applications(request, project).prefetch_related('payments').order_by('-valuation_date', '-number'))
    for a in apps:
        a._paid_total = sum((p.amount for p in a.payments.all()), ZERO)
    shown = request.GET.get('show', '')
    filtered = {
        'open': [a for a in apps if a.status in Application.AWAITING_PAYMENT],
        'overdue': [a for a in apps if a.is_overdue],
        'draft': [a for a in apps if a.is_draft],
        'paid': [a for a in apps if a.status == Application.STATUS_PAID],
    }.get(shown, apps) if shown in dict(FILTERS) else apps
    counted = [a for a in apps if a.status in Application.COUNTED]
    totals = {
        'applied': sum((a.amount_due for a in counted), ZERO),
        'certified': sum((a.notified_sum for a in counted if a.is_certified), ZERO),
        'paid': sum((a.paid_total for a in counted), ZERO),
        'outstanding': sum((a.outstanding for a in counted if a.status in Application.AWAITING_PAYMENT), ZERO),
        'overdue': sum((a.outstanding for a in counted if a.is_overdue), ZERO),
    }
    contract = _contract(project) if manages_payments(request, project) else None
    nxt = None
    if contract and contract.valuation_day:
        from contracts.presets import next_valuation_date
        valuation = next_valuation_date(timezone.localdate(), contract.valuation_day)
        nxt = rules.payment_cycle(valuation, rules.terms_for(contract))
    filters = FILTERS if manages_payments(request, project) else [f for f in FILTERS if f[0] != 'draft']
    return render(request, 'payments/application_list.html', _ctx(
        request, project, applications=filtered, all_count=len(apps), shown=shown, filters=filters, totals=totals,
        contract=contract, next_cycle=nxt, advisory=rules.ADVISORY,
        create_url=reverse('application_create', args=[request.org.slug, project.pk]),
        position=commercial_position(project) if manages_payments(request, project) else None))


def _save_lines(app, formset):
    for i, form in enumerate(formset.forms):
        if not form.has_changed() and not form.instance.pk:
            continue
        if formset.can_delete and formset._should_delete_form(form):
            if form.instance.pk:
                form.instance.delete()
            continue
        if not form.cleaned_data.get('section') and form.cleaned_data.get('value') is None:
            continue
        line = form.save(commit=False)
        line.application = app
        line.position = i
        line.save()


def _edit(request, project, app=None):
    _manager_only(request, project)
    contract = _contract(project)
    words = vocabulary_for(project)
    creating = app is None
    if creating:
        initial = {}
        raw = request.GET.get('valuation_date', '')
        try:
            initial['valuation_date'] = date.fromisoformat(raw) if raw else None
        except ValueError:
            pass
        if not initial.get('valuation_date') and contract and contract.valuation_day:
            from contracts.presets import next_valuation_date
            initial['valuation_date'] = next_valuation_date(timezone.localdate(), contract.valuation_day)
        last = Application.objects.filter(project=project, status__in=Application.COUNTED) \
            .order_by('-valuation_date').first()
        if last:
            initial.update(work_done=last.work_done, materials_on_site=last.materials_on_site,
                           materials_off_site=last.materials_off_site)
        app = Application(project=project)
    elif not app.is_draft:
        messages.error(request, 'Submitted applications cannot be changed. Withdraw it and make a new one if it was wrong.')
        return redirect(app)
    form = ApplicationForm(request.POST or None, instance=app, project=project, words=words,
                           initial=initial if creating else None)
    formset = LineFormSet(request.POST or None, instance=app, prefix='lines')
    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                app = form.save(commit=False)
                app.project = project
                if not app.title or creating or 'valuation_date' in form.changed_data:
                    app.title = f'Valuation to {app.valuation_date.day} {app.valuation_date:%b %Y}'
                if creating:
                    app.recalculate(contract)
                    app.save(actor=request.user)
                _save_lines(app, formset)
                app.recalculate(contract)
                app.save(actor=request.user)
        except RecordConflict:
            form.add_error(None, form.CONFLICT_MESSAGE)
        else:
            if creating:
                log_activity(request, 'drafted', f'{app.reference} {app.title}', project=project,
                             url=app.get_absolute_url(), internal_only=True)
            messages.success(request, f'{app.reference} saved as a draft. Check the figures, then submit it.')
            return redirect(app)
    return render(request, 'payments/application_form.html', _ctx(
        request, project, form=form, formset=formset, app=app if not creating else None, creating=creating,
        contract=contract, advisory=rules.ADVISORY))


def application_create(request, org_slug, pk):
    project = get_project(request, pk)
    return _edit(request, project)


def application_edit(request, org_slug, pk, app_id):
    project = get_project(request, pk)
    _manager_only(request, project)
    app = get_object_or_404(Application, project=project, pk=app_id)
    return _edit(request, project, app)


def _clients(project):
    return [m.user for m in ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CLIENT)
            .select_related('user')]


def _team(project):
    """Who hears about the client's notices and payments: the PM, else the owners and admins."""
    from projects.files import staff_for
    people = [project.manager] if project.manager else []
    return people or staff_for(project, project.organization)


def application_detail(request, org_slug, pk, app_id):
    project = get_project(request, pk)
    app = get_object_or_404(visible_applications(request, project), pk=app_id)
    manages = manages_payments(request, project)
    client = is_client(request) and is_project_client(app, request.user)
    on_behalf = manages and records_for_client(app, request.user)
    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            response = _act(request, project, app, action, manages, client, on_behalf)
        except (InvalidTransition, RecordConflict) as e:
            messages.error(request, str(e))
            response = None
        if response is not None:
            return response  # a form with errors, re-rendered
        return redirect(app)
    return render(request, 'payments/application_detail.html', _detail_ctx(request, project, app, manages, client,
                                                                             on_behalf))


def _detail_ctx(request, project, app, manages, client, on_behalf, **forms):
    contract = _contract(project)
    words = vocabulary_for(project)
    today = timezone.localdate()
    can_notice = app.status == Application.STATUS_SUBMITTED and (client or on_behalf)
    can_pay_less = app.status in (Application.STATUS_SUBMITTED, Application.STATUS_NOTIFIED) and (client or on_behalf)
    pay_less_late = can_pay_less and app.pay_less_by is not None and today > app.pay_less_by
    draft_cycle = app.live_cycle(contract) if app.is_draft else None
    ctx = dict(
        app=app, contract=contract, words=words, today=today, clocks=app.clocks(today, words) if app.cycle else [],
        draft_cycle=draft_cycle, lines=list(app.lines.all()), payments=list(app.payments.select_related('recorded_by')),
        stage_label=dict(rules.STAGE_CHOICES).get(app.retention_stage, ''), client=client, on_behalf=on_behalf,
        can_notice=can_notice, can_pay_less=can_pay_less and not pay_less_late, pay_less_late=pay_less_late,
        pay_less_refusal=rules.pay_less_refusal(today, app.pay_less_by) if pay_less_late else '',
        notice_form=forms.get('notice_form') or NoticeForm(application=app, on_behalf=not client,
                                                           initial={'amount': app.amount_due}),
        pay_less_form=forms.get('pay_less_form') or NoticeForm(application=app, kind='pay_less', on_behalf=not client,
                                                               initial={'amount': app.notified_sum}),
        payment_form=forms.get('payment_form') or PaymentForm(application=app,
                                                              initial={'amount': app.outstanding or None}),
        can_record_payment=manages and app.status in Application.AWAITING_PAYMENT,
        reverse_charge_note=rules.REVERSE_CHARGE_NOTE, advisory=rules.ADVISORY,
        basis_labels=rules.BASIS_LABELS,
    )
    if manages and app.is_draft:
        ctx['no_client'] = not _clients(project)
    return _ctx(request, project, **ctx)


def _act(request, project, app, action, manages, client, on_behalf):
    label = f'{app.reference} {app.title}'
    if action == 'submit':
        if not manages:
            raise PermissionDenied
        apply_transition(app, 'submit', request.user)
        clients = _clients(project)
        notify.alert(clients, f'Application for payment {app.reference} submitted to you', app.get_absolute_url(),
                     project=project, actor=request.user, org=request.org)
        log_activity(request, 'submitted', label, project=project, url=app.get_absolute_url(), internal_only=True)
        messages.success(request, f'{app.reference} submitted. Payment notice due by {app.payment_notice_by:%a %d %b %Y}.'
                         + ('' if clients else ' No client is on this project yet, so nobody was told. Invite them '
                                               'from the Team tab, or send them the PDF.'))
    elif action == 'withdraw':
        if not manages:
            raise PermissionDenied
        apply_transition(app, 'withdraw', request.user)
        if app.submitted_at:
            notify.alert(_clients(project), f'{app.reference} was withdrawn by {request.org.name}',
                         app.get_absolute_url(), project=project, actor=request.user, org=request.org)
        log_activity(request, 'withdrew', label, project=project, url=app.get_absolute_url(), internal_only=True)
        messages.success(request, f'{app.reference} withdrawn.')
    elif action in ('payment_notice', 'pay_less'):
        return _notice(request, project, app, action, manages, client, on_behalf)
    elif action == 'payment':
        if not manages or app.status not in Application.AWAITING_PAYMENT:
            raise PermissionDenied
        form = PaymentForm(request.POST, application=app)
        if not form.is_valid():
            return render(request, 'payments/application_detail.html', _detail_ctx(
                request, project, app, manages, client, on_behalf, payment_form=form))
        with transaction.atomic():
            payment = form.save(commit=False)
            payment.application, payment.recorded_by = app, request.user
            payment.save()
            app._paid_total = None
            symbol = request.org.currency_symbol
            note = f'{symbol}{payment.amount:,} received on {payment.received_on:%d %b %Y}'
            if app.outstanding <= 0:
                apply_transition(app, 'paid', request.user, note=note, paid_on=payment.received_on)
            else:
                app.save(actor=request.user, audit_action='payment_recorded', audit_note=note)
        log_activity(request, 'recorded a payment on', label, project=project, url=app.get_absolute_url(),
                     internal_only=True)
        messages.success(request, f'Payment recorded. {app.reference} is paid in full.' if app.status == app.STATUS_PAID
                         else f'Payment recorded. {request.org.currency_symbol}{app.outstanding:,} still outstanding.')
    else:
        raise Http404
    return None


def _notice(request, project, app, kind, manages, client, on_behalf):
    if not (client or on_behalf):
        raise PermissionDenied
    form = NoticeForm(request.POST, request.FILES, application=app, kind=kind, on_behalf=not client)
    if not form.is_valid():
        key = 'notice_form' if kind == 'payment_notice' else 'pay_less_form'
        return render(request, 'payments/application_detail.html', _detail_ctx(
            request, project, app, manages, client, on_behalf, **{key: form}))
    d = form.cleaned_data
    params = dict(amount=d['amount'], basis=d['basis'], given_on=d['given_on'])
    what = 'payment notice' if kind == 'payment_notice' else 'pay less notice'
    if client:
        apply_transition(app, kind, request.user, note=d['basis'][:200], **params)
        notify.alert(_team(project), f'{request.user.full_name} gave a {what} on {app.reference}: '
                     f'{request.org.currency_symbol}{app.notified_sum:,} considered due', app.get_absolute_url(),
                     project=project, actor=request.user, org=request.org)
        log_activity(request, f'gave a {what} on', f'{app.reference} {app.title}', project=project,
                     url=app.get_absolute_url(), internal_only=True)
        messages.success(request, f'Your {what} is recorded and {request.org.name} has been told.')
    else:
        apply_transition(app, f'{kind}_on_behalf', request.user, note=f"{ON_BEHALF_NOTE}: {d['note']}"[:500],
                         evidence=d['evidence'], **params)
        notify.alert(_clients(project), f'{request.user.full_name} recorded your {what} on {app.reference}',
                     app.get_absolute_url(), project=project, actor=request.user, org=request.org)
        log_activity(request, f"recorded the client's {what} on", f'{app.reference} {app.title}', project=project,
                     url=app.get_absolute_url(), internal_only=True)
        messages.success(request, f"The client's {what} is recorded on their behalf, and they have been told.")
    return None


def application_pdf_view(request, org_slug, pk, app_id):
    project = get_project(request, pk)
    app = get_object_or_404(visible_applications(request, project), pk=app_id)
    contract = _contract(project)
    data = application_pdf(app, contract, vocabulary_for(project))
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{project.reference}-{app.reference}.pdf"'
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


def application_evidence(request, org_slug, pk, app_id, kind):
    """The client's email or letter an admin attached when recording a notice. Whoever can see the application."""
    project = get_project(request, pk)
    app = get_object_or_404(visible_applications(request, project), pk=app_id)
    field = {'payment-notice': app.payment_notice_evidence, 'pay-less': app.pay_less_evidence}.get(kind)
    if field is None:
        raise Http404
    return serve(request, field, project=project, label=f"{app.reference} client's {kind.replace('-', ' ')}")
