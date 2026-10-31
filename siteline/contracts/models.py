"""
A project's main contract with the client: which form, its key terms and who administers it.

Internal only. One per project. Configuration, not a numbered record, so a plain TenantModel.

The contract sum and the start and completion dates are NOT stored here. Project.value
("tender value while bidding, contract sum once won"), Project.start_date and
Project.completion_date stay the single source: the handover already confirms them, and the
pipeline, analytics, workload charts and commercial overview all read them. The contract shows
those fields and its form edits them on the project.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from holidays.calendar import DEFAULT_REGION, REGION_CHOICES
from tenancy.models import TenantModel

from . import presets
from .vocabulary import family, vocabulary

DEFAULT_CLIENT_RESPONSE_DAYS = 14


class MainContract(TenantModel):
    FORM_CHOICES = [
        ('JCT_MW', 'JCT Minor Works Building Contract (MW)'),
        ('JCT_MWD', "JCT Minor Works with contractor's design (MWD)"),
        ('JCT_IC', 'JCT Intermediate Building Contract (IC)'),
        ('JCT_ICD', "JCT Intermediate with contractor's design (ICD)"),
        ('JCT_DB', 'JCT Design and Build Contract (DB)'),
        ('JCT_SBC', 'JCT Standard Building Contract (SBC)'),
        ('NEC4_ECC', 'NEC4 Engineering and Construction Contract (ECC)'),
        ('BESPOKE', 'Bespoke or other'),
    ]
    EDITION_CHOICES = [('2016', '2016'), ('2024', '2024')]
    NEC_OPTION_CHOICES = [
        ('A', 'Option A: priced contract with activity schedule'),
        ('B', 'Option B: priced contract with bill of quantities'),
        ('C', 'Option C: target contract with activity schedule'),
        ('D', 'Option D: target contract with bill of quantities'),
        ('E', 'Option E: cost reimbursable contract'),
        ('F', 'Option F: management contract'),
    ]
    PERIOD_WEEK, PERIOD_DAY = 'WEEK', 'DAY'
    PERIOD_CHOICES = [(PERIOD_WEEK, 'per week'), (PERIOD_DAY, 'per day')]

    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='main_contract')
    form = models.CharField('Form of contract', max_length=10, choices=FORM_CHOICES, default='JCT_IC')
    edition = models.CharField(max_length=4, choices=EDITION_CHOICES, blank=True, default='2016',
                               help_text='JCT forms only.')
    nec_main_option = models.CharField('NEC main option', max_length=1, choices=NEC_OPTION_CHOICES, blank=True,
                                       help_text='NEC4 only.')
    amendments = models.TextField('Amendments and special conditions', blank=True,
                                  help_text='Schedule of amendments, Z clauses, anything that changes the standard terms.')

    lads_rate = models.DecimalField('Damages rate', max_digits=12, decimal_places=2, null=True, blank=True,
                                    validators=[MinValueValidator(Decimal('0'))],
                                    help_text='Liquidated (JCT) or delay (NEC X7) damages.')
    lads_period = models.CharField('Damages per', max_length=4, choices=PERIOD_CHOICES, default=PERIOD_WEEK)

    retention_percent = models.DecimalField('Retention %', max_digits=5, decimal_places=2, null=True, blank=True,
                                            validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])
    retention_released_at_pc_percent = models.DecimalField(
        'Retention released at completion %', max_digits=5, decimal_places=2, default=Decimal('50'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='Share of the retention released at completion; the rest at the end of the defects period. '
                  'Usually half.')
    defects_period_months = models.PositiveSmallIntegerField('Defects period (months)', null=True, blank=True)

    valuation_day = models.PositiveSmallIntegerField(
        'Valuation day of the month', null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text='Day of each month for valuations (assessment dates under NEC). Above 28 means the last day '
                  'in shorter months.')
    due_date_offset_days = models.PositiveSmallIntegerField('Due date (days after valuation)', null=True, blank=True)
    final_date_offset_days = models.PositiveSmallIntegerField('Final date for payment (days after due date)',
                                                              null=True, blank=True)
    pay_less_notice_days = models.PositiveSmallIntegerField('Pay less notice (days before final date)',
                                                            null=True, blank=True)
    payment_notice_days = models.PositiveSmallIntegerField(
        'Payment notice (days after due date)', null=True, blank=True,
        help_text="When the client's payment notice (Interim Certificate, NEC payment certificate) is due. "
                  '5 under JCT and the Scheme, 0 under NEC4 Y(UK)2.')
    vat_reverse_charge = models.BooleanField(
        'VAT domestic reverse charge applies', default=False,
        help_text='Tick if the client is a VAT-registered contractor who uses the works for onward supply '
                  '(construction domestic reverse charge). Applications then show no VAT. Advisory: check '
                  'with the client and your accountant.')
    practical_completion_on = models.DateField(
        'Practical completion certified on', null=True, blank=True,
        help_text='The actual date (Practical Completion under JCT, Completion under NEC). Applications '
                  'after it hold the reduced retention.')

    bank_holiday_region = models.CharField(
        'Bank holidays', max_length=20, choices=REGION_CHOICES, default=DEFAULT_REGION,
        help_text='Which part of the UK the contract is under. Payment and notice dates leave out its bank '
                  'holidays, Christmas Day and Good Friday.')
    client_response_days = models.PositiveSmallIntegerField(
        'Client to decide variations within (days)', default=DEFAULT_CLIENT_RESPONSE_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
        help_text='Submitting a variation sets its respond-by date this many days ahead. Staff can change it '
                  'on each variation.')

    administrator = models.ForeignKey('projects.ProjectMember', null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+', verbose_name="Client's contract administrator",
                                      help_text='Contract Administrator (JCT), Project Manager (NEC), '
                                                "Employer's Agent (JCT Design and Build). Invite them from the "
                                                'Team tab first.')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')

    PAYMENT_FIELDS = ('due_date_offset_days', 'final_date_offset_days', 'pay_less_notice_days', 'payment_notice_days',
                      'retention_percent',
                      'retention_released_at_pc_percent', 'defects_period_months', 'lads_period')

    class Meta(TenantModel.Meta):
        verbose_name = 'main contract'

    def __str__(self):
        return f'{self.form_label} on {self.project}'

    def get_absolute_url(self):
        return reverse('contract_detail', args=[self.organization.slug, self.project_id])

    @property
    def family(self):
        return family(self.form)

    @property
    def is_nec(self):
        return self.family == 'NEC'

    @property
    def is_jct(self):
        return self.family == 'JCT'

    @property
    def vocabulary(self):
        return vocabulary(self.form)

    @property
    def form_label(self):
        label = self.get_form_display()
        if self.is_jct and self.edition:
            label = f'{label} {self.edition}'
        if self.is_nec and self.nec_main_option:
            label = f'{label}, Option {self.nec_main_option}'
        return label

    # --- the figures that live on the project ------------------------------------
    @property
    def contract_sum(self):
        return self.project.value

    @property
    def possession_date(self):
        return self.project.start_date

    @property
    def completion_date(self):
        return self.project.completion_date

    # --- presets -----------------------------------------------------------------
    @property
    def preset(self):
        return presets.preset_for(self.form)

    @property
    def needs_valuation_day(self):
        """A valuation-date payment cycle with no day set: no application reminders until it is."""
        return presets.uses_valuation_dates(self.form) and not self.valuation_day

    def apply_preset(self, only_blank=True, creating=False):
        """
        Fill payment, retention and defects terms from the form's standard ones. Returns the fields
        changed. only_blank leaves values already set; when creating, a value still at the model's
        default (damages per week, half the retention at completion) counts as not set, or an NEC
        contract would never get delay damages per day.
        """
        changed = []
        for name, value in self.preset.values().items():
            current = getattr(self, name)
            untouched = creating and current == self._meta.get_field(name).get_default()
            if only_blank and current not in (None, '') and not untouched:
                continue
            if current != value:
                setattr(self, name, value)
                changed.append(name)
        return changed

    # --- derived figures -----------------------------------------------------------
    @property
    def retention_rule(self):
        if self.retention_percent is None:
            return ''
        if not self.retention_percent:
            return 'No retention'
        at_pc = self.retention_released_at_pc_percent
        words = self.vocabulary
        if at_pc == Decimal('50'):
            release = f"half released at {words['completion']}, half at the end of the {words['defects_period']}"
        elif at_pc == Decimal('100'):
            release = f"all released at {words['completion']}"
        elif not at_pc:
            release = f"all released at the end of the {words['defects_period']}"
        else:
            release = (f"{at_pc.normalize()}% released at {words['completion']}, the rest at the end of the "
                       f"{words['defects_period']}")
        return f'{self.retention_percent.normalize()}%: {release}'

    def retention_held(self, gross):
        """Retention on a gross valuation, rounded half up to 1p."""
        if gross is None or not self.retention_percent:
            return Decimal('0.00')
        return (Decimal(gross) * self.retention_percent / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def next_payment_timetable(self, today):
        valuation = presets.next_valuation_date(today, self.valuation_day)
        return presets.payment_timetable(valuation, self.due_date_offset_days, self.final_date_offset_days,
                                         self.pay_less_notice_days, region=self.bank_holiday_region)
