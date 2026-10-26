"""Small, explicit access helpers used by every view."""
from functools import wraps

from django.core.exceptions import PermissionDenied


def internal_required(view):
    """Staff of the organisation only (not external collaborators)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.membership is None or not request.membership.is_internal:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.membership is None or not request.membership.is_admin:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.membership is None or not request.membership.is_owner:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def estimating_required(view):
    """Staff who work in estimating (bids, tenders, pipeline, analytics)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.membership is None or not request.membership.can_estimate:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def delivery_required(view):
    """Staff who work on won and live projects (procurement, site, commercial)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.membership is None or not request.membership.can_deliver:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper
