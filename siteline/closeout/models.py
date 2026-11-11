"""
Close-out: practical completion, the defects period, the O&M manual and the golden thread
handover pack.

  CompletionSection   Practical completion for the whole works or one section of it: the PC date,
                      the defects period (the contract's, unless this section says otherwise), and
                      when the defects were certified made good. The whole-works date is copied to
                      contracts.MainContract.practical_completion_on, which payments reads.
  Defects             Not a model: a defect is a projects.Issue with issue_type DEFECT. A snag raised
                      on or after whole-works PC becomes one (closeout.signals). The fix-and-verify
                      flow (subcontractor marks it ready, the PM closes it) is the snag flow.
  OMTemplateSection   The company's O&M manual contents: one section per trade and what goes in it.
                      Seeded from DEFAULT_TEMPLATE on first use, editable in Settings.
  OMSection           One section of this job's manual, copied from the template, with the trade
                      package that supplies it.
  OMRequest           Links a section to a procurement.Submittal of type "O&M information": the
                      subcontractor uploads it in their portal and we give it A, B or C, exactly as
                      submittals already work. Accepted (A or B) submissions go in the manual.
  OMDocument          A file we add ourselves (our own H&S file notes, a supplier's emailed data).
  OMCompilation       One compiled manual: a zip with an indexed contents PDF, built by a job.
  Certificate         Certificates and test results (electrical, fire alarm, emergency lighting...).
  HandoverPack        A golden thread handover pack: a frozen manifest of every item handed over,
  HandoverItem        each a link to the exact stored file plus its SHA-256 checksum.

Who sees what is in closeout/access.py.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from projects.models import _private_path
from tenancy.models import TenantModel

from . import rules


def upload_closeout(instance, filename):
    return _private_path(instance, filename, 'closeout')


# ---------------------------------------------------------------------------
# Practical completion and the defects period
# ---------------------------------------------------------------------------

class CloseoutSettings(TenantModel):
    """One row per company; with no row, the defaults below apply (pending owner decision)."""
    defect_give_out_days = models.PositiveSmallIntegerField(
        'Give out client-reported defects within (working days)', default=rules.DEFECT_GIVE_OUT_DAYS,
        validators=[MaxValueValidator(20)],
        help_text='A defect the client reports is on the project manager\'s Needs me until it is given to whoever '
                  'will fix it, due this many working days after it was reported. 0 means the same day.')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='closeout_one_settings_per_org')]
        verbose_name_plural = 'close-out settings'

    def __str__(self):
        return 'Close-out settings'

    @classmethod
    def for_org(cls, org):
        # Pinned to the organisation the caller already holds
        return cls.all_objects.filter(organization=org).first() or cls(organization=org)


class CompletionSection(TenantModel):
    WHOLE_WORKS = 'Whole of the works'

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='completion_sections')
    name = models.CharField('Section', max_length=120, default=WHOLE_WORKS,
                            help_text='"Whole of the works", or a section such as "Ground floor bar".')
    is_whole_works = models.BooleanField(default=True, editable=False)
    pc_date = models.DateField('Practical completion date', null=True, blank=True,
                               help_text='The date on the certificate (Completion under NEC).')
    certificate = models.FileField('Completion certificate', upload_to=upload_closeout, blank=True)
    defects_period_months = models.PositiveSmallIntegerField(
        'Defects period (months)', null=True, blank=True, validators=[MaxValueValidator(60)],
        help_text="Leave blank to use the contract's.")
    made_good_on = models.DateField(
        'Defects made good on', null=True, blank=True,
        help_text='The date on the certificate of making good (JCT) or the defects certificate (NEC).')
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        ordering = ['-is_whole_works', 'pc_date', 'name', 'pk']
        constraints = [models.UniqueConstraint(fields=['project', 'name'], name='closeout_section_name_unique')]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('closeout_overview', args=[self.organization.slug, self.project_id]) + f'#section-{self.pk}'

    def contract(self):
        from contracts.models import MainContract
        cache = getattr(self.project, '_closeout_contract', False)
        if cache is False:
            cache = MainContract.objects.filter(project_id=self.project_id).first()
            self.project._closeout_contract = cache
        return cache

    @property
    def months(self):
        """The defects period in months: this section's, else the contract's, else None."""
        if self.defects_period_months:
            return self.defects_period_months
        contract = self.contract()
        return contract.defects_period_months if contract else None

    @property
    def months_from_contract(self):
        return not self.defects_period_months and self.months is not None

    @property
    def defects_end(self):
        return rules.defects_end(self.pc_date, self.months)

    @property
    def reminder_from(self):
        return rules.reminder_from(self.defects_end)

    def state(self, today=None):
        """(label, tone) in plain words."""
        today = today or timezone.localdate()
        if self.pc_date is None:
            return 'Not yet complete', 'muted'
        if self.pc_date > today:
            return 'Completion expected', 'info'
        if self.made_good_on:
            return 'Defects made good', 'good'
        end = self.defects_end
        if end is None:
            return 'In defects period (no end date)', 'warn'
        if end < today:
            return 'Defects period ended: certify making good', 'bad'
        if rules.reminder_due(end, None, today):
            return 'Defects period ends soon', 'warn'
        return 'In defects period', 'accent'

    @property
    def status_label(self):
        return self.state()[0]

    @property
    def status_tone(self):
        return self.state()[1]


def whole_works_pc(project):
    """The whole-works PC date for a project, or None."""
    return CompletionSection.all_objects.filter(
        organization_id=project.organization_id, project_id=project.pk, is_whole_works=True) \
        .values_list('pc_date', flat=True).first()


# ---------------------------------------------------------------------------
# O&M manual
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE = [
    ('Project information and contacts', '', 'Project directory: client, designers, contractor and subcontractors\n'
                                             'Description of the works\nKey dates: completion and the defects period'),
    ('Health and safety file', '', 'Residual hazards and how to manage them\nHazardous materials used\n'
                                   'Safe access for cleaning and maintenance'),
    ('As-built drawings', '', 'Final drawings at their current revision'),
    ('Fire strategy and fire stopping', 'Fire protection', 'Fire-stopping register and photos\n'
                                                         'Product data and installer certificates'),
    ('Partitions and linings', 'Drylining & partitions', 'System data sheets\nFire and acoustic test evidence\n'
                                                         'Warranties'),
    ('Ceilings', 'Suspended ceilings', 'Product data\nAccess and cleaning guidance\nWarranties'),
    ('Joinery', 'Joinery', 'Materials and finishes schedule\nCare and maintenance\nWarranties'),
    ('Doors and ironmongery', '', 'Door schedule\nFire door certificates\n'
                                                     'Ironmongery maintenance'),
    ('Floor finishes', 'Flooring', 'Product data\nCleaning and maintenance regime\nWarranties'),
    ('Decorations', 'Decorating', 'Paint and finishes schedule (colours and references)\nProduct data sheets'),
    ('Mechanical services', 'Mechanical', 'Plant schedule\nCommissioning records\nMaintenance schedule\n'
                                          'Manufacturers\' literature and warranties'),
    ('Electrical services', 'Electrical', 'Electrical installation certificate\nEmergency lighting and fire '
                                          'alarm certificates\nCircuit charts\nManufacturers\' literature'),
]


class OMTemplateSection(TenantModel):
    title = models.CharField('Section', max_length=120)
    trade = models.CharField('Usually supplied by (trade)', max_length=80, blank=True,
                             help_text='Matched to the trade package on each job, so the right subcontractor is asked.')
    contents = models.TextField('What goes in it', blank=True, help_text='One item per line.')
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.title

    @classmethod
    def for_org(cls, org):
        """The company's template, seeded from DEFAULT_TEMPLATE the first time it is used."""
        rows = list(cls.objects.all())
        if rows:
            return rows
        cls.objects.bulk_create([cls(organization=org, title=t, trade=trade, contents=c, order=(i + 1) * 10)
                                 for i, (t, trade, c) in enumerate(DEFAULT_TEMPLATE)])
        return list(cls.objects.all())


