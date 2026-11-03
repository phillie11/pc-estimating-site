"""
Needs me for the CVR and dayworks (registered in CvrConfig.ready() as two HookProviders, staff only).

  Prepare the CVR for <last month>   the job's manager, or a PM or QS on its team (owners and admins too,
                                     on jobs they run), once last month has ended, while its CVR is not
                                     started or still being prepared. Only jobs with an actual cost or an
                                     application for payment dated by the end of that month. Due on the
                                     company's CVR due day of this month.
  Sign off the CVR for <month>       whoever may sign it off (owners and admins; the job's PM or QS where
                                     the company allows it), while it waits. Due on its due day.
  Add DW-003 to the change register  signed daywork sheets: the job's manager, PM or QS, and whoever
                                     wrote them. Due 7 days after signing (the earliest, for several).
  Get DW-002 signed                  unsigned sheets more than 2 days old: whoever wrote them. Due 7 days
                                     after the work.

It runs behind the badge on every page, and adds no query of its own: everything it needs is a
subquery on the shared actions.providers.my_jobs query (job_annotations below, through its
JOB_ANNOTATIONS hook). One item per job and kind; several sheets link to the filtered register.
"""
from datetime import timedelta

from django.db.models import Count, Exists, IntegerField, Min, OuterRef, Subquery
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, Item
from projects.access import is_internal

from . import rules

SEND_WITHIN_DAYS = 7
CHASE_SIGNATURE_AFTER_DAYS = 2


def _first(qs, field):
    return Subquery(qs.values(field)[:1])


def _count(qs):
    return Subquery(qs.order_by().values('project').annotate(n=Count('pk')).values('n'), output_field=IntegerField())


def _min(qs, field):
    return Subquery(qs.order_by().values('project').annotate(m=Min(field)).values('m'))


def job_annotations(request):
    """Per-job flags for my_jobs(): last month's CVR, CVRs waiting for sign-off, daywork sheets to act on."""
    if request.membership is None or not is_internal(request):
        return {}
    from costs.models import LedgerEntry
    from payments.models import Application

    from .models import CommercialSettings, CVRPeriod, DayworkSheet
    today = timezone.localdate()
    last_month = rules.add_months(today, -1)
    last_end = rules.month_end(last_month)
    me = request.user.pk
    org_settings = CommercialSettings.objects.filter(organization=OuterRef('organization'))
    last = CVRPeriod.objects.filter(project=OuterRef('pk'), month=last_month)
    waiting = CVRPeriod.objects.filter(project=OuterRef('pk'), status=CVRPeriod.SUBMITTED).order_by('month', 'pk')
    signed = DayworkSheet.objects.filter(project=OuterRef('pk'), status=DayworkSheet.SIGNED).order_by('due_date', 'pk')
    signed_mine = signed.filter(ball_in_court_user=me)  # the writer holds the ball (services.new_sheet)
    old_unsigned = DayworkSheet.objects.filter(
        project=OuterRef('pk'), status=DayworkSheet.DRAFT, ball_in_court_user=me,
        work_date__lt=today - timedelta(days=CHASE_SIGNATURE_AFTER_DAYS)).order_by('due_date', 'pk')
    return {
        'cvr_cost_by_last': Exists(LedgerEntry.objects.filter(project=OuterRef('pk'), kind=LedgerEntry.ACTUAL,
                                                             date__lte=last_end)),
        'cvr_valued_by_last': Exists(Application.objects.filter(project=OuterRef('pk'), status__in=Application.COUNTED,
                                                                valuation_date__lte=last_end)),
        'cvr_due_day': _first(org_settings, 'cvr_due_day'), 'cvr_pm_signs': _first(org_settings, 'pm_can_sign_off'),
        'cvr_last_pk': _first(last, 'pk'), 'cvr_last_ref': _first(last, 'ref'), 'cvr_last_status': _first(last, 'status'),
        'cvr_wait_n': _count(waiting), 'cvr_wait_pk': _first(waiting, 'pk'), 'cvr_wait_ref': _first(waiting, 'ref'),
        'cvr_wait_month': _first(waiting, 'month'), 'cvr_wait_due': _first(waiting, 'due_date'),
        'dw_signed_n': _count(signed), 'dw_signed_pk': _first(signed, 'pk'), 'dw_signed_ref': _first(signed, 'ref'),
        'dw_signed_due': _min(signed, 'due_date'),
        'dw_mine_signed_n': _count(signed_mine), 'dw_mine_signed_pk': _first(signed_mine, 'pk'),
        'dw_mine_signed_ref': _first(signed_mine, 'ref'), 'dw_mine_signed_due': _min(signed_mine, 'due_date'),
        'dw_old_n': _count(old_unsigned), 'dw_old_pk': _first(old_unsigned, 'pk'),
        'dw_old_ref': _first(old_unsigned, 'ref'), 'dw_old_due': _min(old_unsigned, 'due_date'),
    }


