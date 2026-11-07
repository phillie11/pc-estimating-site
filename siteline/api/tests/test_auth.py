"""Tokens: hash only, constant-time checks, revoking, running out, the maker's role, limits, the log."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from api import auth
from api.models import SCOPE_COMMERCIAL, SCOPE_SAFETY, ApiAccess, ApiToken
from projects.models import FileAccess
from tenancy.context import org_context
from tenancy.models import Membership, Organization

from .base import ApiTestBase


class TokenStorageTests(ApiTestBase):
    def test_only_a_salted_hash_is_stored(self):
        prefix, secret = auth.parse(self.text_a)
        with org_context(self.org_a):
            row = ApiToken.objects.get(pk=self.token_a.pk)
        self.assertEqual(row.prefix, prefix)
        self.assertNotIn(secret, row.secret_hash)
        self.assertTrue(row.secret_hash.startswith(f'pbkdf2_sha256${auth.TOKEN_HASH_ITERATIONS}$'))
        for value in (row.name, row.prefix, row.secret_hash, str(row.scopes)):
            self.assertNotIn(secret, value)
        # A second token has a different salt and prefix
        _, other = self.make_token(self.org_a, self.alice)
        self.assertNotEqual(auth.parse(other)[0], prefix)

    def test_token_text_shape(self):
        self.assertTrue(self.text_a.startswith('slk_'))
        self.assertIsNotNone(auth.parse(self.text_a))
        for bad in ('', 'slk_', 'slk_short_x', self.text_a + 'x', self.text_a.replace('slk_', 'xyz_'),
                    self.text_a[:-1] + '!'):
            self.assertIsNone(auth.parse(bad), bad)

    def test_the_secret_is_checked_with_the_constant_time_hasher_even_for_unknown_tokens(self):
        calls = []
        real = PBKDF2PasswordHasher.verify

        def spy(self_, password, encoded):
            calls.append(encoded)
            return real(self_, password, encoded)
        with mock.patch.object(PBKDF2PasswordHasher, 'verify', spy):
            self.assertIsNone(auth.find('slk_' + 'A' * 10 + '_' + 'B' * 40))   # no such prefix
            self.assertIsNone(auth.find('not a token'))
            prefix, _ = auth.parse(self.text_a)
            self.assertIsNone(auth.find(f'slk_{prefix}_' + 'C' * 40))           # right prefix, wrong secret
            self.assertEqual(auth.find(self.text_a).pk, self.token_a.pk)
        self.assertEqual(len(calls), 4)  # one hash check every time, found or not

    def test_django_hasher_verify_is_constant_time(self):
        # PBKDF2PasswordHasher.verify compares with constant_time_compare; keep it that way
        with mock.patch('django.contrib.auth.hashers.constant_time_compare', return_value=True) as ctc:
            PBKDF2PasswordHasher().verify('x', auth.hash_secret('y'))
        ctc.assert_called()


class AuthenticationTests(ApiTestBase):
    url = '/api/v1/projects/'

    def test_no_token_wrong_token_and_the_session_are_all_refused(self):
        self.assertEqual(self.api(self.url).status_code, 401)
        self.assertEqual(self.api(self.url).headers['WWW-Authenticate'], 'Bearer realm="siteline"')
        self.assertEqual(self.api(self.url, 'slk_nonsense').status_code, 401)
        prefix, _ = auth.parse(self.text_a)
        self.assertEqual(self.api(self.url, f'slk_{prefix}_' + 'Z' * 40).status_code, 401)
        # Signed in as the owner, with no token: the API never reads the session
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.api(self.url, self.text_a).status_code, 200)

    def test_token_in_the_query_string_is_not_accepted(self):
        resp = self.client.get(self.url, {'token': self.text_a})
        self.assertEqual(resp.status_code, 401)
        resp = self.client.get(self.url, HTTP_AUTHORIZATION=f'Token {self.text_a}')
        self.assertEqual(resp.status_code, 401)

    def test_read_only(self):
        for method in ('post', 'put', 'patch', 'delete'):
            resp = self.api(self.url, self.text_a, method=method)
            self.assertEqual(resp.status_code, 405, method)
            self.assertEqual(resp['Allow'], 'GET, HEAD')
        self.assertEqual(self.client.head(self.url, HTTP_AUTHORIZATION=f'Bearer {self.text_a}').status_code, 200)

    def test_revoked_token_stops_working(self):
        self.assertEqual(self.api(self.url, self.text_a).status_code, 200)
        with org_context(self.org_a):
            ApiToken.objects.filter(pk=self.token_a.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.api(self.url, self.text_a).status_code, 401)

    def test_run_out_token_stops_working(self):
        token, text = self.make_token(self.org_a, self.alice, expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.api(self.url, text).status_code, 200)
        with org_context(self.org_a):
            ApiToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.api(self.url, text).status_code, 401)

    def test_token_stops_when_its_maker_is_no_longer_an_owner_or_admin(self):
        token, text = self.make_token(self.org_a, self.adam)
        self.assertEqual(self.api(self.url, text).status_code, 200)
        with org_context(self.org_a):
            Membership.objects.filter(organization=self.org_a, user=self.adam).update(role=Membership.ROLE_MEMBER)
        self.assertEqual(self.api(self.url, text).status_code, 401)
        with org_context(self.org_a):
            Membership.objects.filter(organization=self.org_a, user=self.adam).update(role=Membership.ROLE_ADMIN,
                                                                                       is_active=False)
        self.assertEqual(self.api(self.url, text).status_code, 401)
        with org_context(self.org_a):
            Membership.objects.filter(organization=self.org_a, user=self.adam).update(is_active=True)
        self.assertEqual(self.api(self.url, text).status_code, 200)
        self.adam.is_active = False
        self.adam.save()
        self.assertEqual(self.api(self.url, text).status_code, 401)

    def test_locked_workspace(self):
        Organization.objects.filter(pk=self.org_a.pk).update(subscription_status=Organization.STATUS_CANCELED)
        resp = self.api(self.url, self.text_a)
        self.assertEqual(resp.status_code, 402)
        self.assertIn('Billing', resp.json()['error']['message'])

    def test_resource_groups(self):
        _, text = self.make_token(self.org_a, self.alice, scopes=[])
        self.assertEqual(self.api('/api/v1/projects/', text).status_code, 200)  # always included
        for path in ('/api/v1/rfis/', '/api/v1/documents/', '/api/v1/snags/', '/api/v1/applications/',
                     '/api/v1/cost-reports/', '/api/v1/sub-payments/', '/api/v1/cvr/', '/api/v1/incidents/'):
            self.assertEqual(self.api(path, text).status_code, 403, path)
        _, text = self.make_token(self.org_a, self.alice, scopes=[SCOPE_COMMERCIAL])
        self.assertEqual(self.api('/api/v1/applications/', text).status_code, 200)
        self.assertEqual(self.api('/api/v1/incidents/', text).status_code, 403)
        _, text = self.make_token(self.org_a, self.alice, scopes=[SCOPE_SAFETY])
        self.assertEqual(self.api('/api/v1/incidents/', text).status_code, 200)

    def test_bad_parameters_say_how_to_fix_them(self):
        resp = self.api('/api/v1/rfis/', self.text_a, projct='1')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Unknown parameter projct', resp.json()['error']['message'])
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, project='x').status_code, 400)
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, status='NOPE').status_code, 400)
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, updated_since='last week').status_code, 400)
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, page_size='500').status_code, 400)
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, page='99').status_code, 404)
        self.assertEqual(self.api('/api/v1/cvr/', self.text_a, month='2099-01').status_code, 400)


class LimitAndLogTests(ApiTestBase):
    url = '/api/v1/projects/'

    def test_every_call_is_logged_with_its_answer(self):
        self.api(self.url, self.text_a)
        self.api('/api/v1/projects/999999/', self.text_a)
        _, narrow = self.make_token(self.org_a, self.alice, scopes=[])
        self.api('/api/v1/incidents/', narrow)
        with org_context(self.org_a):
            calls = list(ApiAccess.objects.order_by('pk').values_list('path', 'status'))
        self.assertEqual(calls, [('/api/v1/projects/', 200), ('/api/v1/projects/999999/', 404),
                                 ('/api/v1/incidents/', 403)])
        with org_context(self.org_b):
            self.assertFalse(ApiAccess.objects.exists())

    def test_last_used_is_recorded(self):
        self.assertIsNone(self.token_a.last_used_at)
        self.api(self.url, self.text_a)
        with org_context(self.org_a):
            self.assertIsNotNone(ApiToken.objects.get(pk=self.token_a.pk).last_used_at)

    def test_old_log_rows_are_pruned(self):
        with org_context(self.org_a):
            old = ApiAccess.objects.create(token=self.token_a, method='GET', path='/old', status=200,
                                           at=timezone.now() - timedelta(days=auth.KEEP_DAYS + 1))
        self.api(self.url, self.text_a)
        with org_context(self.org_a):
            self.assertFalse(ApiAccess.objects.filter(pk=old.pk).exists())

    def test_rate_limit_per_token(self):
        with mock.patch.object(auth, 'LIMIT', 3):
            for _ in range(3):
                self.assertEqual(self.api(self.url, self.text_a).status_code, 200)
            resp = self.api(self.url, self.text_a)
            self.assertEqual(resp.status_code, 429)
            self.assertTrue(resp['Retry-After'])
            # Another token has its own count
            self.assertEqual(self.api(self.url, self.text_b).status_code, 200)

    def test_too_many_bad_tokens_from_one_address(self):
        with mock.patch.object(auth, 'BAD_LIMIT', 2):
            self.assertEqual(self.api(self.url, 'slk_bad').status_code, 401)
            self.assertEqual(self.api(self.url, 'slk_bad').status_code, 401)
            self.assertEqual(self.api(self.url, 'slk_bad').status_code, 429)

    @override_settings(RATELIMIT_ENABLED=False)
    def test_limits_off_on_a_demo_server(self):
        with mock.patch.object(auth, 'LIMIT', 1):
            for _ in range(3):
                self.assertEqual(self.api(self.url, self.text_a).status_code, 200)

    def test_document_download_goes_through_files_serve_and_is_logged(self):
        rev = self.a['rev']
        url = reverse('api_document_download', args=[self.a['doc'].pk, rev.pk])
        resp = self.api(url, self.text_a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content)[:5], b'%PDF-')
        self.assertEqual(resp['Content-Security-Policy'], "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox")
        with org_context(self.org_a):
            log = FileAccess.objects.get(path=rev.file.name)
        self.assertIsNone(log.user)
        self.assertIn('API token "Power BI"', log.label)
        self.assertEqual(log.project_id, self.pa.pk)
        # A token without the documents group can't download
        _, narrow = self.make_token(self.org_a, self.alice, scopes=[])
        self.assertEqual(self.api(url, narrow).status_code, 403)
        # A revision of another document is a 404
        other = reverse('api_document_download', args=[self.a['doc'].pk, self.b['rev'].pk])
        self.assertEqual(self.api(other, self.text_a).status_code, 404)

    def test_index_says_which_company_and_groups(self):
        _, text = self.make_token(self.org_a, self.alice, scopes=[], name='Narrow')
        body = self.api('/api/v1/', text).json()
        self.assertEqual(body['company'], 'Alpha Fitout')
        self.assertEqual(body['scopes'], ['projects'])
        allowed = {e['name']: e['allowed'] for e in body['endpoints']}
        self.assertTrue(allowed['projects'])
        self.assertFalse(allowed['incidents'])
