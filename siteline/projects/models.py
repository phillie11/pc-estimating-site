import os
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from tenancy.models import TenantModel


def _private_path(instance, filename, folder):
    """Files live under org_<id>/<folder>/<random>/<name>, never at a guessable path."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/{folder}/{uuid.uuid4().hex}/{name}'


# Named module-level callables so migrations can reference them
def upload_document(instance, filename):
    return _private_path(instance, filename, 'documents')


def upload_issue(instance, filename):
    return _private_path(instance, filename, 'issues')


def upload_rfi(instance, filename):
    return _private_path(instance, filename, 'rfis')


def upload_variation(instance, filename):
    return _private_path(instance, filename, 'variations')


def save_with_number(instance, save_fn, *args, **kwargs):
    """
    Give a per-project register row (RFI-001, VO-001, IS-001) its next number.
    The project row is locked for the duration so two people saving at once
    cannot get the same number.
    """
    if instance.number:
        return save_fn(*args, **kwargs)
    model = type(instance)
    with transaction.atomic():
        Project.all_objects.select_for_update().filter(pk=instance.project_id).first()
        current = model.all_objects.filter(project_id=instance.project_id).aggregate(m=Max('number'))['m'] or 0
        instance.number = current + 1
        return save_fn(*args, **kwargs)


class Project(TenantModel):
    STAGE_LEAD = 'LEAD'
    STAGE_TENDERING = 'TENDERING'
    STAGE_SUBMITTED = 'SUBMITTED'
    STAGE_WON = 'WON'
    STAGE_LIVE = 'LIVE'
    STAGE_COMPLETE = 'COMPLETE'
    STAGE_LOST = 'LOST'
    STAGE_DECLINED = 'DECLINED'
    STAGE_CHOICES = [
        (STAGE_LEAD, 'Lead'),
        (STAGE_TENDERING, 'Tendering'),
        (STAGE_SUBMITTED, 'Submitted'),
        (STAGE_WON, 'Won – awaiting start'),
        (STAGE_LIVE, 'Live on site'),
        (STAGE_COMPLETE, 'Complete'),
        (STAGE_LOST, 'Lost'),
        (STAGE_DECLINED, 'Declined to bid'),
    ]
    PIPELINE_STAGES = (STAGE_LEAD, STAGE_TENDERING, STAGE_SUBMITTED)
    DELIVERY_STAGES = (STAGE_WON, STAGE_LIVE)
    CLOSED_STAGES = (STAGE_COMPLETE, STAGE_LOST, STAGE_DECLINED)
    BOARD_STAGES = (STAGE_LEAD, STAGE_TENDERING, STAGE_SUBMITTED, STAGE_WON)
    WON_STAGES = (STAGE_WON, STAGE_LIVE, STAGE_COMPLETE)
    DECIDED_STAGES = (STAGE_WON, STAGE_LIVE, STAGE_COMPLETE, STAGE_LOST, STAGE_DECLINED)

    SECTOR_CHOICES = [
        ('RETAIL', 'Retail'),
        ('HOSPITALITY', 'Hospitality & leisure'),
        ('OFFICE', 'Office'),
        ('AUTOMOTIVE', 'Automotive'),
        ('EDUCATION', 'Education'),
        ('HEALTHCARE', 'Healthcare'),
        ('RESIDENTIAL', 'Residential'),
        ('INDUSTRIAL', 'Industrial'),
        ('PUBLIC', 'Public sector'),
        ('OTHER', 'Other'),
    ]

    number = models.PositiveIntegerField(editable=False)
    name = models.CharField(max_length=200)
    client_name = models.CharField('Client', max_length=200, blank=True)
    site_address = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    stage = models.CharField(max_length=12, choices=STAGE_CHOICES, default=STAGE_LEAD, db_index=True)
    sector = models.CharField(max_length=16, choices=SECTOR_CHOICES, blank=True)

    value = models.DecimalField('Value', max_digits=14, decimal_places=2, null=True, blank=True,
                                help_text='Tender value while bidding, contract sum once won.')
    probability = models.PositiveSmallIntegerField('Win probability %', null=True, blank=True)
    tender_due = models.DateTimeField('Tender return', null=True, blank=True)
    start_date = models.DateField('Start on site', null=True, blank=True)
    completion_date = models.DateField('Completion', null=True, blank=True)
    lead_source = models.CharField(max_length=120, blank=True)
    outcome_notes = models.TextField('Outcome / feedback', blank=True)
    decided_at = models.DateField('Decision date', null=True, blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='owned_projects', verbose_name='Project lead')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = [('organization', 'number')]
        indexes = [models.Index(fields=['organization', 'stage'])]

    def __str__(self):
        return f'{self.reference} {self.name}'

    @property
    def reference(self):
        return f'P-{self.number:04d}' if self.number else 'P-new'

    def save(self, *args, **kwargs):
        if not self.number:
            from tenancy.context import get_current_org
            org = self.organization if self.organization_id else get_current_org()
            self.number = org.next_project_number()
        if self.stage in self.DECIDED_STAGES and not self.decided_at:
            self.decided_at = timezone.localdate()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('project_detail', args=[self.organization.slug, self.pk])

    @property
    def is_pipeline(self):
        return self.stage in self.PIPELINE_STAGES

    @property
    def stage_tone(self):
        return {
            'LEAD': 'muted', 'TENDERING': 'info', 'SUBMITTED': 'accent', 'WON': 'good',
            'LIVE': 'good', 'COMPLETE': 'muted', 'LOST': 'bad', 'DECLINED': 'muted',
        }.get(self.stage, 'muted')

    @property
    def weighted_value(self):
        if self.value is None or self.probability is None:
            return None
        return self.value * self.probability / 100

    @property
    def tender_overdue(self):
        return self.is_pipeline and self.stage != self.STAGE_SUBMITTED and self.tender_due and self.tender_due < timezone.now()


class ProjectMember(TenantModel):
    ROLE_LEAD = 'LEAD'
    ROLE_PM = 'PM'
    ROLE_SITE = 'SITE'
    ROLE_QS = 'QS'
    ROLE_ESTIMATOR = 'ESTIMATOR'
    ROLE_SUBCONTRACTOR = 'SUBCONTRACTOR'
    ROLE_CLIENT = 'CLIENT'
    ROLE_CONSULTANT = 'CONSULTANT'
    INTERNAL_ROLE_CHOICES = [
        (ROLE_PM, 'Project manager'),
        (ROLE_SITE, 'Site manager'),
        (ROLE_QS, 'Quantity surveyor'),
        (ROLE_ESTIMATOR, 'Estimator'),
    ]
    EXTERNAL_ROLE_CHOICES = [
        (ROLE_CLIENT, 'Client'),
        (ROLE_CONSULTANT, 'Consultant / designer'),
        (ROLE_SUBCONTRACTOR, 'Subcontractor'),
    ]
    ROLE_CHOICES = INTERNAL_ROLE_CHOICES + EXTERNAL_ROLE_CHOICES
    EXTERNAL_ROLES = (ROLE_CLIENT, ROLE_CONSULTANT, ROLE_SUBCONTRACTOR)

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='project_memberships')
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    company_name = models.CharField(max_length=200, blank=True)
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='project_members')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('project', 'user')]
        ordering = ['role', 'user__full_name']

    def __str__(self):
        return f'{self.user} on {self.project} ({self.get_role_display()})'

    @property
    def is_external(self):
        return self.role in self.EXTERNAL_ROLES

    @property
    def is_client(self):
        return self.role == self.ROLE_CLIENT


def _token():
    return secrets.token_urlsafe(32)


def _expiry():
    return timezone.now() + timedelta(days=21)


class ProjectInvite(TenantModel):
    """Invite an external collaborator into one project's portal."""
    STATUS_PENDING = 'PENDING'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_REVOKED = 'REVOKED'
    STATUS_CHOICES = [(STATUS_PENDING, 'Pending'), (STATUS_ACCEPTED, 'Accepted'), (STATUS_REVOKED, 'Revoked')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='invites')
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=ProjectMember.EXTERNAL_ROLE_CHOICES)
    company_name = models.CharField(max_length=200, blank=True)
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, blank=True, on_delete=models.SET_NULL)
    message = models.TextField(blank=True)
    token = models.CharField(max_length=64, unique=True, default=_token, editable=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    expires_at = models.DateTimeField(default=_expiry)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def is_valid(self):
        return self.status == self.STATUS_PENDING and self.expires_at > timezone.now()


PRIORITY_CHOICES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent')]


class Task(TenantModel):
    STATUS_TODO = 'TODO'
    STATUS_DOING = 'DOING'
    STATUS_DONE = 'DONE'
    STATUS_CHOICES = [(STATUS_TODO, 'To do'), (STATUS_DOING, 'In progress'), (STATUS_DONE, 'Done')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='assigned_tasks')
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=STATUS_CHOICES, default=STATUS_TODO)
    priority = models.CharField(max_length=7, choices=PRIORITY_CHOICES, default='NORMAL')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['status', 'due_date', '-created_at']

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        return self.status != self.STATUS_DONE and self.due_date and self.due_date < timezone.localdate()


