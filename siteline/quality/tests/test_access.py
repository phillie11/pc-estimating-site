"""
Who can open what in Quality, for every role: owner, admin, PM, site manager on the team (200); staff
not on the job (403); the estimator, who can't open live jobs (404); client, consultant, subcontractor
and tendering subcontractor (403); an external not on the job (404); another company (404). Archived
jobs are read only. Nothing fails open without an organisation, and the record index never shows
quality records to externals.
"""
import io
import shutil
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings

import actions
from core.access import visible_index
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .. import rules
from ..models import (ChecklistTemplate, Inspection, InspectionItem, Penetration, Permit, QualityPhoto,
                      QualitySettings, TemplateItem)
from ..pins import viewer_pins
from .base import MEDIA, QualityTestBase, jpeg, tearDownModule  # noqa: F401


@override_settings(MEDIA_ROOT=MEDIA)
class RoleTests(QualityTestBase):
    def setUp(self):
        super().setUp()
        self.ins, self.items = self.inspection()
        self.ptw = self.permit()
        self.fs = self.penetration()
        with org_context(self.org_a):
            from ..photos import add_photo
            self.photo = add_photo(self.pa, self.pat, jpeg(), penetration=self.fs, stage='AFTER')

    def pages(self):
        return [
            self.purl('inspection_list'), self.purl('inspection_create'), self.ins.get_absolute_url(),
            self.purl('permit_list'), self.purl('permit_create'), self.ptw.get_absolute_url(),
            self.purl('permit_edit', self.ptw.pk), self.purl('permit_print', self.ptw.pk),
            self.purl('penetration_list'), self.purl('penetration_create'), self.fs.get_absolute_url(),
            self.purl('penetration_edit', self.fs.pk), self.purl('quality_photo_file', self.photo.pk, 'thumb'),
            self.purl('inspection_list') + '?export=csv', self.purl('permit_list') + '?export=csv',
            self.purl('penetration_list') + '?export=csv',
        ]

    def posts(self):
        return [
            (self.purl('inspection_item', self.ins.pk, self.items[0].pk), {'result': 'PASS'}),
            (self.purl('inspection_action', self.ins.pk), {'action': 'cancel'}),
            (self.purl('permit_action', self.ptw.pk), {'action': 'withdraw'}),
            (self.purl('penetration_action', self.fs.pk), {'action': 'seal'}),
            (self.purl('quality_photo_remove', self.photo.pk), {}),
        ]

    def check(self, user, expected):
        for url in self.pages():
            self.assertEqual(self.status_for(user, url), expected, (user.email, url))
        for url, data in self.posts():
            self.assertEqual(self.status_for(user, url, 'post', data), expected, (user.email, url))

    def test_staff_on_the_job_open_everything(self):
        for user in (self.alice, self.adam, self.pat, self.sam):
            for url in self.pages():
                self.assertEqual(self.status_for(user, url), 200, (user.email, url))

    def test_staff_not_on_the_job_are_refused(self):
        self.check(self.stan, 403)

    def test_estimator_cant_open_a_live_job(self):
        self.check(self.eve, 404)

    def test_externals_are_refused(self):
        for user in (self.carla, self.connie, self.sid, self.bea):
            self.check(user, 403)
        self.check(self.nora, 404)  # not on the job at all
        self.assertEqual(self.fresh(self.ins).status, Inspection.IN_PROGRESS)
        self.assertEqual(self.fresh(self.ptw).status, rules.REQUESTED)

    def test_externals_dont_get_the_tab(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('project_detail', self.pa.pk)),
                            self.purl('inspection_list'))
        for user in (self.carla, self.sid, self.connie):
            self.login(user)
            r = self.client.get(self.url('project_detail', self.pa.pk))
            self.assertNotContains(r, self.purl('inspection_list'))

    def test_signed_out_people_go_to_sign_in(self):
        self.client.logout()
        r = self.client.get(self.purl('inspection_list'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r['Location'])

    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.purl('inspection_list')).status_code, 200)
        self.assertNotContains(self.client.get(self.purl('inspection_list')), 'Start an inspection')
        for url, data in self.posts():
            self.assertEqual(self.client.post(url, data).status_code, 403, url)
        self.assertEqual(self.client.post(self.purl('permit_create'), {}).status_code, 403)
        self.assertEqual(self.fresh(self.items[0]).result, '')

    def test_record_index_never_shows_quality_to_externals(self):
        for user in (self.carla, self.sid, self.connie):
            req = self.request_for(user)
            req.project_member = None
            with org_context(self.org_a):
                rows = visible_index(req).filter(record_type__startswith='quality.')
                self.assertFalse(rows.exists(), user)

    def test_viewer_pins_are_for_staff_on_the_job_only(self):
        from django.test import RequestFactory
        for user, expected in ((self.pat, True), (self.carla, False), (self.stan, False)):
            req = RequestFactory().get('/')
            req.user, req.org = user, self.org_a
            with org_context(self.org_a):
                req.membership = Membership.objects.get(organization=self.org_a, user=user)
                Penetration.objects.filter(pk=self.fs.pk).update(pin_page=1, pin_x='0.5', pin_y='0.5')
                from projects.models import Document
                doc = Document.objects.create(project=self.pa, title='GA')
                Penetration.objects.filter(pk=self.fs.pk).update(drawing=doc)
                self.assertEqual(bool(viewer_pins(req, self.pa, doc)), expected, user)

    def test_no_one_external_gets_quality_needs_me(self):
        for user in (self.carla, self.sid, self.connie, self.bea):
            req = actions.request_for(user, Membership.objects.get(organization=self.org_a, user=user))
            with org_context(self.org_a):
                kinds = {i.kind for i in actions.for_user(req)}
            self.assertFalse(kinds & {'inspection', 'permit', 'firestopping'}, user)