def _jobs(request):
    if request.membership is None or not is_internal(request):
        return None, None
    from actions.providers import my_jobs
    jobs = [p for p in my_jobs(request) if hasattr(p, 'cvr_wait_n')]
    me = request.user.pk
    return jobs, {p.pk for p in jobs if p.manager_id == me or p.on_team_pm or p.on_team_qs}


def cvr_items(request):
    jobs, runs = _jobs(request)
    if not jobs:
        return []
    today = timezone.localdate()
    last_month = rules.add_months(today, -1)
    admin, slug, out = request.membership.is_admin, request.org.slug, []
    for p in jobs:
        due_day = p.cvr_due_day or 10
        if p.pk in runs and (p.cvr_cost_by_last or p.cvr_valued_by_last) and p.cvr_last_status in (None, 'DRAFT'):
            url = reverse('cvr_period', args=[slug, p.pk, p.cvr_last_pk]) if p.cvr_last_pk else \
                reverse('cvr_list', args=[slug, p.pk])
            out.append(Item(kind='cvr', title=f'Prepare the CVR for {rules.month_label(last_month)}', url=url,
                            ref=p.cvr_last_ref or '', project=p, due=rules.due_on(last_month, due_day),
                            status_label='Being prepared' if p.cvr_last_pk else 'Not started', tone='warn',
                            action='Check the figures, add accruals and submit it for sign-off',
                            key=f'cvr_prepare:{p.pk}:{last_month}'))
        if p.cvr_wait_n and (admin or (p.cvr_pm_signs and p.pk in runs)):
            n = p.cvr_wait_n
            title = f'Sign off the CVR for {rules.month_label(p.cvr_wait_month)}' if n == 1 else f'Sign off {n} CVRs'
            out.append(Item(kind='cvr', title=title, url=reverse('cvr_period', args=[slug, p.pk, p.cvr_wait_pk]),
                            ref=p.cvr_wait_ref if n == 1 else '', project=p,
                            due=p.cvr_wait_due or rules.due_on(p.cvr_wait_month, due_day), priority=PRIORITY_HIGH,
                            status_label='Waiting for sign-off', tone='accent',
                            action='Review and sign off' + ('' if n == 1 else ', oldest month first'),
                            key=f'cvr_signoff:{p.pk}'))
    return out


def daywork_items(request):
    jobs, runs = _jobs(request)
    if not jobs:
        return []
    slug, out = request.org.slug, []
    for p in jobs:
        runner = p.pk in runs
        n, pk, ref, due = ((p.dw_signed_n, p.dw_signed_pk, p.dw_signed_ref, p.dw_signed_due) if runner else
                           (p.dw_mine_signed_n, p.dw_mine_signed_pk, p.dw_mine_signed_ref, p.dw_mine_signed_due))
        if n:
            out.append(Item(kind='daywork', title=f'Add {ref} to the change register' if n == 1 else
                            f'Add {n} daywork sheets to the change register',
                            url=reverse('daywork_detail', args=[slug, p.pk, pk]) if n == 1 else
                            reverse('daywork_list', args=[slug, p.pk]) + '?show=SIGNED',
                            ref=ref if n == 1 else '', project=p, due=due, status_label='Signed', tone='accent',
                            action='Put it on a change event so it is valued', key=f'daywork_send:{p.pk}'))
        if p.dw_old_n:
            n = p.dw_old_n
            out.append(Item(kind='daywork', title=f'Get {p.dw_old_ref} signed' if n == 1 else
                            f'Get {n} daywork sheets signed',
                            url=reverse('daywork_detail', args=[slug, p.pk, p.dw_old_pk]) if n == 1 else
                            reverse('daywork_list', args=[slug, p.pk]) + '?show=DRAFT',
                            ref=p.dw_old_ref if n == 1 else '', project=p, due=p.dw_old_due,
                            status_label='Not signed yet', tone='warn',
                            action='Get the client side to sign it, or send it on unsigned', key=f'daywork_sign:{p.pk}'))
    return out
