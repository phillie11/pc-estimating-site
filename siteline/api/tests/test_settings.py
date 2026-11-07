"""Settings → API: who can open it, making a token (shown once), revoking, the log, and step-up."""
import re

from django.test import TestCase
from django.urls import reverse

from api.models import ApiToken
from tenancy.context import org_context
from tenancy.models import ActivityEvent

from .base import ApiTestBase


class SettingsAccessTests(ApiTestBase):
    def test_every_role(self):
        pages = [self.url('api_settings'), self.url('api_docs'), self.url('api_token_detail', self.token_a.pk)]
        expected = {'alice': 200, 'adam': 200, 'pat': 403, 'eve': 403, 'stan': 403, 'carla': 403, 'connie': 403,
                    'sid': 403, 'bea': 403, 'nora': 403, 'bob': 404}
        for who, code in expected.items():
            for page in pages:
                with self.subTest(who=who, page=page):
                    self.assertEqual(self.status_for(getattr(self, who), page), code)
        self.assertEqual(self.status_for(None, pages[0]), 302)  # to the login page

    def test_another_companys_token_pages_are_404(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('api_token_detail', self.token_a.pk, org=self.org_b)).status_code, 404)
        resp = self.client.post(self.url('api_token_revoke', self.token_a.pk, org=self.org_b))
        self.assertEqual(resp.status_code, 404)
        with org_context(self.org_a):
            self.assertIsNone(ApiToken.objects.get(pk=self.token_a.pk).revoked_at)

    def test_only_owners_and_admins_can_revoke(self):
        for who in ('pat', 'carla', 'sid'):
            self.login(getattr(self, who))
            self.assertEqual(self.client.post(self.url('api_token_revoke', self.token_a.pk)).status_code, 403)
        with org_context(self.org_a):
            self.assertIsNone(ApiToken.objects.get(pk=self.token_a.pk).revoked_at)


class MakeAndRevokeTests(ApiTestBase):
    def test_make_a_token_shows_it_once(self):
        self.login(self.adam)
        page = self.client.get(self.url('api_settings'))
        self.assertContains(page, f'action="{self.url("api_settings")}"')
        resp = self.client.post(self.url('api_settings'), {'name': '  Finance   system ', 'scopes': ['registers'],
                                                           'expires': '90'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Cache-Control'], 'no-store')
        text = re.search(r'value="(slk_[A-Za-z0-9]+_[A-Za-z0-9]+)"', resp.content.decode()).group(1)
        with org_context(self.org_a):
            token = ApiToken.objects.get(name='Finance system')
        self.assertEqual(token.created_by, self.adam)
        self.assertEqual(sorted(token.scopes), ['projects', 'registers'])
        self.assertIsNotNone(token.expires_at)
        self.assertEqual(self.api('/api/v1/rfis/', text).status_code, 200)
        self.assertEqual(self.api('/api/v1/applications/', text).status_code, 403)
        # Never shown again: not on the list, the token's page or anywhere else
        for page in (self.url('api_settings'), self.url('api_token_detail', token.pk)):
            body = self.client.get(page).content.decode()
            self.assertNotIn(text, body)
            self.assertIn(token.prefix, body)
        with org_context(self.org_a):
            self.assertTrue(ActivityEvent.objects.filter(verb='made API token', target='Finance system').exists())

    def test_name_is_needed(self):
        self.login(self.alice)
        resp = self.client.post(self.url('api_settings'), {'name': '   ', 'scopes': ['registers']})
        self.assertContains(resp, 'Say what the token is for')
        with org_context(self.org_a):
            self.assertEqual(ApiToken.objects.count(), 1)

    def test_unknown_scope_is_refused(self):
        self.login(self.alice)
        resp = self.client.post(self.url('api_settings'), {'name': 'X', 'scopes': ['everything']})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('slk_', resp.content.decode())

    def test_revoke(self):
        self.login(self.alice)
        resp = self.client.post(self.url('api_token_revoke', self.token_a.pk))
        self.assertRedirects(resp, self.url('api_settings'))
        self.assertEqual(self.api('/api/v1/projects/', self.text_a).status_code, 401)
        with org_context(self.org_a):
            t = ApiToken.objects.get(pk=self.token_a.pk)
            self.assertEqual(t.revoked_by, self.alice)
            self.assertTrue(ActivityEvent.objects.filter(verb='revoked API token').exists())
        page = self.client.get(self.url('api_settings')).content.decode()
        self.assertIn('Revoked', page)
        self.assertNotIn(f'action="{self.url("api_token_revoke", self.token_a.pk)}"', page)
        # Bob's token is untouched
        self.assertEqual(self.api('/api/v1/projects/', self.text_b).status_code, 200)

    def test_log_shows_on_the_tokens_page(self):
        self.api('/api/v1/rfis/', self.text_a)
        self.login(self.alice)
        resp = self.client.get(self.url('api_token_detail', self.token_a.pk))
        self.assertContains(resp, 'GET /api/v1/rfis/')

    def test_a_token_whose_maker_left_is_flagged(self):
        from tenancy.models import Membership
        token, _ = self.make_token(self.org_a, self.adam, name='Adam made this')
        with org_context(self.org_a):
            Membership.objects.filter(organization=self.org_a, user=self.adam).update(role=Membership.ROLE_MEMBER)
        self.login(self.alice)
        self.assertContains(self.client.get(self.url('api_settings')), 'its maker is no longer an owner or admin')


class StepUpTests(ApiTestBase):
    def test_making_and_revoking_need_a_recent_password(self):
        from security.twofactor import AUTH_AT_KEY
        self.login(self.alice)
        session = self.client.session
        session[AUTH_AT_KEY] = 1  # long ago
        session.save()
        resp = self.client.post(self.url('api_settings'), {'name': 'Late', 'scopes': ['registers']})
        self.assertTrue(resp['Location'].startswith(reverse('step_up')))
        resp = self.client.post(self.url('api_token_revoke', self.token_a.pk))
        self.assertTrue(resp['Location'].startswith(reverse('step_up')))
        with org_context(self.org_a):
            self.assertFalse(ApiToken.objects.filter(name='Late').exists())
            self.assertIsNone(ApiToken.objects.get(pk=self.token_a.pk).revoked_at)
        # Reading the page doesn't
        self.assertEqual(self.client.get(self.url('api_settings')).status_code, 200)


class SettingsCardTests(TestCase):
    def test_settings_page_links_to_api(self):
        from changes.tests.base import make_user
        from accounts.views import create_organization
        owner = make_user('o@x.test')
        org = create_organization('Xco', owner)
        self.client.force_login(owner, backend='django.contrib.auth.backends.ModelBackend')
        resp = self.client.get(reverse('org_settings', args=[org.slug]))
        self.assertContains(resp, f'href="{reverse("api_settings", args=[org.slug])}"')
        self.assertContains(resp, f'href="{reverse("reports_home", args=[org.slug])}"')
