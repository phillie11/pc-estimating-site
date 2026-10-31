"""
"Needs me" items for the site diary (registered as a HookProvider in DiaryConfig.ready()).

    items(request) -> [actions.Item]

A project manager gets one item for each working day, among the last five, whose diary entry is
due (10:00 the next working day) but hasn't been written, on each live job they manage. Weekends
don't count unless the company says Saturday does (DiarySettings); bank holidays never count.
The rules are pure functions in diary/rules.py. Staff only: externals always get [].
"""
from django.urls import reverse
from django.utils import timezone

from actions.registry import PRIORITY_HIGH, Item
from projects.access import is_internal
from projects.models import Project

from . import rules
from .models import DiaryEntry, DiarySettings


def _region(project_ids):
    try:
        from contracts.models import MainContract
    except ImportError:  # pragma: no cover
        return {}
    return dict(MainContract.objects.filter(project_id__in=project_ids).values_list('project_id',
                                                                                    'bank_holiday_region'))


def missing_days(project, now, saturday_counts, region=None):
    """The due-but-missing working days for one job, most recent first."""
    expected = rules.expected_days(now, saturday_counts, region, start=project.start_date)
    if not expected:
        return []
    have = set(DiaryEntry.objects.filter(project=project, entry_date__in=expected)
               .values_list('entry_date', flat=True))
    return [d for d in expected if d not in have]


def items(request, now=None):
    if request.membership is None or not is_internal(request):
        return []
    now = now or timezone.localtime().replace(tzinfo=None)
    from actions.providers import my_jobs  # one shared query behind the badge
    projects = [p for p in my_jobs(request) if p.stage == Project.STAGE_LIVE and p.manager_id == request.user.pk]
    if not projects:
        return []
    saturday = DiarySettings.for_org(request.org).saturday_counts
    regions = _region([p.pk for p in projects])
    # The expected days for every job first, then which of them have entries, in one query
    expected = {p.pk: rules.expected_days(now, saturday, regions.get(p.pk), start=p.start_date)
                for p in projects if p.start_date is not None}  # no start on site date: nothing is chased
    days = {d for ds in expected.values() for d in ds}
    have = set(DiaryEntry.objects.filter(project_id__in=[pk for pk, ds in expected.items() if ds], entry_date__in=days)
               .values_list('project_id', 'entry_date')) if days else set()
    out = []
    for project in projects:
        if project.pk not in expected:
            continue
        for day in [d for d in expected[project.pk] if (project.pk, d) not in have]:
            due = rules.next_working_day(day, saturday, regions.get(project.pk))
            out.append(Item(
                kind='diary_missing', ref='Diary', title=f'Site diary missing for {day:%a} {day.day} {day:%b}',
                url=reverse('diary_day', args=[request.org.slug, project.pk, day.isoformat()]),
                project=project, due=due, status_label='Not written', tone='warn',
                priority=PRIORITY_HIGH if (now.date() - day).days > 3 else 'normal',
                action='Write up the site diary for that day', kind_label='Site diary',
                key=f'diary:{project.pk}:{day.isoformat()}'))
    return out
