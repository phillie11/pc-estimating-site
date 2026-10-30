"""Regression tests for the security review of PR #12 (M1-M3, L1-L3)."""
import time
from unittest import mock

from django.core import mail
from django.core.cache import caches
from django.test import override_settings
from django.urls import reverse

from security import checks, ratelimit, totp
from security.models import MAX_FAILED_CODES, RateLimitCounter, RecoveryCode, SecurityEvent, TwoFactorDevice

from .base import PW, SecurityTestBase, code_for


def wrong_code(device):
    right = {code_for(device, o) for o in (-1, 0, 1)}
    return next(c for c in ('000000', '111111', '222222', '333333') if c not in right)


class PendingSecretTests(SecurityTestBase):
    """M1: someone holding the password must not be able to read the key the owner will later scan."""

    def setup_secret(self, client):
        r = client.get(reverse('two_factor_setup'))
        return r.context['secret'].replace(' ', '')

    def test_secret_not_stored_until_confirmed(self):
        self.login(self.staff)
        self.setup_secret(self.client)
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.staff).exists())

    def test_another_session_gets_a_different_secret(self):
        attacker = self.client_class()
        attacker.force_login(self.staff, backend='django.contrib.auth.backends.ModelBackend')
        seen = self.setup_secret(attacker)
        attacker.post(reverse('logout'))
        self.login(self.staff)
        shown = self.setup_secret(self.client)
        self.assertNotEqual(seen, shown)
        # The attacker's key doesn't work for the victim's set-up
        r = self.client.post(reverse('two_factor_setup'), {'code': totp.code_at(seen)})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.staff).exists())

    def test_confirming_uses_the_secret_shown_in_this_session(self):
        self.login(self.staff)
        secret = self.setup_secret(self.client)
        qr = self.client.get(reverse('two_factor_qr'))
        self.assertIn(b'<svg', qr.content)
        self.client.post(reverse('two_factor_setup'), {'code': totp.code_at(secret)})
        device = TwoFactorDevice.objects.get(user=self.staff)
        self.assertTrue(device.is_confirmed)
        self.assertEqual(device.secret, secret)
        self.assertNotIn('sl_2fa_setup_secret', self.client.session)

    def test_left_over_unconfirmed_device_removed_on_logout_and_password_change(self):
        TwoFactorDevice.objects.create(user=self.staff, secret=totp.new_secret())
        self.login(self.staff)
        self.client.post(reverse('logout'))
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.staff).exists())
        TwoFactorDevice.objects.create(user=self.staff, secret=totp.new_secret())
        self.staff.set_password('New-Password-77')
        self.staff.save()
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.staff).exists())

    def test_password_change_keeps_a_confirmed_device(self):
        self.enrol(self.staff)
        self.staff.set_password('New-Password-77')
        self.staff.save()
        self.assertTrue(TwoFactorDevice.objects.filter(user=self.staff, confirmed_at__isnull=False).exists())


class PlatformStaffResetTests(SecurityTestBase):
    """M2: a workspace owner can't reset the two-factor of platform staff."""

    def test_owner_cannot_reset_platform_staff_or_superuser(self):
        for flag in ('is_staff', 'is_superuser'):
            setattr(self.staff, flag, True)
            self.staff.save()
            self.enrol(self.staff) if not TwoFactorDevice.objects.filter(user=self.staff).exists() else None
            self.login(self.owner)
            r = self.client.post(self.url('two_factor_reset_member', self.m_staff.pk), follow=True)
            self.assertContains(r, 'support can reset')
            self.assertTrue(TwoFactorDevice.objects.filter(user=self.staff).exists(), flag)
            setattr(self.staff, flag, False)
            self.staff.save()


