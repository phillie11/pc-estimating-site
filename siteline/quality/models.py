"""
Quality on a live job: inspections against checklists, permits to work and the fire-stopping register.

  QualitySettings     per company: permit defaults (validity, warning before expiry, fire watch) and
                      whether the checklist library has been set up.
  ChecklistTemplate   the company's checklist library (inspection and test plans), one per trade or
  TemplateItem        element, seeded from quality/library.py on first use and editable.
  Inspection (INS-)   a checklist run on an area of a job: core.RecordBase, always internal. The
  InspectionItem      template's checks are copied in when it starts, so editing the library never
                      changes an inspection already done. Each check is pass, fail or N/A, with a
                      note and photos; a failed check can raise a snag (projects.Issue) in one tap.
  Permit (PTW-)       a permit to work: requested, approved, active until it expires, closed.
                      Whether it is active, expiring or expired is worked out from the clock
                      (quality/rules.py), never stored, so it is always right.
  Penetration (FS-)   one fire-stopping penetration: where (optionally pinned on a drawing), what
                      passes through, the rating, the system or product, who installed it, before
                      and after photos and the inspection sign-off.
  QualityPhoto        a photo on an inspection check or a penetration. Files are only served by
                      quality.views through projects.files.serve().

Nothing here is shared with clients or consultants. An appointed subcontractor sees, in the portal,
the permits on their own packages (and can ask for one) and the fire-stopping on their own packages
(and can record it for sign-off): quality/portal.py, with its own access rules. The record index is
never what serves them, so the record types are registered with external_roles=().
"""
import datetime
import os
import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase
from tenancy.models import TenantModel

from . import rules


