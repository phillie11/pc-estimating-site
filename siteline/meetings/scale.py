"""
Programme lines and meetings at a realistic volume for tenancy/scale.py (`manage.py seed_scale` and
e2e/test_query_counts.py). Called inside the company's org_context.

Per live job: `lines_per_job` programme lines (a quarter of them given to appointed subcontractors,
so the subcontractor look-ahead has rows), and `meetings_per_job` site progress meetings, each with
three actions; all but the last are issued, and every other action is the PM's or a subcontractor's.
"""
from datetime import timedelta

from django.utils import timezone

from core.workflow import apply_transition
from projects.models import Activity

from . import services
from .models import CLIENT, PROGRESS, MeetingAction


def seed(org, live, appointed, sub_users, client_for, rng, lines_per_job=40, meetings_per_job=4):
    today = timezone.localdate()
    rows = []
    by_job = {}
    for pkg in appointed:
        by_job.setdefault(pkg.project_id, []).append(pkg)
    for p in live:
        mine = by_job.get(p.pk, [])
        for k in range(lines_per_job):
            start = today + timedelta(days=rng.randrange(-40, 80))
            pkg = mine[k % len(mine)] if mine and k % 4 == 0 else None
            rows.append(Activity(organization=org, project=p, name=f'Activity {k + 1}', start=start,
                                 finish=start + timedelta(days=rng.randrange(0, 20)), progress=rng.choice([0, 0, 25, 50, 100]),
                                 subcontractor=pkg.subcontractor if pkg else None, trade=pkg.trade if pkg else None,
                                 responsible=pkg.subcontractor.company_name if pkg else 'Our team', source_uid=k + 1,
                                 created_by=p.manager))
    Activity.objects.bulk_create(rows, batch_size=2000)

    for p in live:
        mine = by_job.get(p.pk, [])
        sub_user = sub_users.get(mine[0].subcontractor.email) if mine else None
        client = client_for.get(p.pk)
        for k in range(meetings_per_job):
            kind = CLIENT if k == 1 else PROGRESS
            people = [p.manager] + ([client] if kind == CLIENT and client else []) + ([sub_user] if sub_user else [])
            m = services.create(p, p.manager, kind=kind, held_on=today - timedelta(days=7 * (meetings_per_job - k)),
                                attendees=[u for u in people if u is not None])
            for n in range(3):
                owner = [p.manager, sub_user, client if kind == CLIENT else p.manager][n] or p.manager
                MeetingAction(meeting=m, project=p, title=f'Action {n + 1} from {m.ref}', owner=owner,
                              due_date=m.held_on + timedelta(days=rng.randrange(3, 21)),
                              status=MeetingAction.DONE if rng.random() < 0.4 else MeetingAction.OPEN
                              ).save(actor=p.manager)
            if k < meetings_per_job - 1:
                apply_transition(m, 'issue', p.manager)
                services.hand_out_actions(m, p.manager)