class RateLimitCounterTests(SecurityTestBase):
    """M3 and the PostgreSQL CI failure: counters are rows of their own, never culled, safe in a transaction."""

    def test_counters_are_rows_not_cache_entries(self):
        ratelimit.hit('x', 'y', 5, 60)
        key = ratelimit._key('x', 'y', 60, time.time())[0]
        self.assertEqual(RateLimitCounter.objects.get(key=key).count, 1)
        self.assertIsNone(caches['default'].get(key))
        self.assertNotIn('y', key)  # hashed

    def test_flooding_the_cache_does_not_evict_counters(self):
        for _ in range(10):
            ratelimit.hit('totp-session', 'p42', 10, 600)
        default = caches['default']
        with mock.patch.object(default, '_max_entries', 30):
            for i in range(60):
                default.set(f'zz-filler-{i}', i)  # sorts after 'rl:', so a cache cull would take counters first
        self.assertFalse(ratelimit.hit('totp-session', 'p42', 10, 600))

    def test_many_addresses_do_not_reset_another_counter(self):
        for _ in range(10):
            ratelimit.hit('totp-session', 'p42', 10, 600)
        with mock.patch.object(caches['default'], '_max_entries', 30):  # as if the cache were full
            for i in range(60):
                ratelimit.hit('totp', f'198.51.100.{i}', 10, 600)
        self.assertFalse(ratelimit.hit('totp-session', 'p42', 10, 600))

    def test_lost_insert_race_still_counts_and_keeps_the_transaction_usable(self):
        """An IntegrityError on the first insert must not abort the caller's transaction (PostgreSQL)."""
        from django.db import transaction
        ratelimit.hit('race', 'z', 5, 600)
        key = ratelimit._key('race', 'z', 600, time.time())[0]
        real_update = RateLimitCounter.objects.filter(key=key).update
        calls = []

        def first_update_misses(**kwargs):
            calls.append(1)
            return 0 if len(calls) == 1 else real_update(**kwargs)

        with transaction.atomic():
            with mock.patch('django.db.models.query.QuerySet.update', autospec=True,
                            side_effect=lambda qs, **kw: first_update_misses(**kw)):
                self.assertTrue(ratelimit.hit('race', 'z', 5, 600))
            self.assertEqual(RateLimitCounter.objects.get(key=key).count, 2)
            self.assertTrue(self.staff.__class__.objects.filter(pk=self.staff.pk).exists())  # still usable

    def test_finished_windows_are_pruned(self):
        ratelimit.hit('old', 'a', 5, 60, now=1000)
        ratelimit.hit('new', 'b', 5, 60, now=5000)
        self.assertFalse(RateLimitCounter.objects.filter(key__startswith='rl:old:').exists())

    def test_ipv6_counted_per_64(self):
        ident = ratelimit.identity
        req = mock.Mock(user=None, session={})
        self.assertEqual(ident(req, 'ip', '2001:db8:1:2::1'), ident(req, 'ip', '2001:db8:1:2:ffff::9'))
        self.assertNotEqual(ident(req, 'ip', '2001:db8:1:2::1'), ident(req, 'ip', '2001:db8:1:3::1'))
        self.assertEqual(ident(req, 'ip', '81.2.69.160'), '81.2.69.160')
        self.assertEqual(ident(req, 'ip', '::ffff:81.2.69.160'), '81.2.69.160')
        self.assertEqual(ident(req, 'ip', 'unknown'), 'unknown')

    def test_ipv6_flood_hits_one_limit(self):
        with mock.patch.object(ratelimit, 'RULES', (ratelimit.Rule('signup', ('signup',), limit=3, window=600),)):
            codes = [self.client.post(reverse('signup'), {}, REMOTE_ADDR=f'2001:db8:1:2::{i:x}').status_code
                     for i in range(1, 6)]
        self.assertEqual(codes[-1], 429)

    def test_no_address_is_none_not_a_placeholder(self):
        """force_login()'s bare request has no REMOTE_ADDR; axes stores the address in an inet column."""
        from django.http import HttpRequest
        from security.ip import client_ip
        self.assertIsNone(client_ip(HttpRequest()))
        self.login(self.staff)  # records a login with axes; failed on PostgreSQL with 'unknown'


