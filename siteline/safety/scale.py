"""
Safety rows for tenancy/scale.py (`manage.py seed_scale` and e2e/test_query_counts.py), inside the
company's org_context: per live job, incidents (a few with RIDDOR and open actions), a CDM register
with duty holders, and site sign-ins (today's crew plus a fortnight of history).
"""
import datetime

from django.utils import timezone

from . import rules
from .models import CDMProfile, DutyHolder, Incident, IncidentAction, SiteAttendance, SiteGate


def seed(live, today, now, incidents_per_job=10, signins_per_job=30):
    attendance = []
    for p in live:
        SiteGate.for_project(p)
        CDMProfile.objects.create(project=p, notifiable='YES', f10_submitted_on=today - datetime.timedelta(days=60))
        DutyHolder.objects.bulk_create([DutyHolder(project=p, role=role, company=f'{role} company')
                                        for role in DutyHolder.KEY_ROLES])
        for k in range(incidents_per_job):
            when = now - datetime.timedelta(days=k * 3 + 1)
            i = Incident(project=p, description=f'Near miss {k} by the hoist', location='Hoist', occurred_at=when,
                         ball_in_court_user=p.manager, anyone_hurt=k % 5 == 0,
                         riddor_category=rules.RIDDOR_OVER_7 if k % 7 == 0 else '',
                         due_date=rules.investigation_due(timezone.localdate(when)))
            i.save(actor=p.manager)
            if k % 2 == 0:
                IncidentAction.objects.create(incident=i, description='Toolbox talk', owner=p.manager,
                                              due_date=today + datetime.timedelta(days=k - 5))
        for k in range(signins_per_job):
            day_back = 0 if k < signins_per_job // 3 else k % 14 + 1
            start = now - datetime.timedelta(days=day_back, minutes=k)
            attendance.append(SiteAttendance(
                project=p, name=f'Operative {k}', company=f'Sub {k % 6}', signed_in_at=start,
                signed_out_at=None if day_back == 0 else start + datetime.timedelta(hours=8)))
    SiteAttendance.objects.bulk_create(attendance, batch_size=2000)
    return f'{Incident.objects.count()} incidents, {SiteAttendance.objects.count()} site sign-ins'
