"""
Document control: per-project code lists and naming settings, distribution rules,
transmittals (TR-001) and bulk-upload batches. The register itself is projects.Document
and projects.DocumentRevision.
"""
from django.conf import settings
from django.db import models
from django.urls import reverse

from core.models import RecordBase
from projects.models import Document, DocumentRevision, Project, _private_path, upload_document
from tenancy.models import TenantModel

from . import naming


def upload_transmittal(instance, filename):
    return _private_path(instance, filename, 'transmittals')


class RegisterSettings(TenantModel):
    """How one project numbers its documents. Missing row: ISO defaults, naming checked."""
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='register_settings')
    project_code = models.CharField('Project code', max_length=12, blank=True,
                                    help_text='The first part of every number on this job, e.g. SLN.')
    filename_pattern = models.CharField(
        'File name pattern', max_length=200, blank=True,
        help_text='Leave blank for ISO 19650 numbers with the revision after them, e.g. '
                  'SLN-ABC-ZZ-02-DR-A-1001_P03.pdf. Parts: {project} {originator} {volume} {level} {type} '
                  '{role} {number} {revision} {suitability} {title} {reference}.')
    check_naming = models.BooleanField('Check document numbers against ISO 19650', default=True,
                                       help_text='Numbers that don\'t follow it need a "use it anyway" tick.')

    def __str__(self):
        return f'Register settings for {self.project}'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.filename_pattern:
            try:
                naming.compile_pattern(self.filename_pattern)
            except naming.PatternError as err:
                raise ValidationError({'filename_pattern': str(err)})

    @classmethod
    def for_project(cls, project):
        """The project's settings, unsaved defaults if it has none (reading never writes)."""
        return cls.objects.filter(project=project).first() or cls(project=project, organization=project.organization)


class CodeList(TenantModel):
    """
    One allowed code for one naming field on one project. A project with no rows for a
    field uses the UK defaults in documents.naming.DEFAULT_CODES; the first change copies
    them in, so each project can follow its own BIM execution plan.
    """
    FIELD_CHOICES = [(f, naming.FIELD_LABELS[f]) for f in naming.CODE_FIELDS]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='code_lists')
    field = models.CharField(max_length=12, choices=FIELD_CHOICES)
    code = models.CharField(max_length=12)
    description = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['field', 'code']
        unique_together = [('project', 'field', 'code')]

    def __str__(self):
        return f'{self.get_field_display()} {self.code}'


def code_lists(project):
    """{field: [(code, description), ...]} for a project: its own rows, else the UK defaults."""
    own = {}
    for row in CodeList.objects.filter(project=project):
        own.setdefault(row.field, []).append((row.code, row.description))
    return {f: own.get(f, list(naming.DEFAULT_CODES.get(f, []))) for f in naming.CODE_FIELDS}


def code_sets(project):
    """{field: set of codes} for documents.naming.validate."""
    return {f: {c for c, _ in rows} for f, rows in code_lists(project).items()}


def customise_codes(project, field):
    """Copy the defaults for a field into the project's own list (once), so it can be edited."""
    if CodeList.objects.filter(project=project, field=field).exists():
        return
    CodeList.objects.bulk_create([CodeList(project=project, field=field, code=c, description=d,
                                           organization=project.organization)
                                  for c, d in naming.DEFAULT_CODES.get(field, [])])


def check_number(project, text):
    """(parts or None, warnings) for a number on this project, or no warnings if checking is off."""
    rs = RegisterSettings.for_project(project)
    parts, warnings = naming.check_reference(text, code_sets(project), rs.project_code)
    return parts, (warnings if rs.check_naming else [])


class DistributionRule(TenantModel):
    """
    Who usually gets a discipline's documents on this project ("usual recipients").
    A blank discipline means every document.
    """
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='distribution_rules')
    discipline = models.CharField(max_length=60, blank=True, help_text='Blank for every discipline.')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, blank=True, on_delete=models.CASCADE,
                                      related_name='+', verbose_name='Company')

    class Meta:
        ordering = ['discipline', 'pk']

    def __str__(self):
        who = self.user or self.subcontractor
        return f'{self.discipline or "All disciplines"} → {who}'


