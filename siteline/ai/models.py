"""
AI drafting: a company switch, a monthly budget, a record of every call, and the bid brief the tender reader writes.

  AISettings   one per company: on or off (off until an owner or admin switches it on), the monthly cap in
               pounds, and who may use it
  AIRun        one request for a draft (a pack's clarifications, a bid's brief), run by the worker; the page
               shows its progress and, if it failed, why in plain words
  Playbook     a company's own version of an AI task's playbook (how it wants the work done), every save kept
  AIUsage      one call to the model: who, which feature, which project, the model, tokens and what it cost
               in pounds. No prompt or reply text is stored here: only what a feature saves as its draft
  QuoteMapDraft  one subcontractor's quote read against a levelling sheet's scope lines: a rate or sum per
  / Line /       line with its source, the quote's own total and whether the mapping ties to it, items that
  Exclusion      match no line, and the quote's exclusions word for word. Nothing reaches the levelling
                 (QuoteCell, Exclusion) until an estimator accepts a line, an exclusion, or all of them
  BidBrief /   the tender reader's structured brief on a bid, one line per finding with the document and page
  BriefItem    it came from. Every line can be edited, and nothing reaches the project's own fields until
               someone accepts that value

All staff only: no visibility field, no portal page, no API resource.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenancy.models import TenantModel


class AISettings(TenantModel):
    ADMINS, ESTIMATORS, STAFF = 'ADMINS', 'ESTIMATORS', 'STAFF'
    WHO_CHOICES = [(ADMINS, 'Owners and admins only'),
                   (ESTIMATORS, 'Owners, admins and staff who work in estimating'),
                   (STAFF, 'All staff')]

    enabled = models.BooleanField('Use AI drafting', default=False,
                                  help_text='Off until you switch it on. Nothing is sent to the AI service while off.')
    monthly_cap = models.DecimalField(
        'Monthly budget (£)', max_digits=8, decimal_places=2, default=Decimal('20.00'),
        help_text='Drafting stops for the rest of the calendar month once this much has been spent.')
    who = models.CharField('Who may use it', max_length=12, choices=WHO_CHOICES, default=ESTIMATORS)
    # Company profile for the playbooks: what the tender reader weighs sector fit and value against
    profile = models.TextField(
        'Company profile', blank=True, max_length=1500,
        help_text='Optional. The sectors you work in and your usual contract size, e.g. "Office, retail and '
                  'hospitality fit-out, usually £500k to £3m". Sent with tender readings, never anything personal.')
    overheads_profit = models.DecimalField(
        'Overheads and profit (%)', max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Optional. The tender reader adds this to its value range. Left blank, it gives the range '
                  'without it and says so.')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='ai_one_settings_per_org')]
        verbose_name_plural = 'AI settings'

    def __str__(self):
        return f'AI drafting {"on" if self.enabled else "off"}, £{self.monthly_cap} a month'

    @classmethod
    def for_org(cls, org):
        obj = cls.objects.filter(organization=org).first()
        return obj if obj is not None else cls(organization=org)

    def profile_text(self):
        lines = [self.profile.strip()] if self.profile.strip() else []
        if self.overheads_profit is not None:
            lines.append(f'Overheads and profit: {self.overheads_profit}% on the subtotal.')
        return '\n'.join(lines)


CLARIFICATIONS, TENDER_BRIEF, QUOTE_MAPPING = 'clarifications', 'tender_brief', 'quote_mapping'
FEATURE_CHOICES = [(CLARIFICATIONS, 'Handover pack clarifications'), (TENDER_BRIEF, 'Tender reader'),
                   (QUOTE_MAPPING, 'Quote mapping')]


class Playbook(TenantModel):
    """
    One saved version of a company's playbook for one AI task (ai/registry.py). The newest version is the one
    used; blank text means "back to Siteline's default". Older versions are kept, with who saved them.
    """
    task = models.CharField(max_length=40)
    version = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['-version']
        constraints = [models.UniqueConstraint(fields=['organization', 'task', 'version'], name='ai_playbook_version')]

    def __str__(self):
        return f'{self.task} playbook v{self.version}'


class AIRun(TenantModel):
    QUEUED, RUNNING, DONE, FAILED = 'queued', 'running', 'done', 'failed'
    STATUS_CHOICES = [(QUEUED, 'Waiting'), (RUNNING, 'Working'), (DONE, 'Done'), (FAILED, 'Not done')]

    feature = models.CharField(max_length=20, choices=FEATURE_CHOICES)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='+')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=QUEUED)
    # What was asked for (document revision ids for the tender reader); never prompt text
    params = models.JSONField(default=dict, blank=True)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['-created_at']
        indexes = [models.Index(fields=['project', 'feature', 'created_at'], name='ai_run_project_feature')]

    def __str__(self):
        return f'{self.get_feature_display()} on {self.project_id}: {self.status}'

    @property
    def is_open(self):
        return self.status in (self.QUEUED, self.RUNNING)

    @property
    def tone(self):
        return {self.DONE: 'good', self.FAILED: 'bad'}.get(self.status, 'info')


class AIUsage(TenantModel):
    RESERVED, OK, FAILED, REFUSED = 'reserved', 'ok', 'failed', 'refused'
    STATUS_CHOICES = [(RESERVED, 'In progress'), (OK, 'Done'), (FAILED, 'Failed'), (REFUSED, 'Declined by the model')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    feature = models.CharField(max_length=20, choices=FEATURE_CHOICES)
    project = models.ForeignKey('projects.Project', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    run = models.ForeignKey(AIRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='usage')
    model = models.CharField(max_length=60)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RESERVED)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    # While RESERVED: the most the call could cost, held against the budget; afterwards what it did cost
    cost_gbp = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    request_id = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=200, blank=True)  # e.g. "3 documents, 42 pages"; never prompt text
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta(TenantModel.Meta):
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_feature_display()} £{self.cost_gbp}'


class BidBrief(TenantModel):
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='bid_brief')
    # The documents the last reading was given: [{"revision": id, "label": "KW-A-100 rev T1", "pages": 12}]
    sources = models.JSONField(default=list, blank=True)
    BID, CAUTION, NO_BID, UNSURE = 'BID', 'BID_WITH_CAUTION', 'NO_BID', 'UNSURE'
    RECOMMENDATION_CHOICES = [('', 'Not given'), (BID, 'Bid'), (CAUTION, 'Bid with caution'), (NO_BID, 'No bid'),
                              (UNSURE, 'Too little to call')]
    recommendation = models.CharField(max_length=20, choices=RECOMMENDATION_CHOICES, blank=True)
    summary = models.TextField(blank=True)
    value_basis = models.TextField('Basis of the value range', blank=True)
    ai_drafted = models.BooleanField(default=False)
    edited = models.BooleanField(default=False)  # the summary was changed by hand: reading again leaves it
    generated_at = models.DateTimeField(null=True, blank=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Bid brief for {self.project_id}'

    @property
    def recommendation_tone(self):
        return {self.BID: 'good', self.CAUTION: 'warn', self.NO_BID: 'bad'}.get(self.recommendation, 'muted')


class BriefItem(TenantModel):
    PROGRAMME, DATE, SCOPE, RISK, ACTION = 'programme', 'date', 'scope', 'risk', 'action'
    SECTION_CHOICES = [
        (PROGRAMME, 'Project and programme'), (DATE, 'Key dates'), (SCOPE, 'Scope and approximate value'),
        ('contract', 'Form of contract'), ('amendment', 'Amendments'), ('lads', 'Liquidated damages'),
        ('retention', 'Retention'), ('bond', 'Bonds'), ('payment', 'Payment terms'), ('insurance', 'Insurances'),
        ('term', 'Other contract terms'), ('constraint', 'Site constraints'), (RISK, 'Risks'),
        ('pack_present', 'Pack check: present'), ('pack_missing', 'Pack check: missing'),
        ('pack_submit', 'Pack check: the submission must contain'), (ACTION, 'If we bid'),
    ]
    SECTIONS = [k for k, _ in SECTION_CHOICES]
    # A key date the project already has a field for: accepting it copies the date there
    RETURN, SITE_VISIT, START, COMPLETION, OTHER = 'return', 'site_visit', 'start', 'completion', 'other'
    DATE_KINDS = [(RETURN, 'Tender return'), (SITE_VISIT, 'Site visit'), (START, 'Start on site'),
                  (COMPLETION, 'Completion'), (OTHER, 'Other date')]
    PROJECT_FIELDS = {RETURN: 'tender_due', SITE_VISIT: 'site_visit', START: 'start_date',
                      COMPLETION: 'completion_date'}
    SEVERITY_CHOICES = [('critical', 'Critical'), ('high', 'High'), ('medium', 'Medium'), ('low', 'Low')]
    SEVERITY_TONES = {'critical': 'bad', 'high': 'bad', 'medium': 'warn', 'low': 'muted'}

    brief = models.ForeignKey(BidBrief, on_delete=models.CASCADE, related_name='items')
    section = models.CharField(max_length=12, choices=SECTION_CHOICES)
    label = models.CharField(max_length=120, blank=True)
    text = models.TextField()
    date_kind = models.CharField(max_length=12, choices=DATE_KINDS, blank=True)
    date = models.DateField(null=True, blank=True)
    severity = models.CharField(max_length=8, choices=SEVERITY_CHOICES, blank=True)
    value_low = models.DecimalField('Low (£)', max_digits=14, decimal_places=0, null=True, blank=True)
    value_high = models.DecimalField('High (£)', max_digits=14, decimal_places=0, null=True, blank=True)
    rank = models.PositiveSmallIntegerField(default=0)
    source_revision = models.ForeignKey('projects.DocumentRevision', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+')
    source_label = models.CharField('Source', max_length=200, blank=True)
    source_page = models.PositiveIntegerField('Page', null=True, blank=True)
    ai_drafted = models.BooleanField(default=False)
    edited = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    order = models.PositiveIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ['order', 'rank', 'pk']

    def __str__(self):
        return self.label or self.text[:40]

    @property
    def severity_tone(self):
        return self.SEVERITY_TONES.get(self.severity, 'muted')

    @property
    def project_field(self):
        return self.PROJECT_FIELDS.get(self.date_kind) if self.section == self.DATE and self.date else None

    @property
    def source_text(self):
        if not self.source_label:
            return ''
        return f'{self.source_label}, page {self.source_page}' if self.source_page else self.source_label


class QuoteMapDraft(TenantModel):
    """One AI reading of one subcontractor's quote against one levelling sheet. Internal only, like levelling."""
    OPEN, DONE, DISCARDED = 'open', 'done', 'discarded'
    STATUS_CHOICES = [(OPEN, 'To review'), (DONE, 'Reviewed'), (DISCARDED, 'Discarded')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='+')
    sheet = models.ForeignKey('levelling.LevellingSheet', on_delete=models.CASCADE, related_name='ai_maps')
    package = models.ForeignKey('directory.Package', on_delete=models.CASCADE, related_name='ai_maps')
    run = models.ForeignKey(AIRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    source_label = models.CharField('Read from', max_length=200, blank=True)
    # The quote's own total as the model read it, and where
    quote_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_source = models.CharField(max_length=200, blank=True)
    # What the model said about the tie; Siteline's own arithmetic is worked out on the page from the lines
    model_ties = models.BooleanField(null=True, blank=True)
    model_difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    unmatched = models.JSONField(default=list, blank=True)  # [{"text", "amount", "source"}]
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantModel.Meta):
        ordering = ['-created_at']
        indexes = [models.Index(fields=['sheet', 'package', 'status'], name='ai_qmap_sheet_pkg')]

    def __str__(self):
        return f'AI quote map of {self.package_id} on sheet {self.sheet_id}'


class QuoteMapLine(TenantModel):
    PENDING, ACCEPTED, REJECTED = 'pending', 'accepted', 'rejected'
    STATUS_CHOICES = [(PENDING, 'To check'), (ACCEPTED, 'Accepted'), (REJECTED, 'Not used')]

    draft = models.ForeignKey(QuoteMapDraft, on_delete=models.CASCADE, related_name='lines')
    line = models.ForeignKey('levelling.ScopeLine', on_delete=models.CASCADE, related_name='+')
    rate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField('Sum', max_digits=14, decimal_places=2, null=True, blank=True)
    included = models.BooleanField('Included elsewhere', default=False)
    source = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['line__position', 'line__pk']
        constraints = [models.UniqueConstraint(fields=['draft', 'line'], name='ai_qmap_one_per_line')]


class QuoteMapExclusion(TenantModel):
    PENDING, ACCEPTED, REJECTED = QuoteMapLine.PENDING, QuoteMapLine.ACCEPTED, QuoteMapLine.REJECTED
    STATUS_CHOICES = QuoteMapLine.STATUS_CHOICES

    draft = models.ForeignKey(QuoteMapDraft, on_delete=models.CASCADE, related_name='exclusions')
    text = models.CharField(max_length=500)
    source = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ['pk']
