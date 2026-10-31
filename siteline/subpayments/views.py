"""
Subcontractor payments. Staff (owners, admins, the job's PM/QS) work under Commercial -> Sub
payments; an appointed subcontractor sees their own under Decisions -> Payments. Every project
view goes through get_project() and subpayments.access. Company pages (default terms, CIS
details, the CIS monthly summary) are owners' and admins'.
"""
import csv
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from contracts.commercial import commercial_context
from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from directory.models import Package, Subcontractor
from projects.access import get_project
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import rules, services
from .access import STAFF, SUB, applications_for, packages_for, require, require_staff, role
from .forms import (
    ApplicationForm,
    MaterialsForm,
    NoticeForm,
    PaymentForm,
    ReleaseForm,
    ReversalForm,
    SettingsForm,
    SubcontractForm,
    TaxForm,
)
from .models import SubApplication, Subcontract, SubcontractorTax, SubPayment, settings_for, subcontract_for, tax_for
from .pdf import advice_pdf

ZERO = Decimal('0.00')


def _ctx(request, project, who, **extra):
    extra.setdefault('advisory', rules.ADVISORY)
    extra.setdefault('hmrc_check', rules.HMRC_CHECK)
    if who == STAFF:
        return commercial_context(request, project, 'subpayments', staff=True, read_only=project.is_archived, **extra)
    return project_context(request, project, 'subpayments', staff=False, read_only=project.is_archived, **extra)


def _safe(value):
    """A CSV cell a spreadsheet won't run as a formula."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def _form_key(request):
    key = (request.POST.get('form_key') or '').strip()[:60]
    return f'subpay:{key}' if key else None


def _sub_users(project, subcontractor_id):
    from projects.models import ProjectMember
    return [m.user for m in ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                                          subcontractor_id=subcontractor_id).select_related('user')]


def _team(project):
    from projects.files import staff_for
    return [project.manager] if project.manager else staff_for(project, project.organization)


def _summary(sc):
    apps = [a for a in sc.applications.all() if a.status in SubApplication.COUNTED]
    paid = sum((p.gross for a in apps for p in a.payments.all() if not p.is_reversed), ZERO)
    certified = sum((a.notified_sum for a in apps), ZERO)
    last = max(apps, key=lambda a: a.valuation_date, default=None)
    return {'certified': certified, 'paid': paid, 'retention': last.retention if last else ZERO,
            'open': sum(1 for a in apps if a.status in SubApplication.AWAITING),
            'late': any(a.stands_as_notified for a in apps)}


FILTERS = [('', 'All'), ('awaiting', 'Awaiting our payment notice'), ('stands', 'Stands as notified'),
           ('topay', 'To pay'), ('overdue', 'Overdue'), ('paid', 'Paid')]


def subpay_list(request, org_slug, pk):
    project = get_project(request, pk)
    who = require(request, project)
    packages = list(packages_for(request, project).select_related('commitment') if who == STAFF
                    else packages_for(request, project))
    subcontracts = {sc.package_id: sc for sc in Subcontract.objects.filter(package__in=packages)
                    .prefetch_related('applications__payments')}
    taxes = {t.subcontractor_id: t for t in SubcontractorTax.objects.filter(
        subcontractor_id__in={p.subcontractor_id for p in packages})}  # one query, not one per package
    rows = []
    for pkg in packages:
        sc = subcontracts.get(pkg.pk)
        tax = taxes.get(pkg.subcontractor_id) or SubcontractorTax(subcontractor=pkg.subcontractor,
                                                                  organization_id=pkg.organization_id)
        rows.append({'package': pkg, 'sc': sc, 'tax': tax,
                     'summary': _summary(sc) if sc else None,
                     'order': getattr(pkg, 'commitment', None) if who == STAFF else None})
    apps = list(applications_for(request, project).prefetch_related('payments').order_by('-valuation_date', '-number'))
    for a in apps:  # paid so far from the prefetched payments, not a SUM query per application
        a._paid_gross = sum((p.gross for p in a.payments.all() if not p.is_reversed), ZERO)
    shown = request.GET.get('show', '') if request.GET.get('show', '') in dict(FILTERS) else ''
    filtered = {
        'awaiting': [a for a in apps if a.status == SubApplication.RECEIVED and not a.stands_as_notified],
        'stands': [a for a in apps if a.stands_as_notified],
        'topay': [a for a in apps if a.status in SubApplication.AWAITING and a.outstanding > 0],
        'overdue': [a for a in apps if a.is_overdue],
        'paid': [a for a in apps if a.status == SubApplication.PAID],
    }.get(shown, apps)
    return render(request, 'subpayments/list.html', _ctx(
        request, project, who, rows=rows, applications=filtered, all_count=len(apps), shown=shown,
        filters=FILTERS if who == STAFF else [f for f in FILTERS if f[0] in ('', 'paid')],
        late_count=sum(1 for a in apps if a.stands_as_notified)))


def subpay_open(request, org_slug, pk, package_id):
    """Staff: open (and the first time, set up from the company defaults) a package's subcontract terms."""
    project = get_project(request, pk)
    require_staff(request, project)
    pkg = get_object_or_404(Package, project=project, pk=package_id, status=Package.APPOINTED)
    existing = Subcontract.objects.filter(package=pkg).first()
    if existing:
        return redirect(existing)
    if request.method != 'POST':  # setting up writes, so only from the "Set up terms" button
        return redirect('subpay_list', org_slug=org_slug, pk=pk)
    return redirect(subcontract_for(pkg, request.user))


