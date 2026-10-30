"""
Who may see and act on records, for code that reads RecordIndex or declares transitions.

Transition predicates get (record, user) with no request, so they check roles here
rather than through request.membership.
"""
from projects.access import is_internal, portal_projects
from tenancy.models import Membership

from .models import VIS_SHARED, RecordIndex


def _has_role(user, org_id, roles):
    if not getattr(user, 'is_authenticated', False):
        return False
    return Membership.objects.filter(organization_id=org_id, user=user, is_active=True, role__in=roles).exists()


def is_staff(user, org_id):
    """An active owner, admin or staff member of the organisation (not an external collaborator)."""
    return _has_role(user, org_id, Membership.INTERNAL_ROLES)


def is_admin(user, org_id):
    return _has_role(user, org_id, Membership.ADMIN_ROLES)


def is_owner(user, org_id):
    return _has_role(user, org_id, (Membership.ROLE_OWNER,))


# --- Record housekeeping rules --------------------------------------------------
# Recommended defaults, pending the product owner's confirmation (ADR 0001). Change them here:
# RecordBase.soft_delete/restore/purge, legal hold changes and redact() all ask these.

def can_soft_delete(user, record):
    """Admins: anything. Staff: their own records while still a draft (the type's draft_statuses)."""
    from .registry import get_record_type
    if is_admin(user, record.organization_id):
        return True
    return (is_staff(user, record.organization_id) and record.created_by_id == getattr(user, 'pk', None)
            and record.status in get_record_type(record).draft_statuses)


def can_restore(user, record):
    """Admins only."""
    return is_admin(user, record.organization_id)


def can_purge(user, record):
    """Owners only. Nothing purges automatically; an owner-only purge action is still to come."""
    return is_owner(user, record.organization_id)


def can_set_legal_hold(user, org_id):
    """Owners and admins set and clear legal hold."""
    return is_admin(user, org_id)


def can_redact(user, org_id):
    """GDPR erasure: owners and admins of the organisation, or a platform superuser."""
    return bool(getattr(user, 'is_superuser', False)) or is_admin(user, org_id)


def record_staff(record, user):
    """Transition predicate: staff of the record's organisation."""
    return is_staff(user, record.organization_id)


def record_staff_or_ball_in_court(record, user):
    """Transition predicate: staff, or whoever the record is waiting on."""
    return record.ball_in_court_user_id == getattr(user, 'pk', None) or record_staff(record, user)


def visible_index(request):
    """
    Index rows this person may see: records on projects whose shared content they can open
    (portal_projects), and only shared ones for external collaborators. Record types with
    narrower rules than visibility (a subcontractor seeing only their own sub variations)
    must not be read from here for externals until the index carries those rules; see the ADR.
    """
    qs = RecordIndex.objects.filter(project__in=portal_projects(request))
    if not is_internal(request):
        qs = qs.filter(visibility=VIS_SHARED)
    return qs


def needs_me(request):
    """Open records waiting on this user, soonest due first."""
    return visible_index(request).filter(ball_in_court_user=request.user, is_open=True) \
        .order_by('due_date', 'ref').select_related('project')