@override_settings(MEDIA_ROOT=MEDIA)
class TenancyTests(QualityTestBase):
    def test_another_company_gets_a_404(self):
        ins, items = self.inspection()
        ptw, fs = self.permit(), self.penetration()
        urls = [self.purl('inspection_list'), ins.get_absolute_url(), ptw.get_absolute_url(), fs.get_absolute_url(),
                self.purl('permit_print', ptw.pk), self.url('quality_template', self.template.pk)]
        for url in urls:
            self.assertEqual(self.status_for(self.bob, url), 404, url)
        # Bob in his own workspace, asking for Alpha's ids
        for name, args, method in (('inspection_detail', (ins.pk,), 'get'), ('permit_detail', (ptw.pk,), 'get'),
                                   ('penetration_detail', (fs.pk,), 'get'),
                                   ('inspection_item', (ins.pk, items[0].pk), 'post'),
                                   ('permit_action', (ptw.pk,), 'post')):
            url = self.url(name, self.pb.pk, *args, org=self.org_b)
            self.assertEqual(self.status_for(self.bob, url, method, {'result': 'PASS', 'action': 'withdraw'}), 404,
                             name)
        self.assertEqual(self.fresh(items[0]).result, '')
        self.assertEqual(self.fresh(ptw).status, rules.REQUESTED)
        self.assertEqual(self.status_for(self.bob, self.url('quality_template', self.template.pk, org=self.org_b)), 404)
        # Alpha's PM, also a member of Bravo, can't reach Alpha's records through Bravo's job
        with org_context(self.org_b):
            Membership.objects.create(organization=self.org_b, user=self.pat, role=Membership.ROLE_ADMIN)
        self.assertEqual(self.status_for(self.pat, self.url('inspection_detail', self.pb.pk, ins.pk, org=self.org_b)),
                         404)

    def test_no_organisation_fails_closed(self):
        self.inspection()
        self.permit()
        self.penetration()
        for model in (Inspection, InspectionItem, Permit, Penetration, QualityPhoto, ChecklistTemplate, TemplateItem,
                      QualitySettings):
            with self.assertRaises(TenantContextMissing):
                model.objects.count()

    def test_records_stay_in_their_company(self):
        self.inspection()
        self.permit()
        self.penetration()
        with org_context(self.org_b):
            self.assertEqual((Inspection.objects.count(), Permit.objects.count(), Penetration.objects.count(),
                              ChecklistTemplate.objects.count()), (0, 0, 0, 0))

    def test_each_company_has_its_own_numbers(self):
        a, _ = self.inspection()
        b, _ = self.inspection(org=self.org_b, project=self.pb, actor=self.bob, inspector=self.bob)
        self.assertEqual((a.ref, b.ref), ('INS-001', 'INS-001'))


class SeedDemoTests(TestCase):
    def test_demo_has_quality_on_leeds_and_salford(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
        from accounts.models import User
        from projects.models import Issue, Project
        from tenancy.models import Organization
        org = Organization.objects.get(slug='northbank-interiors')
        with org_context(org):
            leeds = Project.objects.get(name='Bar & kitchen fit-out, Call Lane')
            salford = Project.objects.get(name='Office CAT A, Salford Quays')
            ins = {i.ref: i for i in Inspection.objects.filter(project=leeds)}
            self.assertEqual([ins[r].status for r in ('INS-001', 'INS-002', 'INS-003')],
                             [Inspection.PASSED, Inspection.FAILED, Inspection.IN_PROGRESS])
            self.assertTrue(Issue.objects.filter(project=leeds, issue_type='QUALITY').exists())
            states = {p.ref: p.state for p in Permit.objects.filter(project=leeds)}
            self.assertEqual(states, {'PTW-001': 'closed', 'PTW-002': 'requested', 'PTW-003': 'expiring',
                                      'PTW-004': 'expired'})
            fs = list(Penetration.objects.filter(project=leeds).order_by('number').values_list('status', flat=True))
            self.assertEqual(fs, ['SIGNED_OFF', 'SIGNED_OFF', 'SEALED', 'FAILED', 'TO_SEAL', 'TO_SEAL'])
            self.assertTrue(Penetration.objects.filter(project=leeds, drawing__isnull=False).exists())
            for model in (Inspection, Permit, Penetration):
                self.assertTrue(model.objects.filter(project=salford).exists(), model)

            def kinds(email):
                user = User.objects.get(email=email)
                m = Membership.objects.get(organization=org, user=user)
                return [i.kind_label for i in actions.for_user(actions.request_for(user, m)) if i.kind in ('inspection', 'permit', 'firestopping')]
            pm = kinds('pm@demo.test')
            self.assertEqual(pm.count('Inspection'), 1)
            self.assertEqual(pm.count('Permit to work'), 3)  # one to approve, one expiring, one expired
            self.assertEqual(pm.count('Fire-stopping'), 2)  # one to inspect, one to get resealed
            for email in ('client@demo.test', 'sub@demo.test', 'estimator@demo.test'):
                self.assertEqual(kinds(email), [], email)