def _subcontract(request, project, sc_id):
    who = require(request, project)
    sc = get_object_or_404(Subcontract.objects.select_related('package__subcontractor', 'package__trade'),
                           project=project, pk=sc_id)
    if who == SUB and sc.package not in packages_for(request, project):
        raise Http404
    return who, sc


def subpay_subcontract(request, org_slug, pk, sc_id):
    project = get_project(request, pk)
    who, sc = _subcontract(request, project, sc_id)
    apps = list(sc.applications.all().prefetch_related('payments').order_by('valuation_date', 'number'))
    counted = [a for a in apps if a.status in SubApplication.COUNTED]
    ledger_rows, held, at_pc, at_defects = rules.retention_ledger(
        [(a.reference, a.valuation_date, a.retention_stage, a.retention) for a in counted])
    payments = [p for a in apps for p in a.payments.all()]
    today = timezone.localdate()
    latest, nxt = sc.around(today)
    return render(request, 'subpayments/subcontract.html', _ctx(
        request, project, who, sc=sc, tax=tax_for(sc.subcontractor), applications=list(reversed(apps)),
        retention_rows=ledger_rows, retention_held=held, released_pc=at_pc, released_defects=at_defects,
        payments=payments, order=sc.commitment if who == STAFF else None, latest=latest, nxt=nxt,
        next_cycle=sc.cycle(nxt) if nxt else None, release_form=ReleaseForm(initial={'stage_date': sc.pc_date}),
        stage_label=dict(rules.STAGE_CHOICES).get(sc.stage_at(today), ''), summary=_summary(sc)))


def subpay_terms(request, org_slug, pk, sc_id):
    project = get_project(request, pk)
    require_staff(request, project)
    sc = get_object_or_404(Subcontract, project=project, pk=sc_id)
    form = SubcontractForm(request.POST or None, instance=sc)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        messages.success(request, 'Subcontract terms saved. Applications already received keep their dates.')
        return redirect(sc)
    return render(request, 'subpayments/terms_form.html', _ctx(request, project, STAFF, sc=sc, form=form))


def subpay_release(request, org_slug, pk, sc_id):
    project = get_project(request, pk)
    require_staff(request, project)
    sc = get_object_or_404(Subcontract, project=project, pk=sc_id)
    if request.method != 'POST':
        return redirect(sc)
    form = ReleaseForm(request.POST)
    if form.is_valid():
        try:
            app = services.release_retention(sc, request.user, form.cleaned_data['stage_date'])
        except services.SubPayError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f'{app.reference} raised for the retention release. Give the payment notice '
                                      'and pay it as any other application.')
            return redirect(app)
    else:
        messages.error(request, 'Give the release date.')
    return redirect(sc)


