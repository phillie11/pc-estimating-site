from django.conf import settings


def tenant(request):
    org = getattr(request, 'org', None)
    membership = getattr(request, 'membership', None)
    other_orgs = []
    if getattr(request, 'user', None) is not None and request.user.is_authenticated:
        other_orgs = [
            m.organization for m in request.user.memberships.filter(is_active=True).select_related('organization')
            if org is None or m.organization_id != org.pk
        ]
    unread = 0
    if org is not None and membership is not None:
        from .models import Notification
        unread = Notification.all_objects.filter(organization=org, user=request.user, read_at__isnull=True).count()
    return {
        'unread_notifications': unread,
        'PRODUCT_NAME': settings.PRODUCT_NAME,
        'SUPPORT_EMAIL': settings.SUPPORT_EMAIL,
        'org': org,
        'membership': membership,
        'is_internal': bool(membership and membership.is_internal),
        'is_org_admin': bool(membership and membership.is_admin),
        'other_orgs': other_orgs,
        'currency': org.currency_symbol if org else '£',
    }
