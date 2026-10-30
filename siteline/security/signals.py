from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import pre_save
from django.dispatch import receiver

from .twofactor import mark_authenticated


@receiver(user_logged_in, dispatch_uid='security.stamp_sign_in')
def stamp_sign_in(sender, request, user, **kwargs):
    """A sign-in (password, plus a code when two-factor is on) starts the step-up clock."""
    if request is not None and hasattr(request, 'session'):
        mark_authenticated(request)


def _drop_unconfirmed(user):
    """
    Set-up secrets now live in the session, but never leave an unconfirmed device behind (older
    rows, or anything else that made one): its secret could have been read by whoever started it.
    """
    from .models import TwoFactorDevice
    TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=True).delete()


@receiver(user_logged_out, dispatch_uid='security.drop_unconfirmed_on_logout')
def drop_unconfirmed_on_logout(sender, request, user, **kwargs):
    if user is not None and user.pk:
        _drop_unconfirmed(user)


@receiver(pre_save, sender=settings.AUTH_USER_MODEL, dispatch_uid='security.drop_unconfirmed_on_password_change')
def drop_unconfirmed_on_password_change(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not instance.pk or (update_fields is not None and 'password' not in update_fields):
        return
    old = sender._default_manager.filter(pk=instance.pk).values_list('password', flat=True).first()
    if old is not None and old != instance.password:
        _drop_unconfirmed(instance)
