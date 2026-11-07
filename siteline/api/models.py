"""
Company API tokens and the log of every call made with one.

A token belongs to one company (a TenantModel) and reads only that company's data, as a member of
staff who works in both estimating and projects would, plus the resource groups it was given.
Only a salted hash of the secret is stored, as with two-factor recovery codes (security.models):
the token is shown once, when it is made, and can't be read back by anyone afterwards.

    ApiToken      name, scopes (resource groups), who made it, last used, revoked
    ApiAccess     one row per call: when, which token, what was asked for, the answer's status

The secret is checked in api/auth.py, which is the only place a token is looked up without an
organisation current (it is pinned by its public prefix and then by the hash).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from tenancy.models import TenantModel

# Resource groups a token can be given. "projects" is always included: every other group hangs off a job.
SCOPE_PROJECTS = 'projects'
SCOPE_REGISTERS = 'registers'
SCOPE_DOCUMENTS = 'documents'
SCOPE_SNAGS = 'snags'
SCOPE_COMMERCIAL = 'commercial'
SCOPE_SAFETY = 'safety'
SCOPE_CHOICES = [
    (SCOPE_PROJECTS, 'Projects'),
    (SCOPE_REGISTERS, 'RFIs, variations and change events'),
    (SCOPE_DOCUMENTS, 'Drawings and documents (details, and downloads)'),
    (SCOPE_SNAGS, 'Snags and defects'),
    (SCOPE_COMMERCIAL, 'Money: cost report totals, applications, subcontractor payments and the CVR'),
    (SCOPE_SAFETY, 'Incidents'),
]
SCOPE_LABELS = dict(SCOPE_CHOICES)
SCOPE_SHORT = {SCOPE_PROJECTS: 'Projects', SCOPE_REGISTERS: 'Registers', SCOPE_DOCUMENTS: 'Documents',
               SCOPE_SNAGS: 'Snags', SCOPE_COMMERCIAL: 'Money', SCOPE_SAFETY: 'Incidents'}
ALWAYS = (SCOPE_PROJECTS,)
# What a new token gets unless the owner or admin unticks it. Money and incidents are only for
# owners, admins and each job's PM or QS on screen, so they have to be asked for.
DEFAULT_SCOPES = (SCOPE_PROJECTS, SCOPE_REGISTERS, SCOPE_DOCUMENTS, SCOPE_SNAGS)
RESTRICTED_SCOPES = (SCOPE_COMMERCIAL, SCOPE_SAFETY)


class ApiToken(TenantModel):
    name = models.CharField('Name', max_length=80, help_text='What it is for, e.g. "Power BI dashboard".')
    # The first part of the token, stored as it is so the token can be found; on its own it lets nobody in
    prefix = models.CharField(max_length=16, unique=True, editable=False)
    secret_hash = models.CharField(max_length=128, editable=False)
    scopes = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField('Stops working on', null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'API token'

    def __str__(self):
        return f'{self.name} ({self.prefix}…)'

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_live(self):
        return not self.is_revoked and not self.is_expired

    @property
    def scope_set(self):
        return set(self.scopes or ()) | set(ALWAYS)

    def allows(self, scope):
        return scope in self.scope_set

    @property
    def scope_labels(self):
        return [label for code, label in SCOPE_CHOICES if code in self.scope_set]

    @property
    def scope_short(self):
        return [SCOPE_SHORT[code] for code, _ in SCOPE_CHOICES if code in self.scope_set]

    @property
    def state(self):
        """(label, tone) for status_badge-style display."""
        if self.is_revoked:
            return 'Revoked', 'muted'
        if self.is_expired:
            return 'Run out', 'muted'
        if self.last_used_at is None:
            return 'Not used yet', 'info'
        return 'In use', 'good'


class ApiAccess(TenantModel):
    """One API call. Kept for KEEP_DAYS (api/auth.py prunes older rows)."""
    token = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name='calls')
    at = models.DateTimeField(default=timezone.now, db_index=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    status = models.PositiveSmallIntegerField()
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-at', '-pk']
        verbose_name = 'API call'
        indexes = [models.Index(fields=['organization', 'token', 'at'], name='api_access_org_token_at')]

    def __str__(self):
        return f'{self.method} {self.path} {self.status}'
