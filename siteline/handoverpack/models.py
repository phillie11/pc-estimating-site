"""
The internal pricing workbook behind a bid, and the estimating-to-delivery handover pack built from it.

  PricingSettings    one per company: which workbook layout (mapping preset) and the judgement calls
  PricingWorkbook    one per upload: revision 1, 2, 3... on the bid, with the file (through the files app's
                     scan gate and download log) and everything the engine read and worked out (`data`)
  HandoverPack       one per project: the revision it follows, site rules, when it reached delivery and
                     when it was issued to the delivery team
  PackClarification  the Clarifications sheet, editable in Siteline afterwards
  OpenItem           what is still to sort out (gaps, review lines, reconciliation issues), with an owner and
                     due date; open ones feed the owner's Needs me

All of it carries cost, margin and buy-out targets, so it is staff only and only for staff who see the
commercial figures on that project (handoverpack/access.py). Nothing here is shared with a client or
subcontractor: no visibility field, no portal page, no API resource, no transmittal, no share link.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from tenancy.models import TenantModel

from . import codec
from .mapping import PRESET_CHOICES


def upload_workbook(instance, filename):
    from projects.models import _private_path
    return _private_path(instance, filename, 'pricing-workbooks')


class PricingSettings(TenantModel):
    """One row per company; with no row the defaults apply (for_org)."""
    preset = models.CharField('Workbook layout', max_length=40, choices=PRESET_CHOICES, default='standard',
                              help_text='Where Siteline finds each figure in your pricing workbook.')
    # A company's own layout, the shape of mapping.STANDARD, checked by mapping.validate() before use. No editor
    # yet: set by support. When set it wins over `preset`.
    custom_mapping = models.JSONField(null=True, blank=True, editable=False)
    margin_tolerance = models.DecimalField(
        'Margin shortfall allowed (percentage points)', max_digits=5, decimal_places=2, default=Decimal('0'),
        help_text='How far below the workbook\'s target margin a bid may be before it is flagged. 0 flags any '
                  'shortfall.')
    item_due_days = models.PositiveSmallIntegerField(
        'Open items due (days after handover)', default=14,
        help_text='When a bid is won, open items with no due date get one this many days after the handover.')
    assign_to_manager = models.BooleanField(
        'Give unowned open items to the project manager at handover', default=True)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='hpack_one_settings_per_org')]
        verbose_name_plural = 'pricing settings'

    def __str__(self):
        return f'{self.get_preset_display()}'

    @classmethod
    def for_org(cls, org):
        obj = cls.objects.filter(organization=org).first()
        return obj if obj is not None else cls(organization=org)

    def mapping(self):
        from . import mapping
        return mapping.get(self.preset, self.custom_mapping)


class PricingWorkbook(TenantModel):
    """One upload of the pricing workbook for a project. Never changed after it is made."""
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='pricing_workbooks')
    revision = models.PositiveIntegerField()
    file = models.FileField(upload_to=upload_workbook)
    original_name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    mapping_key = models.CharField(max_length=40)
    label = models.CharField('Workbook revision', max_length=40, blank=True)
    # Siteline's recalculation, to the penny (the exact figures are in `data`)
    cost = models.DecimalField(max_digits=16, decimal_places=2)
    charge = models.DecimalField(max_digits=16, decimal_places=2)
    profit = models.DecimalField(max_digits=16, decimal_places=2)
    excel_charge = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    # Recalculated total charge less PRICING SUMMARY's, in pence-rounded pounds; None without Excel's values
    difference = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    cached = models.BooleanField('Has Excel\'s saved values', default=True)
    issue_count = models.PositiveIntegerField(default=0)
    gap_count = models.PositiveIntegerField(default=0)
    data = models.JSONField(default=dict)

    class Meta(TenantModel.Meta):
        ordering = ['-revision']
        constraints = [models.UniqueConstraint(fields=['project', 'revision'], name='hpack_one_revision_number')]

    def __str__(self):
        return f'Pricing workbook revision {self.revision}'

    @property
    def reconciled(self):
        """True: the recalculated total equals the workbook's to the penny. None: no saved values to check against."""
        return None if self.difference is None else self.difference == 0

    @property
    def result(self):
        if not hasattr(self, '_result'):
            self._result = codec.decode(self.data)
        return self._result

    def get_absolute_url(self):
        return reverse('hpack_revision', args=[self.project.organization.slug, self.project_id, self.pk])


