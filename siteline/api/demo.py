"""
Demo API tokens, called by `manage.py seed_demo` inside the demo organisation's org_context.

  Power BI dashboard   made by the owner, reads everything but incidents, used this morning
                       (a few calls in its log)
  Old spreadsheet      revoked last month

The tokens' text is thrown away as soon as they are made, exactly as on the real page, so nobody,
the demo included, can use them. Make a fresh one in Settings → API to try the API.
"""
from datetime import timedelta

from django.utils import timezone

from . import auth
from .models import SCOPE_COMMERCIAL, SCOPE_DOCUMENTS, SCOPE_REGISTERS, SCOPE_SNAGS, ApiAccess, ApiToken


def seed(org, owner):
    now = timezone.now()
    bi, _ = auth.create_token(org, owner, 'Power BI dashboard',
                              [SCOPE_REGISTERS, SCOPE_DOCUMENTS, SCOPE_SNAGS, SCOPE_COMMERCIAL],
                              expires_at=now + timedelta(days=365))
    for minutes, path in ((95, '/api/v1/projects/'), (94, '/api/v1/cost-reports/'), (93, '/api/v1/cvr/'),
                          (92, '/api/v1/rfis/?status=OPEN'), (91, '/api/v1/incidents/')):
        ApiAccess.objects.create(token=bi, at=now - timedelta(minutes=minutes), method='GET', path=path,
                                 status=403 if 'incidents' in path else 200, ip='203.0.113.10')
    ApiToken.objects.filter(pk=bi.pk).update(last_used_at=now - timedelta(minutes=91))
    old, _ = auth.create_token(org, owner, 'Old spreadsheet', [SCOPE_REGISTERS])
    ApiToken.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=200), last_used_at=now - timedelta(days=40),
                                              revoked_at=now - timedelta(days=35), revoked_by=owner)
