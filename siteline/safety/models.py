"""
Safety on a job: incidents and near misses, the CDM register and QR site sign-in.

  Incident (INC-001)  core.models.RecordBase, always internal. Reported from a phone in under a
                      minute (what, where, when, photo, anyone hurt), then investigated and
                      closed by the project manager (safety/workflow.py). A RIDDOR category
                      gives the reporting deadline (safety/rules.py, advisory).
  IncidentAction      something to do after an incident, with an owner and a due date. Open
                      ones go on the owner's Needs me.
  IncidentPhoto       photos of the scene, resized like site photos (diary/photos.py).
  CDMProfile          one per job: notifiable (F10) and its reference, key dates.
  DutyHolder          client, principal designer, principal contractor and others on the job.
  PhasePlan           the construction phase plan, uploaded and versioned (v1, v2, ...).
  SiteGate            one per job: the secret token behind the QR poster. Replacing it stops
                      old posters working.
  SiteAttendance      one sign-in: a person with an account, or a visitor's name and company.
"""
import os
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase
from tenancy.models import TenantModel

from . import rules


def _private(instance, folder, filename):
    """org_<id>/<folder>/<random>/<name>, like every private file (projects.models._private_path)."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/{folder}/{uuid.uuid4().hex}/{name}'


def upload_incident_photo(instance, filename):
    return _private(instance, 'incident-photos', filename)


def upload_phase_plan(instance, filename):
    return _private(instance, 'phase-plans', filename)


# --- incidents ----------------------------------------------------------------------------

class Incident(RecordBase):
    STATUS_REPORTED, STATUS_INVESTIGATING, STATUS_CLOSED = 'REPORTED', 'INVESTIGATING', 'CLOSED'
    STATUS_CHOICES = [(STATUS_REPORTED, 'Reported'), (STATUS_INVESTIGATING, 'Being investigated'),
                      (STATUS_CLOSED, 'Closed')]
    OPEN_STATUSES = (STATUS_REPORTED, STATUS_INVESTIGATING)

    KIND_NEAR_MISS, KIND_INJURY = 'NEAR_MISS', 'INJURY'
    KIND_CHOICES = [
        (KIND_NEAR_MISS, 'Near miss (nobody hurt)'),
        (KIND_INJURY, 'Injury'),
        ('ILL_HEALTH', 'Ill health'),
        ('DAMAGE', 'Damage to property or equipment'),
        ('DANGEROUS', 'Dangerous occurrence'),
        ('UNSAFE', 'Unsafe act or condition'),
        ('ENVIRONMENT', 'Environmental (spill, dust, noise)'),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REPORTED, db_index=True)
    kind = models.CharField('What kind', max_length=12, choices=KIND_CHOICES, default=KIND_NEAR_MISS)
    description = models.TextField('What happened')
    location = models.CharField('Where', max_length=200, help_text='e.g. First floor kitchen, by the goods lift.')
    occurred_at = models.DateTimeField('When', default=timezone.now)
    anyone_hurt = models.BooleanField('Was anyone hurt?', default=False)

    # The injured person (when someone was hurt)
    injured_name = models.CharField('Name of the person hurt', max_length=150, blank=True)
    injured_company = models.CharField('Who they work for', max_length=150, blank=True)
    injured_is_worker = models.BooleanField('They were working on the job', default=True,
                                            help_text='Untick for a member of the public or a visitor.')
    injury_details = models.TextField('Injury', blank=True, help_text='What the injury is and which part of the body.')
    first_aid_given = models.BooleanField('First aid given', default=False)
    taken_to_hospital = models.BooleanField('Went to hospital', default=False)
    days_off = models.PositiveSmallIntegerField(
        'Days off work or on light duties', null=True, blank=True,
        help_text='Not counting the day it happened. Leave blank if you don\'t know yet.')

    # RIDDOR (safety/rules.py; advisory)
    riddor_category = models.CharField('RIDDOR', max_length=10, choices=rules.RIDDOR_CHOICES, blank=True)
    riddor_reported_on = models.DateField('Reported to HSE on', null=True, blank=True)
    riddor_reference = models.CharField('HSE reference', max_length=60, blank=True)

    # Investigation and closure
    immediate_action = models.TextField('What was done straight away', blank=True)
    root_cause = models.TextField('Why it happened', blank=True)
    findings = models.TextField('Findings and lessons learned', blank=True,
                                help_text='Needed before the incident can be closed.')
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-occurred_at', '-pk']
        verbose_name = 'incident or near miss'
        verbose_name_plural = 'incidents and near misses'
        indexes = [models.Index(fields=['organization', 'occurred_at'], name='safety_inc_org_when')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('incident_detail', args=[self.organization.slug, self.project_id, self.pk])

    @staticmethod
    def title_from(description):
        first = (description or '').strip().splitlines()[0] if (description or '').strip() else 'Incident'
        return first if len(first) <= 80 else first[:77].rstrip() + '...'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL  # never shared with the client or subcontractors
        if not self.title:
            self.title = self.title_from(self.description)
        return super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def status_tone(self):
        tones = {self.STATUS_REPORTED: 'warn', self.STATUS_INVESTIGATING: 'info', self.STATUS_CLOSED: 'muted'}
        return tones[self.status]

    @property
    def occurred_on(self):
        return timezone.localdate(self.occurred_at) if self.occurred_at else None

    # --- RIDDOR -------------------------------------------------------------------------
    @property
    def riddor_reportable(self):
        return rules.is_reportable(self.riddor_category)

    @property
    def riddor_deadline(self):
        return rules.riddor_deadline(self.riddor_category, self.occurred_on)

    @property
    def riddor_outstanding(self):
        return self.riddor_reportable and self.riddor_reported_on is None

    @property
    def riddor_guidance(self):
        return rules.riddor_guidance(self.riddor_category, self.occurred_on)

    @property
    def riddor_suggestion(self):
        """A category the injury details point to, when none has been picked yet."""
        if self.riddor_category:
            return ''
        return rules.suggest_riddor(self.anyone_hurt, self.injured_is_worker, self.taken_to_hospital, self.days_off)

    @property
    def riddor_suggestion_label(self):
        return dict(rules.RIDDOR_CHOICES).get(self.riddor_suggestion, '')

    @property
    def riddor_label(self):
        """(words, tone) for the RIDDOR column."""
        if not self.riddor_reportable:
            return ('Record only', 'muted') if self.riddor_category == rules.RIDDOR_OVER_3 else ('', '')
        if self.riddor_reported_on:
            return 'RIDDOR reported', 'good'
        deadline = self.riddor_deadline
        if deadline and deadline < timezone.localdate():
            return 'RIDDOR overdue', 'bad'
        return 'RIDDOR to report', 'bad'


class IncidentAction(TenantModel):
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name='actions')
    description = models.CharField('What needs doing', max_length=300)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='Who will do it')
    due_date = models.DateField('Do it by', null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+',
                                   editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta(TenantModel.Meta):
        ordering = ['done_at', 'due_date', 'pk']
        indexes = [models.Index(fields=['owner', 'done_at'], name='safety_action_owner_done')]

    def __str__(self):
        return self.description

    @property
    def is_done(self):
        return self.done_at is not None

    @property
    def status_label(self):
        if self.is_done:
            return 'Done', 'good'
        if self.due_date and self.due_date < timezone.localdate():
            return 'Overdue', 'bad'
        return 'To do', 'warn'


class IncidentPhoto(TenantModel):
    """Field names match diary.SitePhoto so diary/photos.py can make the copies."""
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name='photos')
    image = models.FileField(upload_to=upload_incident_photo)
    display = models.FileField(upload_to=upload_incident_photo, blank=True)
    thumbnail = models.FileField(upload_to=upload_incident_photo, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    caption = models.CharField(max_length=200, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['uploaded_at', 'pk']

    def __str__(self):
        return self.caption or self.original_name or f'Photo {self.pk}'

    def file_url(self, size='thumb'):
        i = self.incident
        return reverse('incident_photo', args=[self.organization.slug, i.project_id, i.pk, self.pk, size])


# --- CDM ----------------------------------------------------------------------------------

class CDMProfile(TenantModel):
    NOTIFIABLE_CHOICES = [('', 'Not checked yet'), ('YES', 'Yes, notifiable to HSE'), ('NO', 'No, not notifiable')]

    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='cdm')
    notifiable = models.CharField('Notifiable to HSE (F10)', max_length=3, choices=NOTIFIABLE_CHOICES, blank=True)
    working_days = models.PositiveIntegerField('Construction phase length (working days)', null=True, blank=True)
    peak_workers = models.PositiveIntegerField('Most people on site at once', null=True, blank=True)
    person_days = models.PositiveIntegerField('Total person-days', null=True, blank=True,
                                              help_text='Every person on site for every day, added up.')
    f10_reference = models.CharField('F10 reference', max_length=60, blank=True,
                                     help_text='The reference HSE gives the notification.')
    f10_submitted_on = models.DateField('F10 sent to HSE on', null=True, blank=True)
    pci_received_on = models.DateField('Pre-construction information received', null=True, blank=True)
    cpp_in_place_on = models.DateField('Construction phase plan in place', null=True, blank=True,
                                       help_text='Fill this in if the plan is kept somewhere else.')
    hs_file_due_on = models.DateField('Health and safety file due to the client', null=True, blank=True)
    hs_file_issued_on = models.DateField('Health and safety file handed over', null=True, blank=True)
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', editable=False)
    updated_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta(TenantModel.Meta):
        verbose_name = 'CDM register'

    def __str__(self):
        return f'CDM register for {self.project_id}'

    @property
    def notifiable_suggestion(self):
        return rules.is_notifiable(self.working_days, self.peak_workers, self.person_days)

    @classmethod
    def for_project(cls, project):
        return cls.objects.filter(project=project).first() or cls(project=project)


class DutyHolder(TenantModel):
    CLIENT, PD, PC = 'CLIENT', 'PD', 'PC'
    ROLE_CHOICES = [
        (CLIENT, 'Client'),
        (PD, 'Principal designer'),
        (PC, 'Principal contractor'),
        ('DESIGNER', 'Designer'),
        ('CONTRACTOR', 'Contractor'),
        ('ADVISER', 'Client\'s CDM or safety adviser'),
    ]
    KEY_ROLES = (CLIENT, PD, PC)

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='duty_holders')
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    company = models.CharField('Company', max_length=200)
    contact_name = models.CharField('Contact', max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    appointed_on = models.DateField('Appointed in writing on', null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return f'{self.get_role_display()}: {self.company}'


class PhasePlan(TenantModel):
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='phase_plans')
    version = models.PositiveIntegerField(editable=False)
    file = models.FileField(upload_to=upload_phase_plan)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    note = models.CharField('What changed', max_length=300, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['-version']
        constraints = [models.UniqueConstraint(fields=['project', 'version'], name='safety_one_plan_version')]

    def __str__(self):
        return f'Construction phase plan v{self.version}'

    def get_absolute_url(self):
        return reverse('phase_plan_file', args=[self.organization.slug, self.project_id, self.pk])


# --- site sign-in --------------------------------------------------------------------------

def new_gate_token():
    return secrets.token_urlsafe(24)


class SiteGate(TenantModel):
    """The QR poster's secret. Whoever has the link can sign in to this job; nothing else."""
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='site_gate')
    token = models.CharField(max_length=64, unique=True, default=new_gate_token, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    replaced_at = models.DateTimeField(null=True, blank=True, editable=False)
    replaced_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+', editable=False)

    def __str__(self):
        return f'Sign-in poster for {self.project_id}'

    @classmethod
    def for_project(cls, project):
        gate = cls.objects.filter(project=project).first()
        if gate is None:
            gate, _ = cls.objects.get_or_create(project=project)
        return gate

    def replace_token(self, user):
        self.token, self.replaced_at, self.replaced_by = new_gate_token(), timezone.now(), user
        self.save(update_fields=['token', 'replaced_at', 'replaced_by'])

    def public_path(self):
        return reverse('site_signin', args=[self.token])


class SiteAttendance(TenantModel):
    KIND_STAFF, KIND_COLLABORATOR, KIND_VISITOR = 'STAFF', 'COLLABORATOR', 'VISITOR'
    KIND_CHOICES = [(KIND_STAFF, 'Our staff'), (KIND_COLLABORATOR, 'Client, consultant or subcontractor'),
                    (KIND_VISITOR, 'Visitor')]
    METHOD_QR, METHOD_MANUAL = 'QR', 'MANUAL'
    METHOD_CHOICES = [(METHOD_QR, 'QR code'), (METHOD_MANUAL, 'Signed in by the site team')]

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='site_attendance')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='+')
    name = models.CharField('Name', max_length=150)
    company = models.CharField('Company', max_length=150, blank=True)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_VISITOR)
    method = models.CharField(max_length=8, choices=METHOD_CHOICES, default=METHOD_QR)
    signed_in_at = models.DateTimeField(default=timezone.now)
    signed_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', help_text='Who signed them in, if not themselves.')
    signed_out_at = models.DateTimeField(null=True, blank=True)
    signed_out_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+', help_text='Who signed them out, if not themselves.')

    class Meta(TenantModel.Meta):
        ordering = ['-signed_in_at', '-pk']
        indexes = [models.Index(fields=['project', 'signed_in_at'], name='safety_att_proj_in'),
                   models.Index(fields=['project', 'user', 'signed_out_at'], name='safety_att_proj_user')]

    def __str__(self):
        return f'{self.name}{f" ({self.company})" if self.company else ""}'

    @property
    def is_on_site(self):
        return self.signed_out_at is None

    @property
    def hours_on_site(self):
        end = self.signed_out_at or timezone.now()
        return round((end - self.signed_in_at).total_seconds() / 3600, 1)


def on_site_now(project, now=None):
    """Everyone signed in since midnight today and not signed out, earliest first."""
    since = rules.on_site_since(timezone.localtime(now or timezone.now()))
    return SiteAttendance.objects.filter(project=project, signed_in_at__gte=since, signed_out_at__isnull=True) \
        .order_by('signed_in_at', 'pk')
