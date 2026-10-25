from datetime import timedelta

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from tenancy.models import TenantModel

DEFAULT_TRADES = [
    'Strip out & demolition', 'Drylining & partitions', 'Suspended ceilings', 'Joinery', 'Flooring',
    'Decorating', 'Mechanical', 'Electrical', 'Fire protection', 'Plumbing', 'Glazing', 'Shopfronts',
    'Metalwork', 'Signage', 'Cleaning', 'Scaffolding', 'Groundworks', 'Roofing', 'Asbestos',
]


class Trade(TenantModel):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ['name']
        unique_together = [('organization', 'name')]

    def __str__(self):
        return self.name


class Subcontractor(TenantModel):
    STATUS_APPROVED = 'APPROVED'
    STATUS_PENDING = 'PENDING'
    STATUS_ON_HOLD = 'ON_HOLD'
    STATUS_STRUCK_OFF = 'STRUCK_OFF'
    STATUS_CHOICES = [
        (STATUS_APPROVED, 'Approved'), (STATUS_PENDING, 'Pending approval'),
        (STATUS_ON_HOLD, 'On hold'), (STATUS_STRUCK_OFF, 'Struck off'),
    ]

    company_name = models.CharField(max_length=200)
    trades = models.ManyToManyField(Trade, blank=True, related_name='subcontractors')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    contact_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=300, blank=True)
    regions = models.CharField('Regions covered', max_length=200, blank=True, help_text='e.g. North West, Midlands')
    website = models.URLField(blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True, choices=[(i, f'{i} / 5') for i in range(1, 6)])
    public_liability_expiry = models.DateField(null=True, blank=True)
    employers_liability_expiry = models.DateField(null=True, blank=True)
    accreditations = models.CharField(max_length=200, blank=True, help_text='CHAS, SafeContractor, Constructionline...')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['company_name']

    def __str__(self):
        return self.company_name

    def get_absolute_url(self):
        return reverse('sub_detail', args=[self.organization.slug, self.pk])

    @property
    def next_insurance_expiry(self):
        dates = [d for d in (self.public_liability_expiry, self.employers_liability_expiry) if d]
        return min(dates) if dates else None

    @property
    def insurance_status(self):
        d = self.next_insurance_expiry
        if d is None:
            return 'UNKNOWN'
        today = timezone.localdate()
        if d < today:
            return 'EXPIRED'
        if d <= today + timedelta(days=30):
            return 'EXPIRING'
        return 'OK'

    @property
    def insurance_label(self):
        return {'EXPIRED': 'Insurance expired', 'EXPIRING': 'Insurance expiring', 'OK': 'Insured',
                'UNKNOWN': 'No insurance on file'}[self.insurance_status]

    @property
    def insurance_tone(self):
        return {'EXPIRED': 'bad', 'EXPIRING': 'warn', 'OK': 'good', 'UNKNOWN': 'muted'}[self.insurance_status]

    @property
    def status_tone(self):
        return {'APPROVED': 'good', 'PENDING': 'warn', 'ON_HOLD': 'muted', 'STRUCK_OFF': 'bad'}[self.status]


class SubcontractorContact(TenantModel):
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.CASCADE, related_name='contacts')
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


def upload_quote(instance, filename):
    from projects.models import _private_path
    return _private_path(instance, filename, 'quotes')


class Package(TenantModel):
    """One subcontractor invited to price one trade on a project, and how it ended."""
    ENQUIRY, QUOTED, APPOINTED, DECLINED, UNSUCCESSFUL = 'ENQUIRY', 'QUOTED', 'APPOINTED', 'DECLINED', 'UNSUCCESSFUL'
    STATUS_CHOICES = [
        (ENQUIRY, 'Invited'), (QUOTED, 'Quote received'), (APPOINTED, 'Appointed'),
        (DECLINED, 'Declined'), (UNSUCCESSFUL, 'Unsuccessful'),
    ]
    OPEN_STATUSES = (ENQUIRY, QUOTED)
    CLOSED_STATUSES = (DECLINED, UNSUCCESSFUL)

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='packages')
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.CASCADE, related_name='packages')
    trade = models.ForeignKey(Trade, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='ENQUIRY')
    scope = models.TextField('Scope of works', blank=True)
    quote_due = models.DateField('Quote due', null=True, blank=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)

    quote_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    quote_file = models.FileField(upload_to=upload_quote, blank=True)
    exclusions = models.TextField('Exclusions & qualifications', blank=True)
    valid_until = models.DateField('Quote valid until', null=True, blank=True)
    programme_weeks = models.PositiveSmallIntegerField('Programme (weeks on site)', null=True, blank=True)
    quote_notes = models.TextField('Notes', blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decline_reason = models.CharField(max_length=300, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    notes = models.CharField('Internal note', max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['trade__name', 'quote_value']

    def __str__(self):
        return f'{self.trade or "Package"} · {self.subcontractor}'

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def submitted_late(self):
        return bool(self.submitted_at and self.quote_due and timezone.localdate(self.submitted_at) > self.quote_due)

    @property
    def is_overdue(self):
        return self.status == self.ENQUIRY and self.quote_due and self.quote_due < timezone.localdate()

    @property
    def status_tone(self):
        return {'ENQUIRY': 'muted', 'QUOTED': 'info', 'APPOINTED': 'good', 'DECLINED': 'muted', 'UNSUCCESSFUL': 'bad'}[self.status]
