"""
Early warnings and change events: the internal front end of the change chain.

  RFI / site issue / instruction / client request / early warning
      -> ChangeEvent (CE-001): what might change cost or time, priced as cost and revenue lines
      -> client Variation (VO-001) and the subcontractor variations (SV-001) it creates

Both are core.models.RecordBase record types (see docs/adr/0001 and the README checklist) and
are always INTERNAL: clients and subcontractors never see them, in any list or in the record
index. Links to the RFI or early warning a change event came from, and to the client variation
it becomes, are core RecordLinks. Subcontractor variations aren't on the record base yet, so
they point back with SubVariation.change_event until they move (ADR migration plan step 2).
"""
from decimal import Decimal

from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase
from tenancy.models import TenantModel


class InternalRecord(RecordBase):
    """Never shared: whatever is set, a save keeps it internal."""

    class Meta(RecordBase.Meta):
        abstract = True

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL
        return super().save(*args, **kwargs)


class EarlyWarning(InternalRecord):
    SIDE_CONTRACTOR, SIDE_CLIENT = 'CONTRACTOR', 'CLIENT'
    SIDE_CHOICES = [(SIDE_CONTRACTOR, 'Us (contractor)'), (SIDE_CLIENT, 'Client side')]
    STATUS_OPEN, STATUS_CLOSED, STATUS_ESCALATED = 'OPEN', 'CLOSED', 'ESCALATED'
    STATUS_CHOICES = [(STATUS_OPEN, 'Open'), (STATUS_CLOSED, 'Closed'), (STATUS_ESCALATED, 'Escalated')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    description = models.TextField('What could happen', help_text='The matter, and what could cause it.')
    raised_by_side = models.CharField('Raised by', max_length=10, choices=SIDE_CHOICES, default=SIDE_CONTRACTOR)
    risk_cost = models.BooleanField('Could increase cost', default=False)
    risk_time = models.BooleanField('Could delay completion', default=False)
    risk_quality = models.BooleanField('Could affect quality', default=False)
    mitigation = models.TextField('How to avoid or reduce it', blank=True)
    meeting_date = models.DateField('Risk reduction meeting', null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'early warning'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('early_warning_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'OPEN': 'warn', 'CLOSED': 'muted', 'ESCALATED': 'accent'}[self.status]

    @property
    def risks(self):
        return [label for flag, label in ((self.risk_cost, 'cost'), (self.risk_time, 'time'),
                                          (self.risk_quality, 'quality')) if flag]


class ChangeEvent(InternalRecord):
    ORIGIN_CHOICES = [
        ('RFI', 'RFI'), ('INSTRUCTION', 'Instruction'), ('SITE_ISSUE', 'Site issue'),
        ('CLIENT_REQUEST', 'Client request'), ('EARLY_WARNING', 'Early warning'),
    ]
    STATUS_IDENTIFIED, STATUS_NOTIFIED, STATUS_QUOTED = 'IDENTIFIED', 'NOTIFIED', 'QUOTED'
    STATUS_AGREED, STATUS_REJECTED, STATUS_IMPLEMENTED = 'AGREED', 'REJECTED', 'IMPLEMENTED'
    STATUS_CHOICES = [
        (STATUS_IDENTIFIED, 'Identified'), (STATUS_NOTIFIED, 'Notified'), (STATUS_QUOTED, 'Quoted'),
        (STATUS_AGREED, 'Agreed'), (STATUS_REJECTED, 'Rejected'), (STATUS_IMPLEMENTED, 'Implemented'),
    ]
    LIVE_STATUSES = (STATUS_IDENTIFIED, STATUS_NOTIFIED, STATUS_QUOTED, STATUS_AGREED)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_IDENTIFIED, db_index=True)
    origin = models.CharField(max_length=16, choices=ORIGIN_CHOICES, default='INSTRUCTION')
    description = models.TextField('What changed', help_text='What happened or was asked for, and by whom.')
    time_impact_days = models.IntegerField('Time impact (days)', null=True, blank=True,
                                           help_text='Likely delay to completion, if any.')

    # Dates the advisory clocks run from (changes.clocks). All optional.
    aware_on = models.DateField('Became aware on', null=True, blank=True,
                                help_text='When we first knew about it. NEC: the 8-week notification clock starts here.')
    notified_on = models.DateField('Notified to the client side on', null=True, blank=True)
    quotation_instructed_on = models.DateField('Quotation instructed on', null=True, blank=True)
    quotation_submitted_on = models.DateField('Quotation submitted on', null=True, blank=True)
    reply_received_on = models.DateField('Reply received on', null=True, blank=True)
    failure_notified_on = models.DateField('Told them they have not replied on', null=True, blank=True,
                                           help_text='NEC: the date you notified the Project Manager that the reply '
                                                     'is late. Deemed acceptance runs 2 weeks from here.')
    delay_notice_given_on = models.DateField('Notice of delay given on', null=True, blank=True,
                                             help_text='JCT: written notice that progress is or is likely to be delayed.')

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'change event'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('change_event_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return {'IDENTIFIED': 'warn', 'NOTIFIED': 'accent', 'QUOTED': 'info', 'AGREED': 'good', 'REJECTED': 'bad',
                'IMPLEMENTED': 'muted'}[self.status]

    def _total(self, kind):
        # Uses prefetched lines when the caller prefetched them
        return sum((line.amount for line in self.lines.all() if line.kind == kind), start=Decimal('0.00'))

    @property
    def cost_total(self):
        return self._total(ChangeEventLine.KIND_COST)

    @property
    def revenue_total(self):
        return self._total(ChangeEventLine.KIND_REVENUE)

    @property
    def margin(self):
        return self.revenue_total - self.cost_total

    @property
    def is_time_impacting(self):
        return bool(self.time_impact_days and self.time_impact_days > 0)


class ChangeEventLine(TenantModel):
    """One estimated cost (what it costs us) or revenue (what we recover from the client) line."""
    KIND_COST, KIND_REVENUE = 'COST', 'REVENUE'
    KIND_CHOICES = [(KIND_COST, 'Cost'), (KIND_REVENUE, 'Revenue')]

    change_event = models.ForeignKey(ChangeEvent, on_delete=models.CASCADE, related_name='lines')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_COST)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    trade = models.ForeignKey('directory.Trade', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['kind', 'pk']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.description} {self.amount}'