class Issue(TenantModel):
    """Snags, defects and site observations."""
    TYPE_CHOICES = [
        ('SNAG', 'Snag'), ('DEFECT', 'Defect'), ('SAFETY', 'Health & safety'),
        ('QUALITY', 'Quality'), ('OTHER', 'Other'),
    ]
    STATUS_OPEN = 'OPEN'
    STATUS_READY = 'READY'
    STATUS_CLOSED = 'CLOSED'
    STATUS_CHOICES = [(STATUS_OPEN, 'Open'), (STATUS_READY, 'Ready for inspection'), (STATUS_CLOSED, 'Closed')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='issues')
    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    issue_type = models.CharField('Type', max_length=8, choices=TYPE_CHOICES, default='SNAG')
    location = models.CharField(max_length=200, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='assigned_issues')
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=STATUS_CHOICES, default=STATUS_OPEN)
    priority = models.CharField(max_length=7, choices=PRIORITY_CHOICES, default='NORMAL')
    photo = models.FileField(upload_to=upload_issue, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['status', '-created_at']
        unique_together = [('project', 'number')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    @property
    def reference(self):
        return f'IS-{self.number:03d}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)


class Document(TenantModel):
    CATEGORY_CHOICES = [
        ('DRAWING', 'Drawing'), ('SPEC', 'Specification'), ('TENDER', 'Tender document'),
        ('CONTRACT', 'Contract'), ('RAMS', 'RAMS / H&S'), ('REPORT', 'Report'),
        ('PHOTO', 'Photo'), ('OTHER', 'Other'),
    ]
    VIS_INTERNAL = 'INTERNAL'
    VIS_SHARED = 'SHARED'
    VISIBILITY_CHOICES = [(VIS_INTERNAL, 'Internal only'), (VIS_SHARED, 'Shared with project collaborators')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=200)
    reference = models.CharField('Doc / drawing no.', max_length=80, blank=True)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='DRAWING')
    visibility = models.CharField(max_length=8, choices=VISIBILITY_CHOICES, default=VIS_INTERNAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'reference', 'title']

    def __str__(self):
        return self.title

    @property
    def current(self):
        return self.revisions.order_by('-uploaded_at').first()


class DocumentRevision(TenantModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='revisions')
    revision = models.CharField(max_length=20, default='P01')
    file = models.FileField(upload_to=upload_document)
    original_name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)
    notes = models.CharField(max_length=300, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.document} rev {self.revision}'


class Comment(TenantModel):
    """Threaded discussion on RFIs, issues, variations and tasks."""
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey('content_type', 'object_id')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    body = models.TextField()
    internal_only = models.BooleanField(default=False, help_text='Hidden from external collaborators')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['content_type', 'object_id'])]
