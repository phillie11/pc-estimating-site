import os
import re
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
    Give a per-project register row (VO-001, IS-001) its next number.
    The project row is locked for the duration so two people saving at once
    cannot get the same number.

    Only for registers not yet on core.models.RecordBase, which numbers from a
    NumberSequence row instead (RFIs so far). Each moves over as it adopts it.
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
    # Estimating owns bids until they are won; the projects (delivery) team owns them after
    ESTIMATING_STAGES = (STAGE_LEAD, STAGE_TENDERING, STAGE_SUBMITTED, STAGE_LOST, STAGE_DECLINED)
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
    tender_received = models.DateField('Enquiry received', null=True, blank=True,
                                       help_text='When the tender landed. Starts its bar on the workload chart.')
    site_visit = models.DateField('Site visit', null=True, blank=True)
    queries_close = models.DateField('Tender queries close', null=True, blank=True)
    quotes_due = models.DateField('Subcontractor quotes due', null=True, blank=True)
    programme_due = models.DateField('Tender programme due', null=True, blank=True)
    quality_due = models.DateField('Quality submission due', null=True, blank=True)
    logistics_due = models.DateField('Logistics plan due', null=True, blank=True)
    start_date = models.DateField('Start on site', null=True, blank=True)
    completion_date = models.DateField('Completion', null=True, blank=True)
    lead_source = models.CharField(max_length=120, blank=True)
    outcome_notes = models.TextField('Outcome / feedback', blank=True)
    decided_at = models.DateField('Decision date', null=True, blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='owned_projects', verbose_name='Bid lead')
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='managed_projects', verbose_name='Project manager')
    handover_notes = models.TextField(blank=True, help_text='What the projects team needs to know: qualifications, '
                                                            'risks, client expectations, key subcontractor quotes.')
    handed_over_at = models.DateTimeField(null=True, blank=True)
    handed_over_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Archived instead of deleted once it holds records: hidden from lists, the pipeline, workload and plan
    # limits, but kept (with its audit trail and download log) for owners and admins
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')

    # Related records that mean a project must be archived, not deleted
    RECORD_RELATIONS = ('rfis', 'variations', 'rams', 'sub_variations', 'packages', 'documents', 'file_access',
                        'tasks', 'issues', 'activities', 'addenda', 'tender_queries', 'record_index',
                        # Cost control (costs/): the append-only ledger and the records behind it
                        'cost_ledger', 'budget_changes', 'budget_lines', 'commitments', 'commitment_adjustments',
                        'actual_costs', 'cost_forecasts')

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
    def is_archived(self):
        return self.archived_at is not None

    def has_records(self):
        """
        Anything worth keeping hangs off it. Portal access, the activity feed and notifications don't count.
        Soft-deleted records (core.models.RecordBase) count too: they are still rows, kept for the audit trail.
        """
        def rows(name):
            related = getattr(self, name)
            return related(manager='with_deleted') if hasattr(related.model, 'with_deleted') else related
        return any(rows(name).exists() for name in self.RECORD_RELATIONS)

    def archive(self, user):
        self.archived_at, self.archived_by = timezone.now(), user
        self.save(update_fields=['archived_at', 'archived_by', 'updated_at'])

    def restore(self):
        self.archived_at, self.archived_by = None, None
        self.save(update_fields=['archived_at', 'archived_by', 'updated_at'])

    @classmethod
    def active_count(cls, org):
        """Projects counted against the plan limit: open and not archived."""
        return cls.all_objects.filter(organization=org, archived_at__isnull=True) \
            .exclude(stage__in=cls.CLOSED_STAGES).count()

    @classmethod
    def plan_has_room(cls, org):
        limit = org.plan_config['max_active_projects']
        return limit is None or cls.active_count(org) < limit

    @property
    def is_pipeline(self):
        return self.stage in self.PIPELINE_STAGES

    @property
    def section(self):
        return 'estimating' if self.stage in self.ESTIMATING_STAGES else 'delivery'

    @property
    def needs_handover(self):
        """A won or live job with nobody on the projects team running it."""
        return self.section == 'delivery' and self.stage != self.STAGE_COMPLETE and self.manager_id is None

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

    TENDER_MARKS = [('site_visit', 'Site visit', 'visit'), ('queries_close', 'Queries close', 'queries'),
                    ('quotes_due', 'Sub quotes due', 'quotes'), ('programme_due', 'Programme due', 'programme'),
                    ('quality_due', 'Quality submission due', 'quality'), ('logistics_due', 'Logistics plan due', 'logistics')]

    @property
    def tender_start(self):
        return self.tender_received or (timezone.localdate(self.created_at) if self.created_at else None)

    @property
    def tender_return_date(self):
        return timezone.localdate(self.tender_due) if self.tender_due else None

    def tender_marks(self):
        return [(getattr(self, f), label, kind) for f, label, kind in self.TENDER_MARKS if getattr(self, f)]

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
    ROLE_BIDDER = 'BIDDER'
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
    # A subcontractor pricing a package: sees only their tender until appointed
    BIDDER_CHOICE = (ROLE_BIDDER, 'Tendering subcontractor')
    ROLE_CHOICES = INTERNAL_ROLE_CHOICES + EXTERNAL_ROLE_CHOICES + [BIDDER_CHOICE]
    EXTERNAL_ROLES = (ROLE_CLIENT, ROLE_CONSULTANT, ROLE_SUBCONTRACTOR, ROLE_BIDDER)

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

    @property
    def is_bidder(self):
        return self.role == self.ROLE_BIDDER


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
    role = models.CharField(max_length=16, choices=ProjectMember.EXTERNAL_ROLE_CHOICES + [ProjectMember.BIDDER_CHOICE])
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


