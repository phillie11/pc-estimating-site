"""
Deleting visitors' QR sign-ins once the company's retention period has passed (UK GDPR: personal data
is kept no longer than needed). Run daily by `manage.py purge_signins`, which needs no worker.

  deleted     SiteAttendance rows of kind VISITOR with no account (a name and company typed on the
              public page) that signed in before the cut-off: now less the company's
              SafetySettings.visitor_retention_months (default 12; 3 to 36).
  kept        sign-ins by our staff and by people with a portal login (they are part of the job's
              record, like the diary), and every incident. Nothing else is touched.

Pending owner decision: the 12-month default and keeping account holders' rows.
"""
from django.utils import timezone

from . import rules
from .models import SafetySettings, SiteAttendance


def due_for_purge(org, now=None):
    """
    The visitor sign-ins in `org` that are past retention. Pinned to `org` as well as to the current
    organisation (which must be set: no context fails closed), so a caller in another company's context,
    or inside unscoped(), never reaches another company's rows.
    """
    months = SafetySettings.for_org(org).visitor_retention_months
    cutoff = rules.visitor_purge_cutoff(now or timezone.now(), months)
    return SiteAttendance.objects.filter(organization=org, kind=SiteAttendance.KIND_VISITOR, user__isnull=True,
                                         signed_in_at__lt=cutoff), cutoff


def purge_visitor_signins(org, now=None, dry_run=False):
    """Delete them (inside org_context(org)). Returns (how many, the cut-off)."""
    qs, cutoff = due_for_purge(org, now)
    if dry_run:
        return qs.count(), cutoff
    deleted, _ = qs.delete()
    return deleted, cutoff
