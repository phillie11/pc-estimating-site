"""
QR site sign-in.

The public page (/sign-in/<token>/) is the security surface: it works only by token, every kind
of dead token looks the same, it shows nothing but the job's name, it is rate-limited per address
and per poster, a visitor can only sign themselves out, and it is never cached or indexed.
Then the site team's side: on site now, signing people in and out, the log and its CSV, the fire
roll call (printable, offline copy) and the poster.
"""
from datetime import timedelta
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from projects.models import Project
from tenancy.context import org_context

from ..models import SiteAttendance, SiteGate
from .base import SafetyTestBase

INVALID = "This sign-in code isn't in use"


class PublicPageTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.pa.name = 'Bar fit-out, Call Lane'
            self.pa.client_name, self.pa.site_address = 'Secret Client Group', '1 Hidden Street'
            self.pa.save()
        self.g = self.gate()
        self.path = reverse('site_signin', args=[self.g.token])
        self.out = reverse('site_signout', args=[self.g.token])
        self.anon = Client()

    def visitor_signs_in(self, client=None, name='Vic Visitor', company='Acme Scaffolding'):
        return (client or self.anon).post(self.path, {'name': name, 'company': company})

    def open_rows(self):
        with org_context(self.org_a):
            return list(SiteAttendance.objects.filter(project=self.pa, signed_out_at__isnull=True)
                        .values_list('name', flat=True))

    # --- by token only, and every dead token looks the same ---------------------------------

    def test_dead_tokens_all_get_the_same_404(self):
        bad = [reverse('site_signin', args=['nope']), reverse('site_signin', args=['x' * 65])]
        for url in bad:
            r = self.anon.get(url)
            self.assertEqual(r.status_code, 404)
            self.assertContains(r, INVALID, status_code=404)
        # Replaced, archived, finished, not yet won, and a workspace that has lapsed
        cases = []
        old = self.path
        with org_context(self.org_a):
            self.g.replace_token(self.pat)
        cases.append(old)
        with org_context(self.org_a):
            lead = Project.objects.create(name='A lead', stage=Project.STAGE_LEAD)
            done = Project.objects.create(name='Finished', stage=Project.STAGE_COMPLETE)
            arch = Project.objects.create(name='Archived', stage=Project.STAGE_LIVE)
            for p in (lead, done, arch):
                cases.append(reverse('site_signin', args=[SiteGate.for_project(p).token]))
            arch.archive(self.alice)
        for url in cases:
            with self.subTest(url=url):
                r = self.anon.get(url)
                self.assertEqual(r.status_code, 404)
                self.assertContains(r, INVALID, status_code=404)
                self.assertNotContains(r, 'Finished', status_code=404)
                self.assertEqual(self.anon.post(url, {'name': 'X'}).status_code, 404)
        with org_context(self.org_a):
            self.assertFalse(SiteAttendance.objects.exists())

    def test_lapsed_workspace_closes_sign_in(self):
        type(self.org_a).objects.filter(pk=self.org_a.pk).update(subscription_status='canceled')
        r = self.anon.get(reverse('site_signin', args=[self.g.token]))
        self.assertEqual(r.status_code, 404)

    # --- shows nothing beyond the job's name -------------------------------------------------

    def test_shows_only_the_project_name(self):
        with org_context(self.org_a):
            SiteAttendance.objects.create(project=self.pa, name='Olive Other', company='Other Co')
        for client in (self.anon, None):
            if client is None:
                client = Client()
                client.force_login(self.bob, backend='django.contrib.auth.backends.ModelBackend')  # another company
            r = client.get(self.path)
            self.assertContains(r, 'Bar fit-out, Call Lane')
            for secret in ('Alpha Fitout', 'alpha-fitout', 'Secret Client Group', '1 Hidden Street', 'Pat PM',
                           self.pa.reference, 'Olive Other', 'Other Co', '/projects/'):
                self.assertNotContains(r, secret)
        # Nor after signing in: just their own name and time
        self.visitor_signs_in()
        r = self.anon.get(self.path)
        self.assertContains(r, "You're signed in")
        self.assertContains(r, 'Vic Visitor')
        for secret in ('Alpha Fitout', 'Secret Client Group', 'Olive Other'):
            self.assertNotContains(r, secret)

    def test_template_never_gets_the_project_or_company(self):
        r = self.anon.get(self.path)
        names = set(r.context.keys())
        self.assertFalse(names & {'project', 'gate', 'organization'})
        # No workspace is open on this page, so base.html adds no brand colour or company name either
        self.assertIsNone(r.context.get('org'))
        self.assertIsNone(r.context.get('membership'))

    def test_private_headers(self):
        r = self.anon.get(self.path)
        self.assertIn('no-store', r['Cache-Control'])
        self.assertEqual(r['X-Robots-Tag'], 'noindex, nofollow')
        self.assertIn("script-src 'self'", r['Content-Security-Policy'])
        self.assertEqual(self.anon.get(reverse('site_signin', args=['nope']))['X-Robots-Tag'], 'noindex, nofollow')

    def test_csrf_needed(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(self.path, {'name': 'X'}).status_code, 403)
        self.assertEqual(self.open_rows(), [])

    # --- rate limits ------------------------------------------------------------------------

    def test_posts_rate_limited_per_address(self):
        for n in range(60):
            Client(REMOTE_ADDR='203.0.113.9').post(self.path, {'name': f'Person {n}'})
        r = Client(REMOTE_ADDR='203.0.113.9').post(self.path, {'name': 'One too many'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(self.open_rows()), 60)
        self.assertEqual(Client(REMOTE_ADDR='203.0.113.10').post(self.path, {'name': 'Elsewhere'}).status_code, 302)

    def test_views_rate_limited_per_address(self):
        c = Client(REMOTE_ADDR='198.51.100.7')
        for _ in range(120):
            c.get(reverse('site_signin', args=['guess']))
        self.assertEqual(c.get(self.path).status_code, 429)  # guessing tokens uses up the same allowance

    def test_each_poster_capped_whoever_posts(self):
        with mock.patch('safety.public.GATE_LIMIT', (3, 3600)):
            codes = [Client(REMOTE_ADDR=f'192.0.2.{n}').post(self.path, {'name': f'P{n}'}).status_code
                     for n in range(1, 5)]
        self.assertEqual(codes, [302, 302, 302, 429])
        self.assertEqual(len(self.open_rows()), 3)

    # --- visitors -----------------------------------------------------------------------------

    def test_visitor_signs_in_and_out_from_their_phone(self):
        r = self.visitor_signs_in(name='  Vic\nVisitor\x07 ', company='Acme')
        self.assertRedirects(r, self.path, fetch_redirect_response=False)
        cookie = r.cookies[f'sl_site_{self.g.pk}']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['path'], self.path)
        self.assertEqual(cookie['samesite'], 'Lax')
        with org_context(self.org_a):
            a = SiteAttendance.objects.get()
        self.assertEqual((a.name, a.company, a.kind, a.user, a.method), ('Vic Visitor', 'Acme', 'VISITOR', None, 'QR'))
        self.visitor_signs_in()  # tapping again doesn't make a second entry
        self.assertEqual(self.open_rows(), ['Vic Visitor'])
        r = self.anon.post(self.out)
        self.assertRedirects(r, self.path, fetch_redirect_response=False)
        self.assertEqual(self.open_rows(), [])
        self.assertContains(self.anon.get(self.path), 'Your name')

    def test_name_needed(self):
        r = self.anon.post(self.path, {'name': '   ', 'company': 'Acme'})
        self.assertContains(r, 'Type your name')
        self.assertEqual(self.open_rows(), [])

    def test_nobody_else_can_sign_a_visitor_out(self):
        self.visitor_signs_in()
        other = Client()
        self.assertEqual(other.post(self.out).status_code, 302)
        self.assertEqual(self.open_rows(), ['Vic Visitor'])
        # A made-up or tampered cookie is ignored
        with org_context(self.org_a):
            pk = SiteAttendance.objects.get().pk
        other.cookies[f'sl_site_{self.g.pk}'] = str(pk)
        other.post(self.out)
        self.assertEqual(self.open_rows(), ['Vic Visitor'])
        # Nor does a cookie signed for another job's poster work here
        with org_context(self.org_a):
            other_job = Project.objects.create(name='Other live job', stage=Project.STAGE_LIVE)
            g2 = SiteGate.for_project(other_job)
        third = Client()
        third.post(reverse('site_signin', args=[g2.token]), {'name': 'Elsewhere'})
        value = third.cookies[f'sl_site_{g2.pk}'].value
        third.cookies[f'sl_site_{self.g.pk}'] = value
        third.post(self.out)
        self.assertEqual(self.open_rows(), ['Vic Visitor'])

    def test_replacing_the_qr_code_ends_old_cookies(self):
        self.visitor_signs_in()
        with org_context(self.org_a):
            self.g.replace_token(self.pat)
        self.anon.post(reverse('site_signout', args=[self.g.token]))  # the old cookie is still sent
        self.assertEqual(self.open_rows(), ['Vic Visitor'])  # the site team signs them out instead

    # --- people with an account ---------------------------------------------------------------

    def tap(self, user):
        c = Client()
        c.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        page = c.get(self.path)
        return c, page

    def test_staff_one_tap(self):
        c, page = self.tap(self.pat)
        self.assertContains(page, 'Sign in as Pat PM')
        c.post(self.path, {'me': '1'})
        c.post(self.path, {'me': '1'})
        with org_context(self.org_a):
            a = SiteAttendance.objects.get()
        self.assertEqual((a.user, a.kind, a.company), (self.pat, 'STAFF', 'Alpha Fitout'))
        self.assertContains(c.get(self.path), "You're signed in")
        c.post(self.out)
        self.assertEqual(self.open_rows(), [])

    def test_subcontractor_one_tap_records_their_company(self):
        c, page = self.tap(self.sid)
        c.post(self.path, {'me': '1'})
        with org_context(self.org_a):
            a = SiteAttendance.objects.get()
        self.assertEqual((a.user, a.kind, a.company), (self.sid, 'COLLABORATOR', 'Sid Joinery'))

    def test_member_not_on_this_job_still_signs_in(self):
        c, page = self.tap(self.nora)
        self.assertContains(page, 'Sign in as Nora Nobody')

    def test_someone_from_another_company_is_a_visitor(self):
        c, page = self.tap(self.bob)
        self.assertContains(page, 'value="Bob B"')
        self.assertNotContains(page, 'Sign in as')
        c.post(self.path, {'name': 'Bob B', 'company': 'Bravo Build'})
        with org_context(self.org_a):
            a = SiteAttendance.objects.get()
        self.assertEqual((a.user, a.kind), (None, 'VISITOR'))  # never linked to a login outside the company

    def test_yesterdays_sign_in_without_sign_out_doesnt_count_today(self):
        with org_context(self.org_a):
            SiteAttendance.objects.create(project=self.pa, user=self.pat, name='Pat PM', kind='STAFF',
                                          signed_in_at=timezone.now() - timedelta(days=1, hours=2))
        c, page = self.tap(self.pat)
        self.assertContains(page, 'Sign in as Pat PM')


class SiteTeamTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        with org_context(self.org_a):
            self.today = SiteAttendance.objects.create(project=self.pa, name='Tom Today', company='Lund Joinery')
            self.yesterday = SiteAttendance.objects.create(project=self.pa, name='Yuri Yesterday', company='Pennine',
                                                           signed_in_at=now - timedelta(days=1))
            self.formula = SiteAttendance.objects.create(project=self.pa, name='=cmd|calc', company=' +SUM(A1)')
        with org_context(self.org_b):
            SiteAttendance.objects.create(project=self.pb, name='Bravo Person')

    def test_on_site_now(self):
        self.login(self.sam)
        r = self.client.get(self.u('safety_on_site', self.pa.pk))
        self.assertContains(r, 'Tom Today')
        self.assertContains(r, 'Lund Joinery')
        self.assertNotContains(r, 'Yuri Yesterday')
        self.assertNotContains(r, 'Bravo Person')
        self.assertContains(r, '<span class="big">2</span>', html=False)

    def test_sign_out_and_manual_sign_in(self):
        self.login(self.sam)
        self.client.post(self.u('safety_sign_out', self.pa.pk, self.today.pk))
        with org_context(self.org_a):
            t = SiteAttendance.objects.get(pk=self.today.pk)
        self.assertEqual(t.signed_out_by, self.sam)
        self.assertIsNotNone(t.signed_out_at)
        r = self.client.post(self.u('safety_manual_sign_in', self.pa.pk),
                             {'name': 'Driver', 'company': 'Builders Merchant', 'kind': 'VISITOR'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            d = SiteAttendance.objects.get(name='Driver')
        self.assertEqual((d.method, d.signed_in_by), ('MANUAL', self.sam))
        with org_context(self.org_b):
            bravo = SiteAttendance.objects.get()
        self.assertEqual(self.client.post(self.u('safety_sign_out', self.pa.pk, bravo.pk)).status_code, 404)

    def test_log_filters_and_csv(self):
        self.login(self.pat)
        url = self.u('safety_sign_in_log', self.pa.pk)
        r = self.client.get(url)
        self.assertContains(r, 'Tom Today')
        self.assertNotContains(r, 'Yuri Yesterday')
        r = self.client.get(url + f'?date={(timezone.localdate() - timedelta(days=1)).isoformat()}')
        self.assertContains(r, 'Yuri Yesterday')
        self.assertContains(r, 'No sign-out recorded')
        r = self.client.get(url + '?q=lund')
        self.assertContains(r, 'Tom Today')
        self.assertNotContains(r, '=cmd')
        r = self.client.get(url + '?export=csv')
        body = r.content.decode()
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn("'=cmd|calc", body)
        self.assertIn("' +SUM(A1)", body)
        self.assertNotIn('Bravo Person', body)

    def test_roll_call_prints_and_keeps_an_offline_copy(self):
        self.login(self.sam)
        r = self.client.get(self.u('safety_roll_call', self.pa.pk))
        self.assertContains(r, 'Tom Today')
        self.assertNotContains(r, 'Yuri Yesterday')
        self.assertContains(r, 'data-print')
        self.assertContains(r, self.u('safety_roll_call_sw', self.pa.pk))
        self.assertIn('no-store', r['Cache-Control'])
        sw = self.client.get(self.u('safety_roll_call_sw', self.pa.pk))
        self.assertEqual(sw['Content-Type'], 'application/javascript; charset=utf-8')
        body = sw.content.decode()
        self.assertIn("var PAGE = '/alpha\\u002Dfitout/projects/%d/safety/roll\\u002Dcall/'" % self.pa.pk, body)
        self.assertIn('/static/safety/safety.css', body)

    def test_poster_and_replacing_the_code(self):
        g = self.gate()
        self.login(self.sam)
        r = self.client.get(self.u('safety_poster', self.pa.pk))
        self.assertContains(r, g.token)
        self.assertContains(r, '<svg')
        self.assertNotContains(r, 'Make a new QR code')  # site team can print, only the PM or an admin replaces
        printed = self.client.get(self.u('safety_poster', self.pa.pk) + '?print=1')
        self.assertContains(printed, 'Sign in and out here')
        self.assertContains(printed, 'class="qr"')
        self.assertEqual(self.client.post(self.u('safety_poster_replace', self.pa.pk)).status_code, 403)
        self.login(self.pat)
        self.client.post(self.u('safety_poster_replace', self.pa.pk))
        with org_context(self.org_a):
            new = SiteGate.objects.get(pk=g.pk)
        self.assertNotEqual(new.token, g.token)
        self.assertEqual(Client().get(reverse('site_signin', args=[g.token])).status_code, 404)
        self.assertEqual(Client().get(reverse('site_signin', args=[new.token])).status_code, 200)

    def test_empty_states(self):
        with org_context(self.org_a):
            SiteAttendance.objects.all().delete()
        self.login(self.sam)
        self.assertContains(self.client.get(self.u('safety_on_site', self.pa.pk)), 'Print the QR poster')
        self.assertContains(self.client.get(self.u('safety_sign_in_log', self.pa.pk)), 'Nobody signed in')
        self.assertContains(self.client.get(self.u('safety_roll_call', self.pa.pk)), 'Nobody is signed in today')


class ReservedSlugTests(SafetyTestBase):
    def test_sign_in_cant_be_a_workspace_name(self):
        from tenancy.models import RESERVED_SLUGS
        self.assertIn('sign-in', RESERVED_SLUGS)