class Activity(TenantModel):
    """One line on a project programme (Gantt chart)."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='activities')
    name = models.CharField(max_length=200)
    start = models.DateField()
    finish = models.DateField(help_text='For a milestone this is the same as the start.')
    is_milestone = models.BooleanField('Milestone', default=False)
    progress = models.PositiveSmallIntegerField('Progress %', default=0)
    after = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='successors',
                              verbose_name='Follows', help_text='Activity that must finish before this one starts.')
    responsible = models.CharField('Who', max_length=120, blank=True, help_text='Trade, subcontractor or person.')
    is_shared = models.BooleanField('Visible to project collaborators', default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start', 'finish', 'pk']
        verbose_name_plural = 'activities'

    def __str__(self):
        return self.name

    @property
    def duration_days(self):
        return (self.finish - self.start).days + 1

    @property
    def is_complete(self):
        return self.progress >= 100

    @property
    def is_late(self):
        return not self.is_complete and self.finish < timezone.localdate()

    @property
    def is_out_of_sequence(self):
        """Starts before the activity it follows has finished (a milestone can be followed the same day)."""
        if self.after is None:
            return False
        if self.after.is_milestone:
            return self.start < self.after.finish
        return self.start <= self.after.finish


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

    @property
    def status_tone(self):
        return {self.STATUS_OPEN: 'accent', self.STATUS_READY: 'info', self.STATUS_CLOSED: 'good'}.get(self.status, 'muted')

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)


# DocumentRevision statuses (ISO 19650 states plus superseded and withdrawn)
REV_WIP, REV_SHARED, REV_PUBLISHED, REV_SUPERSEDED, REV_WITHDRAWN = 'WIP', 'SHARED', 'PUBLISHED', 'SUPERSEDED', 'WITHDRAWN'


class Document(TenantModel):
    """
    One entry on the drawing register: a drawing, spec, report or other document, with its
    revisions. Numbered to BS EN ISO 19650-2 (UK National Annex) where it can be: the seven
    fields below assemble into `reference` (SLN-ABC-ZZ-02-DR-A-1001); see documents.naming.
    Revision and suitability belong to each DocumentRevision, not to the number.
    """
    CATEGORY_CHOICES = [
        ('DRAWING', 'Drawing'), ('SPEC', 'Specification'), ('TENDER', 'Tender document'),
        ('CONTRACT', 'Contract'), ('RAMS', 'RAMS / H&S'), ('REPORT', 'Report'),
        ('PHOTO', 'Photo'), ('OTHER', 'Other'),
    ]
    VIS_INTERNAL = 'INTERNAL'
    VIS_SHARED = 'SHARED'
    VISIBILITY_CHOICES = [(VIS_INTERNAL, 'Internal only'), (VIS_SHARED, 'Shared with project collaborators')]

    # How well `reference` follows the naming standard
    NAMING_NONE = ''           # no number (photos, letters)
    NAMING_OK = 'OK'           # follows ISO 19650 and the project's code lists
    NAMING_ACCEPTED = 'WARN'   # doesn't fully conform; someone accepted it as it is
    NAMING_UNPARSED = 'UNPARSED'  # an older number we couldn't read: someone should fix or accept it
    NAMING_CHOICES = [(NAMING_NONE, 'No number'), (NAMING_OK, 'Follows naming'),
                      (NAMING_ACCEPTED, 'Accepted with warnings'), (NAMING_UNPARSED, 'Needs checking')]
    SHEET_SIZES = [('A0', 'A0'), ('A1', 'A1'), ('A2', 'A2'), ('A3', 'A3'), ('A4', 'A4'), ('OTHER', 'Other')]
    SHARE_ROLE_CHOICES = [('CLIENT', 'Client'), ('CONSULTANT', 'Consultants / designers'),
                          ('SUBCONTRACTOR', 'Subcontractors')]

    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name='documents')
    title = models.CharField(max_length=200)
    reference = models.CharField('Doc / drawing no.', max_length=80, blank=True)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='DRAWING')
    visibility = models.CharField(max_length=8, choices=VISIBILITY_CHOICES, default=VIS_INTERNAL)
    # Blank: shared with every collaborator role (as before). Otherwise ",CLIENT,CONSULTANT," etc.
    share_roles = models.CharField('Shared with', max_length=60, blank=True, default='')
    tender_issue = models.BooleanField('Issued to tenderers', default=False,
                                       help_text='Subcontractors pricing a package can see and download it.')

    # ISO 19650-2 UK National Annex fields; `reference` is these joined with hyphens
    project_code = models.CharField('Project', max_length=12, blank=True)
    originator = models.CharField(max_length=12, blank=True)
    volume = models.CharField('Volume / system', max_length=12, blank=True)
    level = models.CharField('Level / location', max_length=12, blank=True)
    doc_type = models.CharField('Type', max_length=12, blank=True)
    role = models.CharField(max_length=12, blank=True)
    number = models.CharField(max_length=12, blank=True)
    naming = models.CharField(max_length=8, choices=NAMING_CHOICES, blank=True, default=NAMING_NONE)
    naming_notes = models.CharField(max_length=500, blank=True, help_text='Why the number was flagged.')

    discipline = models.CharField(max_length=60, blank=True)
    sheet_size = models.CharField(max_length=5, choices=SHEET_SIZES, blank=True)
    scale = models.CharField(max_length=30, blank=True, help_text='e.g. 1:50 @ A1')
    folder = models.CharField(max_length=120, blank=True, help_text='Optional, e.g. "Stage 4 / Architecture"')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'reference', 'title']
        indexes = [models.Index(fields=['project', 'reference'])]

    def __str__(self):
        return self.title

    @property
    def label(self):
        return f'{self.reference} {self.title}' if self.reference else self.title

    # --- naming ------------------------------------------------------------------
    @property
    def parts(self):
        from documents.naming import FIELDS
        return {f: getattr(self, f) for f in FIELDS}

    def set_number(self, text, warnings=(), accepted=False):
        """
        Set `reference` from a typed number, filling the seven ISO fields when it has them.
        warnings: what documents.naming.check_reference found. With warnings the number is
        kept as typed and flagged: accepted (someone said "use it anyway") or needs checking.
        """
        from documents.naming import FIELDS, assemble, split_reference
        text = (text or '').strip()
        parts = split_reference(text)
        for f in FIELDS:
            setattr(self, f, parts[f] if parts else '')
        self.reference = (assemble(parts) if parts else text)[:80]
        if not text:
            self.naming, self.naming_notes = self.NAMING_NONE, ''
        elif not warnings:
            self.naming, self.naming_notes = self.NAMING_OK, ''
        else:
            self.naming = self.NAMING_ACCEPTED if accepted else self.NAMING_UNPARSED
            self.naming_notes = ' '.join(warnings)[:500]
        if parts and not self.discipline:
            from documents.naming import DEFAULT_CODES
            self.discipline = dict(DEFAULT_CODES['role']).get(parts['role'], '')

    @property
    def needs_naming_check(self):
        return self.naming == self.NAMING_UNPARSED

    # --- sharing -----------------------------------------------------------------
    @property
    def share_role_list(self):
        return [r for r in self.share_roles.split(',') if r]

    def set_share_roles(self, roles):
        roles = [r for r, _ in self.SHARE_ROLE_CHOICES if r in set(roles or ())]
        self.share_roles = f',{",".join(roles)},' if roles and len(roles) < len(self.SHARE_ROLE_CHOICES) else ''

    def shared_with_role(self, role):
        return self.visibility == self.VIS_SHARED and (not self.share_roles or f',{role},' in self.share_roles)

    # --- revisions ---------------------------------------------------------------
    @property
    def current(self):
        """The revision people should build from: flagged when uploaded, or worked out for unflagged rows."""
        revisions = list(self.revisions.all())  # uses prefetch_related('revisions') where the view set it up
        live = [r for r in revisions if r.status != REV_WITHDRAWN]
        return next((r for r in revisions if r.is_current), None) or latest_revision(live)

    def add_revision(self, revision, make_current=False):
        """
        Save a new revision of this document. It becomes current unless it is older
        than the current one (P02 uploaded after C01 stays a superseded revision),
        or the uploader explicitly marks it current. Whichever is no longer current is
        marked superseded by the one that is.
        """
        with transaction.atomic():
            Document.all_objects.select_for_update().filter(pk=self.pk).first()
            current = self.revisions.filter(is_current=True).first() or latest_revision(
                self.revisions.exclude(status=REV_WITHDRAWN))
            revision.document = self
            revision.is_current = make_current or current is None or not is_older_revision(revision, current)
            revision.save()
            if revision.is_current:
                self.revisions.exclude(pk=revision.pk).filter(is_current=True).update(is_current=False)
                if current is not None:
                    current.supersede(revision)
            else:
                revision.supersede(current)
        return revision

    def set_current(self, revision):
        """Mark an existing revision current, e.g. to go back to an earlier issue."""
        with transaction.atomic():
            Document.all_objects.select_for_update().filter(pk=self.pk).first()
            previous = self.revisions.filter(is_current=True).exclude(pk=revision.pk).first()
            self.revisions.exclude(pk=revision.pk).filter(is_current=True).update(is_current=False)
            self.revisions.filter(pk=revision.pk).update(is_current=True)
            revision.refresh_from_db()
            revision.reinstate()
            if previous is not None:
                previous.supersede(revision)
        revision.is_current = True

    def withdraw(self, revision, note=''):
        """Withdraw a revision (issued in error). If it was current, the latest one left takes over."""
        with transaction.atomic():
            Document.all_objects.select_for_update().filter(pk=self.pk).first()
            was_current = revision.is_current
            DocumentRevision.objects.filter(pk=revision.pk).update(
                status=REV_WITHDRAWN, is_current=False, status_changed_at=timezone.now(),
                notes=(f'{revision.notes} · Withdrawn: {note}' if revision.notes else f'Withdrawn: {note}')[:300]
                if note else revision.notes)
            revision.refresh_from_db()
            if was_current:
                successor = latest_revision(self.revisions.exclude(pk=revision.pk).exclude(status=REV_WITHDRAWN))
                if successor is not None:
                    self.revisions.filter(pk=successor.pk).update(is_current=True)
                    successor.is_current = True
                    successor.reinstate()
        return revision


# Drawing revisions: preliminary (P01, P02 ... P10) come before construction (C01, C02 ...)
REVISION_SERIES = {'P': 0, 'C': 1}
_REVISION_RE = re.compile(r'^\s*([PC])\s*-?\s*(\d+)\s*$', re.IGNORECASE)


def revision_rank(code):
    """(series, number) for a P or C revision, so P10 sorts after P02 and before C01; None for 'A', 'rev 3'."""
    m = _REVISION_RE.match(code or '')
    return (REVISION_SERIES[m.group(1).upper()], int(m.group(2))) if m else None


def is_older_revision(new, current):
    """True when `new` is an earlier revision than `current`. Codes we can't rank fall back to upload order."""
    a, b = revision_rank(new.revision), revision_rank(current.revision)
    return a is not None and b is not None and a < b


