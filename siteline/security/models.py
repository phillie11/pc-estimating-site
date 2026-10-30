"""
Two-factor sign-in records.

These belong to a person, not to a company: a User is global and can work in several
workspaces, and one authenticator app covers all of them. So they are plain models, not
TenantModels, and no workspace's admin can read another's. They are only ever looked up by
the signed-in user, or by a user an owner has picked from their own workspace's team.
"""
import secrets

from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher, check_password
from django.db import models, transaction
from django.utils import timezone

from . import totp

RECOVERY_CODE_COUNT = 10
# 32 letters and digits with the look-alikes (0/O, 1/I/L) taken out: 10 of them is 50 bits
RECOVERY_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
RECOVERY_CODE_LENGTH = 10
# Recovery codes are random, not chosen by people, so they need far fewer PBKDF2 rounds than a
# password to be out of reach offline, and checking ten of them on sign-in stays well under a second.
RECOVERY_HASH_ITERATIONS = 60_000


class TwoFactorDevice(models.Model):
    """An authenticator app. Unconfirmed until the person types a code from it."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='two_factor')
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The last time step a code was accepted for, so a code seen over someone's shoulder can't be reused
    last_used_step = models.BigIntegerField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'two-factor device'

    def __str__(self):
        return f'Authenticator for {self.user}'

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    def verify(self, code, now=None):
        """True if `code` is current and newer than the last one used. Records it as used."""
        step = totp.matching_step(self.secret, code, now=now)
        if step is None:
            return False
        with transaction.atomic():
            # Lock the row so two requests can't both spend the same code
            fresh = TwoFactorDevice.objects.select_for_update().get(pk=self.pk)
            if fresh.last_used_step is not None and step <= fresh.last_used_step:
                return False
            fresh.last_used_step, fresh.last_used_at = step, timezone.now()
            fresh.save(update_fields=['last_used_step', 'last_used_at'])
        self.last_used_step, self.last_used_at = fresh.last_used_step, fresh.last_used_at
        return True


def _hasher():
    return PBKDF2PasswordHasher()


def normalise_recovery_code(code):
    return ''.join(ch for ch in str(code or '').upper() if ch.isalnum())


class RecoveryCode(models.Model):
    """A single-use code for when the phone is lost. Only a salted PBKDF2 hash is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recovery_codes')
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Recovery code for {self.user} ({"used" if self.used_at else "unused"})'

    @classmethod
    def generate_for(cls, user):
        """Replace the user's codes with ten new ones and return them; this is the only time they're seen."""
        codes = [''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
                 for _ in range(RECOVERY_CODE_COUNT)]
        hasher = _hasher()
        with transaction.atomic():
            cls.objects.filter(user=user).delete()
            cls.objects.bulk_create([
                cls(user=user, code_hash=hasher.encode(c, hasher.salt(), RECOVERY_HASH_ITERATIONS))
                for c in codes])
        return [f'{c[:5]}-{c[5:]}' for c in codes]

    @classmethod
    def use(cls, user, code):
        """Spend a matching unused code. True if one matched."""
        code = normalise_recovery_code(code)
        if len(code) != RECOVERY_CODE_LENGTH:
            return False
        with transaction.atomic():
            for rc in cls.objects.select_for_update().filter(user=user, used_at__isnull=True):
                if check_password(code, rc.code_hash):
                    rc.used_at = timezone.now()
                    rc.save(update_fields=['used_at'])
                    return True
        return False

    @classmethod
    def remaining(cls, user):
        return cls.objects.filter(user=user, used_at__isnull=True).count()