class Transmittal(RecordBase):
    """
    A numbered issue of document revisions to named people or companies (TR-001). Issuing
    it is what shares those revisions with external recipients; each recipient then
    acknowledges receipt, and until they all have, the ball is in their court.
    Status changes go through documents.workflow; issuing through documents.services.
    """
    STATUS_DRAFT = 'DRAFT'
    STATUS_ISSUED = 'ISSUED'
    STATUS_ACKNOWLEDGED = 'ACKNOWLEDGED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [(STATUS_DRAFT, 'Draft'), (STATUS_ISSUED, 'Issued'),
                      (STATUS_ACKNOWLEDGED, 'All received'), (STATUS_CANCELLED, 'Cancelled')]
    STATUS_TONES = {STATUS_DRAFT: 'muted', STATUS_ISSUED: 'accent', STATUS_ACKNOWLEDGED: 'good',
                    STATUS_CANCELLED: 'muted'}
    SENT_STATUSES = (STATUS_ISSUED, STATUS_ACKNOWLEDGED)

    PURPOSE_CONSTRUCTION = 'CONSTRUCTION'
    PURPOSE_INFORMATION = 'INFORMATION'
    PURPOSE_TENDER = 'TENDER'
    PURPOSE_COMMENT = 'COMMENT'
    PURPOSE_APPROVAL = 'APPROVAL'
    PURPOSE_CHOICES = [(PURPOSE_CONSTRUCTION, 'For construction'), (PURPOSE_INFORMATION, 'For information'),
                       (PURPOSE_TENDER, 'For tender'), (PURPOSE_COMMENT, 'For comment'),
                       (PURPOSE_APPROVAL, 'For approval')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    purpose = models.CharField(max_length=12, choices=PURPOSE_CHOICES, default=PURPOSE_INFORMATION)
    cover_note = models.TextField(blank=True)
    due_date = models.DateField('Acknowledge by', null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', editable=False)
    sheet = models.FileField('Issue sheet', upload_to=upload_transmittal, blank=True, editable=False)
    sheet_generated_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-number']

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('transmittal_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return self.STATUS_TONES.get(self.status, 'muted')

    @property
    def is_sent(self):
        return self.status in self.SENT_STATUSES

    def pending_recipients(self):
        return [r for r in self.recipients.all() if r.acknowledged_at is None]


class TransmittalItem(TenantModel):
    """One revision on a transmittal, with what it was called when it went out."""
    transmittal = models.ForeignKey(Transmittal, on_delete=models.CASCADE, related_name='items')
    # RESTRICT: a document that has been formally issued can't be deleted from under the record
    revision = models.ForeignKey(DocumentRevision, on_delete=models.RESTRICT, related_name='transmittal_items')
    reference = models.CharField(max_length=80, blank=True)
    title = models.CharField(max_length=200, blank=True)
    revision_code = models.CharField(max_length=20, blank=True)
    suitability = models.CharField(max_length=4, blank=True)

    class Meta:
        ordering = ['reference', 'pk']
        unique_together = [('transmittal', 'revision')]

    def snapshot(self):
        doc = self.revision.document
        self.reference, self.title = doc.reference, doc.title
        self.revision_code, self.suitability = self.revision.revision, self.revision.suitability


class TransmittalRecipient(TenantModel):
    """
    Someone a transmittal went to: a person, or a company (any of its people on the project
    can open it and acknowledge for the company). Pattern from tenders.AddendumReceipt.
    """
    transmittal = models.ForeignKey(Transmittal, on_delete=models.CASCADE, related_name='recipients')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+')
    name = models.CharField(max_length=200, help_text='Who it went to, kept if the person or company is removed.')
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+')
    acknowledged_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['name', 'pk']
        unique_together = [('transmittal', 'user'), ('transmittal', 'subcontractor')]

    def __str__(self):
        return self.name

    @property
    def is_company(self):
        return self.subcontractor_id is not None

    def people(self):
        """The users this recipient stands for on the project."""
        from projects.models import ProjectMember
        if self.user_id:
            return [self.user]
        if self.subcontractor_id:
            return [m.user for m in ProjectMember.objects.filter(
                project_id=self.transmittal.project_id, subcontractor_id=self.subcontractor_id)
                .exclude(role=ProjectMember.ROLE_BIDDER).select_related('user')]
        return []


class UploadBatch(TenantModel):
    """Files dropped onto the bulk-upload page, waiting for someone to check the preview and commit."""
    OPEN, DONE, CANCELLED = 'OPEN', 'DONE', 'CANCELLED'
    STATUS_CHOICES = [(OPEN, 'Waiting to be checked'), (DONE, 'Committed'), (CANCELLED, 'Cancelled')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='upload_batches')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def get_absolute_url(self):
        return reverse('bulk_upload_batch', args=[self.organization.slug, self.project_id, self.pk])


class UploadRow(TenantModel):
    """One file in a bulk upload, with what its name was read as and what committing it would do."""
    NEW_DOC = 'NEW_DOC'
    NEW_REV = 'NEW_REV'
    OLDER_REV = 'OLDER_REV'
    CONFLICT = 'CONFLICT'
    DUPLICATE = 'DUPLICATE'
    INCOMPLETE = 'INCOMPLETE'
    ACTION_CHOICES = [(NEW_DOC, 'New document'), (NEW_REV, 'New revision'), (OLDER_REV, 'Older revision'),
                      (CONFLICT, 'Conflict'), (DUPLICATE, 'Already uploaded'), (INCOMPLETE, 'Needs details')]
    ACTION_TONES = {NEW_DOC: 'good', NEW_REV: 'info', OLDER_REV: 'warn', CONFLICT: 'bad', DUPLICATE: 'muted',
                    INCOMPLETE: 'warn'}
    COMMITTABLE = (NEW_DOC, NEW_REV, OLDER_REV)

    PENDING, COMMITTED, SKIPPED = 'PENDING', 'COMMITTED', 'SKIPPED'
    STATE_CHOICES = [(PENDING, 'Waiting'), (COMMITTED, 'Added to the register'), (SKIPPED, 'Left out')]

    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name='rows')
    file = models.FileField(upload_to=upload_document)
    original_name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    processed = models.BooleanField(default=False, help_text='Checksum and page count worked out.')

    reference = models.CharField('Number', max_length=80, blank=True)
    revision = models.CharField(max_length=20, blank=True)
    suitability = models.CharField(max_length=4, blank=True)
    title = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=10, choices=Document.CATEGORY_CHOICES, default='DRAWING')
    accept_warnings = models.BooleanField(default=False)

    action = models.CharField(max_length=10, choices=ACTION_CHOICES, blank=True)
    message = models.CharField(max_length=300, blank=True)
    warnings = models.TextField(blank=True)
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    supersedes = models.ForeignKey(DocumentRevision, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=PENDING)
    committed_revision = models.ForeignKey(DocumentRevision, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name='+')

    class Meta:
        ordering = ['original_name', 'pk']

    @property
    def action_tone(self):
        return self.ACTION_TONES.get(self.action, 'muted')

    @property
    def warning_list(self):
        return [w for w in self.warnings.split('\n') if w]


def recipient_q(user, prefix='', project=None):
    """
    Q for TransmittalRecipient rows that stand for this user: addressed to them, or to a
    company they represent on that same project. prefix: the path to the recipient from
    the model being filtered ('recipients__' from Transmittal).
    """
    from projects.models import ProjectMember
    q = models.Q(**{f'{prefix}user': user})
    links = ProjectMember.objects.filter(user=user, subcontractor__isnull=False) \
        .exclude(role=ProjectMember.ROLE_BIDDER)
    if project is not None:
        links = links.filter(project=project)
    for project_id, sub_id in links.values_list('project_id', 'subcontractor_id'):
        q |= models.Q(**{f'{prefix}subcontractor_id': sub_id, f'{prefix}transmittal__project_id': project_id})
    return q


def pending_acknowledgements(user, project=None):
    """
    Issued transmittals waiting on this person to acknowledge (as themselves or for their
    company). For the "Needs me" registry: externals can't read transmittals from
    RecordIndex, because each one is only for its recipients.
    """
    rows = TransmittalRecipient.objects.filter(recipient_q(user, project=project), acknowledged_at__isnull=True,
                                               transmittal__status=Transmittal.STATUS_ISSUED,
                                               transmittal__deleted_at__isnull=True)
    qs = Transmittal.objects.filter(pk__in=rows.values('transmittal_id'))
    if project is not None:
        qs = qs.filter(project=project)
    return qs.order_by('due_date', 'number')
