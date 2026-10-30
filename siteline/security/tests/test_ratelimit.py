"""The cache-based rate limiter and the routes it guards."""
import dataclasses
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import caches
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from security import ratelimit
from security.ip import client_ip

from .base import SecurityTestBase, code_for


def with_limit(scope, limit):
    """Shrink one rule's limit for a test."""
    rules = tuple(dataclasses.replace(r, limit=limit) if r.scope == scope else r for r in ratelimit.RULES)
    return mock.patch.object(ratelimit, 'RULES', rules)


class LimiterTests(SecurityTestBase):
    def test_fixed_window(self):
        for i in range(3):
            self.assertTrue(ratelimit.hit('t', 'a', limit=3, window=60, now=1000))
        self.assertFalse(ratelimit.hit('t', 'a', limit=3, window=60, now=1001))
        self.assertTrue(ratelimit.hit('t', 'b', limit=3, window=60, now=1001))  # someone else
        self.assertTrue(ratelimit.hit('t', 'a', limit=3, window=60, now=1020))  # next window

    def test_keys_do_not_hold_raw_identifiers(self):
        key, _ = ratelimit._key('signup', 'person@example.com', 60, 0)
        self.assertNotIn('person', key)

    def test_uses_the_database_cache(self):
        self.assertEqual(type(caches['default']).__name__, 'DatabaseCache')


class RouteLimitTests(SecurityTestBase):
    def assert_limited(self, response):
        self.assertEqual(response.status_code, 429)
        self.assertTemplateUsed(response, 'security/429.html')
        self.assertContains(response, 'Too many tries', status_code=429)
        self.assertGreater(int(response['Retry-After']), 0)

    def test_signup(self):
        for _ in range(10):
            self.assertEqual(self.client.post(reverse('signup'), {}).status_code, 200)
        self.assert_limited(self.client.post(reverse('signup'), {}))
        self.assertEqual(self.client.get(reverse('signup')).status_code, 200)  # only POSTs count

    def test_password_reset_per_email(self):
        for _ in range(3):
            self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'staff@a.test'}).status_code, 302)
        self.assert_limited(self.client.post(reverse('password_reset'), {'email': 'STAFF@a.test'}))
        self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'owner@a.test'}).status_code, 302)

    def test_team_invites(self):
        self.login(self.owner)
        with with_limit('team-invite', 2):
            for i in range(2):
                self.client.post(self.url('team'), {'email': f'n{i}@a.test', 'role': 'MEMBER', 'area': 'BOTH'})
            self.assert_limited(self.client.post(self.url('team'), {'email': 'n9@a.test', 'role': 'MEMBER'}))

    def test_project_invites_but_not_other_team_actions(self):
        self.login(self.owner)
        with with_limit('project-invite', 1):
            u = self.url('project_team', self.project.pk)
            self.client.post(u, {'action': 'invite', 'email': 'x@x.test'})
            self.assert_limited(self.client.post(u, {'action': 'invite', 'email': 'y@x.test'}))
            self.assertNotEqual(self.client.post(u, {'action': 'remove', 'member': 0}).status_code, 429)

    def test_totp_verification(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)
        with with_limit('totp', 2):
            self.client.post(reverse('two_factor_verify'), {'code': '000000'})
            self.client.post(reverse('two_factor_verify'), {'code': '000000'})
            self.assert_limited(self.client.post(reverse('two_factor_verify'), {'code': code_for(device)}))

    def test_uploads(self):
        self.login(self.staff)
        with with_limit('upload', 1):
            u = self.url('issue_create', self.project.pk)
            f = lambda: SimpleUploadedFile('a.png', b'\x89PNG\r\n', content_type='image/png')  # noqa: E731
            self.assertNotEqual(self.client.post(u, {'title': 'x', 'photo': f()}).status_code, 429)
            self.assert_limited(self.client.post(u, {'title': 'x', 'photo': f()}))
            # An ordinary form post isn't an upload
            self.assertNotEqual(self.client.post(u, {'title': 'x'}, content_type='application/x-www-form-urlencoded')
                                .status_code, 429)

    def test_exports_and_search(self):
        self.login(self.staff)
        with with_limit('export', 1), with_limit('search', 1):
            u = self.url('project_list')
            self.assertEqual(self.client.get(u + '?export=csv').status_code, 200)
            self.assert_limited(self.client.get(u + '?export=csv'))
        with with_limit('search', 1):
            self.assertEqual(self.client.get(u + '?q=alpha').status_code, 200)
            self.assert_limited(self.client.get(u + '?q=alpha'))
            self.assertEqual(self.client.get(u).status_code, 200)

    def test_stripe_webhook_is_generous_but_limited(self):
        rule = next(r for r in ratelimit.RULES if r.scope == 'stripe-webhook')
        self.assertGreaterEqual(rule.limit / rule.window, 5)  # several a second is fine
        with with_limit('stripe-webhook', 2):
            for _ in range(2):
                self.assertNotEqual(self.client.post(reverse('stripe_webhook'), b'{}',
                                                     content_type='application/json').status_code, 429)
            self.assert_limited(self.client.post(reverse('stripe_webhook'), b'{}', content_type='application/json'))

    @override_settings(RATELIMIT_ENABLED=False)
    def test_can_be_switched_off(self):
        for _ in range(12):
            self.assertEqual(self.client.post(reverse('signup'), {}).status_code, 200)


class ClientIpTests(SimpleTestCase):
    def req(self, **meta):
        return RequestFactory().get('/', REMOTE_ADDR='10.0.0.1', **meta)

    def test_remote_addr_by_default_and_forged_headers_ignored(self):
        self.assertEqual(client_ip(self.req(HTTP_X_FORWARDED_FOR='1.2.3.4')), '10.0.0.1')

    @override_settings(CLIENT_IP_HEADER='X-Forwarded-For', CLIENT_IP_PROXY_COUNT=1)
    def test_load_balancer_takes_the_address_it_added(self):
        self.assertEqual(client_ip(self.req(HTTP_X_FORWARDED_FOR='6.6.6.6, 81.2.69.160')), '81.2.69.160')

    @override_settings(CLIENT_IP_HEADER='X-Real-IP')
    def test_pythonanywhere_header(self):
        self.assertEqual(client_ip(self.req(HTTP_X_REAL_IP='81.2.69.160')), '81.2.69.160')
        self.assertEqual(client_ip(self.req(HTTP_X_REAL_IP='not-an-ip')), '10.0.0.1')
