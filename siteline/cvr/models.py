"""
Monthly CVR, cashflow and dayworks.

    CommercialSettings  per company: when a month's CVR is due, who may sign it off, and the
                        default daywork percentage additions for a new job
    CVRPeriod (CVR-001) one job's CVR for one month, on core.models.RecordBase. Its figures are
                        worked out live from payments, the cost ledger, contracts and costs
                        (cvr/figures.py) until it is submitted, when they are frozen on the row.
                        Once signed off it is locked: nothing on it changes again (save() refuses).
                        A correction after that goes in a later month, as an adjustment there.
      CVRAdjustment     an accrual or adjustment with its reason, for one month. Append-only, like
                        the cost ledger: a mistake is reversed by an equal and opposite row, once.
    DayworkTerms        per job: the contract's percentage additions on labour, materials and plant
      DayworkRate       per job: the labour and plant rates daywork is priced at
    DayworkSheet (DW-001) labour, plant and materials on a day's daywork, with photos and the
                        client's or contract administrator's signature (a PNG from the phone,
                        stored and served like any upload). Sent to the change register as a
                        change event in one step.
      DayworkLine, DayworkPhoto

Every model is a TenantModel; CVRPeriod and DayworkSheet are RecordBase registers (cvr/apps.py).
"""
import os
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.models import VIS_INTERNAL, RecordBase, RecordManager
from costs.models import AppendOnlyModel
from tenancy.models import TenantManager, TenantModel, TenantQuerySet

from . import rules

MONEY = dict(max_digits=14, decimal_places=2)
PCT = dict(max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal('0')),
                                                       MaxValueValidator(Decimal('500'))])
ZERO = Decimal('0.00')


def _private(instance, filename, kind):
    """Private path, as projects.models._private_path: org_<id>/<kind>/<random>/<name>."""
    name = os.path.basename(filename)[-120:]
    return f'org_{instance.organization_id}/{kind}/{uuid.uuid4().hex}/{name}'


def upload_daywork_photo(instance, filename):
    return _private(instance, filename, 'daywork-photos')


def upload_daywork_signature(instance, filename):
    return _private(instance, filename, 'daywork-signatures')


class CommercialSettings(TenantModel):
    """One row per company (cvr.access.settings_for)."""
    cvr_due_day = models.PositiveSmallIntegerField(
        "Each month's CVR is due by day", default=10, validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text='Day of the following month. Until then it is on the job team\'s Needs me.')
    pm_can_sign_off = models.BooleanField(
        "The job's project manager can sign off its CVR", default=False,
        help_text='Owners and admins can always sign off. Leave unticked if a director signs every CVR.')
    cvr_every_month = models.BooleanField(
        'Every month needs a signed-off CVR', default=True,
        help_text="A month can't be signed off while an earlier month since the job's first CVR has none. "
                  'Untick to allow skipping a month.')
    labour_pct = models.DecimalField('Labour addition %', default=ZERO, **PCT)
    materials_pct = models.DecimalField('Materials addition %', default=ZERO, **PCT)
    plant_pct = models.DecimalField('Plant addition %', default=ZERO, **PCT)

    class Meta(TenantModel.Meta):
        verbose_name_plural = 'commercial settings'
        constraints = [models.UniqueConstraint(fields=['organization'], name='cvr_settings_one_per_org')]

    def __str__(self):
        return 'Commercial reporting settings'


# ---------------------------------------------------------------------------
# The monthly CVR
# ---------------------------------------------------------------------------

# A redaction (core.models.redact) rewrites a record's title with a straight update; nothing else may
# touch a signed-off month that way.
REDACTION_FIELDS = {'title', 'version'}


class _SignedOffLocked:
    """queryset.update() refuses rows that are signed off, as CVRPeriod.save() does."""

    def update(self, **kwargs):
        if not set(kwargs) <= REDACTION_FIELDS and self.filter(status=CVRPeriod.SIGNED_OFF).exists():
            raise PermissionError('A signed-off CVR is locked. Put any correction in a later month as an '
                                  'adjustment.')
        return super().update(**kwargs)

    update.alters_data = True


class CVRQuerySet(_SignedOffLocked, TenantQuerySet):
    pass


class CVRAllQuerySet(_SignedOffLocked, models.QuerySet):
    pass


