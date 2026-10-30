from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_SHARED, RecordBase
from projects.models import upload_variation


class Variation(RecordBase):
    """
    A client variation (VO-001), priced by us and approved or rejected by the client in their
    portal. On core.models.RecordBase (docs/adr/0001, migration plan step 1): number/ref, title,
    status, visibility, ball in court, created/updated by and audit come from there; status
    changes go through variations.workflow.

    Drafts are internal. Submitting shares it with the project's clients and consultants and
    passes the ball to the client. `rfi` stays for now beside a RecordLink(DERIVED_FROM) to
    the same RFI, until every reader uses links.
    """
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
    # Once it has been in front of the client it stays shared (a withdrawn draft stays internal)
    SHARED_STATUSES = (STATUS_SUBMITTED, STATUS_APPROVED, STATUS_REJECTED)

    description = models.TextField(help_text='What changed and why.')
    cause = models.CharField(max_length=20, choices=CAUSE_CHOICES, default='CLIENT_INSTRUCTION')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    value = models.DecimalField('Quoted value', max_digits=14, decimal_places=2, null=True, blank=True)
    approved_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    time_impact_days = models.IntegerField('Time impact (days)', null=True, blank=True)
    eot_days_requested = models.PositiveIntegerField('Extension of time requested (days)', null=True, blank=True)
    eot_days_granted = models.PositiveIntegerField('Extension of time granted (days)', null=True, blank=True)
    quotation_due = models.DateField('Quotation due', null=True, blank=True,
                                     help_text='When the client needs our quotation by.')
    proceed_at_risk = models.BooleanField(default=False, help_text='Work proceeding before approval.')
    client_reference = models.CharField('Client instruction ref', max_length=120, blank=True)
    rfi = models.ForeignKey('rfis.RFI', null=True, blank=True, on_delete=models.SET_NULL, related_name='variations',
                            verbose_name='Related RFI')
    attachment = models.FileField(upload_to=upload_variation, blank=True)

    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decision_notes = models.TextField(blank=True)
    # Set when an owner or admin recorded the client's decision for them, with the client's email or letter as proof
    decision_recorded_on_behalf = models.BooleanField('Recorded on behalf of the client', default=False)
    decision_evidence = models.FileField("Client's decision (evidence)", upload_to=upload_variation, blank=True)

    class Meta(RecordBase.Meta):
        ordering = ['-number']

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        if self.status in self.SHARED_STATUSES:
            self.visibility = VIS_SHARED
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('variation_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'DRAFT': 'muted', 'SUBMITTED': 'accent', 'APPROVED': 'good', 'REJECTED': 'bad', 'WITHDRAWN': 'muted'}[self.status]

    @property
    def is_overdue(self):
        """Waiting on the client past its respond-by date (RecordBase.due_date, set on submit)."""
        return self.status == self.STATUS_SUBMITTED and self.due_date is not None and self.due_date < timezone.localdate()

    @property
    def client_can_see(self):
        return self.is_shared
