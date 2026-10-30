from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class Job(models.Model):
    """
    One unit of background work. Not a TenantModel: the worker lists every company's
    jobs. `organization` is the company the task runs for (inside org_context);
    see jobs/__init__.py.
    """
    QUEUED, RUNNING, DONE, FAILED, DEAD = 'queued', 'running', 'done', 'failed', 'dead'
    STATUS_CHOICES = [(QUEUED, 'Queued'), (RUNNING, 'Running'), (DONE, 'Done'),
                      (FAILED, 'Failed, will retry'), (DEAD, 'Dead (gave up)')]
    READY = (QUEUED, FAILED)  # what a worker may claim once run_after has passed

    name = models.CharField(max_length=100, help_text='The registered task name.')
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    organization = models.ForeignKey('tenancy.Organization', null=True, blank=True, on_delete=models.CASCADE,
                                     related_name='+')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True)
    last_error = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # queryset.update() calls set it explicitly
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'run_after'], name='jobs_job_due')]

    def __str__(self):
        return f'{self.name} #{self.pk} ({self.status})'


class OutboxEmail(models.Model):
    """
    An email written during a request and sent by the worker (task `email.send`).
    The body is rendered when it is queued, so the worker needs no templates or context.
    """
    QUEUED, SENT, FAILED = 'queued', 'sent', 'failed'
    STATUS_CHOICES = [(QUEUED, 'Queued'), (SENT, 'Sent'), (FAILED, 'Failed (gave up)')]

    organization = models.ForeignKey('tenancy.Organization', null=True, blank=True, on_delete=models.CASCADE,
                                     related_name='+')
    to = models.JSONField(default=list)
    reply_to = models.JSONField(default=list, blank=True)
    from_email = models.CharField(max_length=254)
    subject = models.CharField(max_length=300)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'created_at'], name='jobs_outbox_status')]

    def __str__(self):
        return f'{self.subject} → {", ".join(self.to)}'

    @property
    def job_key(self):
        return f'email:{self.pk}'