class CVRPeriod(RecordBase):
    DRAFT, SUBMITTED, SIGNED_OFF = 'DRAFT', 'SUBMITTED', 'SIGNED_OFF'
    STATUS_CHOICES = [(DRAFT, 'Being prepared'), (SUBMITTED, 'Waiting for sign-off'), (SIGNED_OFF, 'Signed off')]
    TONES = {DRAFT: 'warn', SUBMITTED: 'accent', SIGNED_OFF: 'good'}
    FROZEN = (SUBMITTED, SIGNED_OFF)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    month = models.DateField(help_text='The first day of the month this CVR is for.')
    commentary = models.TextField('Commentary', blank=True,
                                  help_text='Why the margin moved, risks and opportunities, anything the figures '
                                            "don't show.")

    # The figures, frozen when it is submitted (cvr.figures.Figures). Blank while it is being prepared.
    value_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    certified_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    under_certified = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    paid_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    retention_held = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    cost_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    committed_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    budget_to_date = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    contract_sum = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    approved_variations = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    forecast_cost_base = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    value_adjustments = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    cost_adjustments = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    final_account_adjustments = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    final_cost_adjustments = models.DecimalField(**MONEY, null=True, blank=True, editable=False)

    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', editable=False)
    signed_off_at = models.DateTimeField(null=True, blank=True, editable=False)
    signed_off_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+', editable=False)

    objects = RecordManager.from_queryset(CVRQuerySet)()
    with_deleted = TenantManager.from_queryset(CVRQuerySet)()
    all_objects = models.Manager.from_queryset(CVRAllQuerySet)()

    class Meta(RecordBase.Meta):
        ordering = ['-month']
        verbose_name = 'monthly CVR'
        constraints = [models.UniqueConstraint(fields=['project', 'month'], name='cvr_one_period_per_month')]

    def __str__(self):
        return f'{self.reference} {self.title}'

    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        obj._stored_status = obj.status
        return obj

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._stored_status = self.status

    def save(self, *args, **kwargs):
        # The copy's own memory of its status, and the row itself: a copy read before someone else signed
        # it off must not write over the signed-off month
        if getattr(self, '_stored_status', None) == self.SIGNED_OFF or (
                self.pk and type(self).all_objects.filter(pk=self.pk, status=self.SIGNED_OFF).exists()):
            raise PermissionError(f'{self.reference} is signed off, so it is locked. Put any correction in a later '
                                  'month as an adjustment.')
        self.visibility = VIS_INTERNAL
        if not self.title:
            self.title = f'CVR for {rules.month_label(self.month)}'
        super().save(*args, **kwargs)
        self._stored_status = self.status

    def get_absolute_url(self):
        return reverse('cvr_period', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return self.TONES.get(self.status, 'muted')

    @property
    def is_frozen(self):
        return self.status in self.FROZEN

    @property
    def is_locked(self):
        return self.status == self.SIGNED_OFF

    @property
    def month_end(self):
        return rules.month_end(self.month)

    @property
    def label(self):
        return rules.month_label(self.month)


class CVRAdjustment(AppendOnlyModel):
    """An accrual or adjustment in one month's CVR. Never edited or deleted: reverse it instead."""
    VALUE, COST, FINAL_ACCOUNT, FINAL_COST = 'VALUE', 'COST', 'FINAL_ACCOUNT', 'FINAL_COST'
    KIND_CHOICES = [
        (VALUE, 'Value: under or over valuation'),
        (COST, 'Cost: accrual or adjustment'),
        (FINAL_ACCOUNT, 'Forecast final account'),
        (FINAL_COST, 'Forecast final cost: risk or opportunity'),
    ]
    SHORT = {VALUE: 'Value', COST: 'Cost', FINAL_ACCOUNT: 'Final account', FINAL_COST: 'Final cost'}

    period = models.ForeignKey(CVRPeriod, on_delete=models.RESTRICT, related_name='adjustments')
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='cvr_adjustments')
    kind = models.CharField('What it adjusts', max_length=14, choices=KIND_CHOICES)
    amount = models.DecimalField(**MONEY, help_text='Plus to add, minus to take off.')
    reason = models.CharField(max_length=300)
    # As costs.LedgerEntry.reverses: no database constraint, one reversal per row
    reverses = models.OneToOneField('self', null=True, blank=True, on_delete=models.DO_NOTHING, db_constraint=False,
                                    related_name='reversed_by')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['created_at', 'pk']

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount}'

    def save(self, *args, **kwargs):
        # The month's row, not a copy that may be stale: only a month being prepared takes adjustments
        if not CVRPeriod.all_objects.filter(pk=self.period_id, status=CVRPeriod.DRAFT).exists():
            raise PermissionError('Adjustments are fixed once a CVR is submitted, and for good once it is signed off.')
        super().save(*args, **kwargs)

    @property
    def short_kind(self):
        return self.SHORT[self.kind]


