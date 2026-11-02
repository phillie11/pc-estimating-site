"""
Needs me for the CVR and dayworks (registered in CvrConfig.ready() as a HookProvider, staff only).

  Prepare the CVR for <last month>   the job's manager, or a PM or QS on its team (owners and admins too,
                                     on jobs they run), once last month has ended, while its CVR is not
                                     started or still being prepared. Jobs with costs only. Due on the
                                     company's CVR due day of this month.
  Sign off CVR-004                   whoever may sign it off (owners and admins; the job's PM or QS where
                                     the company allows it), while it waits. Due the same day.
  Add DW-003 to the change register  a signed daywork sheet: the job's manager, PM or QS, and whoever
                                     wrote it. Due 7 days after it was signed.
  Get DW-002 signed                  an unsigned sheet more than 2 days old: whoever wrote it. Due 7 days
                                     after the work.

It runs behind the badge on every page: three queries whatever the number of jobs, on top of the
shared actions.providers.my_jobs list.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, Item
from projects.access import is_internal

from . import rules

SEND_WITHIN_DAYS = 7
CHASE_SIGNATURE_AFTER_DAYS = 2


def _jobs(request):
    """(jobs, the ones this person runs) or None for externals and people with no jobs."""
    if request.membership is None or not is_internal(request):
        return None
    from actions.providers import my_jobs
    jobs = my_jobs(request)
    if not jobs:
        return None
    me = request.user.pk
    return jobs, {p.pk: p for p in jobs if p.manager_id == me or p.on_team_pm or p.on_team_qs}


def cvr_items(request):
    found = _jobs(request)
    if found is None:
        return []
    jobs, runs = found
    from .models import CVRPeriod
    from .services import settings_for
    admin = request.membership.is_admin
    today = timezone.localdate()
    slug = request.org.slug
    out = []
    settings_obj = settings_for(request.org)
    last_month = rules.add_months(today, -1)
    due = rules.due_on(last_month, settings_obj.cvr_due_day)
    signers = {p.pk: p for p in jobs if admin or (settings_obj.pm_can_sign_off and p.pk in runs)}
    periods = list(CVRPeriod.objects.filter(project_id__in=set(runs) | set(signers))
                   .filter(month=last_month) | CVRPeriod.objects.filter(project_id__in=signers,
                                                                        status=CVRPeriod.SUBMITTED))
    this_month = {p.project_id: p for p in periods if p.month == last_month}
    for pid, project in runs.items():
        started = project.start_date or timezone.localdate(project.created_at)
        if not project.has_ledger or rules.month_start(started) > last_month:
            continue
        period = this_month.get(pid)
        if period is not None and period.status != CVRPeriod.DRAFT:
            continue
        url = period.get_absolute_url() if period else reverse('cvr_list', args=[slug, pid])
        out.append(Item(kind='cvr', title=f'Prepare the CVR for {rules.month_label(last_month)}', url=url,
                        ref=period.reference if period else '', project=project, due=due,
                        status_label='Being prepared' if period else 'Not started', tone='warn',
                        action='Check the figures, add accruals and submit it for sign-off',
                        key=f'cvr_prepare:{pid}:{last_month}'))
    for period in periods:
        if period.status != CVRPeriod.SUBMITTED or period.project_id not in signers:
            continue
        out.append(Item(kind='cvr', title=f'Sign off the CVR for {period.label}', url=period.get_absolute_url(),
                        ref=period.reference, project=signers[period.project_id],
                        due=rules.due_on(period.month, settings_obj.cvr_due_day), priority=PRIORITY_HIGH,
                        status_label='Waiting for sign-off', tone='accent', action='Review and sign off',
                        key=f'cvr_signoff:{period.pk}'))
    return out


def daywork_items(request):
    found = _jobs(request)
    if found is None:
        return []
    jobs, runs = found
    from .models import DayworkSheet
    me, today, out = request.user.pk, timezone.localdate(), []
    job_ids = {p.pk: p for p in jobs}
    for sheet in DayworkSheet.objects.filter(project_id__in=job_ids, status__in=(DayworkSheet.DRAFT,
                                                                                 DayworkSheet.SIGNED)):
        project = job_ids[sheet.project_id]
        mine = sheet.created_by_id == me
        if sheet.status == DayworkSheet.SIGNED and (mine or sheet.project_id in runs):
            signed = timezone.localdate(sheet.signed_at) if sheet.signed_at else today
            out.append(Item(kind='daywork', title=f'Add {sheet.reference} to the change register',
                            url=sheet.get_absolute_url(), ref=sheet.reference, project=project,
                            due=signed + timedelta(days=SEND_WITHIN_DAYS), status_label='Signed', tone='accent',
                            action='Put it on a change event so it is valued', detail=sheet.title,
                            key=f'daywork_send:{sheet.pk}'))
        elif sheet.status == DayworkSheet.DRAFT and mine and \
                (today - sheet.work_date).days > CHASE_SIGNATURE_AFTER_DAYS:
            out.append(Item(kind='daywork', title=f'Get {sheet.reference} signed', url=sheet.get_absolute_url(),
                            ref=sheet.reference, project=project, due=sheet.work_date + timedelta(days=SEND_WITHIN_DAYS),
                            status_label='Not signed yet', tone='warn', detail=sheet.title,
                            action='Get the client side to sign it, or send it on unsigned',
                            key=f'daywork_sign:{sheet.pk}'))
    return out
