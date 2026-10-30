from django.conf import settings


def _lazy(org, fn):
    """Run fn only if a template asks, inside the request's organisation; remember the answer."""
    box = []

    def value():
        if not box:
            from .context import org_context
            with org_context(org):
                box.append(fn())
        return box[0]
    return value


def portal_switcher(request):
    """
    The portal's project switcher: open projects they collaborate on (portal_projects), each
    linking to its overview, then tenders they are pricing, linking to the tender page (the
    only page a tendering subcontractor may open).
    """
    from projects.access import portal_projects, visible_projects
    from projects.models import Project
    from tenders.services import tender_url
    shared = portal_projects(request).exclude(stage__in=Project.CLOSED_STAGES).order_by('name')
    entries = [{'pk': p.pk, 'name': p.name, 'sub': p.site_address, 'url': p.get_absolute_url()} for p in shared]
    seen = {e['pk'] for e in entries}
    tenders = visible_projects(request).exclude(stage__in=Project.CLOSED_STAGES).exclude(pk__in=seen).order_by('name')
    entries += [{'pk': p.pk, 'name': p.name, 'sub': 'Your tender', 'url': tender_url(p)} for p in tenders]
    return entries


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
    needs_me_count = 0
    switcher = []
    if org is not None and membership is not None:
        from projects.access import openable

        from .context import org_context
        from .models import Notification
        with org_context(org):  # the same alerts the notifications page lists
            unread = Notification.objects.filter(openable(request), user=request.user, read_at__isnull=True).count()

        import actions
        needs_me_count = _lazy(org, lambda: actions.count_for_user(request))
        if not membership.is_internal:
            switcher = _lazy(org, lambda: portal_switcher(request))
    # Which half of the system the sidebar shows; project and section pages override it
    section = None
    if membership is not None and membership.is_internal:
        sections = membership.sections
        section = request.session.get('section')
        section = section if section in sections else sections[0]
    is_internal = bool(membership and membership.is_internal)
    return {
        'section': section,
        'unread_notifications': unread,
        'needs_me_count': needs_me_count,
        'portal_switcher': switcher,
        # Staff get the sidebar; external collaborators get the portal's top bar (templates/layouts/)
        'app_layout': 'layouts/staff.html' if is_internal or membership is None else 'layouts/portal.html',
        'PRODUCT_NAME': settings.PRODUCT_NAME,
        'SUPPORT_EMAIL': settings.SUPPORT_EMAIL,
        'org': org,
        'membership': membership,
        'is_internal': is_internal,
        'is_org_admin': bool(membership and membership.is_admin),
        'other_orgs': other_orgs,
        'currency': org.currency_symbol if org else '£',
    }
