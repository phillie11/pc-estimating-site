"""
Meetings: site progress meetings, client meetings and pre-starts, their minutes and actions.

  AgendaTemplate     per company: the standard agenda for each kind of meeting. Seeded with UK
                     fit-out defaults on first use (DEFAULT_AGENDAS) and editable by owners and admins.
  Meeting (MTG-001)  core.models.RecordBase, always internal in the record index. Planned, then
                     minutes issued (emailed to attendees as a link), and can be corrected and
                     issued again. Externals see issued minutes only of meetings they're listed at.
                     Until the minutes are issued the ball is with the chair, due two days after the
                     meeting, so it is on their Needs me.
  Attendee           who was invited: a person with a login (staff or someone on the project), or
                     just a name and company. Present or sent apologies.
  AgendaItem         one heading on the agenda, with the minutes written under it. A heading can be
                     internal only (internal_only): it, its minutes and the actions under it are kept
                     from clients, consultants and subcontractors on the minutes page, in their
                     actions list, their Needs me and the minutes email (pending owner decision).
                     Agenda templates carry it as an "Internal:" prefix on the line.
  MeetingSettings    per company: how many days after a meeting the minutes are due (default 2).
  MeetingAction      (ACT-001) RecordBase: something someone must do, with an owner and a due date.
                     Open actions from earlier meetings of the same kind on the job are carried forward
                     into the next one (Meeting.brought_forward) until they're done. Once the minutes
                     are issued, an owner with a login has it on Needs me.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase
from tenancy.models import TenantModel

MINUTES_DUE_DAYS = 2  # the chair's Needs me: minutes due this many days after the meeting (MeetingSettings)
INTERNAL_PREFIX = 'Internal:'  # an agenda template line starting with this is an internal-only heading
HHMM = RegexValidator(r'^([01]\d|2[0-3]):[0-5]\d$', 'Enter a time like 09:30.')

PROGRESS, CLIENT, PRESTART, OTHER = 'PROGRESS', 'CLIENT', 'PRESTART', 'OTHER'
KIND_CHOICES = [(PROGRESS, 'Site progress meeting'), (CLIENT, 'Client meeting'), (PRESTART, 'Pre-start meeting'),
                (OTHER, 'Other meeting')]

# The agenda each company starts with (one item per line in the editor). Plain UK site wording.
DEFAULT_AGENDAS = {
    PROGRESS: ('Site progress meeting', [
        'Attendance and apologies', 'Previous minutes and actions', 'Health and safety',
        'Programme and progress (look-ahead)', 'Design and information: RFIs and drawings',
        'Procurement and deliveries', 'Subcontractors and labour', 'Quality and snagging',
        'Variations and instructions', 'Any other business', 'Date of next meeting']),
    CLIENT: ('Client meeting', [
        'Attendance and apologies', 'Previous minutes and actions', 'Health and safety',
        'Progress against the programme', 'Decisions needed from the client', 'Changes and early warnings',
        'Risks and issues', 'Handover and completion', 'Any other business', 'Date of next meeting']),
    PRESTART: ('Pre-start meeting', [
        'Introductions and contacts', 'Scope of works', 'Programme, sequence and look-ahead',
        'RAMS and site induction', 'Site rules, welfare and access', 'Permits to work',
        'Deliveries, storage and waste', 'Quality, inspections and hold points', 'Drawings and information',
        'Applications for payment', 'Any other business']),
}


def parse_agenda_line(line):
    """'Internal: Commercial' -> ('Commercial', True); 'Programme' -> ('Programme', False)."""
    text = line.strip()
    if text[:len(INTERNAL_PREFIX)].lower() == INTERNAL_PREFIX.lower():
        return text[len(INTERNAL_PREFIX):].strip(), True
    return text, False


class MeetingSettings(TenantModel):
    """One row per company; with no row, the defaults below apply (pending owner decision)."""
    minutes_due_days = models.PositiveSmallIntegerField(
        'Minutes due (days after the meeting)', default=MINUTES_DUE_DAYS,
        validators=[MinValueValidator(0), MaxValueValidator(14)],
        help_text="The chair has the minutes on their Needs me until they are issued, due this many days after "
                  'the meeting. 0 means the same day.')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='meetings_one_settings_per_org')]
        verbose_name_plural = 'meeting settings'

    def __str__(self):
        return 'Meeting settings'

    @classmethod
    def for_org(cls, org):
        # Pinned to the organisation the caller already holds
        return cls.all_objects.filter(organization_id=getattr(org, 'pk', org)).first() or cls(organization_id=getattr(org, 'pk', org))


class AgendaTemplate(TenantModel):
    kind = models.CharField('Kind of meeting', max_length=10, choices=KIND_CHOICES)
    name = models.CharField(max_length=120)
    items = models.TextField('Agenda items', help_text='One item per line, in order.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['kind', 'name']
        constraints = [models.UniqueConstraint(fields=['organization', 'kind'], name='meetings_one_agenda_per_kind')]

    def __str__(self):
        return self.name

    @property
    def item_list(self):
        return [heading for heading, _ in self.item_rows]

    @property
    def item_rows(self):
        """[(heading, internal_only)]: a line starting "Internal:" is a heading kept from people outside the company."""
        return [parse_agenda_line(line) for line in self.items.splitlines() if line.strip()]

    @classmethod
    def for_org(cls, org):
        """The company's agendas, creating any missing default ones (first use)."""
        have = {t.kind: t for t in cls.objects.all()}
        for kind, (name, items) in DEFAULT_AGENDAS.items():
            if kind not in have:
                have[kind] = cls.objects.create(organization=org, kind=kind, name=name, items='\n'.join(items))
        return have