def upload_quality_photo(instance, filename):
    """org_<id>/quality-photos/<random>/<name>, like every private file."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/quality-photos/{uuid.uuid4().hex}/{name}'


class QualitySettings(TenantModel):
    """One row per company; with no row, the defaults below apply."""
    permit_hours = models.PositiveSmallIntegerField(
        'Usual permit length (hours)', default=8, validators=[MinValueValidator(1), MaxValueValidator(168)],
        help_text='A new permit runs out this long after it starts, unless the person asking changes it.')
    expiry_warning_hours = models.PositiveSmallIntegerField(
        'Warn before a permit runs out (hours)', default=2, validators=[MinValueValidator(1), MaxValueValidator(48)],
        help_text='An approved permit shows as "Expiring soon" this long before it runs out. It is on Needs me '
                  'all day on the day it runs out.')
    fire_watch_minutes = models.PositiveSmallIntegerField(
        'Fire watch after hot works (minutes)', default=60, validators=[MinValueValidator(0), MaxValueValidator(480)],
        help_text='How long someone watches the area after hot works stop. Check your insurer\'s conditions.')
    fs_independent_sign_off = models.BooleanField(
        'Someone else signs off fire-stopping', default=True,
        help_text='Whoever recorded a seal as done can\'t sign it off; another person on the job inspects it. '
                  'Untick to let them sign off their own.')
    library_seeded = models.BooleanField(default=False, editable=False)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='quality_one_settings_per_org')]
        verbose_name_plural = 'quality settings'

    def __str__(self):
        return 'Quality settings'

    @classmethod
    def for_org(cls, org):
        # Pinned to the organisation the caller already holds
        return cls.all_objects.filter(organization=org).first() or cls(organization=org)


# --- checklist library -----------------------------------------------------------------

class ChecklistTemplate(TenantModel):
    name = models.CharField('Checklist name', max_length=120, help_text='e.g. Partitions: before boarding.')
    trade = models.CharField(max_length=80, blank=True, help_text='e.g. Drylining, Ceilings, Electrical.')
    description = models.TextField('When to use it', blank=True)
    is_active = models.BooleanField('In use', default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        ordering = ['trade', 'name']

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('quality_template', args=[self.organization.slug, self.pk])


class TemplateItem(TenantModel):
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, related_name='items')
    order = models.PositiveSmallIntegerField(default=0)
    text = models.CharField('Check', max_length=300)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.text


# --- inspections -----------------------------------------------------------------------

class Inspection(RecordBase):
    IN_PROGRESS, PASSED, FAILED, CANCELLED = 'IN_PROGRESS', 'PASSED', 'FAILED', 'CANCELLED'
    STATUS_CHOICES = [(IN_PROGRESS, 'In progress'), (PASSED, 'Passed'), (FAILED, 'Failed'),
                      (CANCELLED, 'Cancelled')]
    OPEN_STATUSES = (IN_PROGRESS,)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=IN_PROGRESS, db_index=True)
    template = models.ForeignKey(ChecklistTemplate, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='inspections', verbose_name='Checklist')
    area = models.CharField('Area or location', max_length=200, blank=True,
                            help_text='e.g. Level 2 east corridor, Kitchen.')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+', verbose_name='Whose work',
                                help_text='The subcontractor whose work this checks. Snags raised go to them.')
    notes = models.TextField('General notes', blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'inspection'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('inspection_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {self.IN_PROGRESS: 'warn', self.PASSED: 'good', self.FAILED: 'bad',
                self.CANCELLED: 'muted'}[self.status]

    @property
    def is_overdue(self):
        return self.status == self.IN_PROGRESS and self.due_date is not None and self.due_date < timezone.localdate()


class InspectionItem(TenantModel):
    RESULT_CHOICES = [(rules.PASS, 'Pass'), (rules.FAIL, 'Fail'), (rules.NA, 'N/A')]

    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name='items')
    order = models.PositiveSmallIntegerField(default=0)
    text = models.CharField('Check', max_length=300)
    result = models.CharField(max_length=4, choices=RESULT_CHOICES, blank=True)
    note = models.TextField(blank=True)
    answered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    answered_at = models.DateTimeField(null=True, blank=True)
    snag = models.ForeignKey('projects.Issue', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.text

    @property
    def result_tone(self):
        return {rules.PASS: 'good', rules.FAIL: 'bad', rules.NA: 'muted'}.get(self.result, 'warn')

    @property
    def result_label(self):
        return self.get_result_display() if self.result else 'Not checked'


# --- permits to work -------------------------------------------------------------------

class Permit(RecordBase):
    STATUS_CHOICES = [(rules.REQUESTED, 'Waiting for approval'), (rules.APPROVED, 'Approved'),
                      (rules.CLOSED, 'Closed'), (rules.REFUSED, 'Refused'), (rules.WITHDRAWN, 'Withdrawn')]
    OPEN_STATUSES = (rules.REQUESTED, rules.APPROVED)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=rules.REQUESTED, db_index=True)
    permit_type = models.CharField('Type of permit', max_length=10, choices=rules.PERMIT_TYPE_CHOICES)
    location = models.CharField('Where', max_length=200, help_text='e.g. Level 3 plant room, kitchen ceiling void.')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+', verbose_name='Subcontractor doing the work')
    holder = models.CharField('Person in charge of the work', max_length=150, blank=True,
                              help_text='The name of whoever holds the permit on site.')
    starts_at = models.DateTimeField('Work starts')
    expires_at = models.DateTimeField('Permit runs out')
    method_notes = models.TextField('Anything else the approver should know', blank=True,
                                    help_text='Equipment, RAMS reference, other trades nearby.')
    precautions = models.JSONField(default=list, blank=True, editable=False,
                                   help_text='The precautions the approver confirmed were in place.')
    extra_precautions = models.TextField('Extra precautions', blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+', editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    refusal_reason = models.TextField(blank=True, editable=False)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', editable=False)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    close_note = models.TextField(blank=True, editable=False)
    fire_watch_minutes = models.PositiveSmallIntegerField(null=True, blank=True, editable=False,
                                                          help_text='Hot works: the fire watch confirmed at closing.')

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'permit to work'
        verbose_name_plural = 'permits to work'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL
        # Due: approve it by the day work starts; once approved, close it by the day it runs out
        if self.status == rules.APPROVED and self.expires_at:
            self.due_date = timezone.localdate(self.expires_at)
        elif self.status == rules.REQUESTED and self.starts_at:
            self.due_date = timezone.localdate(self.starts_at)
        else:
            self.due_date = None
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('permit_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def type_label(self):
        return rules.PERMIT_TYPES[self.permit_type]['label']

    @property
    def is_hot_works(self):
        return self.permit_type == rules.HOT_WORKS

    def warning_hours(self):
        if getattr(self, '_warning_hours', None) is None:
            self._warning_hours = QualitySettings.for_org(self.organization).expiry_warning_hours
        return self._warning_hours

    def state_at(self, now=None):
        return rules.permit_state(self.status, self.starts_at, self.expires_at, now or timezone.now(),
                                  self.warning_hours())

    @property
    def state(self):
        return self.state_at()

    @property
    def state_label(self):
        return rules.STATE_LABELS[self.state][0]

    @property
    def status_tone(self):
        return rules.STATE_LABELS[self.state][1]

    @property
    def status_label(self):
        return self.state_label

    def can_withdraw_at(self, now=None):
        """Withdraw only before work starts: once it has started, the permit is closed (area safe, fire watch)."""
        if self.status == rules.REQUESTED:
            return True
        return self.status == rules.APPROVED and (now or timezone.now()) < self.starts_at

    @property
    def can_withdraw(self):
        return self.can_withdraw_at()

    @property
    def who(self):
        company = str(self.package.subcontractor) if self.package_id else ''
        return ', '.join(x for x in (self.holder, company) if x)

    def precaution_list(self):
        """The type's precautions, each with whether the approver confirmed it."""
        confirmed = set(self.precautions or [])
        return [(text, text in confirmed) for text in rules.precautions_for(self.permit_type)]


