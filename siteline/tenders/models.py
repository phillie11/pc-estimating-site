"""
Subcontract tenders run through the portal: addenda and tender queries.

Invitations and quotes live on directory.Package (one row per subcontractor
per trade). Bidders never get documents by email; they get a notification
linking to the tender page, where everything is.
"""
from django.conf import settings
from django.db import models

from projects.models import Project, _private_path, save_with_number
from tenancy.models import TenantModel


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
