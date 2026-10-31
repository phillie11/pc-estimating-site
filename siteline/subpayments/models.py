"""
Paying subcontractors: we are the payer.

    SubPaymentSettings   per company: the default subcontract payment terms
    SubcontractorTax     per subcontractor: CIS status, verification, VAT rate and reverse charge
                         (owners and admins edit; kept here so directory.Subcontractor is unchanged)
    Subcontract          the payment terms on one appointed package (from the company defaults, editable)
    SubApplication       the sub's application for payment (SA-001, on core.RecordBase) and our
                         payment notice and pay less notice on it
    SubPayment           money we paid against an application, with its CIS and VAT frozen; posts an
                         ACTUAL cost to costs.ledger at the gross excluding VAT, before CIS; reversed,
                         never deleted

The rules are pure functions in subpayments/rules.py. Advisory: check your contract.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from core.models import RecordBase
from holidays.calendar import DEFAULT_REGION, REGION_CHOICES
from payments.rules import PaymentCycle
from tenancy.models import TenantModel

from . import rules

MONEY = dict(max_digits=14, decimal_places=2)
PCT = dict(max_digits=5, decimal_places=2, validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])
ZERO = Decimal('0.00')
DAYS = dict(validators=[MaxValueValidator(120)])


class TermsFields(models.Model):
    """The payment terms, shared by the company defaults and each subcontract."""
    cycle_kind = models.CharField('Payment cycle', max_length=8, choices=rules.CYCLE_CHOICES,
                                  default=rules.CYCLE_MONTHLY)
    valuation_day = models.PositiveSmallIntegerField(
        'Valuation day of the month', null=True, blank=True, default=25,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text='For a monthly cycle. Above 28 means the last day in shorter months.')
    due_date_offset_days = models.PositiveSmallIntegerField('Due date (days after the valuation date)', default=7, **DAYS)
    payment_notice_days = models.PositiveSmallIntegerField(
        'Our payment notice (days after the due date)', default=5, **DAYS,
        help_text='The Construction Act allows no more than 5.')
    final_date_offset_days = models.PositiveSmallIntegerField('Final date for payment (days after the due date)',
                                                              default=21, **DAYS)
    pay_less_notice_days = models.PositiveSmallIntegerField('Our pay less notice (days before the final date)',
                                                            default=5, **DAYS)
    retention_percent = models.DecimalField('Retention %', default=Decimal('5.00'), **PCT)
    retention_released_at_pc_percent = models.DecimalField(
        'Retention released at our practical completion %', default=Decimal('50.00'), **PCT,
        help_text='The rest is released at the end of the defects period.')
    discount_percent = models.DecimalField('Discount %', default=ZERO, **PCT,
                                           help_text='Leave at 0 unless the subcontract gives us a discount.')

    class Meta:
        abstract = True


class SubPaymentSettings(TermsFields, TenantModel):
    """One row per company: the terms every new subcontract starts from. See settings_for()."""
    class Meta(TenantModel.Meta):
        verbose_name_plural = 'sub payment settings'
        constraints = [models.UniqueConstraint(fields=['organization'], name='subpay_settings_one_per_org')]

    def __str__(self):
        return 'Subcontract payment defaults'


def settings_for(org, create=True):
    """The company's defaults; with create=False an unsaved row with the defaults (for GET pages: no writes)."""
    from django.db import IntegrityError, transaction
    obj = SubPaymentSettings.objects.filter(organization=org).first()
    if obj is None and not create:
        return SubPaymentSettings(organization=org)
    if obj is None:
        try:
            with transaction.atomic():
                obj = SubPaymentSettings.objects.create(organization=org)
        except IntegrityError:  # two first requests at once
            obj = SubPaymentSettings.objects.get(organization=org)
    return obj


class SubcontractorTax(TenantModel):
    """CIS and VAT facts for one subcontractor. Owners and admins only."""
    subcontractor = models.OneToOneField('directory.Subcontractor', on_delete=models.CASCADE, related_name='tax')
    cis_status = models.CharField('CIS status', max_length=12, choices=rules.CIS_CHOICES, default=rules.CIS_UNVERIFIED,
                                  help_text=rules.HMRC_CHECK)
    verification_number = models.CharField('HMRC verification number', max_length=20, blank=True,
                                           help_text='From HMRC when you verify them, e.g. V1234567890 (unverified: '
                                                     'V1234567890/AB).')
    verified_on = models.DateField('Date verified', null=True, blank=True)
    utr = models.CharField('Unique Taxpayer Reference (UTR)', max_length=13, blank=True)
    vat_rate = models.DecimalField('VAT rate %', max_digits=5, decimal_places=2, default=rules.VAT_STANDARD_RATE,
                                   validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
                                   help_text='20 unless they charge another rate; 0 if not VAT registered.')
    reverse_charge = models.BooleanField(
        'VAT domestic reverse charge applies', default=False,
        help_text='Tick when they are VAT registered and the work is reported under CIS: no VAT is added and we '
                  'account for it. Advisory: check with your accountant.')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        verbose_name_plural = 'subcontractor tax details'

    def __str__(self):
        return f'{self.subcontractor_id} CIS {self.cis_status}'

    @property
    def cis_rate(self):
        return rules.cis_rate(self.cis_status)

    @property
    def cis_tone(self):
        return {rules.CIS_GROSS: 'good', rules.CIS_REGISTERED: 'info'}.get(self.cis_status, 'bad')


