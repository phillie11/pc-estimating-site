"""
Subcontract tenders run through the portal: addenda and tender queries.

Invitations and quotes live on directory.Package (one row per subcontractor
per trade). Bidders never get documents by email; they get a notification
linking to the tender page, where everything is.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from projects.models import Project, _private_path, save_with_number
from tenancy.models import TenantModel

from . import scoring


def upload_addendum(instance, filename):
    return _private_path(instance, filename, 'addenda')


class Addendum(TenantModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='addenda')
    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField('What has changed')
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        unique_together = [('project', 'number')]
        verbose_name_plural = 'addenda'

    def __str__(self):
        return f'{self.reference} {self.title}'

    @property
    def reference(self):
        return f'Addendum {self.number:02d}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)


class AddendumFile(TenantModel):
    addendum = models.ForeignKey(Addendum, on_delete=models.CASCADE, related_name='files')
    file = models.FileField(upload_to=upload_addendum)
    original_name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ['original_name']


class AddendumReceipt(TenantModel):
    """A subcontractor confirming they have seen an addendum and priced it."""
    addendum = models.ForeignKey(Addendum, on_delete=models.CASCADE, related_name='receipts')
    subcontractor = models.ForeignKey('directory.Subcontractor', on_delete=models.RESTRICT, related_name='+')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('addendum', 'subcontractor')]


class TenderQuery(TenantModel):
    """A question from a tendering subcontractor. Answers can be issued to every bidder, without naming who asked."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tender_queries')
    number = models.PositiveIntegerField(editable=False)
    package = models.ForeignKey('directory.Package', null=True, blank=True, on_delete=models.SET_NULL, related_name='queries')
    subcontractor = models.ForeignKey('directory.Subcontractor', null=True, on_delete=models.SET_NULL, related_name='+')
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    question = models.TextField()
    answer = models.TextField(blank=True)
    answered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    answered_at = models.DateTimeField(null=True, blank=True)
    published = models.BooleanField('Issue answer to all tenderers', default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        unique_together = [('project', 'number')]
        verbose_name_plural = 'tender queries'

    def __str__(self):
        return f'{self.reference}'

    @property
    def reference(self):
        return f'TQ-{self.number:03d}'

    def save(self, *args, **kwargs):
        return save_with_number(self, super().save, *args, **kwargs)


# ---------------------------------------------------------------------------
# Bid discipline: the bid/no-bid gate, declines and loss reviews
# ---------------------------------------------------------------------------

class TenderSettings(TenantModel):
    """
    One row per organisation; with no row the defaults apply. Kept here, not on Organization,
    so estimating can grow its own settings without touching tenancy.
    """
    DEFAULT_GATE_THRESHOLD = Decimal('0')  # every bid, unless the company raises it
    DEFAULT_MIN_QUOTES = 3

    gate_threshold = models.DecimalField(
        'Bid/no-bid check required from', max_digits=14, decimal_places=2, default=DEFAULT_GATE_THRESHOLD,
        help_text='Bids worth this much or more must pass the bid/no-bid questions before tendering. '
                  'Below it the check is optional. 0 (the default) requires it on every bid; so does a bid with no value or budget.')
    min_quotes = models.PositiveSmallIntegerField(
        'Quotes wanted per trade', default=DEFAULT_MIN_QUOTES,
        help_text='A trade with fewer quotes in than this shows red on the coverage board.')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='tenders_one_settings_per_org')]
        verbose_name_plural = 'tender settings'

    def __str__(self):
        return f'Gate from {self.gate_threshold}, {self.min_quotes} quotes per trade'

    @classmethod
    def for_org(cls, org):
        """The organisation's settings, or unsaved defaults. Pinned to the organisation passed in."""
        return cls.all_objects.filter(organization=org).first() or cls(organization=org)


class BidReview(TenantModel):
    """
    Everything estimating records about whether to bid and how it ended, one per project:
    intake (contract form, bidders, client budget), the bid/no-bid answers and score,
    the decision, why we declined, and the review of a loss.
    """
    PROCEED, SKIPPED, DECLINED, DIRECT_AWARD = 'PROCEED', 'SKIPPED', 'DECLINED', 'DIRECT_AWARD'
    DECISION_CHOICES = [(PROCEED, 'Bid'), (SKIPPED, 'Bid (check skipped)'), (DECLINED, 'Declined to bid'),
                        (DIRECT_AWARD, 'Won without tendering')]

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='bid_review')

    # Intake
    contract_form = models.CharField('Main contract form', max_length=10, choices=scoring.CONTRACT_FORMS, blank=True)
    amendments = models.TextField('Amendments to the contract', blank=True,
                                  help_text='Schedule of amendments, unusual clauses, LADs, retention, bonds.')
    bidders = models.PositiveSmallIntegerField('Number of bidders', null=True, blank=True,
                                               help_text='Including us, if the client has said.')
    client_budget = models.DecimalField('Client budget', max_digits=14, decimal_places=2, null=True, blank=True,
                                        help_text='Only if the client has told you.')

    # The six questions: points 0..4 each (tenders.scoring.QUESTIONS)
    q_client = models.PositiveSmallIntegerField(null=True, blank=True)
    q_sector = models.PositiveSmallIntegerField(null=True, blank=True)
    q_value = models.PositiveSmallIntegerField(null=True, blank=True)
    q_programme = models.PositiveSmallIntegerField(null=True, blank=True)
    q_workload = models.PositiveSmallIntegerField(null=True, blank=True)
    q_terms = models.PositiveSmallIntegerField(null=True, blank=True)
    workload_bids = models.PositiveSmallIntegerField(null=True, blank=True, editable=False,
                                                     help_text="The estimator's other open bids over the same weeks, when scored.")
    score = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    result = models.CharField(max_length=6, choices=scoring.RESULT_CHOICES, blank=True, editable=False)
    decision = models.CharField(max_length=12, choices=DECISION_CHOICES, blank=True)
    decision_note = models.TextField('Notes on the decision', blank=True)
    assessed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    assessed_at = models.DateTimeField(null=True, blank=True)

    # Declined to bid
    decline_reason = models.CharField(max_length=10, choices=scoring.DECLINE_REASONS, blank=True)
    decline_detail = models.CharField('More detail', max_length=300, blank=True)

    # Lost
    our_price = models.DecimalField('Our price', max_digits=14, decimal_places=2, null=True, blank=True)
    winner_price = models.DecimalField("Winner's price", max_digits=14, decimal_places=2, null=True, blank=True,
                                       help_text='If the client told you.')
    loss_reason = models.CharField('Main reason lost', max_length=16, choices=scoring.LOSS_REASONS, blank=True)
    lessons = models.TextField('Lessons for next time', blank=True)
    loss_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name='+')
    loss_reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Bid review: {self.project}'

    def answers(self):
        return {k: getattr(self, f'q_{k}') for k in scoring.QUESTION_KEYS}

    def rescore(self):
        self.score = scoring.score(self.answers())
        self.result = scoring.result(self.score)

    @property
    def passed_gate(self):
        return self.decision in (self.PROCEED, self.SKIPPED)

    @property
    def result_tone(self):
        return scoring.RESULT_TONES.get(self.result, 'muted')

    @property
    def gap_pct(self):
        return scoring.price_gap(self.our_price, self.winner_price)

    @property
    def decline_summary(self):
        if not self.decline_reason:
            return ''
        label = self.get_decline_reason_display()
        return f'{label}: {self.decline_detail}' if self.decline_detail else label
