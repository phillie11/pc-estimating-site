"""
Interim applications for payment (AP-001) from us to the client, and the Construction Act cycle
around each: the client's payment notice and pay less notice, and payments received.

On core.models.RecordBase (see README "Building a new record type"); status changes go through
payments.workflow. Drafts are internal; submitting shares it with the project's clients (never
consultants or subcontractors) and freezes its figures and dates. The money and date rules are
pure functions in payments/rules.py, held to fixed tables. Advisory: check your contract.
"""
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_SHARED, RecordBase
from tenancy.models import TenantModel

from . import rules

MONEY = dict(max_digits=14, decimal_places=2)
ZERO = Decimal('0.00')


def upload_payment_evidence(instance, filename):
    """Private path, as projects.models._private_path: org_<id>/payments/<random>/<name>."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/payments/{uuid.uuid4().hex}/{name}'


class Application(RecordBase):
    STATUS_DRAFT = 'DRAFT'
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_NOTIFIED = 'NOTIFIED'
    STATUS_PAY_LESS = 'PAY_LESS'
    STATUS_PAID = 'PAID'
    STATUS_WITHDRAWN = 'WITHDRAWN'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_SUBMITTED, 'Submitted to client'),
        (STATUS_NOTIFIED, 'Certified'),
        (STATUS_PAY_LESS, 'Pay less notice given'),
        (STATUS_PAID, 'Paid'),
        (STATUS_WITHDRAWN, 'Withdrawn'),
    ]
    TONES = {STATUS_DRAFT: 'muted', STATUS_SUBMITTED: 'accent', STATUS_NOTIFIED: 'info', STATUS_PAY_LESS: 'warn',
             STATUS_PAID: 'good', STATUS_WITHDRAWN: 'muted'}
    SHARED_STATUSES = (STATUS_SUBMITTED, STATUS_NOTIFIED, STATUS_PAY_LESS, STATUS_PAID)
    # Waiting to be paid: payments can be recorded, and the final date can go overdue
    AWAITING_PAYMENT = (STATUS_SUBMITTED, STATUS_NOTIFIED, STATUS_PAY_LESS)
    # Count towards "previous amounts" and "certified to date"
    COUNTED = SHARED_STATUSES

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    valuation_date = models.DateField(help_text='The valuation (NEC: assessment) date this application is made up to.')
    work_done = models.DecimalField('Work done to date', **MONEY, default=ZERO,
                                    validators=[MinValueValidator(Decimal('0'))],
                                    help_text='The value of work done to date, cumulative. Leave it if you enter lines '
                                              'by section: it becomes their total.')
    variations_value = models.DecimalField('Approved variations to date', **MONEY, default=ZERO, editable=False)
    materials_on_site = models.DecimalField('Materials on site', **MONEY, default=ZERO,
                                            validators=[MinValueValidator(Decimal('0'))])
    materials_off_site = models.DecimalField('Materials off site', **MONEY, default=ZERO,
                                             validators=[MinValueValidator(Decimal('0'))],
                                             help_text='Only where the contract allows it (JCT: listed items; NEC: if the Contract Data '
                                                       'say so). Advisory: check your contract.')
    notes = models.TextField('Notes for the client', blank=True,
                             help_text='Shown on the application and its PDF once submitted.')

    # Terms when it was made up (copied from the contract; frozen on submission)
    retention_percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, editable=False)
    retention_released_at_pc_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('50'),
                                                           editable=False)
    retention_stage = models.CharField(max_length=16, choices=rules.STAGE_CHOICES, default=rules.STAGE_BEFORE_PC,
                                       editable=False)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=rules.VAT_STANDARD_RATE, editable=False)
    reverse_charge = models.BooleanField('Domestic reverse charge applies', default=False, editable=False)

    # The figures (rules.valuation), kept so the application reads the same after the contract changes
    gross_valuation = models.DecimalField(**MONEY, default=ZERO, editable=False)
    retention = models.DecimalField('Retention held', **MONEY, default=ZERO, editable=False)
    net_valuation = models.DecimalField(**MONEY, default=ZERO, editable=False)
    previous_certified = models.DecimalField('Less previous amounts', **MONEY, default=ZERO, editable=False)
    amount_due = models.DecimalField('Amount due this time', **MONEY, default=ZERO, editable=False)
    vat = models.DecimalField('VAT', **MONEY, default=ZERO, editable=False)
    total = models.DecimalField('Total including VAT', **MONEY, default=ZERO, editable=False)

    # The cycle (rules.payment_cycle), fixed on submission
    due_on = models.DateField('Due date', null=True, blank=True, editable=False)
    payment_notice_by = models.DateField(null=True, blank=True, editable=False)
    pay_less_by = models.DateField(null=True, blank=True, editable=False)
    final_date = models.DateField('Final date for payment', null=True, blank=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)

    # The client's payment notice
    payment_notice_amount = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    payment_notice_basis = models.TextField(blank=True, editable=False)
    payment_notice_on = models.DateField(null=True, blank=True, editable=False)
    payment_notice_given_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                                on_delete=models.SET_NULL, related_name='+', editable=False)
    payment_notice_on_behalf = models.BooleanField(default=False, editable=False)
    payment_notice_evidence = models.FileField(upload_to=upload_payment_evidence, blank=True, editable=False)

    # The client's pay less notice
    pay_less_amount = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    pay_less_basis = models.TextField(blank=True, editable=False)
    pay_less_on = models.DateField(null=True, blank=True, editable=False)
    pay_less_given_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='+', editable=False)
    pay_less_on_behalf = models.BooleanField(default=False, editable=False)
    pay_less_evidence = models.FileField(upload_to=upload_payment_evidence, blank=True, editable=False)

    paid_in_full_on = models.DateField(null=True, blank=True, editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'application for payment'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        if self.status in self.SHARED_STATUSES:
            self.visibility = VIS_SHARED
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('application_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return self.TONES.get(self.status, 'muted')

    @property
    def is_draft(self):
        return self.status == self.STATUS_DRAFT

    # --- the cycle ---------------------------------------------------------------
    @property
    def cycle(self):
        """The stored cycle once submitted; None for a draft (use live_cycle())."""
        if self.due_on is None:
            return None
        return rules.PaymentCycle(self.valuation_date, self.due_on, self.payment_notice_by, self.pay_less_by,
                                  self.final_date)

    def live_cycle(self, contract, claim_date=None):
        return rules.payment_cycle(self.valuation_date, rules.terms_for(contract), claim_date=claim_date)

    def set_cycle(self, cycle):
        self.due_on, self.payment_notice_by = cycle.due_date, cycle.payment_notice_by
        self.pay_less_by, self.final_date = cycle.pay_less_by, cycle.final_date

    # --- the figures ---------------------------------------------------------------
    def previous_applications(self):
        qs = Application.objects.filter(project_id=self.project_id, status__in=self.COUNTED,
                                        valuation_date__lt=self.valuation_date)
        return qs.exclude(pk=self.pk) if self.pk else qs

    def recalculate(self, contract):
        """
        Refresh the terms and figures from the contract, approved variations, any lines and the
        earlier applications. Only while it is a draft: submitting freezes them.
        """
        from variations.models import Variation
        if contract is not None:
            self.retention_percent = contract.retention_percent or ZERO
            self.retention_released_at_pc_percent = contract.retention_released_at_pc_percent
            self.reverse_charge = contract.vat_reverse_charge
            self.retention_stage = rules.retention_stage(self.valuation_date, contract.practical_completion_on,
                                                         contract.defects_period_months)
        else:
            self.retention_percent, self.reverse_charge = ZERO, False
            self.retention_stage = rules.STAGE_BEFORE_PC
        if self.pk:
            values = list(self.lines.values_list('value', flat=True))
            if values:
                self.work_done = rules.lines_total(values)
        self.variations_value = Variation.objects.filter(project_id=self.project_id,
                                                         status=Variation.STATUS_APPROVED) \
            .aggregate(s=Sum('approved_value'))['s'] or ZERO
        previous = sum((a.notified_sum for a in self.previous_applications()), ZERO)
        v = rules.valuation(self.work_done, self.variations_value, self.materials_on_site, self.materials_off_site,
                            self.retention_percent, self.retention_released_at_pc_percent, self.retention_stage,
                            previous, self.vat_rate, self.reverse_charge)
        self.gross_valuation, self.retention, self.net_valuation = v.gross, v.retention, v.net
        self.previous_certified, self.amount_due, self.vat, self.total = v.previous, v.amount_due, v.vat, v.total
        return v

    @property
    def retention_released(self):
        """Retention released by this application compared with the last one (a positive amount), else 0."""
        last = self.previous_applications().order_by('-valuation_date').first()
        if last is None or last.retention <= self.retention:
            return ZERO
        return last.retention - self.retention

    # --- notices and the notified sum ---------------------------------------------
    @property
    def notified(self):
        return rules.notified_sum(self.amount_due, self.payment_notice_amount, self.payment_notice_on,
                                  self.payment_notice_by, self.pay_less_amount, self.pay_less_on, self.pay_less_by)

    @property
    def notified_sum(self):
        """The sum due this time (excluding VAT) as the notices stand: drafts and withdrawn ones count nil."""
        if self.status not in self.COUNTED:
            return ZERO
        return self.notified[0]

    @property
    def notified_basis(self):
        basis = self.notified[1]
        if basis == rules.BASIS_DEFAULT and self.payment_notice_by and not self.stands_as_notified \
                and timezone.localdate() <= self.payment_notice_by:
            return f'the application, until a payment notice is given (due by {self.payment_notice_by:%d %b %Y})'
        return rules.BASIS_LABELS[basis]

    @property
    def notified_vat(self):
        return rules.vat_on(self.notified_sum, self.vat_rate, self.reverse_charge)

    @property
    def notified_total(self):
        return self.notified_sum + self.notified_vat

    @property
    def stands_as_notified(self):
        return self.status in self.AWAITING_PAYMENT and rules.stands_as_notified(
            timezone.localdate(), self.payment_notice_by, self.payment_notice_on)

    @property
    def is_certified(self):
        """Has a sum the client must pay: a notice was given, it is paid, or it stands by default."""
        return self.status in (self.STATUS_NOTIFIED, self.STATUS_PAY_LESS, self.STATUS_PAID) or self.stands_as_notified

    # --- payments ------------------------------------------------------------------
    @property
    def paid_total(self):
        cached = getattr(self, '_paid_total', None)
        if cached is None:
            cached = self.payments.aggregate(s=Sum('amount'))['s'] or ZERO
            self._paid_total = cached
        return cached

    @property
    def outstanding(self):
        if self.status not in self.COUNTED:
            return ZERO
        return rules.outstanding(self.notified_total, self.paid_total)

    @property
    def is_overdue(self):
        return self.status in self.AWAITING_PAYMENT and rules.is_payment_overdue(
            timezone.localdate(), self.final_date, self.outstanding)

    def clocks(self, today=None, words=None):
        today = today or timezone.localdate()
        submitted = timezone.localdate(self.submitted_at) if self.submitted_at else None
        return rules.payment_clocks(self.cycle, today, submitted_on=submitted, payment_notice_on=self.payment_notice_on,
                                    pay_less_on=self.pay_less_on, paid_in_full_on=self.paid_in_full_on, words=words)

    @property
    def pay_less_open(self):
        """A pay less notice can still be given today."""
        return (self.status in (self.STATUS_SUBMITTED, self.STATUS_NOTIFIED) and self.pay_less_by is not None
                and timezone.localdate() <= self.pay_less_by)


class ApplicationLine(TenantModel):
    """Work done to date by section, if the user wants lines; the application's work done is their total."""
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='lines')
    section = models.CharField(max_length=120)
    value = models.DecimalField('Value to date', **MONEY, validators=[MinValueValidator(Decimal('0'))])
    position = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ['position', 'pk']

    def __str__(self):
        return f'{self.section}: {self.value}'


class Payment(TenantModel):
    """Money received against an application. Several part payments are allowed."""
    application = models.ForeignKey(Application, on_delete=models.RESTRICT, related_name='payments')
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal('0.01')),
                                                      MaxValueValidator(Decimal('999999999999.99'))])
    received_on = models.DateField()
    reference = models.CharField('Payment reference', max_length=120, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['received_on', 'pk']

    def __str__(self):
        return f'{self.amount} on {self.received_on}'