class Meeting(RecordBase):
    STATUS_PLANNED, STATUS_ISSUED, STATUS_CANCELLED = 'PLANNED', 'ISSUED', 'CANCELLED'
    STATUS_CHOICES = [(STATUS_PLANNED, 'Minutes not issued'), (STATUS_ISSUED, 'Minutes issued'),
                      (STATUS_CANCELLED, 'Cancelled')]

    # How the Needs me page words it for the chair (registered as a record formatter in apps.py)
    NEEDS_ME_ACTION = 'Finish the minutes and issue them'
    NEEDS_ME_LABEL = 'Minutes to issue'

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PLANNED, db_index=True)
    kind = models.CharField('Kind of meeting', max_length=10, choices=KIND_CHOICES, default=PROGRESS)
    held_on = models.DateField('Date')
    starts_at = models.CharField('Time', max_length=5, blank=True, validators=[HHMM], help_text='For example 09:30.')
    location = models.CharField('Where', max_length=200, blank=True, help_text='Site office, Teams, client\'s office.')
    chair = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='Chaired by')
    notes = models.TextField('General notes', blank=True)
    issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+', editable=False)
    issue_count = models.PositiveSmallIntegerField(default=0, editable=False)
    brought_forward = models.ManyToManyField('MeetingAction', blank=True, related_name='carried_into',
                                             help_text='Open actions from earlier meetings, carried into this one.')

    class Meta(RecordBase.Meta):
        ordering = ['-held_on', '-number']
        indexes = [models.Index(fields=['organization', 'project', 'kind', 'held_on'], name='meeting_org_proj_kind')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL  # externals reach issued minutes through attendance, never the index
        if not self.title and self.held_on:
            self.title = self.default_title(self.kind, self.held_on)
        if self.held_on:
            self.due_date = self.minutes_due  # Needs me: the chair has the minutes to issue until they are
        if self._state.adding and self.ball_in_court_user_id is None:
            self.ball_in_court_user_id = self.chair_id
        return super().save(*args, **kwargs)

    @staticmethod
    def default_title(kind, day):
        return f'{dict(KIND_CHOICES)[kind]} {day:%a} {day.day} {day:%b %Y}'

    def get_absolute_url(self):
        return reverse('meeting_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {self.STATUS_PLANNED: 'warn', self.STATUS_ISSUED: 'good', self.STATUS_CANCELLED: 'muted'}[self.status]

    @property
    def is_issued(self):
        return self.status == self.STATUS_ISSUED

    @property
    def is_planned(self):
        return self.status == self.STATUS_PLANNED

    @property
    def minutes_due(self):
        """When the minutes are due: the company's MeetingSettings.minutes_due_days (default 2) after the meeting."""
        from datetime import timedelta
        if getattr(self, '_minutes_due_days', None) is None:
            from tenancy.context import get_current_org
            org_id = self.organization_id or getattr(get_current_org(), 'pk', None)  # a new meeting: not set yet
            self._minutes_due_days = MeetingSettings.for_org(org_id).minutes_due_days
        return self.held_on + timedelta(days=self._minutes_due_days)


class Attendee(TenantModel):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='attendees')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='+')
    name = models.CharField(max_length=150, blank=True)
    company = models.CharField(max_length=150, blank=True)
    present = models.BooleanField(default=True, help_text='Unticked: sent apologies.')

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['meeting', 'user'], name='meetings_attendee_once')]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.user.full_name if self.user_id else self.name


