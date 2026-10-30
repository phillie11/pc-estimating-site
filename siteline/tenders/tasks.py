import jobs
from django.contrib.auth import get_user_model

from projects.models import Project


@jobs.task(name='tenders.chase_outstanding', max_attempts=3)
def chase_outstanding(payload):
    """Remind every subcontractor still to quote on a tender (tenders.coverage.run_chase). Safe to run twice."""
    project = Project.objects.filter(pk=payload['project_id']).first()  # runs inside the job's organisation
    if project is None or project.is_archived:
        return
    actor = get_user_model().objects.filter(pk=payload.get('actor_id')).first()
    from .coverage import run_chase
    run_chase(project, payload['package_ids'], actor=actor)
