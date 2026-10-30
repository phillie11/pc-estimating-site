"""
Live-job portal for subcontractors: RAMS for review, and subcontractor variations.

Both are submitted by the subcontractor in the portal and decided by the
contractor's projects team. Each subcontractor only ever sees their own.
"""
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from projects.models import Project, _private_path, save_with_number
from tenancy.models import TenantModel


def upload_rams(instance, filename):
    return _private_path(instance, filename, 'rams')


def upload_subvariation(instance, filename):
    return _private_path(instance, filename, 'subvariations')


class Rams(TenantModel):
    """A risk assessment and method statement, one revision per row."""
    SUBMITTED, APPROVED, COMMENTS, REJECTED, SUPERSEDED = 'SUBMITTED', 'APPROVED', 'COMMENTS', 'REJECTED', 'SUPERSEDED'
    STATUS_CHOICES = [
        (SUBMITTED, 'Awaiting review'), (APPROVED, 'Approved'), (COMMENTS, 'Approved with comments'),
        (REJECTED, 'Revise and resubmit'), (SUPERSEDED, 'Superseded'),
    ]
    DECISIONS = [(APPROVED, 'Approve'), (COMMENTS, 'Approve with comments'), (REJECTED, 'Revise and resubmit')]
    ACCEPTED = (APPROVED, COMMENTS)

    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name='rams')
    subcontractor = models.ForeignKey('directory.Subcontractor', on_delete=models.RESTRICT, related_name='rams')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL, related_name='rams')
    number = models.PositiveIntegerField(editable=False)
    revision = models.PositiveSmallIntegerField(default=1)
    title = models.CharField(max_length=200)
    scope = models.CharField('Work it covers', max_length=300, blank=True)
    file = models.FileField(upload_to=upload_rams)
    original_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=SUBMITTED, db_index=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comments = models.TextField(blank=True)

    class Meta:
        ordering = ['-number', '-revision']
        unique_together = [('project', 'number', 'revision')]
        verbose_name = verbose_name_plural = 'RAMS'

    def __str__(self):
        return f'{self.reference} {self.title}'

    @property
    def reference(self):
        return f'RAMS-{self.number:03d} rev {self.revision}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)

    def get_absolute_url(self):
        return reverse('rams_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'SUBMITTED': 'accent', 'APPROVED': 'good', 'COMMENTS': 'good', 'REJECTED': 'bad', 'SUPERSEDED': 'muted'}[self.status]


class SubVariation(TenantModel):
    """A subcontractor's claim for changed or extra work, decided by the contractor."""
    SUBMITTED, APPROVED, REJECTED, WITHDRAWN = 'SUBMITTED', 'APPROVED', 'REJECTED', 'WITHDRAWN'
    STATUS_CHOICES = [(SUBMITTED, 'Awaiting decision'), (APPROVED, 'Approved'), (REJECTED, 'Rejected'),
                      (WITHDRAWN, 'Withdrawn')]
    REASON_CHOICES = [
        ('INSTRUCTION', 'Instructed by the contractor'), ('DESIGN', 'Design or drawing change'),
        ('SITE', 'Site condition'), ('DELAY', 'Delay or disruption'), ('OTHER', 'Other'),
    ]

    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name='sub_variations')
    subcontractor = models.ForeignKey('directory.Subcontractor', on_delete=models.RESTRICT, related_name='variations')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='variations')
    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField('What changed and why')
    reason = models.CharField(max_length=12, choices=REASON_CHOICES, default='INSTRUCTION')
    instructed_by = models.CharField('Instructed by / reference', max_length=200, blank=True,
                                     help_text='Who asked for it and when, e.g. site manager, 4 Nov.')
    value = models.DecimalField('Value claimed', max_digits=14, decimal_places=2)
    time_impact_days = models.IntegerField('Time impact (days)', null=True, blank=True)
    file = models.FileField('Supporting document', upload_to=upload_subvariation, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=SUBMITTED, db_index=True)
    approved_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    decision_notes = models.TextField(blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    client_variation = models.ForeignKey('variations.Variation', null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name='sub_variations', verbose_name='Recovered from client as')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        unique_together = [('project', 'number')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    @property
    def reference(self):
        return f'SV-{self.number:03d}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)

    def get_absolute_url(self):
        return reverse('subvariation_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'SUBMITTED': 'accent', 'APPROVED': 'good', 'REJECTED': 'bad', 'WITHDRAWN': 'muted'}[self.status]
