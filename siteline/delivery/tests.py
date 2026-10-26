"""Estimating and projects as two connected halves, and the live-job portal for subcontractors."""
import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile

from directory.models import Package, Subcontractor, Trade
from projects.access import staff_users
from projects.models import Document, Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership, Notification
from tenancy.tests import Base, make_user
from variations.models import Variation

from .models import Rams, SubVariation


def pdf(name='f.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4')


class SectionBase(Base):
    def setUp(self):
        super().setUp()
        self.est = make_user('est@a.test', 'Esme Estimator')
        self.pm = make_user('pm@a.test', 'Paul Manager')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=self.est, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_ESTIMATING)
            Membership.objects.create(organization=self.org_a, user=self.pm, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_DELIVERY)
            self.bid = Project.objects.create(name='Bank bid', stage=Project.STAGE_TENDERING, owner=self.est,
                                              value=Decimal('500000'))
            self.pa.manager = self.pm
            self.pa.save()


class SectionAccessTests(SectionBase):
    def test_estimator_sees_bids_not_live_jobs(self):
        self.login(self.est)
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.pa.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url('delivery_home', self.org_a)).status_code, 403)
        self.assertRedirects(self.client.get(self.url('dashboard', self.org_a)), self.url('estimating_home', self.org_a))
        self.assertRedirects(self.client.get(self.url('project_list', self.org_a)), self.url('bid_list', self.org_a))
        r = self.client.get(self.url('workload', self.org_a) + '?view=secured')
        self.assertEqual(r.context['view'], 'tenders')

    def test_projects_team_sees_jobs_not_bids(self):
        self.login(self.pm)
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.pa.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)).status_code, 404)
        for name in ('pipeline', 'tender_analytics', 'estimating_home', 'bid_list'):
            self.assertEqual(self.client.get(self.url(name, self.org_a)).status_code, 403, name)
        self.assertRedirects(self.client.get(self.url('dashboard', self.org_a)), self.url('delivery_home', self.org_a))
        self.assertNotContains(self.client.get(self.url('project_list', self.org_a) + '?view=all'), 'Bank bid')

    def test_owner_switches_between_both(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('estimating_home', self.org_a)).status_code, 200)
        r = self.client.get(self.url('delivery_home', self.org_a))
        self.assertContains(r, 'class="switch"')
        self.assertRedirects(self.client.get(self.url('dashboard', self.org_a)), self.url('delivery_home', self.org_a))

    def test_tabs_follow_the_section(self):
        self.login(self.alice)
        bid_tabs = [t[0] for t in self.client.get(self.url('project_detail', self.org_a, self.bid.pk)).context['tabs']]
        job_tabs = [t[0] for t in self.client.get(self.url('project_detail', self.org_a, self.pa.pk)).context['tabs']]
        self.assertIn('tender', bid_tabs)
        self.assertNotIn('rams', bid_tabs)
        self.assertIn('rams', job_tabs)
        self.assertIn('subvariations', job_tabs)

    def test_people_pickers_respect_area_in_this_company_only(self):
        with org_context(self.org_b):
            # The estimator also works in projects at another company; that must not leak in here
            Membership.objects.create(organization=self.org_b, user=self.est, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_DELIVERY)
        delivery = set(staff_users(self.org_a, 'delivery'))
        estimating = set(staff_users(self.org_a, 'estimating'))
        self.assertIn(self.pm, delivery)
        self.assertNotIn(self.est, delivery)
        self.assertIn(self.est, estimating)
        self.assertIn(self.alice, delivery & estimating)

    def test_admin_sets_area(self):
        self.login(self.alice)
        m = Membership.objects.get(organization=self.org_a, user=self.est)
        self.client.post(self.url('member_update', self.org_a, m.pk), {'action': 'area', 'area': Membership.AREA_BOTH})
        m.refresh_from_db()
        self.assertEqual(m.area, Membership.AREA_BOTH)


