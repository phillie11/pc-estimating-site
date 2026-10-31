"""
What subcontractor payments are waiting on this person, for "Needs me" (a HookProvider registered
in SubpaymentsConfig.ready(), which also drops anything outside the person's portal_projects).

Staff (the job's PM/QS; owners and admins where the job has no PM), one item per application:
  - payment notice due: give our payment notice by its deadline
  - pay less deadline: the last day to pay less than the notified sum (shown red once our notice
    was missed, because the sub's application then stands as the notified sum)
  - pay by the final date: money still to pay (overdue after the final date)
Appointed subcontractor:
  - apply for payment: a valuation date within the next 7 days, or passed, with no application for it

Dates are advisory: check your contract.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, PRIORITY_NORMAL, Item
from directory.models import Package
from projects.access import is_internal, visible_projects
from projects.models import Project, ProjectMember

from . import rules
from .models import SubApplication, Subcontract

WARN_DAYS = 7


def items(request):
    if request.membership is None:
        return []
    return _staff(request) if is_internal(request) else _sub(request)


def _money(org, value):
    return f'{getattr(org, "currency_symbol", "£") or "£"}{rules.money(value):,}'


def _staff(request):
    today = timezone.localdate()
    # The job's PM or QS (costs.access.runs_job), or owners and admins where the job has no PM, from the
    # one shared query of the person's jobs (actions.providers.my_jobs)
    from actions.providers import my_jobs
    admin, me = request.membership.is_admin, request.user.pk
    jobs = [p for p in my_jobs(request)
            if p.manager_id == me or p.on_team_pm or p.on_team_qs or (admin and p.manager_id is None)]
    if not jobs:
        return []
    by_pk = {p.pk: p for p in jobs}
    out = []
    qs = SubApplication.objects.filter(project__in=jobs, status__in=SubApplication.AWAITING) \
        .select_related('subcontract__package__subcontractor').order_by('valuation_date')
    for a in qs:
        a.project = by_pk[a.project_id]
        who = a.subcontract.package.subcontractor.company_name
        common = dict(kind='subpayment', url=a.get_absolute_url(), ref=a.reference, project=a.project,
                      key=f'sa:{a.pk}', detail=who)
        if a.status == SubApplication.RECEIVED and a.payment_notice_by and today <= a.payment_notice_by:
            out.append(Item(title=a.title, due=a.payment_notice_by, action='Give our payment notice',
                            status_label='Payment notice due', tone='accent', priority=PRIORITY_HIGH,
                            amount=a.amount_due, kind_label='Sub payment notices due', **common))
        elif a.pay_less_open:
            late = a.stands_as_notified
            out.append(Item(title=a.title, due=a.pay_less_by,
                            action='Pay less notice, only if paying less than the notified sum',
                            status_label='No payment notice given: stands as notified' if late else 'Pay less deadline',
                            tone='bad' if late else ('warn' if (a.pay_less_by - today).days <= WARN_DAYS else 'info'),
                            priority=PRIORITY_HIGH if late else PRIORITY_NORMAL, amount=a.notified_sum,
                            kind_label='Sub pay less deadlines', **common))
        elif a.outstanding > 0:
            out.append(Item(title=a.title, due=a.final_date, action='Pay by the final date',
                            status_label='Payment overdue' if a.is_overdue else 'To pay',
                            tone='bad' if a.is_overdue else 'info',
                            priority=PRIORITY_HIGH if a.is_overdue else PRIORITY_NORMAL, amount=a.outstanding,
                            kind_label='Sub payments to make', **common))
    return out


def _sub(request):
    """Fixed queries whatever the number of jobs and packages (it runs behind the badge on every page)."""
    today = timezone.localdate()
    pairs = list(ProjectMember.objects.filter(
        user=request.user, role=ProjectMember.ROLE_SUBCONTRACTOR, subcontractor__isnull=False,
        project__in=visible_projects(request).filter(stage__in=Project.DELIVERY_STAGES).values('pk'))
        .values_list('project_id', 'subcontractor_id'))
    if not pairs:
        return []
    from django.db.models import Q
    q = Q()
    for project_id, sub_id in pairs:
        q |= Q(project_id=project_id, subcontractor_id=sub_id)
    packages = list(Package.objects.filter(q, status=Package.APPOINTED).select_related('trade', 'project'))
    subcontracts = {sc.package_id: sc for sc in Subcontract.objects.filter(package__in=packages)}
    latest_app = {}
    for sc_id, valuation in SubApplication.objects.filter(subcontract__in=subcontracts.values()) \
            .exclude(status=SubApplication.WITHDRAWN).values_list('subcontract_id', 'valuation_date'):
        latest_app[sc_id] = max(valuation, latest_app.get(sc_id, valuation))
    out = []
    for pkg in packages:
        p = pkg.project
        sc = subcontracts.get(pkg.pk)
        if sc is None:
            continue  # terms not set up yet: the contractor hasn't opened this subcontract
        latest, nxt = sc.around(today)
        target = nxt if nxt and (nxt - today).days <= WARN_DAYS else latest
        if target is None or (p.start_date and target < p.start_date - timedelta(days=31)):
            continue
        if sc.pk in latest_app and latest_app[sc.pk] >= target - timedelta(days=12):
            continue
        url = reverse('subpay_apply', args=[request.org.slug, p.pk, pkg.pk]) + f'?valuation_date={target}'
        out.append(Item(kind='subpayment', title=f'Apply for payment: {pkg.trade or "your package"}', url=url,
                        project=p, due=target, action='Apply for payment', status_label='To apply',
                        tone='accent', detail=f'Valuation date {target.day} {target:%b %Y}',
                        kind_label='Applications for payment', key=f'sa-due:{pkg.pk}:{target}'))
    return out
