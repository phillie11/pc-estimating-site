"""
Who gets into the Safety tab: every page for every role (owner, admin, PM, site team, staff not on
the job, estimator, client, consultant, subcontractor, tendering sub, external not on the job,
another company), the tab itself, archived jobs read only, bids and other companies' records,
failing closed with no organisation, and the demo data.
"""
from django.core.files.base import ContentFile
from django.test import override_settings

from projects.models import Project
from tenancy.context import TenantContextMissing, org_context

from ..models import (CDMProfile, DutyHolder, Incident, IncidentAction, PhasePlan, SiteAttendance, SiteGate)
from .base import SafetyTestBase


class EveryPageEveryRoleTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        self.i = self.incident()
        with org_context(self.org_a):
            self.a = IncidentAction.objects.create(incident=self.i, description='Guard rail')
            self.h = DutyHolder.objects.create(project=self.pa, role='CLIENT', company='Client Co')
            self.plan = PhasePlan.objects.create(project=self.pa, version=1, file=ContentFile(b'%PDF', name='cpp.pdf'))
            self.att = SiteAttendance.objects.create(project=self.pa, name='Someone')

    def pages(self):
        pk, i = self.pa.pk, self.i.pk
        return [self.u(n, pk) for n in ('safety_home', 'incident_list', 'incident_report', 'cdm_register', 'cdm_edit',
                                        'duty_holder_add', 'phase_plan_upload', 'safety_on_site',
                                        'safety_manual_sign_in', 'safety_sign_in_log', 'safety_roll_call',
                                        'safety_roll_call_sw', 'safety_poster')] + [
            self.u('incident_detail', pk, i), self.u('incident_edit', pk, i), self.u('incident_action_add', pk, i),
            self.u('incident_photo_add', pk, i), self.u('duty_holder_edit', pk, self.h.pk),
            self.u('phase_plan_file', pk, self.plan.pk), self.u('incident_list', pk) + '?export=csv',
            self.u('safety_sign_in_log', pk) + '?export=csv']

    POSTS = ('incident_transition', 'incident_action_update', 'safety_sign_out', 'safety_poster_replace')

    def post_urls(self):
        pk, i = self.pa.pk, self.i.pk
        return [self.u('incident_transition', pk, i), self.u('incident_action_update', pk, i, self.a.pk),
                self.u('safety_sign_out', pk, self.att.pk), self.u('safety_poster_replace', pk)]

    def test_staff_on_the_job_open_every_page(self):
        for name in self.STAFF_ON_JOB:
            for url in self.pages():
                with self.subTest(user=name, url=url):
                    self.assertEqual(self.status_for(getattr(self, name), url), 200)

    def test_everyone_else_is_refused_every_page_and_post(self):
        for name, code in self.REFUSED:
            for url in self.pages():
                with self.subTest(user=name, url=url):
                    self.assertEqual(self.status_for(getattr(self, name), url), code)
            for url in self.post_urls():
                with self.subTest(user=name, url=url, method='post'):
                    self.assertEqual(self.status_for(getattr(self, name), url, 'post',
                                                     {'transition': 'close', 'do': 'done'}), code)
        with org_context(self.org_a):
            self.assertEqual(Incident.objects.get().status, Incident.STATUS_REPORTED)
            self.assertIsNone(IncidentAction.objects.get().done_at)
            self.assertIsNone(SiteAttendance.objects.get().signed_out_at)

    def test_the_tab_shows_for_staff_on_the_job_only(self):
        for name in self.STAFF_ON_JOB:
            self.login(getattr(self, name))
            r = self.client.get(self.u('project_detail', self.pa.pk))
            self.assertContains(r, self.u('safety_home', self.pa.pk))
        for user in (self.stan, self.carla, self.sid, self.connie):
            self.login(user)
            r = self.client.get(self.u('project_detail', self.pa.pk))
            self.assertNotContains(r, '/safety/')

    def test_not_on_a_bid(self):
        with org_context(self.org_a):
            bid = Project.objects.create(name='A bid', stage=Project.STAGE_TENDERING)
        self.assertEqual(self.status_for(self.alice, self.u('safety_home', bid.pk)), 403)

    def test_another_companys_records_are_404_through_our_job(self):
        other = self.incident(project=self.pb, org=self.org_b, actor=self.bob)
        with org_context(self.org_b):
            plan = PhasePlan.objects.create(project=self.pb, version=1, file=ContentFile(b'%PDF', name='b.pdf'))
        self.login(self.alice)
        for url in (self.u('incident_detail', self.pa.pk, other.pk), self.u('phase_plan_file', self.pa.pk, plan.pk)):
            self.assertEqual(self.client.get(url).status_code, 404)
        # and bob can't reach ours through his own workspace
        self.login(self.bob)
        self.assertEqual(self.client.get(self.u('incident_detail', self.pb.pk, self.i.pk, org=self.org_b)).status_code,
                         404)

    def test_no_organisation_fails_closed(self):
        for model in (Incident, IncidentAction, CDMProfile, DutyHolder, PhasePlan, SiteGate, SiteAttendance):
            with self.subTest(model=model.__name__), self.assertRaises(TenantContextMissing):
                list(model.objects.all())

    @override_settings(RATELIMIT_ENABLED=False)
    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.u('incident_list', self.pa.pk)).status_code, 200)
        self.assertEqual(self.client.post(self.u('incident_report', self.pa.pk),
                                          {'description': 'x', 'location': 'y'}).status_code, 403)
        for url in self.post_urls():
            self.assertEqual(self.client.post(url, {'transition': 'close', 'do': 'done'}).status_code, 403)


class DemoSeedTests(SafetyTestBase):
    def test_demo_seed(self):
        from ..demo import seed
        with org_context(self.org_a):
            seed(leeds=self.pa, pm=self.pat, sub_user=self.sid, demo_pdf=lambda title, sub: b'%PDF-1.4 demo')
            self.assertEqual(list(Incident.objects.order_by('number').values_list('ref', 'status')),
                             [('INC-001', 'CLOSED'), ('INC-002', 'INVESTIGATING'), ('INC-003', 'REPORTED')])
            self.assertTrue(Incident.objects.get(number=2).riddor_outstanding)
            self.assertEqual(PhasePlan.objects.filter(project=self.pa).count(), 2)
            self.assertEqual(set(DutyHolder.objects.values_list('role', flat=True)) >= {'CLIENT', 'PD', 'PC'}, True)
            self.assertTrue(SiteGate.objects.filter(project=self.pa).exists())
            self.assertTrue(SiteAttendance.objects.filter(project=self.pa).exists())
