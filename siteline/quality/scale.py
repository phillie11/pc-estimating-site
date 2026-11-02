"""
Quality at a realistic volume for `manage.py seed_scale` and the query-count tests (tenancy/scale.py
calls seed() inside the company's org_context). Per live job, by default: 12 inspections from the
library with their checks answered, 8 permits (two still waiting, one expired and not closed) and
40 fire-stopping penetrations. Records are saved the normal way so numbering, the index and the
audit trail are real; the checks are bulk-inserted.
"""
import datetime

from django.utils import timezone

from . import rules
from .library import ensure_library
from .models import ChecklistTemplate, Inspection, InspectionItem, Penetration, Permit

STATES = (Penetration.TO_SEAL, Penetration.SEALED, Penetration.SIGNED_OFF, Penetration.FAILED)


def seed(org, owner, live, rng, inspections_per_job=12, permits_per_job=8, penetrations_per_job=40):
    ensure_library(org, owner)
    templates = list(ChecklistTemplate.objects.prefetch_related('items'))
    now = timezone.now()
    items = []
    for p in live:
        pm = p.manager or owner
        for k in range(inspections_per_job):
            t = templates[k % len(templates)]
            done = k % 4 != 0
            fail = done and k % 5 == 0
            ins = Inspection(project=p, template=t, title=f'{t.name}, Level {k % 6}', area=f'Level {k % 6}',
                             status=(Inspection.FAILED if fail else Inspection.PASSED) if done else Inspection.IN_PROGRESS,
                             ball_in_court_user=None if done else pm,
                             due_date=timezone.localdate() - datetime.timedelta(days=k))
            ins.save(actor=pm)
            for n, check in enumerate(t.items.all()):
                result = (rules.FAIL if fail and n == 0 else rules.PASS) if done else ('' if n % 2 else rules.PASS)
                items.append(InspectionItem(organization=org, inspection=ins, order=check.order, text=check.text,
                                            result=result))
        for k in range(permits_per_job):
            start = now - datetime.timedelta(days=permits_per_job - k, hours=rng.randrange(0, 4))
            status = rules.REQUESTED if k >= permits_per_job - 2 else (
                rules.APPROVED if k == permits_per_job - 3 else rules.CLOSED)
            kind = rules.PERMIT_TYPE_CHOICES[k % len(rules.PERMIT_TYPE_CHOICES)][0]
            Permit(project=p, permit_type=kind, title=f'Permit {k}', location=f'Level {k % 6}', starts_at=start,
                   expires_at=start + datetime.timedelta(hours=8), status=status,
                   ball_in_court_user=pm if status in Permit.OPEN_STATUSES else None,
                   approved_by=pm if status != rules.REQUESTED else None).save(actor=pm)
        for k in range(penetrations_per_job):
            pen = Penetration(project=p, location=f'Level {k % 6}, riser {k % 3}', element='WALL', fire_rating=60,
                              service='CABLE', size='Cable bundle', system='Intumescent sealant' if k % 2 else '',
                              status=STATES[k % len(STATES)],
                              ball_in_court_user=pm if STATES[k % len(STATES)] in Penetration.OPEN_STATUSES else None)
            pen.title = pen.default_title()
            pen.save(actor=pm)
    InspectionItem.objects.bulk_create(items, batch_size=2000)
