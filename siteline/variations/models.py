from django.conf import settings
from django.db import models
from django.urls import reverse

from projects.models import Project, save_with_number, upload_variation
from tenancy.models import TenantModel


class Variation(TenantModel):
    CAUSE_CHOICES = [
        ('CLIENT_INSTRUCTION', 'Client instruction'), ('DESIGN_CHANGE', 'Design change'),
        ('SITE_CONDITION', 'Unforeseen site condition'), ('SPEC_CHANGE', 'Specification change'),
        ('OMISSION', 'Omission / clarification'), ('ACCELERATION', 'Programme acceleration'),
        ('OTHER', 'Other'),
    ]
    STATUS_DRAFT = 'DRAFT'
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_APPROVED = 'APPROVED'
    STATUS_REJECTED = 'REJECTED'
    STATUS_WITHDRAWN = 'WITHDRAWN'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'), (STATUS_SUBMITTED, 'Submitted to client'), (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'), (STATUS_WITHDRAWN, 'Withdrawn'),
    ]

    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name='variations')
    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(help_text='What changed and why.')
    cause = models.CharField(max_length=20, choices=CAUSE_CHOICES, default='CLIENT_INSTRUCTION')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    value = models.DecimalField('Quoted value', max_digits=14, decimal_places=2, null=True, blank=True)
    approved_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    time_impact_days = models.IntegerField('Time impact (days)', null=True, blank=True)
    proceed_at_risk = models.BooleanField(default=False, help_text='Work proceeding before approval.')
    client_reference = models.CharField('Client instruction ref', max_length=120, blank=True)
    rfi = models.ForeignKey('rfis.RFI', null=True, blank=True, on_delete=models.SET_NULL, related_name='variations',
                            verbose_name='Related RFI')
    attachment = models.FileField(upload_to=upload_variation, blank=True)

    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decision_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-number']
        unique_together = [('project', 'number')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    @property
    def reference(self):
        return f'VO-{self.number:03d}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)

    def get_absolute_url(self):
        return reverse('variation_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'DRAFT': 'muted', 'SUBMITTED': 'accent', 'APPROVED': 'good', 'REJECTED': 'bad', 'WITHDRAWN': 'muted'}[self.status]

    @property
    def client_can_see(self):
        return self.status != self.STATUS_DRAFT
