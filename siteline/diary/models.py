"""
Site diary: one entry per job per day, what was on site that day, and site photos.

  DiaryEntry (DY-001)  core.models.RecordBase, always internal. One per project per date.
                       Locked after its day and the next working day (diary/rules.py); the project
                       manager or an admin unlocks it, with a reason, through the `unlock`
                       transition. Every change, including adding or removing a line below,
                       goes through entry.save(actor=...) so it is in the audit trail.
  Lines                labour, plant, deliveries, visitors, instructions and delays, each a
                       TenantModel row under its entry.
  SitePhoto            a photo on the job, attached to an entry or straight to the job. Internal
                       unless marked "share with client", which shows it, read only, on the
                       client's project overview. Files are only served by diary.views.
  DiarySettings        per company: whether Saturday counts as a working day.
"""
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase
from tenancy.models import TenantModel

from . import rules


def upload_photo(instance, filename):
    """org_<id>/site-photos/<random>/<name>, like every private file (projects.models._private_path)."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/site-photos/{uuid.uuid4().hex}/{name}'


HHMM = RegexValidator(r'^([01]\d|2[0-3]):[0-5]\d$', 'Enter a time like 07:30.')

WEATHER_CHOICES = [
    ('DRY', 'Dry'), ('SUNNY', 'Sunny'), ('OVERCAST', 'Overcast'), ('SHOWERS', 'Showers'), ('RAIN', 'Rain'),
    ('HEAVY_RAIN', 'Heavy rain'), ('WIND', 'High wind'), ('SNOW', 'Snow or ice'), ('HOT', 'Very hot'),
    ('FOG', 'Fog'),
]


class DiaryEntry(RecordBase):
    STATUS_OPEN, STATUS_UNLOCKED = 'OPEN', 'UNLOCKED'
    STATUS_CHOICES = [(STATUS_OPEN, 'Recorded'), (STATUS_UNLOCKED, 'Unlocked for correction')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    entry_date = models.DateField('Date')

    weather_am = models.CharField('Weather (morning)', max_length=12, choices=WEATHER_CHOICES, blank=True)
    weather_pm = models.CharField('Weather (afternoon)', max_length=12, choices=WEATHER_CHOICES, blank=True)
    weather_note = models.CharField('Weather note', max_length=200, blank=True,
                                    help_text='Temperature, rain stopping work, wind too high for the hoist.')
    # Kept as 'HH:MM' text rather than TimeField so the audit trail (JSON) can record changes to them
    site_start = models.CharField('Site opened', max_length=5, blank=True, validators=[HHMM])
    site_finish = models.CharField('Site closed', max_length=5, blank=True, validators=[HHMM])
    own_staff_count = models.PositiveSmallIntegerField('Our own people on site', default=0)
    own_staff = models.TextField('Who (our staff)', blank=True, help_text='Names and roles, e.g. site manager, labourer.')
    hs_incident = models.BooleanField('Accident, incident or near miss today', default=False)
    hs_notes = models.TextField('Health and safety notes', blank=True,
                                help_text='Toolbox talks, inspections, permits. If there was an incident, what happened.')
    notes = models.TextField('General notes', blank=True)

    unlocked_until = models.DateField(null=True, blank=True, editable=False)
    unlock_reason = models.TextField(blank=True, editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-entry_date']
        verbose_name = 'site diary entry'
        verbose_name_plural = 'site diary entries'
        constraints = [models.UniqueConstraint(fields=['project', 'entry_date'], name='diary_one_entry_per_day')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    @staticmethod
    def title_for(day):
        return f'Site diary {day:%a} {day.day} {day:%b %Y}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL  # never shared: only photos marked for the client go outside
        if not self.title and self.entry_date:
            self.title = self.title_for(self.entry_date)
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('diary_day', args=[self.organization.slug, self.project_id, self.entry_date.isoformat()])

    # --- locking ---------------------------------------------------------------------
    @property
    def written_on(self):
        return timezone.localdate(self.created_at) if self.created_at else timezone.localdate()

    @property
    def calendar(self):
        """(Saturday counts?, bank holiday region) for this job; set by the caller for a page of entries."""
        if getattr(self, '_calendar', None) is None:
            self._calendar = calendar_for(self.project)
        return self._calendar

    def active_unlock(self):
        return self.unlocked_until if self.status == self.STATUS_UNLOCKED else None

    def is_locked(self, today=None):
        return rules.is_locked(self.entry_date, self.written_on, today or timezone.localdate(), self.active_unlock(),
                               *self.calendar)

    @property
    def locked(self):
        return self.is_locked()

    @property
    def normal_edit_until(self):
        return rules.edit_until(self.entry_date, self.written_on, *self.calendar)

    @property
    def editable_until(self):
        return max(self.normal_edit_until, self.active_unlock() or self.entry_date)

    @property
    def written_late(self):
        return rules.is_written_late(self.entry_date, self.written_on, *self.calendar)

    @property
    def lock_label(self):
        today = timezone.localdate()
        if self.is_locked(today):
            return 'Locked', 'muted'
        if self.active_unlock() and today > self.normal_edit_until:
            return 'Unlocked', 'warn'
        return 'Open', 'good'

    @property
    def status_tone(self):
        return self.lock_label[1]

    # --- totals ----------------------------------------------------------------------
    @property
    def sub_headcount(self):
        return sum(line.headcount for line in self.labour.all())

    @property
    def labour_total(self):
        return self.sub_headcount + (self.own_staff_count or 0)

    @property
    def hours_lost(self):
        return sum((d.hours_lost for d in self.delays.all()), start=Decimal('0'))


def calendar_for(project):
    """(Saturday counts?, bank holiday region) for a job: the company's diary setting and the contract's region."""
    from contracts.models import MainContract
    region = MainContract.objects.filter(project=project).values_list('bank_holiday_region', flat=True).first()
    return DiarySettings.for_org(project.organization).saturday_counts, region or None


class LineBase(TenantModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+',
                                   editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta(TenantModel.Meta):
        abstract = True
        ordering = ['pk']


class LabourLine(LineBase):
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='labour')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                verbose_name='Subcontractor on this job')
    name = models.CharField('Or type a name', max_length=200, blank=True,
                            help_text='A subcontractor or labour agency not on the list.')
    trade = models.CharField(max_length=100, blank=True)
    headcount = models.PositiveSmallIntegerField('People on site', default=1)
    notes = models.CharField('Working on', max_length=200, blank=True)

    def __str__(self):
        return f'{self.name}{f" ({self.trade})" if self.trade else ""}: {self.headcount}'

    def save(self, *args, **kwargs):
        # Keep the name and trade as they were that day, even if the package changes later
        if self.package_id and not self.name:
            self.name = self.package.subcontractor.company_name
        if self.package_id and not self.trade and self.package.trade_id:
            self.trade = self.package.trade.name
        super().save(*args, **kwargs)


class PlantLine(LineBase):
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='plant')
    description = models.CharField('Plant or equipment', max_length=200)
    quantity = models.PositiveSmallIntegerField(default=1)
    supplier = models.CharField('Hired from', max_length=200, blank=True)
    notes = models.CharField('Note', max_length=200, blank=True, help_text='Arrived, off-hired, broken down, idle.')

    def __str__(self):
        return f'{self.quantity} x {self.description}'


class DeliveryLine(LineBase):
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='deliveries')
    supplier = models.CharField(max_length=200)
    description = models.CharField('What arrived', max_length=200)
    arrived_at = models.TimeField('Time', null=True, blank=True)
    notes = models.CharField('Note', max_length=200, blank=True, help_text='Short, damaged, delivery note number.')

    def __str__(self):
        return f'{self.supplier}: {self.description}'


class VisitorLine(LineBase):
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='visitors')
    name = models.CharField(max_length=150)
    company = models.CharField(max_length=150, blank=True)
    reason = models.CharField('Reason for visit', max_length=200, blank=True)
    arrived_at = models.TimeField('Time', null=True, blank=True)

    def __str__(self):
        return f'{self.name}{f", {self.company}" if self.company else ""}'


class InstructionLine(LineBase):
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='instructions')
    given_by = models.CharField('From', max_length=150, help_text='Who gave it, e.g. the client\'s project manager.')
    description = models.TextField('Instruction')
    rfi = models.ForeignKey('rfis.RFI', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                            verbose_name='Related RFI')
    change_event = models.ForeignKey('changes.ChangeEvent', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', verbose_name='Related change event')

    def __str__(self):
        return f'From {self.given_by}: {self.description[:60]}'


class DelayLine(LineBase):
    CAUSE_CHOICES = [
        ('WEATHER', 'Weather'),
        ('INFORMATION', 'Late or missing information'),
        ('CLIENT', 'Client change or instruction'),
        ('ACCESS', 'No access or area not ready'),
        ('OTHERS', 'Client\'s other contractors'),
        ('SERVICES', 'Utilities or services'),
        ('STATUTORY', 'Statutory body or inspection'),
        ('MATERIALS', 'Late or wrong materials'),
        ('LABOUR', 'Labour shortage'),
        ('PLANT', 'Plant breakdown'),
        ('OTHER', 'Other'),
    ]
    entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='delays')
    cause = models.CharField(max_length=12, choices=CAUSE_CHOICES)
    description = models.TextField('What happened', help_text='What stopped or slowed work, where, and who it affected.')
    hours_lost = models.DecimalField(max_digits=5, decimal_places=1, help_text='Working hours lost, e.g. 3 or 1.5.')
    trades_affected = models.CharField('Trades affected', max_length=200, blank=True)

    def __str__(self):
        return f'{self.get_cause_display()}: {self.hours_lost} h'


class SitePhoto(TenantModel):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='site_photos')
    entry = models.ForeignKey(DiaryEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='photos')
    image = models.FileField(upload_to=upload_photo)
    display = models.FileField(upload_to=upload_photo, blank=True, help_text='Resized, location data removed.')
    thumbnail = models.FileField(upload_to=upload_photo, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    caption = models.CharField(max_length=200, blank=True)
    area = models.CharField('Area or location', max_length=120, blank=True, help_text='e.g. Kitchen, Level 2 corridor.')
    taken_on = models.DateField(default=timezone.localdate, db_index=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)
    shared_with_client = models.BooleanField('Share with client', default=False)
    shared_at = models.DateTimeField(null=True, blank=True)
    shared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')

    class Meta(TenantModel.Meta):
        ordering = ['-taken_on', '-uploaded_at', '-pk']
        indexes = [models.Index(fields=['project', 'taken_on'])]

    def __str__(self):
        return self.caption or self.original_name or f'Photo {self.pk}'

    def file_url(self, size='thumb'):
        return reverse('diary_photo_file', args=[self.organization.slug, self.project_id, self.pk, size])

    @property
    def thumb_url(self):
        return self.file_url('thumb')

    @property
    def display_url(self):
        return self.file_url('display')


class DiarySettings(TenantModel):
    """One row per company; with no row, the defaults below apply."""
    saturday_counts = models.BooleanField(
        'Saturday is a working day', default=False,
        help_text='Expect a diary entry for Saturdays too, and chase it on Monday morning if it is missing.')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='diary_one_settings_per_org')]
        verbose_name_plural = 'diary settings'

    def __str__(self):
        return 'Saturday counts' if self.saturday_counts else 'Monday to Friday'

    @classmethod
    def for_org(cls, org):
        # Pinned to the organisation the caller already holds
        return cls.all_objects.filter(organization=org).first() or cls(organization=org)


def entry_totals(entries):
    """Labour, delay hours and photo counts for a page of entries, in three queries."""
    ids = [e.pk for e in entries]
    labour = dict(LabourLine.objects.filter(entry_id__in=ids).values('entry_id').annotate(n=Sum('headcount'))
                  .values_list('entry_id', 'n'))
    delays = {}
    for row in DelayLine.objects.filter(entry_id__in=ids).values('entry_id').annotate(h=Sum('hours_lost'),
                                                                                       n=models.Count('pk')):
        delays[row['entry_id']] = (row['n'], row['h'])
    photos = dict(SitePhoto.objects.filter(entry_id__in=ids).values('entry_id').annotate(n=models.Count('pk'))
                  .values_list('entry_id', 'n'))
    for e in entries:
        e.total_labour = (labour.get(e.pk) or 0) + (e.own_staff_count or 0)
        e.delay_count, e.delay_hours = delays.get(e.pk, (0, Decimal('0')))
        e.photo_count = photos.get(e.pk, 0)
    return entries