# --- fire-stopping ---------------------------------------------------------------------

class Penetration(RecordBase):
    TO_SEAL, SEALED, SIGNED_OFF, FAILED = 'TO_SEAL', 'SEALED', 'SIGNED_OFF', 'FAILED'
    STATUS_CHOICES = [(TO_SEAL, 'To seal'), (SEALED, 'Sealed, to inspect'), (SIGNED_OFF, 'Signed off'),
                      (FAILED, 'Failed inspection')]
    OPEN_STATUSES = (SEALED, FAILED)

    ELEMENT_CHOICES = [('WALL', 'Wall'), ('FLOOR', 'Floor'), ('CEILING', 'Ceiling'), ('RISER', 'Riser or shaft'),
                       ('CAVITY', 'Cavity barrier'), ('OTHER', 'Other')]
    RATING_CHOICES = [(30, '30 minutes'), (60, '60 minutes'), (90, '90 minutes'), (120, '120 minutes'),
                      (240, '240 minutes')]
    SERVICE_CHOICES = [('PIPE_METAL', 'Metal pipe'), ('PIPE_PLASTIC', 'Plastic pipe'), ('CABLE', 'Cables'),
                       ('CABLE_TRAY', 'Cable tray or basket'), ('DUCT', 'Ductwork'), ('MIXED', 'Mixed services'),
                       ('GAP', 'Gap or joint (no service)'), ('OTHER', 'Other')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=TO_SEAL, db_index=True)
    location = models.CharField('Where', max_length=200, help_text='Level, room and wall or grid, e.g. L2, riser 1, north wall.')
    element = models.CharField('Through a', max_length=8, choices=ELEMENT_CHOICES, default='WALL')
    fire_rating = models.PositiveSmallIntegerField('Fire rating', choices=RATING_CHOICES, null=True, blank=True,
                                                   help_text='The rating of the wall or floor it passes through.')
    service = models.CharField('What passes through', max_length=12, choices=SERVICE_CHOICES, blank=True)
    size = models.CharField(max_length=80, blank=True, help_text='e.g. 110 mm pipe, 300 x 100 mm opening.')
    system = models.CharField('System or product', max_length=200, blank=True,
                              help_text='The manufacturer\'s product and detail used, e.g. intumescent pipe collar.')
    detail_reference = models.CharField('Manufacturer\'s detail reference', max_length=120, blank=True)
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+', verbose_name='Installed by (subcontractor)')
    installer = models.CharField('Installer\'s name', max_length=150, blank=True)
    installed_on = models.DateField('Sealed on', null=True, blank=True)
    notes = models.TextField(blank=True)
    # Pinned on a drawing (the viewer pin pattern: viewer.pins.pending_pin / placement)
    drawing = models.ForeignKey('projects.Document', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+')
    drawing_revision = models.ForeignKey('projects.DocumentRevision', null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name='+')
    pin_page = models.PositiveIntegerField(null=True, blank=True)
    pin_x = models.DecimalField(max_digits=7, decimal_places=6, null=True, blank=True)
    pin_y = models.DecimalField(max_digits=7, decimal_places=6, null=True, blank=True)

    sealed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', editable=False,
                                  help_text='Who last marked it sealed or resealed.')
    inspected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', editable=False)
    inspected_at = models.DateTimeField(null=True, blank=True, editable=False)
    inspection_note = models.TextField(blank=True, editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'fire-stopping penetration'
        constraints = [
            models.CheckConstraint(condition=models.Q(pin_x__isnull=True) | models.Q(pin_x__gte=0, pin_x__lte=1),
                                   name='quality_pin_x_on_page'),
            models.CheckConstraint(condition=models.Q(pin_y__isnull=True) | models.Q(pin_y__gte=0, pin_y__lte=1),
                                   name='quality_pin_y_on_page'),
        ]

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('penetration_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {self.TO_SEAL: 'warn', self.SEALED: 'accent', self.SIGNED_OFF: 'good', self.FAILED: 'bad'}[self.status]

    def default_title(self):
        """A short name from what passes through what: '110 mm plastic pipe through a 60 minute wall'."""
        what = ' '.join(x for x in (self.size, self.get_service_display().lower() if self.service else '') if x)
        what = what or 'Penetration'
        through = self.get_element_display().lower()
        rated = f'{self.fire_rating} minute ' if self.fire_rating else ''
        article = 'an' if (rated or through)[:1] in 'aeiou' else 'a'
        return f'{what[:1].upper()}{what[1:]} through {article} {rated}{through}'[:200]

    @property
    def is_pinned(self):
        return self.drawing_id is not None and self.pin_page is not None

    @property
    def installed_by(self):
        company = str(self.package.subcontractor) if self.package_id else ''
        return ', '.join(x for x in (self.installer, company) if x)


class QualityPhoto(TenantModel):
    BEFORE, AFTER, OTHER = 'BEFORE', 'AFTER', ''
    STAGE_CHOICES = [(BEFORE, 'Before'), (AFTER, 'After'), (OTHER, 'Photo')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='quality_photos')
    item = models.ForeignKey(InspectionItem, null=True, blank=True, on_delete=models.CASCADE, related_name='photos')
    penetration = models.ForeignKey(Penetration, null=True, blank=True, on_delete=models.CASCADE,
                                    related_name='photos')
    stage = models.CharField(max_length=6, choices=STAGE_CHOICES, blank=True)
    image = models.FileField(upload_to=upload_quality_photo)
    display = models.FileField(upload_to=upload_quality_photo, blank=True)
    thumbnail = models.FileField(upload_to=upload_quality_photo, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['uploaded_at', 'pk']
        constraints = [models.CheckConstraint(
            condition=models.Q(item__isnull=False, penetration__isnull=True)
            | models.Q(item__isnull=True, penetration__isnull=False), name='quality_photo_one_record')]

    def __str__(self):
        return self.original_name or f'Photo {self.pk}'

    def file_url(self, size='thumb'):
        return reverse('quality_photo_file', args=[self.organization.slug, self.project_id, self.pk, size])

    @property
    def thumb_url(self):
        return self.file_url('thumb')

    @property
    def display_url(self):
        return self.file_url('display')


def default_expiry(start, hours):
    return start + datetime.timedelta(hours=hours)
