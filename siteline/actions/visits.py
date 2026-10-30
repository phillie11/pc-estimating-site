"""'What's changed since your last visit' for external collaborators."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

# Pages closer together than this are one visit
VISIT_GAP = timedelta(minutes=30)
# Don't write on every page of a visit; moving last_seen once a minute is plenty
TOUCH_EVERY = timedelta(minutes=1)


def touch(request, project):
    """
    Record that this external member opened the project. Returns the time "since your last
    visit" counts from (None on a first visit). Staff aren't tracked.
    """
    from projects.access import is_internal

    from .models import ProjectVisit
    pm = getattr(request, 'project_member', None)
    if pm is None or is_internal(request) or getattr(pm, 'project_id', None) != project.pk:
        return None
    now = timezone.now()
    visit = ProjectVisit.objects.filter(member=pm).first()
    if visit is None:
        try:
            with transaction.atomic():
                ProjectVisit.objects.create(member=pm, last_seen=now, organization_id=pm.organization_id)
        except IntegrityError:  # two tabs opened at once
            pass
        return None
    if now - visit.last_seen > VISIT_GAP:
        visit.previous_seen, visit.last_seen = visit.last_seen, now
        visit.save(update_fields=['previous_seen', 'last_seen'])
    elif now - visit.last_seen > TOUCH_EVERY:
        visit.last_seen = now
        visit.save(update_fields=['last_seen'])
    return visit.previous_seen


def changes_since(request, project, since, limit=8):
    """What other people did on the project since `since` that this person may see (the portal feed's rule)."""
    from tenancy.models import ActivityEvent
    if since is None:
        return []
    return list(ActivityEvent.objects.filter(project=project, internal_only=False, created_at__gt=since)
                .exclude(actor=request.user).select_related('actor')[:limit])


def unseen_counts(request, projects):
    """{project_id: n} of updates since each project's last visit, for the portal dashboard."""
    from django.db.models import Count, F, OuterRef, Subquery

    from tenancy.models import ActivityEvent

    from .models import ProjectVisit
    seen = ProjectVisit.objects.filter(member__project=OuterRef('project'), member__user=request.user).values('last_seen')[:1]
    rows = ActivityEvent.objects.filter(project__in=projects, internal_only=False) \
        .exclude(actor=request.user).annotate(seen=Subquery(seen)).filter(seen__isnull=False, created_at__gt=F('seen')) \
        .values('project').annotate(n=Count('pk')).order_by()
    return {r['project']: r['n'] for r in rows}