# ---------------------------------------------------------------------------
# Dayworks
# ---------------------------------------------------------------------------

class DayworkTerms(TenantModel):
    """The job's percentage additions on daywork prime cost (Contract Particulars or schedule of rates)."""
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='daywork_terms')
    labour_pct = models.DecimalField('Labour addition %', default=ZERO, **PCT)
    materials_pct = models.DecimalField('Materials addition %', default=ZERO, **PCT)
    plant_pct = models.DecimalField('Plant addition %', default=ZERO, **PCT)
    basis = models.CharField('Where the rates come from', max_length=200, blank=True,
                             help_text='e.g. Contract Particulars, Definition of Prime Cost of Daywork, or the agreed '
                                       'schedule of daywork rates.')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        verbose_name_plural = 'daywork terms'

    def __str__(self):
        return f'Daywork terms for {self.project_id}'

    def percentages(self):
        return {rules.LABOUR: self.labour_pct, rules.MATERIALS: self.materials_pct, rules.PLANT: self.plant_pct}


class DayworkRate(TenantModel):
    KIND_CHOICES = [(rules.LABOUR, 'Labour'), (rules.PLANT, 'Plant')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='daywork_rates')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=rules.LABOUR)
    name = models.CharField('Trade, grade or plant', max_length=100)
    unit = models.CharField(max_length=20, default='hour')
    rate = models.DecimalField('Rate (£)', **MONEY, validators=[MinValueValidator(Decimal('0'))])
    is_active = models.BooleanField('In use', default=True)

    class Meta(TenantModel.Meta):
        ordering = ['kind', 'name', 'pk']

    def __str__(self):
        return f'{self.name} (£{self.rate}/{self.unit})'