def tax_for(subcontractor):
    """The sub's tax row, or an unsaved one with the safe defaults (unverified, 30%)."""
    row = SubcontractorTax.objects.filter(subcontractor=subcontractor).first()
    return row or SubcontractorTax(subcontractor=subcontractor, organization_id=subcontractor.organization_id)


class Subcontract(TermsFields, TenantModel):
    """Payment terms on one appointed package. Made from the company defaults the first time it is opened."""
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='subcontracts')
    package = models.OneToOneField('directory.Package', on_delete=models.RESTRICT, related_name='subcontract')
    valuation_dates = models.JSONField('Valuation dates', default=list, blank=True)
    region = models.CharField('Bank holidays', max_length=20, choices=REGION_CHOICES, default=DEFAULT_REGION)
    practical_completion_on = models.DateField(
        'Our practical completion', null=True, blank=True,
        help_text='Leave blank to follow the main contract. Applications from this date hold half the retention.')
    defects_period_months = models.PositiveSmallIntegerField(
        'Defects period (months)', null=True, blank=True, validators=[MaxValueValidator(60)],
        help_text='Leave blank to follow the main contract. The rest of the retention is released at its end.')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        ordering = ['package__trade__name', 'pk']

    def __str__(self):
        return f'Subcontract {self.package}'

    def get_absolute_url(self):
        return reverse('subpay_subcontract', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def subcontractor(self):
        return self.package.subcontractor

    @property
    def commitment(self):
        from costs.models import Commitment
        return Commitment.objects.filter(package_id=self.package_id).select_related('cost_code').first()

    def _contract(self):
        from contracts.models import MainContract
        return MainContract.objects.filter(project_id=self.project_id).first()

    @property
    def pc_date(self):
        if self.practical_completion_on:
            return self.practical_completion_on
        c = self._contract()
        return c.practical_completion_on if c else None

    @property
    def defects_months(self):
        if self.defects_period_months is not None:
            return self.defects_period_months
        c = self._contract()
        return c.defects_period_months if c else None

    @property
    def listed_dates(self):
        from datetime import date
        out = []
        for s in self.valuation_dates or []:
            try:
                out.append(date.fromisoformat(s))
            except (TypeError, ValueError):
                continue
        return sorted(out)

    def terms(self):
        return rules.sub_terms(self.due_date_offset_days, self.payment_notice_days, self.final_date_offset_days,
                               self.pay_less_notice_days, self.region)

    def cycle(self, valuation_date, received_on=None):
        """The cycle from the valuation date, moved to the day received if that is after the due date."""
        return rules.sub_cycle(valuation_date, self.terms(), received_on)

    def allowed_dates(self, today):
        """Valuation dates an application can be made up to today: this cycle's or the one before."""
        return rules.allowed_valuation_dates(self.cycle_kind, today, self.valuation_day, self.listed_dates)

    @property
    def gross_cap(self):
        """Three times the order's current value, or None with no order."""
        c = self.commitment
        return rules.gross_cap(c.value) if c is not None else None

    def around(self, today):
        """(latest valuation date on or before today, next one)."""
        return rules.valuation_dates(self.cycle_kind, today, self.valuation_day, self.listed_dates)

    def stage_at(self, valuation_date):
        return rules.retention_stage(valuation_date, self.pc_date, self.defects_months)


def subcontract_for(package, actor=None, create=True):
    """
    The package's subcontract, made from the company defaults (and the main contract's region) the
    first time. With create=False (GET pages) an unsaved one, so viewing never writes.
    """
    from django.db import IntegrityError, transaction
    existing = Subcontract.objects.filter(package=package).first()
    if existing:
        return existing
    from contracts.models import MainContract
    defaults = settings_for(package.organization, create=create)
    contract = MainContract.objects.filter(project_id=package.project_id).first()
    fields = {f.name: getattr(defaults, f.name) for f in TermsFields._meta.get_fields()}
    if not create:
        return Subcontract(organization_id=package.organization_id, project_id=package.project_id, package=package,
                           region=(contract.bank_holiday_region if contract else DEFAULT_REGION), **fields)
    try:
        with transaction.atomic():
            return Subcontract.objects.create(project_id=package.project_id, package=package, updated_by=actor,
                                              region=(contract.bank_holiday_region if contract else DEFAULT_REGION),
                                              **fields)
    except IntegrityError:
        return Subcontract.objects.get(package=package)


def approved_sub_variations_to(package_id, valuation_date):
    """Approved sub variations on the package at their approved value, decided on or before the valuation date."""
    from delivery.models import SubVariation
    qs = SubVariation.objects.filter(package_id=package_id, status=SubVariation.APPROVED,
                                     approved_value__isnull=False).filter(
        Q(decided_at__date__lte=valuation_date) | Q(decided_at__isnull=True, created_at__date__lte=valuation_date))
    return qs.aggregate(s=Sum('approved_value'))['s'] or ZERO


class SubApplication(RecordBase):
    RECEIVED, NOTIFIED, PAY_LESS, PAID, WITHDRAWN = 'RECEIVED', 'NOTIFIED', 'PAY_LESS', 'PAID', 'WITHDRAWN'
    STATUS_CHOICES = [
        (RECEIVED, 'Awaiting our payment notice'),
        (NOTIFIED, 'Payment notice given'),
        (PAY_LESS, 'Pay less notice given'),
        (PAID, 'Paid'),
        (WITHDRAWN, 'Withdrawn'),
    ]
    SUB_LABELS = {RECEIVED: 'With the contractor', NOTIFIED: 'Payment notice issued', PAY_LESS: 'Pay less notice issued',
                  PAID: 'Paid', WITHDRAWN: 'Withdrawn'}
    TONES = {RECEIVED: 'accent', NOTIFIED: 'info', PAY_LESS: 'warn', PAID: 'good', WITHDRAWN: 'muted'}
    COUNTED = (RECEIVED, NOTIFIED, PAY_LESS, PAID)
    AWAITING = (RECEIVED, NOTIFIED, PAY_LESS)
    INTERIM, RELEASE = 'INTERIM', 'RELEASE'
    KIND_CHOICES = [(INTERIM, 'Interim application'), (RELEASE, 'Retention release')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=RECEIVED, db_index=True)
    subcontract = models.ForeignKey(Subcontract, on_delete=models.RESTRICT, related_name='applications')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=INTERIM)
    valuation_date = models.DateField()
    received_on = models.DateField('Application received', default=timezone.localdate)
    applied_by_sub = models.BooleanField(default=False, editable=False)
    gross_to_date = models.DecimalField('Gross value of work to date', **MONEY,
                                        validators=[MinValueValidator(Decimal('0'))],
                                        help_text='Cumulative, excluding VAT, before retention. Approved variations '
                                                  'are added for you.')
    materials_stated = models.DecimalField('Cost of materials in this application', **MONEY, default=ZERO,
                                           validators=[MinValueValidator(Decimal('0'))],
                                           help_text='What the materials cost the subcontractor, in the sum applied '
                                                     'for this time. CIS is not deducted from it.')
    materials = models.DecimalField('Materials for CIS', **MONEY, default=ZERO, editable=False)
    notes = models.TextField('Notes', blank=True)
    # One-time key from the apply form, so sending it twice makes one application
    form_key = models.CharField(max_length=80, null=True, blank=True, editable=False)

    # Frozen on receipt
    variations_value = models.DecimalField(**MONEY, default=ZERO, editable=False)
    retention_percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, editable=False)
    retention_released_at_pc_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('50'),
                                                           editable=False)
    retention_stage = models.CharField(max_length=16, choices=rules.STAGE_CHOICES, default=rules.STAGE_BEFORE_PC,
                                       editable=False)
    gross_valuation = models.DecimalField(**MONEY, default=ZERO, editable=False)
    retention = models.DecimalField(**MONEY, default=ZERO, editable=False)
    net_valuation = models.DecimalField(**MONEY, default=ZERO, editable=False)
    previous_certified = models.DecimalField(**MONEY, default=ZERO, editable=False)
    amount_due = models.DecimalField('Applied for this time', **MONEY, default=ZERO, editable=False)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, editable=False)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=rules.VAT_STANDARD_RATE, editable=False)
    reverse_charge = models.BooleanField(default=False, editable=False)

    due_on = models.DateField('Due date', null=True, blank=True, editable=False)
    payment_notice_by = models.DateField(null=True, blank=True, editable=False)
    pay_less_by = models.DateField(null=True, blank=True, editable=False)
    final_date = models.DateField('Final date for payment', null=True, blank=True, editable=False)

    payment_notice_amount = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    payment_notice_basis = models.TextField(blank=True, editable=False)
    payment_notice_on = models.DateField(null=True, blank=True, editable=False)
    payment_notice_given_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                                on_delete=models.SET_NULL, related_name='+', editable=False)
    pay_less_amount = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    pay_less_basis = models.TextField(blank=True, editable=False)
    pay_less_on = models.DateField(null=True, blank=True, editable=False)
    pay_less_given_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='+', editable=False)
    paid_in_full_on = models.DateField(null=True, blank=True, editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-number']
        verbose_name = 'subcontractor application'
        constraints = [models.UniqueConstraint(fields=['subcontract', 'form_key'], name='subpay_application_form_key')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    def get_absolute_url(self):
        return reverse('subpay_application', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return self.TONES.get(self.status, 'muted')

    @property
    def sub_status_label(self):
        return self.SUB_LABELS.get(self.status, self.get_status_display())

    # --- figures --------------------------------------------------------------------
    def previous_applications(self):
        qs = SubApplication.objects.filter(subcontract_id=self.subcontract_id, status__in=self.COUNTED,
                                           valuation_date__lt=self.valuation_date)
        return qs.exclude(pk=self.pk) if self.pk else qs

    def calculate(self, today=None):
        """Work out and freeze the figures and the cycle from the subcontract, sub variations and earlier applications."""
        sc = self.subcontract
        tax = tax_for(sc.subcontractor)
        self.retention_percent = sc.retention_percent
        self.retention_released_at_pc_percent = sc.retention_released_at_pc_percent
        self.retention_stage = sc.stage_at(self.valuation_date)
        self.discount_percent = sc.discount_percent
        self.vat_rate, self.reverse_charge = tax.vat_rate, tax.reverse_charge
        self.variations_value = approved_sub_variations_to(sc.package_id, self.valuation_date)
        previous = sum((a.notified_sum for a in self.previous_applications()), ZERO)
        v = rules.sub_valuation(self.gross_to_date, self.variations_value, self.retention_percent,
                                self.retention_released_at_pc_percent, self.retention_stage, previous)
        self.gross_valuation, self.retention, self.net_valuation = v.gross, v.retention, v.net
        self.previous_certified, self.amount_due = v.previous, v.amount_due
        if not self.materials:
            self.materials = self.materials_stated
        cycle = sc.cycle(self.valuation_date, self.received_on)
        self.due_on, self.payment_notice_by = cycle.due_date, cycle.payment_notice_by
        self.pay_less_by, self.final_date = cycle.pay_less_by, cycle.final_date
        return v

    @property
    def cycle(self):
        if self.due_on is None:
            return None
        return PaymentCycle(self.valuation_date, self.due_on, self.payment_notice_by, self.pay_less_by,
                            self.final_date)

    # --- the notified sum ---------------------------------------------------------
    @property
    def notified(self):
        return rules.notified_sum(self.amount_due, self.payment_notice_amount, self.payment_notice_on,
                                  self.payment_notice_by, self.pay_less_amount, self.pay_less_on, self.pay_less_by)

    @property
    def notified_sum(self):
        return self.notified[0] if self.status in self.COUNTED else ZERO

    @property
    def basis(self):
        return self.notified[1]

    @property
    def discount(self):
        return rules.discount_on(self.notified_sum, self.discount_percent)

    @property
    def payable(self):
        """What we pay this time, excluding VAT and before CIS: the notified sum less any discount."""
        return self.notified_sum - self.discount

    @property
    def stands_as_notified(self):
        return self.status in self.AWAITING and rules.stands_as_notified(
            timezone.localdate(), self.payment_notice_by, self.payment_notice_on)

    @property
    def late_warning(self):
        if self.status not in self.AWAITING:
            return ''
        return rules.late_notice_warning(timezone.localdate(), self.payment_notice_by, self.payment_notice_on)

    @property
    def pay_less_open(self):
        return (self.status in (self.RECEIVED, self.NOTIFIED) and self.pay_less_by is not None
                and timezone.localdate() <= self.pay_less_by)

    # --- payments -----------------------------------------------------------------
    def live_payments(self):
        return self.payments.filter(reversed_at__isnull=True)

    @property
    def paid_gross(self):
        cached = getattr(self, '_paid_gross', None)
        if cached is None:
            cached = self.live_payments().aggregate(s=Sum('gross'))['s'] or ZERO
            self._paid_gross = cached
        return cached

    @property
    def materials_paid(self):
        return self.live_payments().aggregate(s=Sum('materials'))['s'] or ZERO

    def cap_materials(self):
        """Materials can't be more than the sum we pay (nor less than what payments already took)."""
        self.materials = max(min(self.materials, max(self.payable, ZERO)), self.materials_paid if self.pk else ZERO)

    @property
    def materials_left(self):
        return max(self.materials - self.materials_paid, ZERO)

    @property
    def outstanding(self):
        """Gross still to pay (excluding VAT, before CIS)."""
        if self.status not in self.COUNTED:
            return ZERO
        return rules.money(self.payable) - rules.money(self.paid_gross)

    @property
    def is_overdue(self):
        return (self.status in self.AWAITING and self.final_date is not None
                and timezone.localdate() > self.final_date and self.outstanding > 0)

    def clocks(self, today=None):
        today = today or timezone.localdate()
        return rules.payment_clocks(self.cycle, today, submitted_on=self.received_on,
                                    payment_notice_on=self.payment_notice_on, pay_less_on=self.pay_less_on,
                                    paid_in_full_on=self.paid_in_full_on,
                                    words={'payment_document': 'our payment notice'})

    @property
    def projected_advice(self):
        """The advice for paying everything outstanding now (for the screen before a payment is recorded)."""
        tax = tax_for(self.subcontract.subcontractor)
        out = max(self.outstanding, ZERO)
        return self.part_advice(out, tax)

    def part_advice(self, gross, tax=None, materials=None):
        """The advice for paying `gross` now: materials pro rata (or as given), VAT so the parts add up."""
        tax = tax or tax_for(self.subcontract.subcontractor)
        self._paid_gross = None
        outstanding = self.outstanding
        mats = rules.materials_share(self.materials_left, gross, outstanding) if materials is None \
            else min(rules.money(materials), self.materials_left, rules.money(gross))
        vat_before = self.live_payments().aggregate(s=Sum('vat'))['s'] or ZERO
        vat = rules.part_vat(self.paid_gross, vat_before, gross, self.vat_rate, self.reverse_charge)
        return rules.advice(gross, mats, tax.cis_status, self.vat_rate, self.reverse_charge, vat=vat)


class SubPayment(TenantModel):
    """A payment to a subcontractor. Frozen when recorded; reversed (with its ledger entry), never deleted."""
    application = models.ForeignKey(SubApplication, on_delete=models.RESTRICT, related_name='payments')
    paid_on = models.DateField()
    gross = models.DecimalField('Gross paid (excluding VAT, before CIS)', **MONEY,
                                validators=[MinValueValidator(Decimal('0.01'))])
    materials = models.DecimalField(**MONEY, default=ZERO)
    labour = models.DecimalField(**MONEY, default=ZERO)
    cis_status = models.CharField(max_length=12, choices=rules.CIS_CHOICES)
    cis_rate = models.DecimalField(max_digits=5, decimal_places=2)
    cis_deduction = models.DecimalField(**MONEY, default=ZERO)
    verification_number = models.CharField(max_length=20, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    reverse_charge = models.BooleanField(default=False)
    vat = models.DecimalField(**MONEY, default=ZERO)
    net = models.DecimalField('Net paid to the subcontractor', **MONEY)
    reference = models.CharField('Payment reference', max_length=120, blank=True)
    ledger_entry = models.ForeignKey('costs.LedgerEntry', null=True, blank=True, on_delete=models.RESTRICT,
                                     related_name='+', editable=False)
    form_key = models.CharField(max_length=80, null=True, blank=True, editable=False)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    recorded_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    reversal_reason = models.CharField(max_length=300, blank=True)
    reversal_entry = models.ForeignKey('costs.LedgerEntry', null=True, blank=True, on_delete=models.RESTRICT,
                                       related_name='+', editable=False)

    class Meta(TenantModel.Meta):
        ordering = ['paid_on', 'pk']
        constraints = [models.UniqueConstraint(fields=['application', 'form_key'], name='subpay_payment_form_key')]

    def __str__(self):
        return f'{self.gross} on {self.paid_on}'

    @property
    def is_reversed(self):
        return self.reversed_at is not None

    @property
    def tax_month(self):
        return rules.tax_month(self.paid_on)

    @property
    def ref(self):
        return f'{self.application.reference}-P{self.pk}'

    def delete(self, *args, **kwargs):
        raise PermissionError('Payments are reversed, never deleted.')
