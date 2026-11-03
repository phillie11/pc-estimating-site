"""
Close-out at volume for `manage.py seed_scale` (tenancy.scale.build calls seed() inside the
company's org_context). On every live job: a dozen defects reported by its client, the O&M manual
set up with requests to two subcontractors, three certificates; every other job has reached
practical completion, and every fourth has a handover pack. Files are names only, as the rest of
the scale data: packs list them as missing and nothing is read.
"""
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone


def seed(org, live, say=lambda m: None):
    from projects.models import Issue, ProjectMember

    from . import services
    from .models import Certificate, CompletionSection, OMSection

    today = timezone.localdate()
    clients = dict(ProjectMember.objects.filter(project__in=live, role=ProjectMember.ROLE_CLIENT)
                   .values_list('project_id', 'user_id'))
    numbers = dict(Issue.objects.filter(project__in=live).values('project_id').annotate(m=Max('number'))
                   .values_list('project_id', 'm'))
    sections, issues, certs = [], [], []
    for j, p in enumerate(live):
        if j % 2 == 0:
            sections.append(CompletionSection(organization=org, project=p, is_whole_works=True,
                                              name=CompletionSection.WHOLE_WORKS,
                                              pc_date=today - timedelta(days=30 + j), defects_period_months=6))
        start = numbers.get(p.pk) or 0
        for k in range(12):
            issues.append(Issue(organization=org, project=p, number=start + k + 1, title=f'Defect {k}',
                                issue_type='DEFECT', location=f'Area {k % 4}', created_by_id=clients.get(p.pk),
                                assignee=p.manager if k % 3 == 0 else None,
                                status='CLOSED' if k % 4 == 0 else 'OPEN'))
        for k, kind in enumerate(('ELECTRICAL', 'FIRE_ALARM', 'EMERGENCY_LIGHTING')):
            certs.append(Certificate(organization=org, project=p, kind=kind, file=f'scale/cert-{p.pk}-{k}.pdf',
                                     original_name=f'cert-{k}.pdf', issued_on=today - timedelta(days=k)))
    CompletionSection.objects.bulk_create(sections)
    Issue.objects.bulk_create(issues, batch_size=2000)
    Certificate.objects.bulk_create(certs)
    for j, p in enumerate(live):
        services.setup_om(p, org)
        for sec in list(OMSection.objects.filter(project=p, package__isnull=False))[:2]:
            services.request_om(sec, p.manager, due_date=today + timedelta(days=10))
        if j % 4 == 0:
            services.build_pack(p, p.manager)
    say(f'close-out on {len(live)} live jobs')
