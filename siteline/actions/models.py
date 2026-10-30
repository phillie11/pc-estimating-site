from django.conf import settings
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


class DigestPreference(models.Model):
    """
    How often a person wants the round-up email. Anyone without a row gets the default
    (daily). It belongs to the person, not to a company (like their login), so it isn't a
    TenantModel and it covers every workspace they're in, staff and external alike.
    """
    DAILY, WEEKLY, OFF = 'DAILY', 'WEEKLY', 'OFF'
    CHOICES = [(DAILY, 'Daily, only when something is overdue or due within 3 days'),
               (WEEKLY, 'Weekly, on Monday mornings'),
               (OFF, 'Never')]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='digest_preference')
    frequency = models.CharField('Round-up email', max_length=6, choices=CHOICES, default=DAILY)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'digest preference'

    def __str__(self):
        return f'{self.user} · {self.get_frequency_display()}'

    @classmethod
    def frequency_for(cls, user):
        row = cls.objects.filter(user=user).only('frequency').first()
        return row.frequency if row else cls.DAILY

    @classmethod
    def set_for(cls, user, frequency):
        if frequency not in dict(cls.CHOICES):
            raise ValueError(frequency)
        cls.objects.update_or_create(user=user, defaults={'frequency': frequency})