def latest_revision(revisions):
    """
    The revision that should be current: the highest P/C code, or, when any code
    can't be ranked, the most recently uploaded. A data migration copies this rule.
    """
    revisions = list(revisions)
    if not revisions:
        return None
    if all(revision_rank(r.revision) for r in revisions):
        return max(revisions, key=lambda r: (revision_rank(r.revision), r.uploaded_at, r.pk))
    return max(revisions, key=lambda r: (r.uploaded_at, r.pk))


class DocumentRevision(TenantModel):
    """
    One issue of a document. Status follows ISO 19650: work in progress (internal), shared,
    published, then superseded once a newer revision is current, or withdrawn if issued in
    error. Suitability (S2, A1...) says what it may be used for.
    """
    WIP, SHARED, PUBLISHED, SUPERSEDED, WITHDRAWN = REV_WIP, REV_SHARED, REV_PUBLISHED, REV_SUPERSEDED, REV_WITHDRAWN
    STATUS_CHOICES = [(WIP, 'Work in progress'), (SHARED, 'Shared'), (PUBLISHED, 'Published'),
                      (SUPERSEDED, 'Superseded'), (WITHDRAWN, 'Withdrawn')]
    STATUS_TONES = {WIP: 'muted', SHARED: 'info', PUBLISHED: 'good', SUPERSEDED: 'warn', WITHDRAWN: 'bad'}
    LIVE_STATUSES = (WIP, SHARED, PUBLISHED)

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='revisions')
    revision = models.CharField(max_length=20, default='P01')
    suitability = models.CharField(max_length=4, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, blank=True, db_index=True,
                              help_text='Left blank, it is worked out on save from the suitability and sharing.')
    status_changed_at = models.DateTimeField(null=True, blank=True)
    # When it first left work in progress (or was transmitted). Never cleared: externals' "shared to
    # their role" route is gated on this, so a revision that was only ever WIP stays internal even
    # after it is superseded or withdrawn.
    shared_at = models.DateTimeField(null=True, blank=True, editable=False)
    superseded_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    file = models.FileField(upload_to=upload_document)
    original_name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    is_current = models.BooleanField('Current revision', default=False,
                                     help_text='Set by Document.add_revision / set_current; one per document.')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.document} rev {self.revision}'

    @property
    def status_tone(self):
        return self.STATUS_TONES.get(self.status, 'muted')

    @property
    def is_superseded(self):
        return self.status == self.SUPERSEDED

    @property
    def is_withdrawn(self):
        return self.status == self.WITHDRAWN

    def live_status(self):
        """What this revision's status is while it is in use: from its suitability, else how widely it's shared."""
        from documents.naming import status_for_suitability
        return status_for_suitability(self.suitability) or (
            self.SHARED if self.document.visibility == Document.VIS_SHARED or self._transmitted() else self.WIP)

    def _transmitted(self):
        if not self.pk:
            return False
        from documents.models import TransmittalItem
        return TransmittalItem.objects.filter(revision=self, transmittal__issued_at__isnull=False).exists()

    def save(self, *args, **kwargs):
        if not self.status:
            self.status = self.live_status()
        if self.status_changed_at is None:
            self.status_changed_at = timezone.now()
        if self.shared_at is None and self.status in (self.SHARED, self.PUBLISHED):
            self.shared_at = self.status_changed_at
        super().save(*args, **kwargs)

    @property
    def was_shared(self):
        return self.shared_at is not None

    def supersede(self, by):
        """Mark this revision superseded by `by` (left alone if withdrawn)."""
        if self.status == self.WITHDRAWN or by is None or by.pk == self.pk:
            return
        self.status, self.superseded_by = self.SUPERSEDED, by
        self.status_changed_at = timezone.now()
        DocumentRevision.objects.filter(pk=self.pk).update(status=self.status, superseded_by=by,
                                                            status_changed_at=self.status_changed_at)

    def reinstate(self):
        """Back in use (made current again): its live status, no longer superseded."""
        if self.status not in (self.SUPERSEDED, self.WITHDRAWN) and self.superseded_by_id is None:
            return
        # Never shared: back to work in progress, not to "shared because the document is"
        self.status = self.live_status() if self.shared_at else self.WIP
        self.superseded_by = None
        self.status_changed_at = timezone.now()
        DocumentRevision.objects.filter(pk=self.pk).update(status=self.status, superseded_by=None,
                                                            status_changed_at=self.status_changed_at)

    def set_status(self, status):
        if status == self.status:
            return
        self.status, self.status_changed_at = status, timezone.now()
        fields = {'status': status, 'status_changed_at': self.status_changed_at}
        if self.shared_at is None and status in (self.SHARED, self.PUBLISHED):
            self.shared_at = fields['shared_at'] = self.status_changed_at
        DocumentRevision.objects.filter(pk=self.pk).update(**fields)

    def mark_shared(self):
        """Record that it has gone outside the company (on a transmittal), whatever its status says."""
        if self.shared_at is None:
            self.shared_at = timezone.now()
            DocumentRevision.objects.filter(pk=self.pk, shared_at__isnull=True).update(shared_at=self.shared_at)

    @property
    def current_revision(self):
        """The document's current revision, when it isn't this one (for the superseded banner)."""
        cur = self.document.current
        return cur if cur and cur.pk != self.pk else None


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


class FileAccess(TenantModel):
    """Who opened which file, and when: settles "we never received addendum 2"."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.RESTRICT, related_name='file_access')
    label = models.CharField(max_length=255)
    path = models.CharField(max_length=500, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-at']
        verbose_name = 'file download'

    def __str__(self):
        return f'{self.user} opened {self.label}'


class FileScan(TenantModel):
    """Malware scan result for one stored file, as tagged by GuardDuty. Only final results are kept."""
    CLEAN = 'NO_THREATS_FOUND'
    INFECTED = 'THREATS_FOUND'
    FINAL = (CLEAN, INFECTED, 'UNSUPPORTED', 'ACCESS_DENIED', 'FAILED')

    path = models.CharField(max_length=500, unique=True)
    status = models.CharField(max_length=20)
    checked_at = models.DateTimeField(auto_now=True)
    alerted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.path}: {self.status}'

    @property
    def is_clean(self):
        return self.status == self.CLEAN