class OMSection(TenantModel):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='om_sections')
    title = models.CharField('Section', max_length=120)
    contents = models.TextField('What goes in it', blank=True, help_text='One item per line.')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='om_sections', verbose_name='Supplied by',
                                help_text='The appointed subcontractor who provides this section. Leave blank if '
                                          'we provide it ourselves.')
    not_needed = models.BooleanField('Not needed on this job', default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('closeout_om', args=[self.organization.slug, self.project_id]) + f'#om-{self.pk}'

    @property
    def content_lines(self):
        return [x.strip() for x in self.contents.splitlines() if x.strip()]


class OMRequest(TenantModel):
    """One O&M submittal asked for against a section. The submittal does the work; this says which section."""
    section = models.ForeignKey(OMSection, on_delete=models.CASCADE, related_name='requests')
    submittal = models.OneToOneField('procurement.Submittal', on_delete=models.CASCADE, related_name='om_request')

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return f'{self.section}: {self.submittal}'


class OMDocument(TenantModel):
    section = models.ForeignKey(OMSection, on_delete=models.CASCADE, related_name='documents')
    title = models.CharField('What it is', max_length=200)
    file = models.FileField(upload_to=upload_closeout)
    original_name = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return self.title


class OMCompilation(TenantModel):
    QUEUED, READY, FAILED = 'QUEUED', 'READY', 'FAILED'
    STATUS_CHOICES = [(QUEUED, 'Being put together'), (READY, 'Ready to download'), (FAILED, 'Could not be built')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='om_compilations')
    number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=QUEUED)
    file = models.FileField(upload_to=upload_closeout, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    file_count = models.PositiveIntegerField(default=0)
    left_out = models.JSONField(default=list, blank=True,
                                help_text='Files left out (still being virus-checked, or blocked).')
    shared_with_client = models.BooleanField('Shared with the client', default=False)
    error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    manifest = models.JSONField(default=list, blank=True, help_text='[[section number, title, [[path in the zip, '
                                'document title, size, sha256], ...]], ...] as compiled, so the one-PDF version '
                                'holds exactly what the zip holds.')
    # The same manual as one merged PDF (closeout/merge.py), made on request by the worker
    MERGE_QUEUED, MERGE_READY, MERGE_FAILED = 'QUEUED', 'READY', 'FAILED'
    merged_status = models.CharField(max_length=8, blank=True, choices=[
        ('', 'Not made'), (MERGE_QUEUED, 'Being put together'), (MERGE_READY, 'Ready to download'),
        (MERGE_FAILED, 'Could not be built')], default='')
    merged_file = models.FileField(upload_to=upload_closeout, blank=True)
    merged_size = models.PositiveBigIntegerField(default=0)
    merged_pages = models.PositiveIntegerField(default=0)
    merged_skipped = models.JSONField(default=list, blank=True,
                                      help_text='[[section, file, why], ...] left out of the one-PDF version.')
    merged_error = models.CharField(max_length=300, blank=True)
    merged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')
    merged_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['-number']

    def __str__(self):
        return f'O&M manual, version {self.number}'

    @property
    def can_merge(self):
        return self.status == self.READY and bool(self.manifest)

    @property
    def status_tone(self):
        return {self.QUEUED: 'info', self.READY: 'good', self.FAILED: 'bad'}[self.status]


# ---------------------------------------------------------------------------
# Certificates and test results
# ---------------------------------------------------------------------------

class Certificate(TenantModel):
    KIND_CHOICES = [
        ('ELECTRICAL', 'Electrical installation certificate'),
        ('FIRE_ALARM', 'Fire alarm certificate'),
        ('EMERGENCY_LIGHTING', 'Emergency lighting certificate'),
        ('FIRE_DOORS', 'Fire door inspection'),
        ('FIRE_STOPPING', 'Fire-stopping certificate'),
        ('WATER', 'Water hygiene / legionella'),
        ('GAS', 'Gas safety'),
        ('COMMISSIONING', 'Commissioning and test results'),
        ('AIR', 'Air tightness or ventilation test'),
        ('BUILDING_CONTROL', 'Building control completion certificate'),
        ('WARRANTY', 'Warranty or guarantee'),
        ('OTHER', 'Other certificate or test result'),
    ]
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='closeout_certificates')
    kind = models.CharField('Type', max_length=20, choices=KIND_CHOICES, default='ELECTRICAL')
    title = models.CharField('Title', max_length=200, blank=True,
                             help_text='Leave blank to use the type, e.g. "Electrical installation certificate".')
    issued_by = models.CharField('Issued by', max_length=200, blank=True, help_text='Company or body.')
    issued_on = models.DateField('Date on it', null=True, blank=True)
    file = models.FileField(upload_to=upload_closeout)
    original_name = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['kind', '-issued_on', 'pk']

    def __str__(self):
        return self.label

    @property
    def label(self):
        return self.title or self.get_kind_display()


