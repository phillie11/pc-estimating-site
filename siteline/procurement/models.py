"""
Procurement: the schedule of what has to be bought for a live job and when, and the
submittals (shop drawings, samples, product data, O&M information) that must be approved
before it is ordered.

  ProcurementSettings  per job: our review period and the resubmission period (working days),
                       and who reviews submittals (default: the project manager).
  ScheduleLine         one package or long-lead item: required on site, lead time and approval
                       period (weeks), design information needed by, status. The order-by and
                       submittals-due dates are worked out on save (procurement.dates).
  Submittal            SUB-001, a core.RecordBase record. Linked to a schedule line and/or a
                       trade package; the package's subcontractor is who submits it.
  SubmittalRevision    revision 00, 01, 02... of one submittal: what was sent, the review code
                       (A / B / C) and comments, and the designer's comments if it was forwarded.
  SubmittalFile        files on a revision: the submission, our marked-up copy, the designer's.

Who sees what is in procurement/access.py, not in RecordBase.visibility: the type is registered
with external_roles=(), so the record index never shows submittals to externals, and the
subcontractor and consultant pages apply their own narrower rules.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import RecordBase
from projects.models import _private_path
from tenancy.models import TenantModel

from . import dates


def upload_submittal(instance, filename):
    return _private_path(instance, filename, 'submittals')


class ProcurementSettings(TenantModel):
    """How one job runs its submittals. No row: the defaults below."""
    DEFAULT_REVIEW_DAYS = 10
    DEFAULT_RESUBMIT_DAYS = 10

    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='procurement_settings')
    review_days = models.PositiveSmallIntegerField(
        'Our review period (working days)', default=DEFAULT_REVIEW_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text='How long we take to review a submittal, from the day it arrives. Check your subcontract.')
    resubmit_days = models.PositiveSmallIntegerField(
        'Time to resubmit (working days)', default=DEFAULT_RESUBMIT_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text='How long a subcontractor has to resubmit after a C (revise and resubmit).')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+', verbose_name='Reviews submittals',
                                 help_text='Leave blank for the project manager.')

    def __str__(self):
        return f'Procurement settings for {self.project}'

    @classmethod
    def for_project(cls, project):
        """The job's settings, or unsaved defaults (reading never writes)."""
        return cls.objects.filter(project=project).first() or cls(project=project, organization=project.organization)


def region_for(project):
    """Bank holiday region for working-day counts: the main contract's, else England and Wales."""
    from contracts.models import MainContract
    from holidays.calendar import DEFAULT_REGION
    # all_objects, pinned to this project and its organisation, so a line can be saved from a job or command too
    region = MainContract.all_objects.filter(organization_id=project.organization_id, project_id=project.pk) \
        .values_list('bank_holiday_region', flat=True).first()
    return region or DEFAULT_REGION


def recalculate_for_contract(sender, instance, **kwargs):
    """
    post_save on contracts.MainContract: a change of bank holiday region moves working-day dates,
    so re-work that job's schedule lines. Writes only the lines whose dates change, with update()
    (no save() signal loop). Pinned to the contract's organisation and project, so all_objects is safe.
    """
    lines = ScheduleLine.all_objects.filter(organization_id=instance.organization_id, project_id=instance.project_id)
    for line in lines:
        before = (line.order_by, line.submittals_due)
        line.recalculate(region=instance.bank_holiday_region)
        if (line.order_by, line.submittals_due) != before:
            ScheduleLine.all_objects.filter(pk=line.pk).update(order_by=line.order_by, submittals_due=line.submittals_due)


