"""Step-up: billing, team and role changes, exports and the danger zone need a recent password or code."""
from urllib.parse import quote

from django.urls import reverse

from tenancy.models import Membership

from .base import PW, SecurityTestBase, code_for


class StepUpTests(SecurityTestBase):
    def step_up_url(self, next_url):
        return f"{reverse('step_up')}?next={quote(next_url, safe='')}"

    def test_fresh_sign_in_counts(self):
        self.login(self.owner)
        r = self.client.post(self.url('member_update', self.m_staff.pk), {'action': 'role', 'role': 'ADMIN'})
        self.assertRedirects(r, self.url('team'), fetch_redirect_response=False)
        self.m_staff.refresh_from_db()
        self.assertEqual(self.m_staff.role, Membership.ROLE_ADMIN)

    def test_role_change_after_ten_minutes_asks_first(self):
        self.login(self.owner)
        self.make_stale()
        team = 'http://testserver' + self.url('team')
        r = self.client.post(self.url('member_update', self.m_staff.pk), {'action': 'role', 'role': 'ADMIN'},
                             HTTP_REFERER=team)
        self.assertRedirects(r, self.step_up_url(team), fetch_redirect_response=False)
        self.m_staff.refresh_from_db()
        self.assertEqual(self.m_staff.role, Membership.ROLE_MEMBER)  # nothing changed

        r = self.client.post(reverse('step_up'), {'password': 'wrong', 'next': team})
        self.assertContains(r, "didn&#x27;t match")
        r = self.client.post(reverse('step_up'), {'password': PW, 'next': team})
        self.assertRedirects(r, team, fetch_redirect_response=False)
        self.client.post(self.url('member_update', self.m_staff.pk), {'action': 'role', 'role': 'ADMIN'})
        self.m_staff.refresh_from_db()
        self.assertEqual(self.m_staff.role, Membership.ROLE_ADMIN)

    def test_code_also_confirms(self):
        device, _ = self.enrol(self.owner)
        self.login(self.owner)
        self.make_stale()
        r = self.client.get(reverse('step_up'))
        self.assertContains(r, 'Or a code from your authenticator app')
        r = self.client.post(reverse('step_up'), {'code': code_for(device), 'next': self.url('team')})
        self.assertRedirects(r, self.url('team'), fetch_redirect_response=False)
        r = self.client.post(self.url('project_archive', self.project.pk))
        self.assertNotIn(reverse('step_up'), r.url)

    def test_each_protected_action(self):
        self.login(self.owner)
        self.make_stale()
        posts = [
            self.url('billing_checkout'), self.url('team'), self.url('member_update', self.m_staff.pk),
            self.url('invite_revoke', 1), self.url('project_archive', self.project.pk),
            self.url('project_delete', self.project.pk), self.url('sub_archive', 1),
            self.url('two_factor_reset_member', self.m_staff.pk),
            reverse('two_factor_disable'), reverse('two_factor_regenerate'),
        ]
        for u in posts:
            r = self.client.post(u)
            self.assertEqual(r.status_code, 302, u)
            self.assertTrue(r.url.startswith(reverse('step_up')), u)
        for u in (self.url('billing_portal'), self.url('project_list') + '?export=csv',
                  self.url('rfi_register') + '?export=csv', self.url('sub_list') + '?export=csv',
                  self.url('variation_list', self.project.pk) + '?export=csv',
                  self.url('rfi_list', self.project.pk) + '?export=csv'):
            r = self.client.get(u)
            self.assertRedirects(r, self.step_up_url(u), fetch_redirect_response=False, msg_prefix=u)

    def test_export_returns_to_the_download(self):
        self.login(self.owner)
        self.make_stale()
        export = self.url('project_list') + '?export=csv'
        self.client.get(export)
        r = self.client.post(reverse('step_up'), {'password': PW, 'next': export})
        self.assertRedirects(r, export, fetch_redirect_response=False)
        r = self.client.get(export)
        self.assertEqual(r['Content-Type'], 'text/csv')

    def test_ordinary_pages_not_affected(self):
        self.login(self.owner)
        self.make_stale()
        for u in (self.url('team'), self.url('project_list'), self.url('billing')):
            self.assertEqual(self.client.get(u).status_code, 200, u)

    def test_non_member_gets_404_not_step_up(self):
        self.login(self.stranger)
        self.make_stale()
        self.assertEqual(self.client.post(self.url('member_update', self.m_staff.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url('project_list') + '?export=csv').status_code, 404)

    def test_next_must_be_on_this_site(self):
        self.login(self.owner)
        r = self.client.post(reverse('step_up'), {'password': PW, 'next': 'https://evil.example/'})
        self.assertRedirects(r, reverse('home'), fetch_redirect_response=False)
        self.make_stale()
        r = self.client.post(self.url('team'), HTTP_REFERER='https://evil.example/team/')
        self.assertIn('next=%2Fhome%2F', r.url)
