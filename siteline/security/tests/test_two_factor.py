"""Two-factor sign-in: enrolment, the two-step login, recovery codes, enforcement and resets."""
import time
from unittest import mock

from axes.models import AccessAttempt
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.http import HttpResponse
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from security import twofactor
from security.models import RecoveryCode, TwoFactorDevice
from tenancy.context import org_context
from tenancy.models import ActivityEvent

from .base import SecurityTestBase, code_for


def signed_in(client):
    return SESSION_KEY in client.session


class EnrolmentTests(SecurityTestBase):
    def test_setup_shows_qr_and_key_then_confirms_with_a_code(self):
        self.login(self.staff)
        r = self.client.get(reverse('two_factor_setup'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('no-store', r['Cache-Control'])
        device = TwoFactorDevice.objects.get(user=self.staff)
        self.assertFalse(device.is_confirmed)
        self.assertContains(r, device.secret[:4])  # the key for typing in by hand
        self.assertContains(r, reverse('two_factor_qr'))

        qr = self.client.get(reverse('two_factor_qr'))
        self.assertEqual(qr['Content-Type'], 'image/svg+xml')
        self.assertIn('no-store', qr['Cache-Control'])
        self.assertTrue(qr.content.startswith(b'<svg'))
        self.assertNotIn(b'http', qr.content.replace(b'http://www.w3.org/2000/svg', b''))  # drawn here, no service

        # A reload keeps the same secret, so a code already scanned still works
        self.client.get(reverse('two_factor_setup'))
        self.assertEqual(TwoFactorDevice.objects.get(user=self.staff).secret, device.secret)

        r = self.client.post(reverse('two_factor_setup'), {'code': '000000'})
        self.assertContains(r, "didn&#x27;t match")
        r = self.client.post(reverse('two_factor_setup'), {'code': code_for(device)})
        self.assertRedirects(r, reverse('two_factor_codes'), fetch_redirect_response=False)
        device.refresh_from_db()
        self.assertTrue(device.is_confirmed)

        codes = self.client.get(reverse('two_factor_codes'))
        self.assertIn('no-store', codes['Cache-Control'])
        shown = codes.context['codes']
        self.assertEqual(len(shown), 10)
        self.assertEqual(len(set(shown)), 10)
        # Shown once only
        self.assertRedirects(self.client.get(reverse('two_factor_codes')), reverse('profile'))
        # QR only exists while setting up
        self.assertEqual(self.client.get(reverse('two_factor_qr')).status_code, 404)

    def test_recovery_codes_are_stored_hashed(self):
        _, codes = self.enrol(self.staff)
        stored = list(RecoveryCode.objects.filter(user=self.staff).values_list('code_hash', flat=True))
        self.assertEqual(len(stored), 10)
        for plain in codes:
            bare = plain.replace('-', '')
            self.assertFalse(any(bare in h or plain in h for h in stored))
        self.assertTrue(all(h.startswith('pbkdf2_sha256$') for h in stored))
        self.assertEqual(len({h.split('$')[2] for h in stored}), 10)  # every one salted separately

    def test_regenerating_replaces_old_codes(self):
        _, old = self.enrol(self.staff)
        self.login(self.staff)
        self.client.post(reverse('two_factor_regenerate'))
        new = self.client.get(reverse('two_factor_codes')).context['codes']
        self.assertFalse(RecoveryCode.use(self.staff, old[0]))
        self.assertTrue(RecoveryCode.use(self.staff, new[0]))

    def test_staff_can_turn_it_off_owner_cannot(self):
        self.enrol(self.staff)
        self.login(self.staff)
        self.client.post(reverse('two_factor_disable'))
        self.assertFalse(twofactor.has_two_factor(self.staff))
        self.assertFalse(RecoveryCode.objects.filter(user=self.staff).exists())

        self.enrol(self.owner)
        self.login(self.owner)
        r = self.client.post(reverse('two_factor_disable'), follow=True)
        self.assertContains(r, 'must keep two-factor sign-in on')
        self.assertTrue(twofactor.has_two_factor(self.owner))

    def test_profile_shows_status(self):
        self.login(self.staff)
        self.assertContains(self.client.get(reverse('profile')), 'Set up two-factor sign-in')
        self.enrol(self.staff)
        r = self.client.get(reverse('profile'))
        self.assertContains(r, '10 recovery codes left')
        self.assertContains(r, 'Turn off')


class LoginTests(SecurityTestBase):
    def test_without_two_factor_password_signs_in(self):
        r = self.password_step(self.staff)
        self.assertRedirects(r, reverse('home'), fetch_redirect_response=False)
        self.assertTrue(signed_in(self.client))

    def test_password_alone_does_not_sign_in(self):
        self.enrol(self.staff)
        r = self.password_step(self.staff, next='/account/')
        self.assertRedirects(r, reverse('two_factor_verify'))
        self.assertFalse(signed_in(self.client))
        # Still anonymous everywhere until the code is in
        self.assertRedirects(self.client.get(reverse('profile')), f"{reverse('login')}?next=/account/")
        self.assertEqual(self.client.get(self.url('dashboard')).status_code, 302)

    def test_code_signs_in_and_returns_to_next(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff, next='/account/')
        r = self.client.post(reverse('two_factor_verify'), {'code': code_for(device)})
        self.assertRedirects(r, '/account/', fetch_redirect_response=False)
        self.assertTrue(signed_in(self.client))
        self.assertNotIn(twofactor.PENDING_KEY, self.client.session)

    def test_wrong_code_and_replayed_code_refused(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)
        r = self.client.post(reverse('two_factor_verify'), {'code': '123456' if code_for(device) != '123456' else '654321'})
        self.assertContains(r, "That code didn&#x27;t work")
        self.assertFalse(signed_in(self.client))

        code = code_for(device)
        self.client.post(reverse('two_factor_verify'), {'code': code})
        self.assertTrue(signed_in(self.client))
        self.client.post(reverse('logout'))
        self.password_step(self.staff)
        self.client.post(reverse('two_factor_verify'), {'code': code})
        self.assertFalse(signed_in(self.client), 'the same code must not work twice')

    def test_recovery_code_works_once(self):
        _, codes = self.enrol(self.staff)
        self.password_step(self.staff)
        r = self.client.post(reverse('two_factor_verify'), {'recovery_code': codes[3].lower()}, follow=True)
        self.assertTrue(signed_in(self.client))
        self.assertContains(r, 'You have 9 left')
        self.client.post(reverse('logout'))
        self.password_step(self.staff)
        self.client.post(reverse('two_factor_verify'), {'recovery_code': codes[3]})
        self.assertFalse(signed_in(self.client))

    def test_verify_page_needs_the_password_step_first(self):
        self.assertRedirects(self.client.get(reverse('two_factor_verify')), reverse('login'))

    def test_half_sign_in_expires(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)
        session = self.client.session
        session[twofactor.PENDING_KEY]['at'] = int(time.time()) - 301
        session.save()
        self.client.post(reverse('two_factor_verify'), {'code': code_for(device)})
        self.assertFalse(signed_in(self.client))

    def test_wrong_codes_count_towards_the_axes_lockout(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)
        wrong = '000000' if code_for(device) != '000000' else '111111'
        for _ in range(6):
            r = self.client.post(reverse('two_factor_verify'), {'code': wrong})
        self.assertEqual(r.status_code, 429)
        self.assertTemplateUsed(r, 'security/locked_out.html')
        self.assertTrue(AccessAttempt.objects.filter(username=self.staff.email).exists())
        # Locked: even the right code, or the right password again, doesn't get in
        r = self.client.post(reverse('two_factor_verify'), {'code': code_for(device)})
        self.assertFalse(signed_in(self.client))
        r = self.password_step(self.staff)
        self.assertEqual(r.status_code, 429)
        self.assertFalse(signed_in(self.client))

    def test_successful_code_clears_failures(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)
        self.client.post(reverse('two_factor_verify'), {'code': '000000' if code_for(device) != '000000' else '111111'})
        self.client.post(reverse('two_factor_verify'), {'code': code_for(device)})
        self.assertTrue(signed_in(self.client))
        self.assertFalse(AccessAttempt.objects.filter(username=self.staff.email, failures_since_start__gt=0).exists())

    def test_password_reset_keeps_two_factor(self):
        device, _ = self.enrol(self.staff)
        self.password_step(self.staff)  # half signed in, then the password is reset elsewhere
        uid = urlsafe_base64_encode(force_bytes(self.staff.pk))
        token = default_token_generator.make_token(self.staff)
        other = self.client_class()
        r = other.get(reverse('password_reset_confirm', args=[uid, token]), follow=True)
        r = other.post(r.redirect_chain[-1][0], {'new_password1': 'Brand-New-Pass-77', 'new_password2': 'Brand-New-Pass-77'})
        self.assertRedirects(r, reverse('password_reset_complete'))
        # The half sign-in made with the old password is void
        self.client.post(reverse('two_factor_verify'), {'code': code_for(device)})
        self.assertFalse(signed_in(self.client))
        # The new password still needs a code
        r = self.password_step(self.staff, password='Brand-New-Pass-77')
        self.assertRedirects(r, reverse('two_factor_verify'))
        self.assertFalse(signed_in(self.client))
        self.client.post(reverse('two_factor_verify'), {'code': code_for(device, 1)})
        self.assertTrue(signed_in(self.client))

    def test_platform_admin_login_goes_through_main_login(self):
        r = self.client.get('/platform-admin/login/?next=/platform-admin/')
        self.assertRedirects(r, f"{reverse('login')}?next=/platform-admin/", fetch_redirect_response=False)
        self.login(self.staff)
        self.assertEqual(self.client.get('/platform-admin/login/').status_code, 403)


class RememberDeviceTests(SecurityTestBase):
    def sign_in_remembering(self, user, device):
        self.password_step(user)
        r = self.client.post(reverse('two_factor_verify'), {'code': code_for(device), 'remember': 'on'})
        self.client.post(reverse('logout'))
        return r

    def test_cookie_is_signed_httponly_secure_and_skips_the_code(self):
        device, _ = self.enrol(self.staff)
        r = self.sign_in_remembering(self.staff, device)
        cookie = r.cookies[twofactor.remember_cookie_name()]
        self.assertTrue(cookie['httponly'])
        self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertEqual(int(cookie['max-age']), 30 * 86400)
        self.assertNotIn(str(self.staff.password), cookie.value)

        r = self.password_step(self.staff)
        self.assertRedirects(r, reverse('home'), fetch_redirect_response=False)
        self.assertTrue(signed_in(self.client))

    def test_forged_cookie_ignored(self):
        device, _ = self.enrol(self.staff)
        self.sign_in_remembering(self.staff, device)
        name = twofactor.remember_cookie_name()
        value = self.client.cookies[name].value
        self.client.cookies[name] = value[:-2] + ('AA' if not value.endswith('AA') else 'BB')
        self.assertRedirects(self.password_step(self.staff), reverse('two_factor_verify'))
        self.client.cookies[name] = f'{self.staff.pk}:anything'
        self.assertRedirects(self.password_step(self.staff), reverse('two_factor_verify'))

    def test_expired_cookie_ignored(self):
        device, _ = self.enrol(self.staff)
        response = HttpResponse()
        with mock.patch('time.time', return_value=time.time() - 31 * 86400):
            twofactor.set_remember_cookie(response, self.staff, device)
        name = twofactor.remember_cookie_name()
        self.client.cookies[name] = response.cookies[name].value
        self.assertRedirects(self.password_step(self.staff), reverse('two_factor_verify'))

    def test_cookie_is_for_one_user(self):
        device, _ = self.enrol(self.staff)
        self.enrol(self.admin)
        self.sign_in_remembering(self.staff, device)
        self.assertRedirects(self.password_step(self.admin), reverse('two_factor_verify'))

    def test_password_change_or_reset_voids_remembered_devices(self):
        device, _ = self.enrol(self.staff)
        self.sign_in_remembering(self.staff, device)
        self.staff.set_password('Another-Pass-88')
        self.staff.save()
        self.assertRedirects(self.password_step(self.staff, password='Another-Pass-88'), reverse('two_factor_verify'))

    def test_two_factor_reset_voids_remembered_devices(self):
        device, _ = self.enrol(self.staff)
        self.sign_in_remembering(self.staff, device)
        TwoFactorDevice.objects.filter(user=self.staff).delete()
        self.enrol(self.staff)
        self.assertRedirects(self.password_step(self.staff), reverse('two_factor_verify'))


@override_settings(TWO_FACTOR_ENFORCE=True)
class EnforcementTests(SecurityTestBase):
    def assert_not_forced(self, response):
        self.assertIn(response.status_code, (200, 302))
        if response.status_code == 302:
            self.assertNotIn(reverse('two_factor_setup'), response.url)

    def test_owner_and_admin_sent_to_setup(self):
        for user in (self.owner, self.admin):
            self.login(user)
            r = self.client.get(self.url('dashboard'))
            self.assertRedirects(r, f"{reverse('two_factor_setup')}?next=%2F{self.org.slug}%2F",
                                 fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse('profile')).status_code, 302)
            r = self.client.post(self.url('project_archive', self.project.pk))
            self.assertRedirects(r, reverse('two_factor_setup'), fetch_redirect_response=False)

    def test_setup_logout_and_static_still_reachable(self):
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse('two_factor_setup')).status_code, 200)
        self.assertEqual(self.client.get(reverse('two_factor_qr')).status_code, 200)
        self.assertNotEqual(self.client.get('/static/css/app.css').status_code, 302)
        self.client.post(reverse('logout'))
        self.assertFalse(signed_in(self.client))

    def test_after_setup_owner_carries_on(self):
        self.login(self.owner)
        self.client.get(reverse('two_factor_setup'))
        device = TwoFactorDevice.objects.get(user=self.owner)
        r = self.client.post(reverse('two_factor_setup'), {'code': code_for(device), 'next': self.url('dashboard')})
        self.assertRedirects(r, f"{reverse('two_factor_codes')}?next=%2F{self.org.slug}%2F",
                             fetch_redirect_response=False)
        codes = self.client.get(r.url)
        self.assertContains(codes, f'href="/{self.org.slug}/"')
        self.assert_not_forced(self.client.get(self.url('dashboard')))

    def test_staff_and_externals_not_forced(self):
        for user in (self.staff, self.client_user, self.sub_user):
            self.login(user)
            self.assert_not_forced(self.client.get(self.url('dashboard')))

    def test_platform_staff_forced(self):
        self.staff.is_staff = True
        self.staff.save()
        self.login(self.staff)
        self.assertEqual(self.client.get('/platform-admin/').status_code, 302)

    def test_promoted_to_admin_is_then_forced(self):
        self.login(self.staff)
        self.assert_not_forced(self.client.get(self.url('dashboard')))
        self.m_staff.role = 'ADMIN'
        self.m_staff.save()
        self.assertTrue(self.client.get(self.url('dashboard')).url.startswith(reverse('two_factor_setup')))

    def test_signup_lands_on_setup(self):
        r = self.client.post(reverse('signup'), {'company_name': 'Charlie Interiors', 'full_name': 'Cara C',
                                                 'email': 'cara@c.test', 'password': 'Correct-Horse-9'}, follow=True)
        self.assertTemplateUsed(r, 'accounts/two_factor_setup.html')


