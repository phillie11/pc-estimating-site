"""
CVRs and daywork sheets for tenancy.scale (`manage.py seed_scale` and the query-count tests), at a
realistic volume: on every live job, the last three months' CVRs (the two oldest signed off, the
last one being prepared, each with accruals), and a handful of daywork sheets, one of them on the
change register. Saved through cvr.services so numbering, the record index and the audit are real.
"""
from decimal import Decimal as D

from django.utils import timezone

from core.workflow import apply_transition

from . import rules, services
from .models import DayworkLine, DayworkRate


def build(live, owner, sheets_per_job=6, say=print):
    today = timezone.localdate()
    months = [rules.add_months(today, -k) for k in (3, 2, 1)]
    periods = sheets = 0
    for p in live:
        actor = p.manager or owner
        for i, month in enumerate(months):
            try:
                period, _ = services.start_period(p, month, actor, today)
            except services.CVRError:
                continue
            services.add_adjustment(period, 'COST', D(1500 + 250 * i), 'Accrual: invoices not yet received', actor)
            periods += 1
            if i < 2:
                apply_transition(period, 'submit', actor, today=today)
                apply_transition(period, 'sign_off', owner)
        rate = DayworkRate.objects.create(project=p, kind='LABOUR', name='Joiner', rate=D('42.50'))
        for k in range(sheets_per_job):
            sheet = services.new_sheet(p, actor, title=f'Daywork {k + 1}', work_date=today - timezone.timedelta(days=k))
            DayworkLine.objects.create(sheet=sheet, kind='LABOUR', description='Joiner', quantity=D('4'),
                                       unit_rate=rate.rate, rate=rate, created_by=actor)
            DayworkLine.objects.create(sheet=sheet, kind='MATERIALS', description='Timber', quantity=D('2'),
                                       unit_rate=D('18.40'), created_by=actor)
            if k == 0:
                services.send_to_change(sheet, actor)
            sheets += 1
    say(f'{periods} monthly CVRs, {sheets} daywork sheets')