def subpay_apply(request, org_slug, pk, package_id):
    """The sub applies through the portal, or staff record an application the sub sent another way."""
    project = get_project(request, pk)
    who = require(request, project)
    pkg = get_object_or_404(packages_for(request, project), pk=package_id)
    sc = Subcontract.objects.filter(package=pkg).first()
    if sc is None and not project.is_archived:
        # Terms from the company defaults: saved only when the application is sent (no writes on GET)
        sc = subcontract_for(pkg, request.user, create=request.method == 'POST')
    if request.method == 'POST' and _form_key(request) is None:
        return HttpResponseBadRequest('Please reload the page and try again.')
    initial = {}
    raw = request.GET.get('valuation_date', '')
    try:
        initial['valuation_date'] = date.fromisoformat(raw) if raw else None
    except ValueError:
        pass
    if not initial.get('valuation_date') and sc is not None:
        initial['valuation_date'] = sc.around(timezone.localdate())[0]
    if sc is not None and sc.pk:
        last = sc.applications.filter(status__in=SubApplication.COUNTED).order_by('-valuation_date').first()
        if last:
            initial['gross_to_date'] = last.gross_to_date
    form = ApplicationForm(request.POST or None, staff=who == STAFF, subcontract=sc, initial=initial)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            app = services.receive_application(sc, request.user, d['valuation_date'], d['gross_to_date'],
                                               d.get('materials') or ZERO, d.get('notes', ''), by_sub=who == SUB,
                                               received_on=d.get('received_on'),
                                               override_reason=d.get('override_reason', '') if who == STAFF else '',
                                               form_key=_form_key(request))
        except services.AlreadyRecorded as e:
            messages.info(request, f'{e.record.reference} was already sent from this form, so it was not added again.')
            return redirect(e.record)
        except IntegrityError:  # the same form twice at the same moment
            messages.info(request, 'That application was already sent from this form.')
            return redirect('subpay_list', org_slug=org_slug, pk=pk)
        except services.SubPayError as e:
            form.add_error(None, str(e))
        else:
            label = f'{app.reference} {app.title}'
            if who == SUB:
                notify.alert(_team(project), f'{pkg.subcontractor.company_name} applied for payment: {app.reference}',
                             app.get_absolute_url(), project=project, actor=request.user, org=request.org)
                messages.success(request, f'Your application {app.reference} has been sent. {request.org.name} must '
                                          f'give a payment notice by {app.payment_notice_by:%a %d %b %Y}.')
            else:
                messages.success(request, f'{app.reference} recorded. Our payment notice is due by '
                                          f'{app.payment_notice_by:%a %d %b %Y}.')
            log_activity(request, 'applied for payment' if who == SUB else 'recorded a sub application', label,
                         project=project, url=app.get_absolute_url(), internal_only=True)
            return redirect(app)
    shown = form.cleaned_data.get('valuation_date') if form.is_bound and hasattr(form, 'cleaned_data') else None
    shown = shown or initial.get('valuation_date')
    cycle = sc.cycle(shown, timezone.localdate()) if sc is not None and shown else None
    return render(request, 'subpayments/apply.html', _ctx(request, project, who, form=form, package=pkg, sc=sc,
                                                          cycle=cycle))


def _application(request, project, app_id):
    return get_object_or_404(applications_for(request, project), pk=app_id)


def subpay_application(request, org_slug, pk, app_id):
    project = get_project(request, pk)
    who = require(request, project)
    app = _application(request, project, app_id)
    if request.method == 'POST':
        try:
            response = _act(request, project, app, who, request.POST.get('action', ''))
        except (InvalidTransition, RecordConflict, services.SubPayError) as e:
            messages.error(request, str(e))
            response = None
        return response or redirect(app)
    return render(request, 'subpayments/application.html', _detail(request, project, app, who))


def _detail(request, project, app, who, **forms):
    from .workflow import withdraw_refusal
    today = timezone.localdate()
    tax = tax_for(app.subcontract.subcontractor)
    staff = who == STAFF and not project.is_archived
    can_notice = staff and app.status == SubApplication.RECEIVED
    can_pay_less = staff and app.status in (SubApplication.RECEIVED, SubApplication.NOTIFIED)
    pay_less_late = can_pay_less and app.pay_less_by is not None and today > app.pay_less_by
    refusal = withdraw_refusal(app)
    return _ctx(
        request, project, who, app=app, tax=tax, today=today, clocks=app.clocks(today),
        payments=list(app.payments.select_related('recorded_by', 'reversed_by')),
        stage_label=dict(rules.STAGE_CHOICES).get(app.retention_stage, ''),
        can_notice=can_notice, can_pay_less=can_pay_less and not pay_less_late, pay_less_late=pay_less_late,
        pay_less_refusal=rules.pay_less_refusal(today, app.pay_less_by) if pay_less_late else '',
        notice_late=can_notice and app.payment_notice_by and today > app.payment_notice_by,
        notice_form=forms.get('notice_form') or NoticeForm(application=app, initial={'amount': app.amount_due}),
        pay_less_form=forms.get('pay_less_form') or NoticeForm(application=app, kind='pay_less',
                                                               initial={'amount': app.notified_sum}),
        payment_form=forms.get('payment_form') or PaymentForm(application=app,
                                                              initial={'gross': app.outstanding or None}),
        materials_form=forms.get('materials_form') or MaterialsForm(application=app,
                                                                    initial={'amount': app.materials}),
        reversal_form=forms.get('reversal_form') or ReversalForm(),
        can_pay=staff and app.status in SubApplication.AWAITING and app.outstanding > 0,
        can_materials=staff and app.status in SubApplication.AWAITING,
        can_reverse=staff and request.membership.is_admin,
        can_withdraw=not project.is_archived and not refusal and app.status == SubApplication.RECEIVED,
        projected=app.projected_advice if staff else None, no_order=staff and app.subcontract.commitment is None,
        reverse_charge_note=rules.REVERSE_CHARGE_NOTE)


def _act(request, project, app, who, action):
    label = f'{app.reference} {app.title}'
    sub_users = _sub_users(project, app.subcontract.package.subcontractor_id)
    sym = request.org.currency_symbol or '£'
    if action == 'withdraw':
        apply_transition(app, 'withdraw', request.user)
        notify.alert(_team(project) if who == SUB else sub_users, f'{app.reference} was withdrawn',
                     app.get_absolute_url(), project=project, actor=request.user, org=request.org)
        messages.success(request, f'{app.reference} withdrawn.')
        return None
    if who != STAFF:
        raise PermissionDenied
    if action in ('payment_notice', 'pay_less'):
        form = NoticeForm(request.POST, application=app, kind=action)
        if not form.is_valid():
            key = 'notice_form' if action == 'payment_notice' else 'pay_less_form'
            return render(request, 'subpayments/application.html', _detail(request, project, app, who, **{key: form}))
        d = form.cleaned_data
        apply_transition(app, action, request.user, note=d['basis'][:200], amount=d['amount'], basis=d['basis'])
        from .workflow import settle_if_covered
        settle_if_covered(app, request.user)
        what = 'payment notice' if action == 'payment_notice' else 'pay less notice'
        notify.alert(sub_users, f'{request.org.name} issued a {what} on {app.reference}: {sym}{d["amount"]:,} '
                                'considered due', app.get_absolute_url(), project=project, actor=request.user,
                     org=request.org)
        log_activity(request, f'gave a {what} on', label, project=project, url=app.get_absolute_url(),
                     internal_only=True)
        late = action == 'payment_notice' and app.payment_notice_on > app.payment_notice_by
        messages.success(request, f'Our {what} is recorded and the subcontractor has been told.' + (
            ' It was given after the deadline, so the application still stands as the notified sum: give a pay less '
            'notice in time if you mean to pay less.' if late else ''))
    elif action == 'materials':
        form = MaterialsForm(request.POST, application=app)
        if not form.is_valid():
            return render(request, 'subpayments/application.html', _detail(request, project, app, who,
                                                                          materials_form=form))
        services.adjust_materials(app, request.user, form.cleaned_data['amount'], form.cleaned_data['reason'])
        messages.success(request, 'Materials for CIS updated.')
    elif action == 'payment':
        key = _form_key(request)
        if key is None:
            return HttpResponseBadRequest('Please reload the page and try again.')
        form = PaymentForm(request.POST, application=app)
        if not form.is_valid():
            return render(request, 'subpayments/application.html', _detail(request, project, app, who,
                                                                          payment_form=form))
        d = form.cleaned_data
        try:
            p = services.record_payment(app, request.user, d['gross'], d['paid_on'], d.get('materials'),
                                        d.get('reference', ''), form_key=key)
        except (services.AlreadyRecorded, IntegrityError):
            messages.info(request, 'That payment was already recorded from this form, so it was not added again.')
            return None
        if services.earlier_tax_month(p.paid_on):
            messages.warning(request, f'This payment is dated {p.paid_on:%d %b %Y}, in {p.tax_month.label.split(":")[0]}, '
                                      'an earlier tax month than this one. If that CIS return has been filed it may '
                                      'need amending.')
        notify.alert(sub_users, f'Payment advice for {app.reference}: {sym}{p.net:,} paid to you',
                     app.get_absolute_url(), project=project, actor=request.user, org=request.org)
        log_activity(request, 'paid a subcontractor on', label, project=project, url=app.get_absolute_url(),
                     internal_only=True)
        messages.success(request, f'Payment recorded: {sym}{p.gross:,} gross posted to the cost ledger, CIS '
                                  f'{sym}{p.cis_deduction:,}, net paid {sym}{p.net:,}.')
    elif action == 'reverse_payment':
        if not request.membership.is_admin:
            raise PermissionDenied
        payment = get_object_or_404(SubPayment, application=app, pk=request.POST.get('payment') or 0,
                                    reversed_at__isnull=True)
        form = ReversalForm(request.POST)
        if not form.is_valid():
            return render(request, 'subpayments/application.html', _detail(request, project, app, who,
                                                                          reversal_form=form))
        services.reverse_payment(payment, request.user, form.cleaned_data['reason'])
        log_activity(request, 'reversed a sub payment on', label, project=project, url=app.get_absolute_url(),
                     internal_only=True)
        messages.success(request, 'Payment reversed, and its cost ledger entry with it.')
    else:
        raise Http404
    return None