class HandoverTests(SectionBase):
    def test_win_goes_through_handover(self):
        self.login(self.est)
        r = self.client.post(self.url('project_stage', self.org_a, self.bid.pk), json.dumps({'stage': 'WON'}),
                             content_type='application/json')
        self.assertEqual(r.json()['redirect'], self.url('project_handover', self.org_a, self.bid.pk))
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.stage, Project.STAGE_TENDERING)
        r = self.client.post(self.url('project_handover', self.org_a, self.bid.pk), {
            'manager': self.pm.pk, 'value': '480000', 'start_date': '2026-11-02', 'completion_date': '2027-01-29',
            'handover_notes': 'Client wants weekly progress photos.'})
        self.assertRedirects(r, self.url('bid_list', self.org_a))
        self.bid.refresh_from_db()
        self.assertEqual((self.bid.stage, self.bid.manager, self.bid.value), (Project.STAGE_WON, self.pm, Decimal('480000')))
        self.assertTrue(Notification.all_objects.filter(user=self.pm, title__startswith='Handed over').exists())
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)).status_code, 404)
        self.login(self.pm)
        self.assertContains(self.client.get(self.url('project_detail', self.org_a, self.bid.pk)), 'weekly progress photos')

    def test_editing_stage_to_won_redirects_to_handover(self):
        self.login(self.alice)
        r = self.client.post(self.url('project_edit', self.org_a, self.bid.pk), {'name': 'Bank bid', 'stage': 'LIVE'})
        self.assertRedirects(r, self.url('project_handover', self.org_a, self.bid.pk))
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.stage, Project.STAGE_TENDERING)

    def test_handover_needs_a_projects_person(self):
        self.login(self.est)
        r = self.client.post(self.url('project_handover', self.org_a, self.bid.pk), {'manager': self.est.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIn('manager', r.context['form'].errors)


class SitePortalBase(SectionBase):
    def setUp(self):
        super().setUp()
        self.sub1 = make_user('s1@one.test', 'Sam One')
        self.sub2 = make_user('s2@two.test', 'Tia Two')
        self.cli = make_user('cli@c.test', 'Cara Client')
        with org_context(self.org_a):
            trade = Trade.objects.get_or_create(name='Joinery')[0]
            self.co1 = Subcontractor.objects.create(company_name='One Joinery', email='s1@one.test')
            self.co2 = Subcontractor.objects.create(company_name='Two Electrical', email='s2@two.test')
            for co, val in ((self.co1, '40000'), (self.co2, '25000')):
                Package.objects.create(project=self.pa, subcontractor=co, trade=trade, status=Package.APPOINTED,
                                       quote_value=Decimal(val))
            for u, co in ((self.sub1, self.co1), (self.sub2, self.co2)):
                Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
                ProjectMember.objects.create(project=self.pa, user=u, role=ProjectMember.ROLE_SUBCONTRACTOR, subcontractor=co)
            Membership.objects.create(organization=self.org_a, user=self.cli, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.pa, user=self.cli, role=ProjectMember.ROLE_CLIENT)


class RamsTests(SitePortalBase):
    def submit(self):
        self.login(self.sub1)
        self.client.post(self.url('rams_list', self.org_a, self.pa.pk),
                         {'title': 'Joinery install', 'scope': 'Level 1', 'file': pdf('rams.pdf')})
        return Rams.all_objects.get(subcontractor=self.co1, revision=1)

    def test_submit_review_resubmit(self):
        r = self.submit()
        self.assertTrue(Notification.all_objects.filter(user=self.pm, title__startswith='RAMS to review').exists())
        self.login(self.pm)
        url = self.url('rams_detail', self.org_a, self.pa.pk, r.pk)
        resp = self.client.post(url, {'action': 'review', 'decision': Rams.REJECTED})
        self.assertIn('comments', resp.context['review_form'].errors)
        self.client.post(url, {'action': 'review', 'decision': Rams.REJECTED, 'comments': 'Add the lifting plan.'})
        r.refresh_from_db()
        self.assertEqual(r.status, Rams.REJECTED)
        self.assertTrue(Notification.all_objects.filter(user=self.sub1, title__contains='revise').exists())
        self.login(self.sub1)
        self.client.post(url, {'action': 'resubmit', 'file': pdf('rams2.pdf'), 'note': 'Lifting plan added'})
        r.refresh_from_db()
        self.assertEqual(r.status, Rams.SUPERSEDED)
        new = Rams.all_objects.get(subcontractor=self.co1, revision=2)
        self.assertEqual(new.number, r.number)
        self.assertEqual(self.client.post(self.url('rams_detail', self.org_a, self.pa.pk, new.pk),
                                          {'action': 'review', 'decision': Rams.APPROVED}).status_code, 403)

    def test_other_sub_and_client_cannot_see(self):
        r = self.submit()
        self.login(self.sub2)
        self.assertEqual(self.client.get(self.url('rams_detail', self.org_a, self.pa.pk, r.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url('rams_file', self.org_a, self.pa.pk, r.pk)).status_code, 404)
        self.assertNotContains(self.client.get(self.url('rams_list', self.org_a, self.pa.pk)), 'Joinery install')
        self.login(self.cli)
        self.assertEqual(self.client.get(self.url('rams_list', self.org_a, self.pa.pk)).status_code, 403)

    def test_other_company_cannot_reach(self):
        r = self.submit()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('rams_detail', self.org_b, self.pb.pk, r.pk)).status_code, 404)


class SubVariationTests(SitePortalBase):
    def submit(self, user=None):
        self.login(user or self.sub1)
        self.client.post(self.url('subvariation_list', self.org_a, self.pa.pk), {
            'title': 'Extra bar shelving', 'reason': 'INSTRUCTION', 'instructed_by': 'Site manager, 4 Nov',
            'value': '1800', 'description': 'Two extra shelves to back bar.'})
        return SubVariation.all_objects.get(title='Extra bar shelving')

    def test_decide_and_pass_on_to_client(self):
        v = self.submit()
        self.assertTrue(Notification.all_objects.filter(user=self.pm, title__contains=v.reference).exists())
        self.login(self.pm)
        self.client.post(self.url('subvariation_detail', self.org_a, self.pa.pk, v.pk), {
            'action': 'decide', 'decision': SubVariation.APPROVED, 'approved_value': '1500',
            'decision_notes': 'Agreed at reduced rate.', 'raise_client_variation': 'on'})
        v.refresh_from_db()
        self.assertEqual((v.status, v.approved_value), (SubVariation.APPROVED, Decimal('1500')))
        self.assertEqual(v.client_variation.status, Variation.STATUS_DRAFT)
        self.assertEqual(v.client_variation.value, Decimal('1500'))
        self.assertTrue(Notification.all_objects.filter(user=self.sub1, title__contains='approved').exists())
        self.login(self.sub1)
        r = self.client.get(self.url('project_detail', self.org_a, self.pa.pk))
        self.assertContains(r, '41,500')   # order 40,000 + approved 1,500
        self.assertNotContains(r, 'Two Electrical')

    def test_reject_needs_a_reason_and_subs_are_isolated(self):
        v = self.submit()
        self.login(self.pm)
        r = self.client.post(self.url('subvariation_detail', self.org_a, self.pa.pk, v.pk),
                             {'action': 'decide', 'decision': SubVariation.REJECTED})
        self.assertIn('decision_notes', r.context['decision_form'].errors)
        self.login(self.sub2)
        self.assertEqual(self.client.get(self.url('subvariation_detail', self.org_a, self.pa.pk, v.pk)).status_code, 404)
        self.login(self.cli)
        self.assertEqual(self.client.get(self.url('subvariation_list', self.org_a, self.pa.pk)).status_code, 403)
        self.login(self.sub1)
        self.assertEqual(self.client.post(self.url('subvariation_detail', self.org_a, self.pa.pk, v.pk), {
            'action': 'decide', 'decision': SubVariation.APPROVED}).status_code, 403)

    def test_sub_can_withdraw(self):
        v = self.submit()
        self.client.post(self.url('subvariation_detail', self.org_a, self.pa.pk, v.pk), {'action': 'withdraw'})
        v.refresh_from_db()
        self.assertEqual(v.status, SubVariation.WITHDRAWN)


class DrawingIssueTests(SitePortalBase):
    def upload(self, visibility):
        self.login(self.pm)
        self.client.post(self.url('document_list', self.org_a, self.pa.pk), {
            'title': 'Bar elevations', 'reference': 'A-201', 'category': 'DRAWING', 'visibility': visibility,
            'file': pdf('A-201.pdf'), 'revision': 'C02'})

    def test_shared_issue_notifies_collaborators(self):
        self.upload(Document.VIS_SHARED)
        for u in (self.sub1, self.sub2, self.cli):
            self.assertTrue(Notification.all_objects.filter(user=u, title__contains='A-201').exists())

    def test_internal_upload_notifies_nobody(self):
        self.upload(Document.VIS_INTERNAL)
        self.assertFalse(Notification.all_objects.filter(title__contains='A-201').exists())
