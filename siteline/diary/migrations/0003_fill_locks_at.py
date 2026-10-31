"""
Fill locks_at and written_late for diary entries made before they were stored (data only; 0002 added
the columns). Uses the historical models and the rule as it stood when this was written, copied
here so later changes to diary/rules.py don't change what this migration does:

  an entry can be changed on its day and the next working day (not Sunday, not a bank holiday in the
  contract's region, not Saturday unless the company counts it); one written after that window
  gets the same window from the day it was written; one that is unlocked stays open to the end of
  its unlocked_until day if that is later.

Reverse does nothing: 0002's reverse drops the columns.
"""
import datetime

from django.db import migrations
from django.utils import timezone

ONE_DAY = datetime.timedelta(days=1)


def _next_working_day(day, saturday, region):
    from holidays.calendar import DEFAULT_REGION, is_excluded
    day += ONE_DAY
    while day.weekday() == 6 or (day.weekday() == 5 and not saturday) or is_excluded(day, region or DEFAULT_REGION):
        day += ONE_DAY
    return day


def _start_of(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def forwards(apps, schema_editor):
    DiaryEntry = apps.get_model('diary', 'DiaryEntry')
    DiarySettings = apps.get_model('diary', 'DiarySettings')
    MainContract = apps.get_model('contracts', 'MainContract')
    saturday = dict(DiarySettings.objects.values_list('organization_id', 'saturday_counts'))
    regions = dict(MainContract.objects.values_list('project_id', 'bank_holiday_region'))
    for e in DiaryEntry.objects.filter(locks_at__isnull=True).order_by('pk').iterator():
        sat, region = saturday.get(e.organization_id, False), regions.get(e.project_id)
        written = timezone.localdate(e.created_at)
        late = written > _next_working_day(e.entry_date, sat, region)
        until = _next_working_day(written if late else e.entry_date, sat, region)
        if e.status == 'UNLOCKED' and e.unlocked_until:
            until = max(until, e.unlocked_until)
        DiaryEntry.objects.filter(pk=e.pk).update(locks_at=_start_of(until + ONE_DAY), written_late=late)


class Migration(migrations.Migration):
    dependencies = [
        ('diary', '0002_entry_locks_at'),
        ('contracts', '0002_holidays_and_response_days'),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