class HandoverPack(TenantModel):
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='handover_pack')
    # CASCADE, not PROTECT: deleting a whole workspace (or an empty project) must be able to take both
    workbook = models.ForeignKey(PricingWorkbook, on_delete=models.CASCADE, related_name='+')
    site_rules = models.TextField(blank=True)
    released_at = models.DateTimeField('Reached delivery', null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')
    issued_workbook = models.ForeignKey(PricingWorkbook, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Handover pack for {self.project}'

    @property
    def is_issued(self):
        return self.issued_at is not None

    def get_absolute_url(self):
        return reverse('hpack_pack', args=[self.project.organization.slug, self.project_id])


class PackClarification(TenantModel):
    OPEN, AGREED, CLOSED = 'Open', 'Agreed', 'Closed'
    STATUS_CHOICES = [(OPEN, 'Open'), (AGREED, 'Agreed'), (CLOSED, 'Closed')]
    FROM_WORKBOOK, FROM_SITELINE, FROM_DRAFT = 'WORKBOOK', 'SITELINE', 'DRAFT'
    SOURCE_CHOICES = [(FROM_WORKBOOK, 'Workbook'), (FROM_SITELINE, 'Added in Siteline'),
                      (FROM_DRAFT, 'Suggested draft')]

    pack = models.ForeignKey(HandoverPack, on_delete=models.CASCADE, related_name='clarifications')
    ref = models.CharField(max_length=20, blank=True)
    trade = models.CharField('Trade / section', max_length=80, blank=True)
    text = models.TextField('Clarification / assumption')
    response = models.TextField('Response / resolution', blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    cost_effect = models.CharField(max_length=200, blank=True)
    owner_text = models.CharField('Owner', max_length=120, blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=FROM_SITELINE)
    workbook_row = models.PositiveIntegerField(null=True, blank=True)
    # Once someone changes a workbook clarification here, a later upload leaves it alone
    edited = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'pk']

    def __str__(self):
        return self.ref or self.text[:40]

    @property
    def status_tone(self):
        return {self.OPEN: 'warn', self.AGREED: 'good', self.CLOSED: 'muted'}.get(self.status, 'muted')


class OpenItem(TenantModel):
    OPEN, DONE = 'OPEN', 'DONE'
    STATUS_CHOICES = [(OPEN, 'Open'), (DONE, 'Done')]
    GAP, RECONCILE = 'GAP', 'RECONCILE'
    KIND_CHOICES = [(GAP, 'Gap'), (RECONCILE, 'Reconciliation')]

    pack = models.ForeignKey(HandoverPack, on_delete=models.CASCADE, related_name='items')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='+')
    # Stable across revisions (the gap's code, trade and cell), so an owner and due date survive a new upload
    key = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    code = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    trade = models.CharField(max_length=80, blank=True)
    source = models.CharField('Where', max_length=120, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    severity = models.CharField(max_length=8, default='warn')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=STATUS_CHOICES, default=OPEN)
    note = models.CharField(max_length=500, blank=True)
    # Closed because a later revision no longer has it (reopened if it comes back)
    cleared_by_upload = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['pack', 'key'], name='hpack_one_item_per_key')]
        indexes = [models.Index(fields=['owner', 'status'], name='hpack_item_owner_status')]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status == self.OPEN

    def get_absolute_url(self):
        return reverse('hpack_items', args=[self.project.organization.slug, self.project_id]) + f'#item-{self.pk}'