class DayworkSheet(RecordBase):
    DRAFT, SIGNED, IN_CHANGE, VOID = 'DRAFT', 'SIGNED', 'IN_CHANGE', 'VOID'
    STATUS_CHOICES = [(DRAFT, 'Not signed yet'), (SIGNED, 'Signed'), (IN_CHANGE, 'On the change register'),
                      (VOID, 'Cancelled')]
    TONES = {DRAFT: 'warn', SIGNED: 'accent', IN_CHANGE: 'good', VOID: 'muted'}
    EDITABLE = (DRAFT,)
    SIGNER_ROLES = [('CLIENT', "Client or client's representative"), ('CA', 'Contract administrator'),
                    ('PM', 'Project Manager (NEC)'), ('OTHER', 'Someone else for the client')]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    work_date = models.DateField('Date of the work', default=timezone.localdate)
    location = models.CharField('Where', max_length=200, blank=True)
    instructed_by = models.CharField('Instructed by', max_length=200, blank=True,
                                     help_text="Who asked for it and how: the client's site instruction, CA "
                                               'instruction number.')
    description = models.TextField('Details', blank=True)
    # The job's percentage additions when it was made, kept so it prices the same later
    labour_pct = models.DecimalField(default=ZERO, editable=False, **PCT)
    materials_pct = models.DecimalField(default=ZERO, editable=False, **PCT)
    plant_pct = models.DecimalField(default=ZERO, editable=False, **PCT)

    signature = models.FileField(upload_to=upload_daywork_signature, blank=True, editable=False)
    signed_by_name = models.CharField('Name of the person signing', max_length=150, blank=True, editable=False)
    signed_by_role = models.CharField('Signing as', max_length=8, choices=SIGNER_ROLES, blank=True, editable=False)
    signed_at = models.DateTimeField(null=True, blank=True, editable=False)
    signature_recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                              on_delete=models.SET_NULL, related_name='+', editable=False)
    change_event = models.ForeignKey('changes.ChangeEvent', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+', editable=False)
    # Asked to sign in their portal instead of on our phone (cvr/portal.py): a client or consultant on the job
    sign_requested_from = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                            related_name='+', editable=False)
    sign_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    sign_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='+', editable=False)

    class Meta(RecordBase.Meta):
        ordering = ['-work_date', '-number']
        verbose_name = 'daywork sheet'

    def __str__(self):
        return f'{self.reference} {self.title}'

    def save(self, *args, **kwargs):
        self.visibility = VIS_INTERNAL
        # Due a week after the work while unsigned, a week after signing once signed (cvr/actions.py)
        if self.status == self.DRAFT and self.work_date:
            self.due_date = self.work_date + timedelta(days=7)
        elif self.status == self.SIGNED and self.signed_at:
            self.due_date = timezone.localdate(self.signed_at) + timedelta(days=7)
        else:
            self.due_date = None
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('daywork_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def status_tone(self):
        return self.TONES.get(self.status, 'muted')

    @property
    def is_editable(self):
        return self.status in self.EDITABLE

    @property
    def waiting_for_portal_signature(self):
        return self.status == self.DRAFT and self.sign_requested_from_id is not None

    def percentages(self):
        return {rules.LABOUR: self.labour_pct, rules.MATERIALS: self.materials_pct, rules.PLANT: self.plant_pct}

    def price(self, lines=None):
        lines = list(self.lines.all()) if lines is None else lines
        return rules.daywork_price([(ln.kind, ln.quantity, ln.unit_rate) for ln in lines], self.percentages())


def _sheet_open(sheet_id):
    """The sheet's lines and photos can change: it is still being written (read from the row, not a copy)."""
    status = DayworkSheet.all_objects.filter(pk=sheet_id).values_list('status', flat=True).first()
    return status in DayworkSheet.EDITABLE


class FixedOnceSigned:
    """Lines and photos are fixed once the sheet is signed, sent or cancelled: save() and delete() refuse."""

    def _check_open(self):
        if not _sheet_open(self.sheet_id):
            raise PermissionError('The daywork sheet is signed or sent on, so its lines and photos are fixed.')

    def save(self, *args, **kwargs):
        self._check_open()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_open()
        return super().delete(*args, **kwargs)


class DayworkLine(FixedOnceSigned, TenantModel):
    KIND_CHOICES = [(rules.LABOUR, 'Labour'), (rules.PLANT, 'Plant'), (rules.MATERIALS, 'Materials')]
    UNITS = {rules.LABOUR: 'hours', rules.PLANT: 'hours'}

    sheet = models.ForeignKey(DayworkSheet, on_delete=models.CASCADE, related_name='lines')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    description = models.CharField('Who or what', max_length=200)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    unit = models.CharField(max_length=20, blank=True)
    unit_rate = models.DecimalField('Rate (£)', **MONEY, validators=[MinValueValidator(Decimal('0'))])
    rate = models.ForeignKey(DayworkRate, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantModel.Meta):
        ordering = ['kind', 'pk']

    def __str__(self):
        return f'{self.description} {self.quantity} x {self.unit_rate}'

    @property
    def amount(self):
        return rules.line_amount(self.quantity, self.unit_rate)


class DayworkPhoto(FixedOnceSigned, TenantModel):
    sheet = models.ForeignKey(DayworkSheet, on_delete=models.CASCADE, related_name='photos')
    image = models.FileField(upload_to=upload_daywork_photo)
    thumbnail = models.FileField(upload_to=upload_daywork_photo, blank=True)
    # A re-encoded copy without the camera's data (location, device): what the client portal shows
    display = models.FileField(upload_to=upload_daywork_photo, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    caption = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['uploaded_at', 'pk']

    def __str__(self):
        return self.original_name or f'Photo {self.pk}'


def sheet_totals(sheets):
    """{sheet_id: priced total} for many sheets in one query, by the same rules as DayworkSheet.price()."""
    by_sheet = {}
    for ln in DayworkLine.objects.filter(sheet__in=[s.pk for s in sheets]).only('sheet_id', 'kind', 'quantity',
                                                                                 'unit_rate'):
        by_sheet.setdefault(ln.sheet_id, []).append((ln.kind, ln.quantity, ln.unit_rate))
    return {s.pk: rules.daywork_price(by_sheet.get(s.pk, []), s.percentages())['total'] for s in sheets}


def adjustment_totals(period_ids):
    """{period_id: {kind: Decimal}} for live adjustments (reversals net them off), one query."""
    out = {}
    for row in CVRAdjustment.objects.filter(period_id__in=period_ids).values('period_id', 'kind') \
            .annotate(s=Sum('amount')):
        out.setdefault(row['period_id'], {})[row['kind']] = row['s'] or ZERO
    return out
