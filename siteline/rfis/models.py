from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import RecordBase
from projects.models import PRIORITY_CHOICES, upload_rfi


class RFI(RecordBase):
    """
    A request for information. The first record type on core.models.RecordBase: number/ref,
    title (shown as "Subject"), status, visibility, due date, ball in court, created/updated by,
    soft delete and audit all come from there. Status changes go through rfis.workflow.
    """
    CATEGORY_CHOICES = [
        ('DESIGN', 'Design'), ('TECHNICAL', 'Technical'), ('SPECIFICATION', 'Specification'),
        ('DRAWING', 'Drawing'), ('COMMERCIAL', 'Commercial'), ('PROGRAMME', 'Programme'),
        ('SITE', 'Site / access'), ('HEALTH_SAFETY', 'Health & safety'), ('OTHER', 'Other'),
    ]
    STATUS_DRAFT = 'DRAFT'
    STATUS_OPEN = 'OPEN'
    STATUS_RESPONDED = 'RESPONDED'
    STATUS_CLOSED = 'CLOSED'
    STATUS_VOID = 'VOID'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'), (STATUS_OPEN, 'Open'), (STATUS_RESPONDED, 'Responded'),
        (STATUS_CLOSED, 'Closed'), (STATUS_VOID, 'Void'),
    ]
    LIVE_STATUSES = (STATUS_OPEN, STATUS_RESPONDED)
    IMPACT_CHOICES = [('NONE', 'None'), ('POSSIBLE', 'Possible'), ('YES', 'Yes'), ('TBC', 'TBC')]

    question = models.TextField()
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES, default='DESIGN')
    priority = models.CharField(max_length=7, choices=PRIORITY_CHOICES, default='NORMAL')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    document_reference = models.CharField('Drawing / spec ref', max_length=200, blank=True)
    attachment = models.FileField(upload_to=upload_rfi, blank=True)

    visibility = models.CharField(max_length=8, choices=RecordBase.VISIBILITY_CHOICES, default=RecordBase.VIS_SHARED)
    due_date = models.DateField('Response required by', null=True, blank=True)

    response = models.TextField(blank=True)
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    responded_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    cost_impact = models.CharField(max_length=8, choices=IMPACT_CHOICES, default='TBC')
    programme_impact_days = models.IntegerField('Programme impact (days)', null=True, blank=True)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'RFI'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('rfi_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def is_overdue(self):
        return self.status == self.STATUS_OPEN and self.due_date and self.due_date < timezone.localdate()

    @property
    def days_open(self):
        end = self.responded_at or timezone.now()
        return (end - self.created_at).days

    @property
    def status_tone(self):
        return {'DRAFT': 'muted', 'OPEN': 'accent', 'RESPONDED': 'info', 'CLOSED': 'good', 'VOID': 'muted'}[self.status]
