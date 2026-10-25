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


class Package(TenantModel):
    """A trade package on a project: who was asked to price, who was appointed."""
    STATUS_CHOICES = [
        ('ENQUIRY', 'Enquiry sent'), ('QUOTED', 'Quote received'), ('APPOINTED', 'Appointed'),
        ('DECLINED', 'Declined'), ('UNSUCCESSFUL', 'Unsuccessful'),
    ]
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='packages')
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.CASCADE, related_name='packages')
    trade = models.ForeignKey(Trade, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='ENQUIRY')
    quote_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['trade__name', 'quote_value']

    @property
    def status_tone(self):
        return {'ENQUIRY': 'muted', 'QUOTED': 'info', 'APPOINTED': 'good', 'DECLINED': 'muted', 'UNSUCCESSFUL': 'bad'}[self.status]