# ---------------------------------------------------------------------------
# Golden thread handover pack
# ---------------------------------------------------------------------------

class HandoverPack(TenantModel):
    BUILDING, READY, ISSUED, FAILED = 'BUILDING', 'READY', 'ISSUED', 'FAILED'
    STATUS_CHOICES = [(BUILDING, 'Working out checksums'), (READY, 'Ready to check and issue'),
                      (ISSUED, 'Issued to the client'), (FAILED, 'Could not be built')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='handover_packs')
    number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=BUILDING)
    note = models.TextField('Note to go with it', blank=True)
    manifest_sha256 = models.CharField(max_length=64, blank=True,
                                       help_text='Checksum of the manifest itself, so a copy can be checked.')
    error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['-number']

    def __str__(self):
        return f'Handover pack {self.number}'

    def get_absolute_url(self):
        return reverse('closeout_pack', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {self.BUILDING: 'info', self.READY: 'accent', self.ISSUED: 'good', self.FAILED: 'bad'}[self.status]


class HandoverItem(TenantModel):
    DRAWING, FIRE_STOPPING, OM, CERTIFICATE, COMPLETION = 'DRAWING', 'FIRE_STOPPING', 'OM', 'CERTIFICATE', 'COMPLETION'
    CATEGORY_CHOICES = [(COMPLETION, 'Completion certificates'), (DRAWING, 'Drawings at current revision'),
                        (FIRE_STOPPING, 'Fire-stopping register'), (OM, 'O&M manual'),
                        (CERTIFICATE, 'Certificates and test results')]
    OK, WAITING, BLOCKED, MISSING = 'OK', 'WAITING', 'BLOCKED', 'MISSING'
    STATE_CHOICES = [(OK, 'Checked'), (WAITING, 'Waiting for virus check'), (BLOCKED, 'Blocked by virus check'),
                     (MISSING, 'File missing')]

    pack = models.ForeignKey(HandoverPack, on_delete=models.CASCADE, related_name='items')
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES)
    title = models.CharField(max_length=255)
    reference = models.CharField(max_length=120, blank=True)
    revision = models.CharField(max_length=20, blank=True)
    # The exact stored file handed over (the same storage name as its source row, never a copy)
    file = models.FileField(upload_to=upload_closeout, max_length=500)
    file_name = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=8, choices=STATE_CHOICES, default=WAITING)
    order = models.PositiveIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.title

    @property
    def status_tone(self):
        return {self.OK: 'good', self.WAITING: 'info', self.BLOCKED: 'bad', self.MISSING: 'bad'}[self.state]

    @property
    def status_label(self):
        return self.get_state_display()