class ScheduleLine(TenantModel):
    NOT_STARTED, TENDER, APPOINTED, IN_REVIEW = 'NOT_STARTED', 'TENDER', 'APPOINTED', 'IN_REVIEW'
    APPROVED, ORDERED, DELIVERED = 'APPROVED', 'ORDERED', 'DELIVERED'
    STATUS_CHOICES = [
        (NOT_STARTED, 'Not started'), (TENDER, 'Out to tender'), (APPOINTED, 'Appointed'),
        (IN_REVIEW, 'Submittals in review'), (APPROVED, 'Approved'), (ORDERED, 'Ordered'), (DELIVERED, 'Delivered'),
    ]
    RANK = {code: i for i, (code, _) in enumerate(STATUS_CHOICES)}
    DONE_STATUSES = (ORDERED, DELIVERED)

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='procurement_lines')
    title = models.CharField('Package or item', max_length=200)
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='procurement_lines', verbose_name='Trade package',
                                help_text='The appointed (or tendering) subcontractor for this, if there is one.')
    long_lead = models.BooleanField('Long-lead item', default=False)
    supplier = models.CharField('Supplier', max_length=200, blank=True,
                                help_text='Who it is ordered from, if not a trade package.')
    required_on_site = models.DateField('Required on site', null=True, blank=True)
    lead_time_weeks = models.PositiveSmallIntegerField('Manufacture / lead time (weeks)', default=0,
                                                       validators=[MaxValueValidator(104)])
    approval_weeks = models.PositiveSmallIntegerField('Approval period for submittals (weeks)', default=2,
                                                      validators=[MaxValueValidator(52)])
    design_info_by = models.DateField('Design information needed by', null=True, blank=True)
    order_by = models.DateField('Order by', null=True, blank=True, editable=False)
    submittals_due = models.DateField('Submittals due', null=True, blank=True, editable=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=NOT_STARTED, db_index=True)
    ordered_on = models.DateField('Ordered on', null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        ordering = ['required_on_site', 'pk']

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('procurement_line', args=[self.organization.slug, self.project_id, self.pk])

    def recalculate(self, region=None):
        region = region or region_for(self.project)
        self.order_by = dates.order_by(self.required_on_site, self.lead_time_weeks, self.approval_weeks, region)
        self.submittals_due = dates.submittals_due(self.required_on_site, self.lead_time_weeks, self.approval_weeks,
                                                   region)

    def save(self, *args, **kwargs):
        self.recalculate()
        if self.status in self.DONE_STATUSES and not self.ordered_on:
            self.ordered_on = timezone.localdate()
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = {*kwargs['update_fields'], 'order_by', 'submittals_due', 'ordered_on',
                                       'updated_at'}
        super().save(*args, **kwargs)

    @property
    def is_done(self):
        return self.status in self.DONE_STATUSES

    def rag_on(self, today=None):
        return dates.rag(self.order_by, self.is_done, today or timezone.localdate())

    @property
    def rag(self):
        return self.rag_on()

    @property
    def rag_label(self):
        return dates.RAG_LABELS[self.rag]

    @property
    def rag_tone(self):
        return dates.RAG_TONES[self.rag]

    @property
    def status_tone(self):
        return {'NOT_STARTED': 'muted', 'TENDER': 'info', 'APPOINTED': 'info', 'IN_REVIEW': 'accent',
                'APPROVED': 'good', 'ORDERED': 'good', 'DELIVERED': 'muted'}[self.status]

    @property
    def design_info_late(self):
        """Design information needed after the submittals are due: the subcontractor can't meet the date."""
        return bool(self.design_info_by and self.submittals_due and self.design_info_by > self.submittals_due)

    @property
    def who(self):
        if self.package_id:
            return str(self.package.subcontractor)
        return self.supplier

    def advance_to(self, status):
        """Move forward to status (never back: an ordered line stays ordered). True if it changed."""
        if self.RANK[status] > self.RANK[self.status]:
            self.status = status
            self.save(update_fields=['status'])
            return True
        return False


class Submittal(RecordBase):
    REQUIRED, SUBMITTED, WITH_CONSULTANT = 'REQUIRED', 'SUBMITTED', 'WITH_CONSULTANT'
    APPROVED, APPROVED_COMMENTS, REJECTED, WITHDRAWN = 'APPROVED', 'APPROVED_COMMENTS', 'REJECTED', 'WITHDRAWN'
    STATUS_CHOICES = [
        (REQUIRED, 'To submit'), (SUBMITTED, 'In review'), (WITH_CONSULTANT, 'With designer for comment'),
        (APPROVED, 'A: Approved'), (APPROVED_COMMENTS, 'B: Approved with comments'),
        (REJECTED, 'C: Revise and resubmit'), (WITHDRAWN, 'Withdrawn'),
    ]
    OPEN_STATUSES = (REQUIRED, SUBMITTED, WITH_CONSULTANT, REJECTED)
    ACCEPTED = (APPROVED, APPROVED_COMMENTS)
    IN_REVIEW = (SUBMITTED, WITH_CONSULTANT)
    TO_SUBMIT = (REQUIRED, REJECTED)  # waiting on the subcontractor
    CODE_FOR = {APPROVED: 'A', APPROVED_COMMENTS: 'B', REJECTED: 'C'}

    SHOP_DRAWING, SAMPLE, PRODUCT_DATA, OM, OTHER = 'SHOP_DRAWING', 'SAMPLE', 'PRODUCT_DATA', 'OM', 'OTHER'
    KIND_CHOICES = [(SHOP_DRAWING, 'Shop drawings'), (SAMPLE, 'Samples'), (PRODUCT_DATA, 'Product data'),
                    (OM, 'O&M information'), (OTHER, 'Other')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=REQUIRED, db_index=True)
    kind = models.CharField('Type', max_length=14, choices=KIND_CHOICES, default=SHOP_DRAWING)
    line = models.ForeignKey(ScheduleLine, null=True, blank=True, on_delete=models.SET_NULL, related_name='submittals',
                             verbose_name='Procurement line')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='submittals', verbose_name='Trade package',
                                help_text='Its subcontractor submits it through the portal.')
    spec_reference = models.CharField('Specification or drawing reference', max_length=120, blank=True)
    description = models.TextField('What is needed', blank=True)
    revision = models.PositiveSmallIntegerField(default=0, editable=False,
                                                help_text='The latest revision submitted (00 is the first).')

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'submittal'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('submittal_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'REQUIRED': 'warn', 'SUBMITTED': 'accent', 'WITH_CONSULTANT': 'info', 'APPROVED': 'good',
                'APPROVED_COMMENTS': 'good', 'REJECTED': 'bad', 'WITHDRAWN': 'muted'}[self.status]

    @property
    def code(self):
        return self.CODE_FOR.get(self.status, '')

    @property
    def revision_label(self):
        return f'{self.revision:02d}'

    @property
    def subcontractor(self):
        return self.package.subcontractor if self.package_id else None

    @property
    def latest(self):
        return self.revisions.order_by('-number').first()

    @property
    def is_overdue(self):
        return self.is_open and self.due_date is not None and self.due_date < timezone.localdate()

    def ball_label(self, external=False):
        """Who has it now, in plain words. Externals never see a member of staff's name, only the company's."""
        if not self.is_open:
            return ''
        if self.ball_in_court_company_id:
            return str(self.ball_in_court_company)
        if self.status == self.WITH_CONSULTANT and self.ball_in_court_user_id:
            return self.ball_in_court_user.full_name or str(self.ball_in_court_user)
        if external:
            return self.organization.name
        user = self.ball_in_court_user
        return (user.full_name or str(user)) if user else self.organization.name


