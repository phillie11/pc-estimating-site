from django.db import models

from tenancy.models import TenantModel


class ProjectVisit(TenantModel):
    """
    When an external collaborator last looked at a project, for "What's changed since your
    last visit". One row per ProjectMember, kept here rather than as a column on ProjectMember
    so the portal can change without touching the projects tables.

    `last_seen` moves on every project page. When a new visit starts (more than VISIT_GAP
    after the previous page), the old value moves to `previous_seen`, which is what "since
    your last visit" counts from.
    """
    member = models.OneToOneField('projects.ProjectMember', on_delete=models.CASCADE, related_name='visit')
    last_seen = models.DateTimeField()
    previous_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'project visit'

    def __str__(self):
        return f'{self.member} last seen {self.last_seen:%Y-%m-%d %H:%M}'
