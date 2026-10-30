from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .twofactor import mark_authenticated


@receiver(user_logged_in, dispatch_uid='security.stamp_sign_in')
def stamp_sign_in(sender, request, user, **kwargs):
    """A sign-in (password, plus a code when two-factor is on) starts the step-up clock."""
    if request is not None and hasattr(request, 'session'):
        mark_authenticated(request)
