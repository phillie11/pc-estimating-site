"""
What applications for payment are waiting on this person, for the "Needs me" registry
(registered in PaymentsConfig.ready() as a HookProvider, which also drops anything on a project
outside the person's portal_projects).

Contractor (owners, admins and the job's PM; the PM only when the job has one):
  - an application due to be submitted: a valuation date within the next 7 days, or passed,
    with nothing submitted for it (a draft is pointed to so it can be finished)
  - payment overdue: the final date for payment has passed with money outstanding
Payer (the project's client, or the consultant named on the contract as administrator):
  - an application to respond to: give the payment notice, by its deadline
  - the pay less deadline approaching (while a pay less notice can still be given)
  - the notified sum to pay by the final date

Dates are advisory: check your contract.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, Item
from contracts.models import MainContract
from projects.access import is_internal, visible_projects
from projects.models import Project, ProjectMember

from . import rules
from .models import Application
from .summary import prime_paid_totals

WARN_DAYS = 7
NEAR = timedelta(days=12)  # an application within this of a valuation date counts as "for" it


def items(request):
    if request.membership is None:
        return []
    return _contractor(request) if is_internal(request) else _client(request)


def _my_jobs(request):
    """
    Live jobs whose payments wait on this person: the PM (the job's manager or a PM on its team, as
    payments.access.is_project_pm); owners and admins where the job has no PM. From the one shared
    query of the person's jobs (actions.providers.my_jobs), as this runs behind the badge on every page.
    """
    from actions.providers import my_jobs
    admin, me = request.membership.is_admin, request.user.pk
    return [p for p in my_jobs(request) if p.stage in Project.DELIVERY_STAGES
            and (p.manager_id == me or p.on_team_pm or (admin and p.manager_id is None))]


def _contractor(request):
    today = timezone.localdate()
    jobs = _my_jobs(request)
    if not jobs:
        return []
    by_pk = {p.pk: p for p in jobs}
    contracts = {c.project_id: c for c in MainContract.objects.filter(project__in=jobs)}
    apps = prime_paid_totals(list(Application.objects.filter(project__in=jobs)
                                  .exclude(status=Application.STATUS_WITHDRAWN)))
    out = []
    for p in jobs:
        contract = contracts.get(p.pk)
        if contract is None or not contract.valuation_day:
            continue
        for valuation in rules.submission_window(contract.valuation_day, today, WARN_DAYS):
            if p.start_date and valuation < p.start_date:
                continue
            near = [a for a in apps if a.project_id == p.pk and abs(a.valuation_date - valuation) <= NEAR]
            if any(a.status != Application.STATUS_DRAFT for a in near):
                continue
            draft = next((a for a in near if a.is_draft), None)
            url = draft.get_absolute_url() if draft else \
                reverse('application_create', args=[request.org.slug, p.pk]) + f'?valuation_date={valuation}'
            out.append(Item(
                kind='payment', title=f'Application for payment for {valuation.day} {valuation:%b %Y}', url=url,
                ref=draft.reference if draft else '', project=p, due=valuation,
                action='Finish and submit it' if draft else 'Make up and submit the application',
                status_label='Draft' if draft else 'To submit', tone='warn' if valuation <= today else 'accent',
                detail=f'{contract.vocabulary["valuation_date"].capitalize()} {valuation.day} {valuation:%b}',
                kind_label='Applications to submit', key=f'ap-due:{p.pk}:{valuation}'))
            break  # one prompt per job
    for a in apps:
        if a.status in Application.AWAITING_PAYMENT and a.is_overdue:
            a.project = by_pk[a.project_id]
            out.append(Item(
                kind='payment', title=f'Payment overdue: {a.title}', url=a.get_absolute_url(), ref=a.reference,
                project=a.project, due=a.final_date, priority=PRIORITY_HIGH, action='Chase the payment',
                status_label='Payment overdue', tone='bad', amount=a.outstanding,
                detail=f'Final date for payment was {a.final_date.day} {a.final_date:%b}',
                kind_label='Payments overdue', key=f'ap-late:{a.pk}'))
    return out


def _client(request):
    today = timezone.localdate()
    mine = ProjectMember.objects.filter(user=request.user, role__in=(ProjectMember.ROLE_CLIENT,
                                                                     ProjectMember.ROLE_CONSULTANT),
                                        project__in=visible_projects(request).values('pk')).values_list('project_id', 'role')
    # Clients, and a consultant only on a job whose contract names them as its administrator (as
    # payments.access.is_payer, for an active external member, which the request already established)
    mine = list(mine)
    projects = [pid for pid, role in mine if role == ProjectMember.ROLE_CLIENT]
    consulting = [pid for pid, role in mine if role == ProjectMember.ROLE_CONSULTANT]
    if consulting:
        from django.db.models import F
        projects += list(MainContract.objects.filter(
            project_id__in=consulting, administrator__user=request.user, administrator__role=ProjectMember.ROLE_CONSULTANT,
            administrator__project_id=F('project_id')).values_list('project_id', flat=True))
    if not projects:
        return []
    qs = prime_paid_totals(list(Application.objects.filter(
        project_id__in=projects, status__in=Application.AWAITING_PAYMENT, visibility=Application.VIS_SHARED)
        .select_related('project', 'project__organization').order_by('valuation_date')))
    out = []
    for a in qs:
        common = dict(kind='payment', url=a.get_absolute_url(), ref=a.reference, project=a.project, key=f'ap:{a.pk}')
        if a.status == Application.STATUS_SUBMITTED and a.payment_notice_by and today <= a.payment_notice_by:
            out.append(Item(title=a.title, due=a.payment_notice_by, action='Give your payment notice',
                            status_label='Payment notice due', tone='accent', priority=PRIORITY_HIGH,
                            detail=f'Applied for {_money(a, a.total)} including VAT',
                            kind_label='Applications to respond to', **common))
        elif a.pay_less_open:
            out.append(Item(title=a.title, due=a.pay_less_by, action='Check the sum due',
                            status_label='Pay less deadline', tone='warn' if (a.pay_less_by - today).days <= WARN_DAYS
                            else 'info', detail=f'Notified sum {_money(a, a.notified_total)} including VAT, payable by '
                            f'{a.final_date.day} {a.final_date:%b}. Only if you intend to pay less, give a pay less '
                            f'notice by {a.pay_less_by.day} {a.pay_less_by:%b}.',
                            kind_label='Pay less deadlines', **common))
        elif a.outstanding > 0:
            out.append(Item(title=a.title, due=a.final_date, action='Pay the notified sum',
                            status_label='Payment overdue' if a.is_overdue else 'To pay',
                            tone='bad' if a.is_overdue else 'info',
                            detail=f'{_money(a, a.outstanding)} outstanding',
                            kind_label='Payments to make', **common))
    return out


def _money(app, value):
    symbol = getattr(app.project.organization, 'currency_symbol', '£') or '£'
    return f'{symbol}{rules.money(value):,}'
