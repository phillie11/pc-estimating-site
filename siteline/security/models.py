"""
Two-factor sign-in records.

These belong to a person, not to a company: a User is global and can work in several
workspaces, and one authenticator app covers all of them. So they are plain models, not
TenantModels, and no workspace's admin can read another's. They are only ever looked up by
the signed-in user, or by a user an owner has picked from their own workspace's team.
"""
import secrets

from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher
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


# Wrong codes in a row, from anywhere, before codes stop being accepted for the account. This
# doesn't depend on the cache or the IP address, so it holds however the attempts are spread out.
MAX_FAILED_CODES = 10


class SecurityEvent(models.Model):
    """
    Append-only record of two-factor changes for one person: turned on or off, locked after too
    many wrong codes, unlocked, reset by an owner. Like the device it belongs to the person, so
    `organization` is only set when one workspace's owner did it.
    """
    ENABLED, DISABLED, LOCKED, UNLOCKED, RESET = 'enabled', 'disabled', 'locked', 'unlocked', 'reset'
    KIND_CHOICES = [(ENABLED, 'Two-factor turned on'), (DISABLED, 'Two-factor turned off'),
                    (LOCKED, 'Locked after too many wrong codes'), (UNLOCKED, 'Unlocked'),
                    (RESET, 'Reset by an owner')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='security_events')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    organization = models.ForeignKey('tenancy.Organization', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+')
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.user}'

    @classmethod
    def record(cls, user, kind, actor=None, organization=None, detail=''):
        import logging
        logging.getLogger('security').warning('Two-factor %s for user %s (actor %s, organisation %s) %s',
                                              kind, user.pk, getattr(actor, 'pk', None),
                                              getattr(organization, 'pk', None), detail)
        return cls.objects.create(user=user, kind=kind, actor=actor, organization=organization, detail=detail[:255])


class TwoFactorDevice(models.Model):
    """
    A confirmed authenticator app. Rows are only created once the person has typed a code from
    it: the secret being set up lives in their session until then (security.views.two_factor_setup),
    so nobody else holding the password can read it in advance.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='two_factor')
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The last time step a code was accepted for, so a code seen over someone's shoulder can't be reused
    last_used_step = models.BigIntegerField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Wrong codes since the last right one; at MAX_FAILED_CODES codes stop working until unlocked
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'two-factor device'

    def __str__(self):
        return f'Authenticator for {self.user}'

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def is_locked(self):
        return self.locked_at is not None

    def _sync(self, fresh):
        for f in ('last_used_step', 'last_used_at', 'failed_attempts', 'locked_at'):
            setattr(self, f, getattr(fresh, f))

    def _failure(self, fresh):
        fresh.failed_attempts += 1
        fields = ['failed_attempts']
        if fresh.failed_attempts >= MAX_FAILED_CODES and fresh.locked_at is None:
            fresh.locked_at = timezone.now()
            fields.append('locked_at')
            SecurityEvent.record(self.user, SecurityEvent.LOCKED, detail=f'{fresh.failed_attempts} wrong codes in a row')
        fresh.save(update_fields=fields)

    def verify(self, code, now=None):
        """
        True if `code` is current, newer than the last one used, and the device isn't locked.
        Records it as used. A wrong or replayed code counts towards the lock.
        """
        step = totp.matching_step(self.secret, code, now=now)
        with transaction.atomic():
            # Lock the row so two requests can't both spend the same code or race the counter
            fresh = TwoFactorDevice.objects.select_for_update().get(pk=self.pk)
            if fresh.is_locked:
                ok = False
            elif step is None or (fresh.last_used_step is not None and step <= fresh.last_used_step):
                self._failure(fresh)
                ok = False
            else:
                fresh.last_used_step, fresh.last_used_at, fresh.failed_attempts = step, timezone.now(), 0
                fresh.save(update_fields=['last_used_step', 'last_used_at', 'failed_attempts'])
                ok = True
        self._sync(fresh)
        return ok

    def use_recovery_code(self, code):
        """Spend a recovery code. A right one also unlocks the device; a wrong one counts as a failure."""
        ok = RecoveryCode.use(self.user, code)
        with transaction.atomic():
            fresh = TwoFactorDevice.objects.select_for_update().get(pk=self.pk)
            if ok:
                was_locked = fresh.is_locked
                fresh.failed_attempts, fresh.locked_at = 0, None
                fresh.save(update_fields=['failed_attempts', 'locked_at'])
                if was_locked:
                    SecurityEvent.record(self.user, SecurityEvent.UNLOCKED, detail='with a recovery code')
            else:
                self._failure(fresh)
        self._sync(fresh)
        return ok


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
        # Verify with the hasher that made the codes, not the site's PASSWORD_HASHERS list.
        hasher = _hasher()
        with transaction.atomic():
            for rc in cls.objects.select_for_update().filter(user=user, used_at__isnull=True):
                if hasher.verify(code, rc.code_hash):
                    rc.used_at = timezone.now()
                    rc.save(update_fields=['used_at'])
                    return True
        return False

    @classmethod
    def remaining(cls, user):
        return cls.objects.filter(user=user, used_at__isnull=True).count()


class RateLimitCounter(models.Model):
    """
    One rate-limit window's count (security/ratelimit.py). A table of its own rather than the
    cache, so counters are never culled to make room for something else, and each increment is a
    single atomic UPDATE on SQLite, MySQL and PostgreSQL alike. Keys hold hashes, not addresses or
    emails. Rows for finished windows are deleted as new windows start.
    """
    key = models.CharField(max_length=100, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f'{self.key}: {self.count}'