class OwnerResetTests(SecurityTestBase):
    def test_team_page_by_role(self):
        self.enrol(self.staff)
        for user, status in ((self.owner, 200), (self.admin, 200), (self.staff, 403), (self.client_user, 403),
                             (self.sub_user, 403), (self.stranger, 404), (self.other_owner, 404)):
            self.login(user)
            r = self.client.get(self.url('two_factor_team'))
            self.assertEqual(r.status_code, status, user)
        self.login(self.owner)
        r = self.client.get(self.url('two_factor_team'))
        self.assertContains(r, 'Sam Staff')
        self.assertContains(r, 'Not set up')
        self.assertContains(r, self.url('two_factor_reset_member', self.m_staff.pk))
        self.login(self.admin)
        self.assertNotContains(self.client.get(self.url('two_factor_team')),
                               self.url('two_factor_reset_member', self.m_staff.pk))

    def test_owner_resets_with_audit_event_and_email(self):
        self.enrol(self.staff)
        self.login(self.owner)
        r = self.client.post(self.url('two_factor_reset_member', self.m_staff.pk), follow=True)
        self.assertContains(r, 'Two-factor sign-in reset for Sam Staff')
        self.assertFalse(twofactor.has_two_factor(self.staff))
        self.assertFalse(RecoveryCode.objects.filter(user=self.staff).exists())
        with org_context(self.org):
            ev = ActivityEvent.objects.get(verb='reset two-factor sign-in for')
        self.assertEqual((ev.actor, ev.target, ev.internal_only), (self.owner, 'Sam Staff', True))
        self.assertEqual(mail.outbox[-1].to, [self.staff.email])
        self.assertIn('reset your two-factor sign-in', mail.outbox[-1].body)

    def test_only_owners_can_reset(self):
        self.enrol(self.staff)
        for user, status in ((self.admin, 403), (self.staff, 403), (self.client_user, 403),
                             (self.stranger, 404), (self.other_owner, 404)):
            self.login(user)
            r = self.client.post(self.url('two_factor_reset_member', self.m_staff.pk))
            self.assertEqual(r.status_code, status, user)
        self.assertTrue(twofactor.has_two_factor(self.staff))

    def test_other_companys_member_is_404(self):
        device, _ = self.enrol(self.other_owner)
        m_b = self.other_owner.memberships.get(organization=self.org_b)
        self.login(self.owner)
        self.assertEqual(self.client.post(self.url('two_factor_reset_member', m_b.pk)).status_code, 404)
        self.assertTrue(twofactor.has_two_factor(self.other_owner))

    def test_someone_in_another_workspace_needs_support(self):
        from tenancy.models import Membership
        self.enrol(self.staff)
        Membership.objects.create(organization=self.org_b, user=self.staff, role=Membership.ROLE_MEMBER)
        self.login(self.owner)
        r = self.client.post(self.url('two_factor_reset_member', self.m_staff.pk), follow=True)
        self.assertContains(r, 'also works in another company')
        self.assertTrue(twofactor.has_two_factor(self.staff))

    def test_cannot_reset_yourself_here(self):
        self.enrol(self.owner)
        self.login(self.owner)
        m = self.owner.memberships.get(organization=self.org)
        self.client.post(self.url('two_factor_reset_member', m.pk))
        self.assertTrue(twofactor.has_two_factor(self.owner))