class AgendaItem(TenantModel):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='items')
    position = models.PositiveSmallIntegerField(default=0)
    heading = models.CharField(max_length=200)
    minutes = models.TextField(blank=True)
    internal_only = models.BooleanField(
        'Internal only', default=False,
        help_text='Kept from clients, consultants and subcontractors: the heading, its minutes and its actions.')

    class Meta:
        ordering = ['position', 'pk']

    def __str__(self):
        return self.heading


class MeetingAction(RecordBase):
    """
    An action from a meeting (ACT-001, numbered per job on core.models.RecordBase, so every change is in its
    audit trail). `owner` is who it was given to. The ball is in the owner's court only once the minutes are
    issued (services.issue), which is what puts it on their Needs me: through the record index for staff,
    and through meetings/actions.py for people outside the company (it stays internal in the index).
    """
    OPEN, DONE, DROPPED = 'OPEN', 'DONE', 'DROPPED'
    STATUS_CHOICES = [(OPEN, 'Open'), (DONE, 'Done'), (DROPPED, 'No longer needed')]
    # How the Needs me page words it (registered as a record formatter in apps.py)
    NEEDS_ME_ACTION = 'Do it, then mark it done'
    NEEDS_ME_LABEL = 'Meeting actions'

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    due_date = models.DateField('Due', null=True, blank=True)
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='actions',
                                help_text='The meeting it was raised at.')
    item = models.ForeignKey(AgendaItem, null=True, blank=True, on_delete=models.SET_NULL, related_name='actions',
                             verbose_name='Under agenda item')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='meeting_actions', verbose_name='Who')
    owner_name = models.CharField('Who (no login)', max_length=150, blank=True,
                                  help_text='Someone without a login, e.g. "Client\'s M&E designer".')
    update = models.TextField('Latest update', blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')

    class Meta(RecordBase.Meta):
        ordering = ['meeting__held_on', 'number']
        verbose_name = 'meeting action'
        # The external Needs me hook: open actions owned by someone, within the company
        indexes = [models.Index(fields=['organization', 'owner', 'status'], name='meeting_action_owner')]

    def __str__(self):
        return f'{self.reference} {self.title[:60]}'

    def get_absolute_url(self):
        return reverse('meeting_detail', args=[self.organization.slug, self.project_id, self.meeting_id]) \
            + f'#action-{self.pk}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL  # externals reach their own through the meetings hook and page
        if self.project_id is None and self.meeting_id:
            self.project_id = self.meeting.project_id
        return super().save(*args, **kwargs)

    @property
    def who(self):
        return self.owner.full_name if self.owner_id else (self.owner_name or 'Not given to anyone')

    @property
    def is_overdue(self):
        return self.status == self.OPEN and self.due_date is not None and self.due_date < timezone.localdate()

    @property
    def status_tone(self):
        if self.is_overdue:
            return 'bad'
        return {self.OPEN: 'warn', self.DONE: 'good', self.DROPPED: 'muted'}[self.status]

    @property
    def status_label(self):
        return 'Overdue' if self.is_overdue else self.get_status_display()