class SubmittalRevision(TenantModel):
    """One go at a submittal: 00 first, 01 after the first C, and so on. Never deleted."""
    CODE_CHOICES = [('A', 'A: Approved'), ('B', 'B: Approved with comments'), ('C', 'C: Revise and resubmit')]

    submittal = models.ForeignKey(Submittal, on_delete=models.CASCADE, related_name='revisions')
    number = models.PositiveSmallIntegerField()
    note = models.TextField('Note', blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    submitted_at = models.DateTimeField(default=timezone.now)
    review_due = models.DateField(null=True, blank=True)

    code = models.CharField(max_length=1, choices=CODE_CHOICES, blank=True)
    review_comments = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['-number']
        unique_together = [('submittal', 'number')]

    def __str__(self):
        return f'{self.submittal.reference} rev {self.label}'

    @property
    def label(self):
        return f'{self.number:02d}'

    @property
    def code_tone(self):
        return {'A': 'good', 'B': 'good', 'C': 'bad'}.get(self.code, 'muted')

    def files_of(self, role):
        return [f for f in self.files.all() if f.role == role]

    @property
    def submission_files(self):
        return self.files_of(SubmittalFile.SUBMISSION)

    @property
    def markup_files(self):
        return self.files_of(SubmittalFile.MARKUP)



class SubmittalForward(TenantModel):
    """
    One revision sent to one designer or consultant for comment. Each time is its own row, so
    asking a second consultant (or the same one again) never shows or overwrites earlier comments.
    A consultant sees only revisions and files on their own forwards (procurement.access).
    """
    revision = models.ForeignKey(SubmittalRevision, on_delete=models.CASCADE, related_name='forwards')
    consultant = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    forwarded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+')
    forwarded_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)
    comments = models.TextField(blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['forwarded_at', 'pk']

    def __str__(self):
        return f'{self.revision} to {self.consultant}'

    @property
    def is_open(self):
        return self.replied_at is None

    @property
    def files(self):
        return [f for f in self.revision.files.all() if f.forward_id == self.pk]


class SubmittalFile(TenantModel):
    SUBMISSION, MARKUP, CONSULTANT = 'SUBMISSION', 'MARKUP', 'CONSULTANT'
    ROLE_CHOICES = [(SUBMISSION, 'Submitted'), (MARKUP, 'Marked up by reviewer'), (CONSULTANT, "Designer's mark-up")]

    revision = models.ForeignKey(SubmittalRevision, on_delete=models.CASCADE, related_name='files')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=SUBMISSION)
    # The designer's mark-ups belong to the forward they answer, so only that consultant (and staff) see them
    forward = models.ForeignKey('SubmittalForward', null=True, blank=True, on_delete=models.CASCADE,
                                related_name='file_rows')
    file = models.FileField(upload_to=upload_submittal)
    original_name = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return self.original_name or self.file.name