def subpay_advice(request, org_slug, pk, payment_id):
    project = get_project(request, pk)
    require(request, project)
    payment = get_object_or_404(SubPayment.objects.select_related('application'), pk=payment_id,
                                application__in=applications_for(request, project))
    # The HMRC verification number: the sub's own copy and owners/admins, not the job's PM or QS
    show_number = role(request, project) == SUB or request.membership.is_admin
    resp = HttpResponse(advice_pdf(payment, show_verification=show_number), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{project.reference}-{payment.application.reference}-advice.pdf"'
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


# --- Company pages (owners and admins) --------------------------------------------------

@admin_required
def subpay_settings(request, org_slug):
    obj = settings_for(request.org, create=request.method == 'POST')
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Default subcontract terms saved. Subcontracts already set up keep their own.')
        return redirect('subpay_settings', org_slug=org_slug)
    return render(request, 'subpayments/settings.html', {'form': form, 'advisory': rules.ADVISORY})


@admin_required
def subpay_tax_list(request, org_slug):
    subs = list(Subcontractor.objects.filter(archived_at__isnull=True).order_by('company_name'))
    taxes = {t.subcontractor_id: t for t in SubcontractorTax.objects.filter(subcontractor__in=subs)}
    status = request.GET.get('cis', '')
    rows = [(s, taxes.get(s.pk) or tax_for(s)) for s in subs]
    if status in rules.CIS_RATES:
        rows = [r for r in rows if r[1].cis_status == status]
    return render(request, 'subpayments/tax_list.html', {'rows': rows, 'status': status,
                                                         'choices': rules.CIS_CHOICES, 'hmrc_check': rules.HMRC_CHECK})


@admin_required
def subpay_tax(request, org_slug, sub_id):
    sub = get_object_or_404(Subcontractor, pk=sub_id)
    obj = tax_for(sub)
    form = TaxForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        t = form.save(commit=False)
        t.updated_by = request.user
        t.save()
        messages.success(request, f'CIS and VAT details for {sub.company_name} saved. Payments already made keep '
                                  'the rate they were made at.')
        return redirect('subpay_tax_list', org_slug=org_slug)
    return render(request, 'subpayments/tax_form.html', {'sub': sub, 'form': form, 'tax': obj,
                                                         'hmrc_check': rules.HMRC_CHECK})


def _row(rows, key, sub, number, adjustment=False):
    return rows.setdefault(key, {'sub': sub, 'tax': tax_for(sub), 'number': number, 'gross': ZERO, 'materials': ZERO,
                                 'cis': ZERO, 'vat': ZERO, 'net': ZERO, 'count': 0, 'statuses': set(),
                                 'adjustment': adjustment, 'months': set()})


def _add(r, p, sign):
    for field, attr in (('gross', 'gross'), ('materials', 'materials'), ('cis', 'cis_deduction'), ('vat', 'vat'),
                        ('net', 'net')):
        r[field] += sign * getattr(p, attr)
    r['count'] += 1
    r['statuses'].add(p.cis_status)


def cis_summary(month):
    """
    One tax month, per subcontractor and the verification number frozen on the payment: payments
    made in the month (as recorded), less any reversed in the same month. A payment reversed in a
    LATER tax month stays in its own month (that return may already be filed) and shows as an
    adjustment line in the month it was reversed, with a note that the earlier return may need amending.
    """
    rows = {}
    base = SubPayment.objects.select_related('application__subcontract__package__subcontractor')
    for p in base.filter(paid_on__gte=month.start, paid_on__lte=month.end).order_by('paid_on', 'pk'):
        if p.is_reversed and rules.tax_month(timezone.localdate(p.reversed_at)).start == month.start:
            continue  # reversed within the same month: nothing to return
        sub = p.application.subcontract.package.subcontractor
        _add(_row(rows, (sub.pk, p.verification_number, False), sub, p.verification_number), p, 1)
    for p in base.filter(reversed_at__isnull=False).order_by('paid_on', 'pk'):
        if rules.tax_month(timezone.localdate(p.reversed_at)).start != month.start or p.paid_on >= month.start:
            continue
        sub = p.application.subcontract.package.subcontractor
        r = _row(rows, (sub.pk, p.verification_number, True), sub, p.verification_number, adjustment=True)
        _add(r, p, -1)
        r['months'].add(rules.tax_month(p.paid_on).label.split(':')[0])
    out = sorted(rows.values(), key=lambda r: (r['sub'].company_name.lower(), r['number'], r['adjustment']))
    totals = {k: sum((r[k] for r in out), ZERO) for k in ('gross', 'materials', 'cis', 'vat', 'net')}
    return out, totals


@admin_required
def subpay_cis(request, org_slug):
    today = timezone.localdate()
    month = rules.tax_month_from_key(request.GET.get('month', '')) or rules.tax_month(today)
    rows, totals = cis_summary(month)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="cis-{month.key}-not-a-submission.csv"'
        w = csv.writer(resp)
        w.writerow([_safe(f'CIS figures, {month.label}. {rules.NOT_A_RETURN}')])
        w.writerow(['Subcontractor', 'UTR', 'Verification number (when paid)', 'CIS status (when paid)',
                    'Total paid (excluding VAT)', 'Cost of materials', 'CIS deducted', 'Payments', 'Note'])
        for r in rows:
            statuses = ', '.join(sorted(rules.CIS_SHORT.get(s, s) for s in r['statuses']))
            note = (f'Adjustment: payment from {", ".join(sorted(r["months"]))} reversed this month. The earlier '
                    'return may need amending.') if r['adjustment'] else ''
            w.writerow([_safe(r['sub'].company_name), _safe(r['tax'].utr), _safe(r['number']), statuses,
                        r['gross'], r['materials'], r['cis'], r['count'], note])
        w.writerow(['Total', '', '', '', totals['gross'], totals['materials'], totals['cis'], '', ''])
        return resp
    prev = rules.tax_month(month.start - timedelta(days=1))
    nxt = rules.tax_month(month.end + timedelta(days=1))
    return render(request, 'subpayments/cis.html', {
        'month': month, 'rows': rows, 'totals': totals, 'prev': prev, 'next': nxt if nxt.start <= today else None,
        'not_a_return': rules.NOT_A_RETURN, 'hmrc_check': rules.HMRC_CHECK,
        'export_url': reverse('subpay_cis', args=[org_slug]) + f'?month={month.key}&export=csv'})
