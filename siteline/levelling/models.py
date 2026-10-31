"""
Quote levelling: subcontractor quotes for one trade on one bid, compared line by line.

A trade package in Siteline is the set of directory.Package rows for one project and trade
(one row per subcontractor invited). A LevellingSheet sits on that pair: its scope lines are
the rows, each Package that has quoted is a column, and QuoteCell holds that subcontractor's
rate or sum for a line. Adjustments (exclusions we price in, qualifications, provisional sums,
arithmetic corrections and plugs for missing items) turn each quoted total into a levelled one;
the maths is in levelling/maths.py.

Internal only. Nothing here may reach a subcontractor: they must never see another bidder's
prices or the levelling.
"""
from django.conf import settings
from django.db import models

from tenancy.models import TenantModel


class LevellingSheet(TenantModel):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='levelling_sheets')
    # RESTRICT, not CASCADE: deleting a trade or a package must never take levelling with it. RESTRICT
    # rather than PROTECT so deleting a whole workspace (which cascades to both sides) still works.
    trade = models.ForeignKey('directory.Trade', on_delete=models.RESTRICT, related_name='levelling_sheets')
    # The subcontractor carried in our tender for this trade (one per sheet). Carrying is not
    # appointing: nobody is told anything until the bid is won and the projects team appoints.
    carried = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.RESTRICT,
                                related_name='+')
    carried_reason = models.TextField('Why this one', blank=True)
    carried_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    carried_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['project', 'trade'], name='levelling_one_sheet_per_trade')]

    def __str__(self):
        return f'Levelling: {self.trade} on {self.project}'


class ScopeLine(TenantModel):
    """One bill or schedule line of the package, entered by hand or imported."""
    sheet = models.ForeignKey(LevellingSheet, on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveIntegerField(default=0)
    ref = models.CharField('Ref', max_length=40, blank=True)
    description = models.CharField(max_length=500)
    qty = models.DecimalField('Qty', max_digits=14, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['position', 'pk']

    def __str__(self):
        return f'{self.ref} {self.description}'.strip()


class QuoteCell(TenantModel):
    """
    One subcontractor's price for one line: a rate (times the line's qty), or a sum, or
    "included" (priced elsewhere in their quote). Blank everywhere means missing.
    """
    line = models.ForeignKey(ScopeLine, on_delete=models.CASCADE, related_name='cells')
    package = models.ForeignKey('directory.Package', on_delete=models.RESTRICT, related_name='+')
    rate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField('Sum', max_digits=14, decimal_places=2, null=True, blank=True)
    included = models.BooleanField('Included elsewhere', default=False)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['line', 'package'], name='levelling_one_cell_per_line_sub')]

    @property
    def is_blank(self):
        return self.rate is None and self.amount is None and not self.included


class Exclusion(TenantModel):
    """An exclusion or qualification in a subcontractor's quote, and what we did about it."""
    OPEN, PRICED, ACCEPTED = 'OPEN', 'PRICED', 'ACCEPTED'
    STATUS_CHOICES = [(OPEN, 'Not priced yet'), (PRICED, 'Priced by us'), (ACCEPTED, 'Accepted, no cost')]

    sheet = models.ForeignKey(LevellingSheet, on_delete=models.CASCADE, related_name='exclusions')
    package = models.ForeignKey('directory.Package', on_delete=models.RESTRICT, related_name='+')
    text = models.CharField('Exclusion or qualification', max_length=500)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=OPEN)
    amount = models.DecimalField('Priced by us', max_digits=14, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return self.text

    @property
    def status_tone(self):
        return {self.OPEN: 'bad', self.PRICED: 'info', self.ACCEPTED: 'good'}[self.status]


class Adjustment(TenantModel):
    """Money the estimator adds to (or takes off) one subcontractor's quote to level it, with the reason."""
    EXCLUSION, QUALIFICATION, PROVISIONAL, CORRECTION, PLUG = 'EXCLUSION', 'QUALIFICATION', 'PROVISIONAL', \
        'CORRECTION', 'PLUG'
    KIND_CHOICES = [
        (EXCLUSION, 'Exclusion priced by us'), (QUALIFICATION, 'Qualification'), (PROVISIONAL, 'Provisional sum'),
        (CORRECTION, 'Arithmetic correction'), (PLUG, 'Missing item plugged'),
    ]
    AVERAGE, HIGHEST = 'AVERAGE', 'HIGHEST'
    BASIS_CHOICES = [(AVERAGE, 'average of the others'), (HIGHEST, 'highest of the others')]

    sheet = models.ForeignKey(LevellingSheet, on_delete=models.CASCADE, related_name='adjustments')
    package = models.ForeignKey('directory.Package', on_delete=models.RESTRICT, related_name='+')
    kind = models.CharField(max_length=13, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2, help_text='Negative to take money off.')
    reason = models.CharField(max_length=500)
    # A plug is for one missing line; an exclusion adjustment mirrors one register entry priced by us
    line = models.ForeignKey(ScopeLine, null=True, blank=True, on_delete=models.CASCADE, related_name='plugs')
    basis = models.CharField(max_length=8, choices=BASIS_CHOICES, blank=True)
    exclusion = models.OneToOneField(Exclusion, null=True, blank=True, on_delete=models.CASCADE,
                                     related_name='adjustment')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['pk']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.reason}'