class FailedCodeLockTests(SecurityTestBase):
    """M3: ten wrong codes in a row lock two-factor whatever the IP address."""

    def test_locks_after_ten_failures_across_addresses(self):
        device, codes = self.enrol(self.staff)
        bad = wrong_code(device)
        for i in range(MAX_FAILED_CODES):
            self.assertFalse(device.verify(bad))
        device.refresh_from_db()
        self.assertTrue(device.is_locked)
        self.assertFalse(device.verify(code_for(device)))  # even the right code
        self.assertTrue(SecurityEvent.objects.filter(user=self.staff, kind=SecurityEvent.LOCKED).exists())

        # Through the sign-in page, from new addresses each time, the right code still fails
        for i in range(3):
            c = self.client_class(REMOTE_ADDR=f'10.0.0.{i + 1}')
            c.post(reverse('login'), {'username': self.staff.email, 'password': PW})
            r = c.post(reverse('two_factor_verify'), {'code': code_for(device)})
            self.assertContains(r, 'locked')
            self.assertNotIn('_auth_user_id', c.session)

    def test_correct_code_resets_the_run(self):
        device, _ = self.enrol(self.staff)
        bad = wrong_code(device)
        for _ in range(MAX_FAILED_CODES - 1):
            device.verify(bad)
        self.assertTrue(device.verify(code_for(device)))
        device.refresh_from_db()
        self.assertEqual(device.failed_attempts, 0)
        device.verify(bad)
        device.refresh_from_db()
        self.assertFalse(device.is_locked)

    def test_recovery_code_unlocks(self):
        device, codes = self.enrol(self.staff)
        bad = wrong_code(device)
        for _ in range(MAX_FAILED_CODES):
            device.verify(bad)
        self.password_step(self.staff)
        r = self.client.post(reverse('two_factor_verify'), {'recovery_code': codes[0]})
        self.assertIn('_auth_user_id', self.client.session)
        device.refresh_from_db()
        self.assertFalse(device.is_locked)
        self.assertEqual(device.failed_attempts, 0)
        self.assertTrue(SecurityEvent.objects.filter(user=self.staff, kind=SecurityEvent.UNLOCKED).exists())
        del r

    def test_owner_reset_unlocks_and_is_audited(self):
        device, _ = self.enrol(self.staff)
        for _ in range(MAX_FAILED_CODES):
            device.verify(wrong_code(device))
        self.login(self.owner)
        self.client.post(self.url('two_factor_reset_member', self.m_staff.pk))
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.staff).exists())
        ev = SecurityEvent.objects.get(user=self.staff, kind=SecurityEvent.RESET)
        self.assertEqual((ev.actor, ev.organization), (self.owner, self.org))

    def test_step_up_code_refused_while_locked_password_still_works(self):
        device, _ = self.enrol(self.staff)
        for _ in range(MAX_FAILED_CODES):
            device.verify(wrong_code(device))
        self.login(self.staff)
        r = self.client.post(reverse('step_up'), {'code': code_for(device), 'next': '/home/'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse('step_up'), {'password': PW, 'next': '/home/'})
        self.assertEqual(r.status_code, 302)


class StepUpAdditionsTests(SecurityTestBase):
    """L1: inviting external collaborators and company settings need a recent sign-in too."""

    def test_project_invites_and_settings(self):
        self.login(self.owner)
        self.make_stale()
        r = self.client.post(self.url('project_team', self.project.pk), {'action': 'invite', 'email': 'x@x.test'})
        self.assertTrue(r.url.startswith(reverse('step_up')))
        r = self.client.post(self.url('org_settings'), {'name': 'Renamed'})
        self.assertTrue(r.url.startswith(reverse('step_up')))
        # Other project-team actions and reading settings are unaffected
        self.assertEqual(self.client.get(self.url('org_settings')).status_code, 200)
        r = self.client.post(self.url('project_team', self.project.pk), {'action': 'remove', 'member': 0})
        self.assertNotIn(reverse('step_up'), r.get('Location', ''))


class ResetNoticeTests(SecurityTestBase):
    """L2: the member is told, and told what to do if they didn't expect it."""

    def test_reset_email_wording(self):
        self.enrol(self.staff)
        self.login(self.owner)
        self.client.post(self.url('two_factor_reset_member', self.m_staff.pk))
        body = mail.outbox[-1].body
        self.assertIn('your two-factor sign-in was reset', body.lower())
        self.assertIn("If this wasn't expected", body)
        self.assertIn('contact', body)
        self.assertIn('Olive Owner', body)


class ClientIpCheckTests(SecurityTestBase):
    """L3: warn if CLIENT_IP_HEADER is trusted while the origin can be reached directly by IP."""

    @override_settings(DEBUG=False, CLIENT_IP_HEADER='X-Real-IP', ALLOWED_HOSTS=['siteline.example', '203.0.113.7'])
    def test_warns_with_a_bare_ip_host(self):
        self.assertEqual([w.id for w in checks.client_ip_header_trusted(None)], ['security.W003'])

    @override_settings(DEBUG=False, CLIENT_IP_HEADER='X-Real-IP', ALLOWED_HOSTS=['siteline.example'])
    def test_quiet_with_names_only(self):
        self.assertEqual(checks.client_ip_header_trusted(None), [])

    @override_settings(DEBUG=False, CLIENT_IP_HEADER='', ALLOWED_HOSTS=['203.0.113.7'])
    def test_quiet_without_the_header(self):
        self.assertEqual(checks.client_ip_header_trusted(None), [])


class RecoveryCodeStillSingleUse(SecurityTestBase):
    def test_used_code_cannot_unlock_twice(self):
        device, codes = self.enrol(self.staff)
        self.assertTrue(device.use_recovery_code(codes[1]))
        self.assertFalse(device.use_recovery_code(codes[1]))
        self.assertEqual(RecoveryCode.remaining(self.staff), 9)
