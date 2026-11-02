"""
Repro tests from the adversarial review of the safety stream (claude/r3-safety-review).
Each test failed on origin/claude/r3-safety before its fix.
"""
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from tenancy.context import org_context

from .. import rules
from ..models import Incident, IncidentAction, SiteAttendance
from .base import SafetyTestBase


class PublicPageReviewTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        self.g = self.gate()
        self.path = reverse('site_signin', args=[self.g.token])
        self.out = reverse('site_signout', args=[self.g.token])

    def open_rows(self):
        with org_context(self.org_a):
            return list(SiteAttendance.objects.filter(project=self.pa, signed_out_at__isnull=True)
                        .values_list('name', flat=True))

    def test_head_requests_count_against_the_view_limit(self):
        # HEAD is answered like GET (404 for a dead token, 200 for a live one), so it must be counted the same
        c = Client(REMOTE_ADDR='198.51.100.8')
        for _ in range(120):
            c.head(reverse('site_signin', args=['guess']))
        self.assertEqual(c.head(self.path).status_code, 429)

    def test_a_full_poster_still_lets_people_sign_out(self):
        # Signing out only ever closes your own row: blocking it leaves people who have left on the fire roll call
        vic = Client(REMOTE_ADDR='192.0.2.50')
        with mock.patch('safety.public.GATE_LIMIT', (2, 3600)):
            vic.post(self.path, {'name': 'Vic Visitor'})
            Client(REMOTE_ADDR='192.0.2.51').post(self.path, {'name': 'Other'})
            self.assertEqual(Client(REMOTE_ADDR='192.0.2.52').post(self.path, {'name': 'Third'}).status_code, 429)
            r = vic.post(self.out)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.open_rows(), ['Other'])

    def test_junk_posts_dont_use_up_the_poster_cap(self):
        # The cap is there to stop a script filling the roll call; posts that sign nobody in mustn't lock the site out
        with mock.patch('safety.public.GATE_LIMIT', (2, 3600)):
            for n in range(5):
                Client(REMOTE_ADDR=f'192.0.2.{60 + n}').post(self.path, {'name': ''})
            r = Client(REMOTE_ADDR='192.0.2.70').post(self.path, {'name': 'Real Person'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.open_rows(), ['Real Person'])

    def test_log_out_on_the_sign_in_page_is_a_post(self):
        # logout_view is POST only: a plain link answers 405
        self.login(self.sam)
        r = self.client.get(self.path)
        self.assertNotContains(r, f'href="{reverse("logout")}"')
        self.assertContains(r, f'action="{reverse("logout")}"')


class SharedDeviceTests(SafetyTestBase):
    def test_logging_out_clears_the_offline_roll_call(self):
        # The roll call's service worker keeps names and companies in Cache Storage; on a shared phone they
        # must not survive logging out
        self.login(self.sam)
        r = self.client.post(reverse('logout'))
        self.assertIn('"storage"', r.get('Clear-Site-Data', ''))
        self.assertIn('"cache"', r.get('Clear-Site-Data', ''))

    def test_service_worker_drops_its_copy_when_access_ends(self):
        self.login(self.sam)
        body = self.client.get(self.u('safety_roll_call_sw', self.pa.pk)).content.decode()
        self.assertIn('caches.delete(CACHE)', body)
        self.assertIn('registration.unregister()', body)


class IncidentReviewTests(SafetyTestBase):
    def reload(self, i):
        with org_context(self.org_a):
            return Incident.objects.get(pk=i.pk)

    def test_action_added_while_closing_is_refused(self):
        # The page was opened while the incident was open; the PM closes it before the action is posted
        i = self.incident(findings='Latch loose.')
        from safety import views
        real = views._incident

        def stale_then_closed(project, incident_id):
            stale = real(project, incident_id)
            with org_context(self.org_a):
                fresh = Incident.objects.get(pk=incident_id)
                from core.workflow import apply_transition
                apply_transition(fresh, 'close', self.pat)
            return stale

        self.login(self.sam)
        with mock.patch('safety.views._incident', side_effect=stale_then_closed):
            self.client.post(self.u('incident_action_add', self.pa.pk, i.pk), {'description': 'Fix the latch'})
        i = self.reload(i)
        self.assertEqual(i.status, Incident.STATUS_CLOSED)
        with org_context(self.org_a):
            self.assertFalse(IncidentAction.objects.filter(incident=i, done_at__isnull=True).exists())

    def test_cannot_close_with_a_worker_off_over_7_days_and_no_riddor_answer(self):
        i = self.incident(anyone_hurt=True, injured_is_worker=True, days_off=10, findings='Slipped on cable.')
        self.assertEqual(i.riddor_suggestion, rules.RIDDOR_OVER_7)
        self.login(self.pat)
        self.client.post(self.u('incident_transition', self.pa.pk, i.pk), {'transition': 'close'})
        self.assertEqual(self.reload(i).status, Incident.STATUS_REPORTED)
        # Once answered (and reported), it closes
        with org_context(self.org_a):
            i = Incident.objects.get(pk=i.pk)
            i.riddor_category, i.riddor_reported_on = rules.RIDDOR_OVER_7, timezone.localdate()
            i.save(actor=self.pat)
        self.client.post(self.u('incident_transition', self.pa.pk, i.pk), {'transition': 'close'})
        self.assertEqual(self.reload(i).status, Incident.STATUS_CLOSED)
