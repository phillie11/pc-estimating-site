"""
Cost control models.

    CostCode            the company's cost code library (editable; codes with money on them can only be retired)
    CostSettings        per company: margin warning threshold
    BudgetChange        the original budget, or a later transfer or revision, with its reason
      BudgetLine          one cost code's share of it (signed for transfers and revisions)
    Commitment          what the contractor has agreed to spend: a subcontract order from an appointed
                        package, or a supplier order entered by hand
      CommitmentAdjustment  every later change to an order (an approved sub variation, a correction)
    ActualCost          a supplier invoice or a timesheet total
    ForecastOverride    a manual forecast final cost for one code, with its reason (latest wins)
    LedgerEntry         the append-only cost ledger every one of the above posts into

Everything here except CostCode and CostSettings is append-only: rows are inserted, never
updated or deleted (save() of an existing row, delete() and queryset update()/delete() raise
PermissionError). A mistake is put right with a reversing ledger entry (costs.ledger.reverse) or
a further adjustment, so the original and every change stay visible. Deleting a whole workspace
still removes them through Django's cascade, as for core.AuditEntry.
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.models import AppendOnlyQuerySet, TenantAppendOnlyQuerySet
from tenancy.models import TenantManager, TenantModel


def upload_cost(instance, filename):
    from projects.models import _private_path
    return _private_path(instance, filename, 'costs')


def _actor_name(user):
    return ((getattr(user, 'full_name', '') or getattr(user, 'email', '')) if user else '')[:150]


class AppendOnlyModel(TenantModel):
    """Insert-only rows: save() never UPDATEs, delete() refuses, and so do queryset writes."""
    objects = TenantManager.from_queryset(TenantAppendOnlyQuerySet)()
    all_objects = models.Manager.from_queryset(AppendOnlyQuerySet)()

    class Meta(TenantModel.Meta):
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError(f'{type(self).__name__} rows are append-only: post a correction instead.')
        kwargs['force_insert'] = True  # never an UPDATE, even with an existing pk
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(f'{type(self).__name__} rows are append-only: post a correction instead.')


# ---------------------------------------------------------------------------
# Cost codes and settings
# ---------------------------------------------------------------------------

class CostCode(TenantModel):
    PRELIMS, PACKAGES, LABOUR, MATERIALS, PLANT, FEES, CONTINGENCY = (
        'PRELIMS', 'PACKAGES', 'LABOUR', 'MATERIALS', 'PLANT', 'FEES', 'CONTINGENCY')
    GROUP_CHOICES = [
        (PRELIMS, 'Preliminaries'), (PACKAGES, 'Trade packages'), (LABOUR, 'Labour'), (MATERIALS, 'Materials'),
        (PLANT, 'Plant'), (FEES, 'Fees'), (CONTINGENCY, 'Contingency'),
    ]

    code = models.CharField(max_length=12, help_text='Short and sortable, e.g. 230.')
    name = models.CharField(max_length=120)
    group = models.CharField(max_length=12, choices=GROUP_CHOICES, blank=True)
    is_active = models.BooleanField('In use', default=True,
                                    help_text='Retired codes stay on past jobs but are not offered for new costs.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=['organization', 'code'], name='costs_code_unique_per_org')]

    def __str__(self):
        return f'{self.code} {self.name}'

    @property
    def has_money(self):
        return LedgerEntry.all_objects.filter(cost_code=self).exists()


class CostSettings(TenantModel):
    """One row per company. See costs.access.settings_for()."""
    margin_threshold = models.DecimalField(
        'Margin warning below (%)', max_digits=5, decimal_places=2, default=Decimal('10.00'),
        help_text='A job whose forecast margin falls below this shows red and goes on the PM\'s and admins\' Needs me.')
    codes_seeded_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta(TenantModel.Meta):
        verbose_name_plural = 'cost settings'
        constraints = [models.UniqueConstraint(fields=['organization'], name='costs_settings_one_per_org')]

    def __str__(self):
        return f'Cost settings ({self.margin_threshold}%)'


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class LedgerEntry(AppendOnlyModel):
    """
    One money movement against a job and cost code. Never edited or deleted: post through
    costs.ledger.post() and correct with costs.ledger.reverse(), which adds the opposite entry.
    """
    BUDGET, COMMITMENT, ACTUAL = 'BUDGET', 'COMMITMENT', 'ACTUAL'
    KIND_CHOICES = [(BUDGET, 'Budget'), (COMMITMENT, 'Committed'), (ACTUAL, 'Actual cost')]

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='cost_ledger')
    cost_code = models.ForeignKey(CostCode, on_delete=models.RESTRICT, related_name='ledger')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    # The record that caused it (a budget line, an order, an adjustment, an invoice, another app's row)
    source_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.RESTRICT, related_name='+')
    source_id = models.PositiveBigIntegerField(null=True, blank=True)
    source = GenericForeignKey('source_type', 'source_id')
    reverses = models.OneToOneField('self', null=True, blank=True, on_delete=models.RESTRICT,
                                    related_name='reversed_by', help_text='The entry this one cancels.')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    actor_name = models.CharField(max_length=150, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['date', 'pk']
        verbose_name_plural = 'ledger entries'
        indexes = [models.Index(fields=['project', 'cost_code', 'kind'], name='costs_ledger_proj_code_kind')]

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount} on {self.cost_code_id} ({self.date})'

    def save(self, *args, **kwargs):
        if self.actor_id and not self.actor_name:
            self.actor_name = _actor_name(self.actor)
        super().save(*args, **kwargs)

    @property
    def is_reversed(self):
        return LedgerEntry.all_objects.filter(reverses=self).exists()

    @property
    def can_reverse(self):
        return self.reverses_id is None and not self.is_reversed

    @property
    def tone(self):
        return {'BUDGET': 'info', 'COMMITMENT': 'accent', 'ACTUAL': 'good'}[self.kind]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class BudgetChange(AppendOnlyModel):
    ORIGINAL, TRANSFER, REVISION = 'ORIGINAL', 'TRANSFER', 'REVISION'
    KIND_CHOICES = [(ORIGINAL, 'Original budget'), (TRANSFER, 'Budget transfer'), (REVISION, 'Budget revision')]

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='budget_changes')
    number = models.PositiveIntegerField(editable=False)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    reason = models.TextField(blank=True)
    # Original budget only: what it was worked out from
    basis_value = models.DecimalField('Tender or contract value', max_digits=14, decimal_places=2, null=True, blank=True)
    margin_pct = models.DecimalField('Margin %', max_digits=5, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['project', 'number'], name='costs_budgetchange_number')]

    def __str__(self):
        return f'{self.reference} {self.get_kind_display()}'

    @property
    def reference(self):
        return 'Original' if self.kind == self.ORIGINAL else f'BC-{self.number - 1:03d}'

    @property
    def total(self):
        return self.lines.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    @property
    def tone(self):
        return {'ORIGINAL': 'good', 'TRANSFER': 'info', 'REVISION': 'warn'}[self.kind]


class BudgetLine(AppendOnlyModel):
    change = models.ForeignKey(BudgetChange, on_delete=models.RESTRICT, related_name='lines')
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='budget_lines')
    cost_code = models.ForeignKey(CostCode, on_delete=models.RESTRICT, related_name='+')
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['change__number', 'cost_code__code', 'pk']

    def __str__(self):
        return f'{self.cost_code} {self.amount}'


# ---------------------------------------------------------------------------
# Commitments
# ---------------------------------------------------------------------------

class Commitment(AppendOnlyModel):
    SUBCONTRACT, ORDER = 'SUBCONTRACT', 'ORDER'
    KIND_CHOICES = [(SUBCONTRACT, 'Subcontract order'), (ORDER, 'Supplier order')]

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='commitments')
    number = models.PositiveIntegerField(editable=False)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=ORDER)
    cost_code = models.ForeignKey(CostCode, on_delete=models.RESTRICT, related_name='+')
    package = models.OneToOneField('directory.Package', null=True, blank=True, on_delete=models.RESTRICT,
                                   related_name='commitment')
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, blank=True, on_delete=models.RESTRICT,
                                      related_name='+')
    supplier = models.CharField('Supplier or subcontractor', max_length=200)
    reference = models.CharField('Their reference or our order number', max_length=60, blank=True)
    description = models.CharField(max_length=300, blank=True)
    order_date = models.DateField(default=timezone.localdate)
    original_value = models.DecimalField('Order value', max_digits=14, decimal_places=2)
    # One-time key from the form that placed it, so a double submit can't place the order twice
    form_key = models.CharField(max_length=80, null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['project', 'number'], name='costs_commitment_number'),
                       models.UniqueConstraint(fields=['project', 'form_key'], name='costs_commitment_form_key')]

    def __str__(self):
        return f'{self.ref} {self.supplier}'

    @property
    def ref(self):
        return f'ORD-{self.number:03d}'

    def get_absolute_url(self):
        return reverse('cost_commitment_detail', args=[self.organization.slug, self.project_id, self.pk])

    @property
    def adjusted_total(self):
        return self.adjustments.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    @property
    def value(self):
        """Current order value: as placed plus every adjustment."""
        return self.original_value + self.adjusted_total


class CommitmentAdjustment(AppendOnlyModel):
    commitment = models.ForeignKey(Commitment, on_delete=models.RESTRICT, related_name='adjustments')
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='commitment_adjustments')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.CharField(max_length=300)
    sub_variation = models.OneToOneField('delivery.SubVariation', null=True, blank=True, on_delete=models.RESTRICT,
                                         related_name='cost_adjustment')
    form_key = models.CharField(max_length=80, null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['created_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['commitment', 'form_key'], name='costs_adjustment_form_key')]

    def __str__(self):
        return f'{self.commitment_id} {self.amount}'


# ---------------------------------------------------------------------------
# Actual costs and forecasts
# ---------------------------------------------------------------------------

class ActualCost(AppendOnlyModel):
    INVOICE, TIMESHEET, OTHER = 'INVOICE', 'TIMESHEET', 'OTHER'
    KIND_CHOICES = [(INVOICE, 'Supplier invoice'), (TIMESHEET, 'Timesheet total'), (OTHER, 'Other cost')]

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='actual_costs')
    cost_code = models.ForeignKey(CostCode, on_delete=models.RESTRICT, related_name='+')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=INVOICE)
    supplier = models.CharField('Supplier or who worked', max_length=200)
    reference = models.CharField('Invoice number or week', max_length=60, blank=True)
    cost_date = models.DateField('Date', default=timezone.localdate)
    amount = models.DecimalField('Amount (excluding VAT)', max_digits=14, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    commitment = models.ForeignKey(Commitment, null=True, blank=True, on_delete=models.RESTRICT,
                                   related_name='actuals', verbose_name='Against order')
    file = models.FileField('Invoice or timesheet', upload_to=upload_cost, blank=True)
    original_name = models.CharField(max_length=200, blank=True)
    form_key = models.CharField(max_length=80, null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['-cost_date', '-pk']
        constraints = [models.UniqueConstraint(fields=['project', 'form_key'], name='costs_actual_form_key')]

    def __str__(self):
        return f'{self.supplier} {self.reference} {self.amount}'


class ForecastOverride(AppendOnlyModel):
    """A manual forecast final cost for one code. The latest row wins; amount None goes back to the default."""
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='cost_forecasts')
    cost_code = models.ForeignKey(CostCode, on_delete=models.RESTRICT, related_name='+')
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reason = models.CharField(max_length=300)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(AppendOnlyModel.Meta):
        ordering = ['created_at', 'pk']

    def __str__(self):
        return f'{self.cost_code_id} forecast {self.amount}'
