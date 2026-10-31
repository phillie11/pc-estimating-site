from django.db import connection
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from .context import clear_memo_on_write, close_request_memo, open_request_memo, reset_current_org, set_current_org
from .models import Membership

# Views under an org that stay reachable when the subscription has lapsed,
# so an admin can pay and get back in.
BILLING_EXEMPT_URL_NAMES = {'billing', 'billing_checkout', 'billing_portal', 'billing_success', 'org_locked'}


class TenantMiddleware:
    """
    For any URL with an ``org_slug`` kwarg:
      * the user must be logged in,
      * the user must have an active Membership in that organisation
        (otherwise 404, so we never confirm a workspace exists),
      * the subscription must be in good standing (otherwise a lock page),
      * request.org / request.membership are set and every TenantModel query
        is scoped to that organisation for the rest of the request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.org = None
        request.membership = None
        request._org_token = None
        memo = open_request_memo()
        try:
            with connection.execute_wrapper(clear_memo_on_write):
                return self.get_response(request)
        finally:
            if request._org_token is not None:
                reset_current_org(request._org_token)
                request._org_token = None
            close_request_memo(memo)

    def process_view(self, request, view_func, view_args, view_kwargs):
        slug = view_kwargs.get('org_slug')
        if not slug:
            return None
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")

        # One query for both: no such workspace and not a member are the same 404
        membership = (
            Membership.objects.filter(organization__slug=slug, user=request.user, is_active=True)
            .select_related('organization')
            .first()
        )
        if membership is None:
            raise Http404
        org = membership.organization

        request.org = org
        request.membership = membership
        request._org_token = set_current_org(org)

        if request.user.last_org_slug != org.slug:
            request.user.last_org_slug = org.slug
            request.user.save(update_fields=['last_org_slug'])

        url_name = getattr(request.resolver_match, 'url_name', None)
        if not org.has_access and url_name not in BILLING_EXEMPT_URL_NAMES:
            if membership.is_admin and org.access_state() != 'suspended':
                return redirect('billing', org_slug=org.slug)
            return render(request, 'tenancy/locked.html', {'state': org.access_state()}, status=402)
        return None
