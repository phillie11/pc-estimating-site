"""
Release 3 wave 2 safety defaults (pending owner decision, each a company setting):

  - a subcontractor reports a near miss from their portal (same form; goes to the PM; internal record)
  - the incident investigation target (default 7 days)
  - visitors' QR sign-ins are deleted after the company's retention period (default 12 months, 3 to 36)
    by `manage.py purge_signins`; staff's and portal users' sign-ins and incidents are kept

Access: an appointed sub sees only their own company's reports; clients, consultants and tendering subs
get a 403; another company a 404; no organisation fails closed.
"""
import datetime
import io

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

import actions
from directory.models import Subcontractor
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .. import rules
from ..models import Incident, IncidentPhoto, SafetySettings, SiteAttendance
from ..retention import purge_visitor_signins
from .base import MEDIA, SafetyTestBase, jpeg, make_user


class Base(SafetyTestBase):
    def setUp(self):
        super().setUp()
        self.sue = make_user('sue@e.test', 'Sue Sparks')
        Membership.objects.create(organization=self.org_a, user=self.sue, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.sparks = Subcontractor.objects.create(company_name='Sparks Electrical')
            ProjectMember.objects.create(project=self.pa, user=self.sue, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.sparks)
        self.page = self.u('portal_near_miss', self.pa.pk)

    def report(self, user, **data):
        self.login(user)
        return self.client.post(self.page, {'description': 'Pallet fell off the hoist', 'location': 'Goods lift',
                                            'anyone_hurt': 'False', **data})


@override_settings(MEDIA_ROOT=MEDIA)
class NearMissPortalTests(Base):
    def test_sub_reports_a_near_miss_and_it_goes_to_the_pm(self):
        r = self.report(self.sid, photo=jpeg())
        self.assertRedirects(r, self.page)
        with org_context(self.org_a):
            i = Incident.objects.get()
            self.assertEqual(IncidentPhoto.objects.filter(incident=i).count(), 1)
        self.assertEqual((i.kind, i.visibility, i.created_by, i.reported_by_company, i.ball_in_court_user),
                         (Incident.KIND_NEAR_MISS, 'INTERNAL', self.sid, self.subco, self.pat))
        self.assertEqual(i.due_date, timezone.localdate() + datetime.timedelta(days=7))
        # On the PM's Needs me (through the record index), told by the bell, and the staff page names the company
        req = actions.request_for(self.pat, Membership.objects.get(organization=self.org_a, user=self.pat))
        with org_context(self.org_a):
            self.assertIn(i.ref, [x.ref for x in actions.for_user(req)])
            from tenancy.models import Notification
            self.assertTrue(Notification.objects.filter(user=self.pat, title__contains='Sid Joinery').exists())
        self.login(self.pat)
        self.assertContains(self.client.get(i.get_absolute_url()), 'Sid Joinery, through their portal')

    def test_anyone_hurt_makes_it_an_injury(self):
        self.report(self.sid, anyone_hurt='True')
        with org_context(self.org_a):
            self.assertEqual(Incident.objects.get().kind, Incident.KIND_INJURY)

    def test_what_and_where_are_needed(self):
        r = self.report(self.sid, description='', location='')
        self.assertContains(r, 'Say what happened')
        with org_context(self.org_a):
            self.assertFalse(Incident.objects.exists())

    def test_sub_sees_only_their_own_companys_reports_and_never_the_investigation(self):
        self.report(self.sid, description='Sid trip hazard')
        self.report(self.sue, description='Sparks cable across walkway')
        staff = self.incident(description='Our own scaffold board', root_cause='SECRET ROOT CAUSE')
        self.login(self.sid)
        r = self.client.get(self.page)
        self.assertContains(r, 'Sid trip hazard')
        self.assertNotContains(r, 'Sparks cable')
        self.assertNotContains(r, 'Our own scaffold board')
        self.assertNotContains(r, 'SECRET ROOT CAUSE')
        # The staff incident pages stay shut to them
        self.assertEqual(self.client.get(staff.get_absolute_url()).status_code, 403)
        self.assertEqual(self.client.get(self.u('incident_list', self.pa.pk)).status_code, 403)
        self.login(self.sue)
        r = self.client.get(self.page)
        self.assertContains(r, 'Sparks cable')
        self.assertNotContains(r, 'Sid trip hazard')

    def test_clients_consultants_and_bidders_are_refused(self):
        for user in (self.carla, self.connie, self.bea):
            self.assertEqual(self.status_for(user, self.page), 403, user)
            self.assertEqual(self.status_for(user, self.page, 'post', {'description': 'x', 'location': 'y'}), 403)
        self.assertEqual(self.status_for(self.nora, self.page), 404)
        with org_context(self.org_a):
            self.assertFalse(Incident.objects.exists())

    def test_staff_are_sent_to_the_staff_form(self):
        self.login(self.sam)
        self.assertRedirects(self.client.get(self.page), self.u('incident_report', self.pa.pk))

    def test_another_company_gets_a_404(self):
        self.assertEqual(self.status_for(self.bob, self.page), 404)
        self.assertEqual(self.status_for(self.bob, self.page, 'post', {'description': 'x', 'location': 'y'}), 404)

    def test_no_organisation_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(Incident.objects.filter(reported_by_company=self.subco))

    def test_tab_is_for_appointed_subs_only(self):
        self.login(self.sid)
        self.assertContains(self.client.get(self.u('project_decisions', self.pa.pk)), self.page)
        for user in (self.carla, self.connie):
            self.login(user)
            self.assertNotContains(self.client.get(self.u('project_decisions', self.pa.pk)), self.page)

    def test_no_manager_goes_to_an_owner_or_admin(self):
        with org_context(self.org_a):
            type(self.pa).objects.filter(pk=self.pa.pk).update(manager=None)
        self.report(self.sid)
        with org_context(self.org_a):
            self.assertIn(Incident.objects.get().ball_in_court_user, (self.alice, self.adam))


@override_settings(MEDIA_ROOT=MEDIA)
class InvestigationTargetTests(Base):
    def test_company_sets_the_investigation_target(self):
        with org_context(self.org_a):
            self.assertEqual(SafetySettings.for_org(self.org_a).investigation_days, 7)
        self.login(self.alice)
        r = self.client.post(self.u('safety_settings'), {'investigation_days': 3, 'visitor_retention_months': 12})
        self.assertRedirects(r, self.u('safety_settings'))
        self.login(self.pat)
        self.client.post(self.u('incident_report', self.pa.pk), {'description': 'Slip', 'location': 'Stairs',
                                                                'anyone_hurt': 'False'})
        self.report(self.sid)
        with org_context(self.org_a):
            dues = set(Incident.objects.values_list('due_date', flat=True))
        self.assertEqual(dues, {timezone.localdate() + datetime.timedelta(days=3)})

    def test_settings_page_is_for_owners_and_admins_and_says_how_to_fix(self):
        for user, code in ((self.alice, 200), (self.adam, 200), (self.pat, 403), (self.sid, 403), (self.bob, 404)):
            self.assertEqual(self.status_for(user, self.u('safety_settings')), code, user)
        self.login(self.alice)
        r = self.client.post(self.u('safety_settings'), {'investigation_days': 0, 'visitor_retention_months': 2})
        self.assertContains(r, 'Enter a number of days from 1 to 60.')
        self.assertContains(r, 'Keep them for at least 3 months.')
        r = self.client.post(self.u('safety_settings'), {'investigation_days': 7, 'visitor_retention_months': 37})
        self.assertContains(r, 'Keep them for 36 months at most.')

    def test_rule_table(self):
        d = datetime.date(2026, 3, 10)
        for days, due in ((None, datetime.date(2026, 3, 17)), (7, datetime.date(2026, 3, 17)),
                          (1, datetime.date(2026, 3, 11)), (30, datetime.date(2026, 4, 9))):
            self.assertEqual(rules.investigation_due(d, days), due, days)
        self.assertEqual(rules.investigation_due(d), datetime.date(2026, 3, 17))


@override_settings(MEDIA_ROOT=MEDIA)
class VisitorRetentionTests(Base):
    def attend(self, ago_days, kind=SiteAttendance.KIND_VISITOR, user=None, org=None, project=None, name='Vic'):
        with org_context(org or self.org_a):
            return SiteAttendance.objects.create(
                project=project or self.pa, kind=kind, user=user, name=name,
                signed_in_at=timezone.now() - datetime.timedelta(days=ago_days))

    def test_rule_table(self):
        now = timezone.make_aware(datetime.datetime(2026, 11, 7, 9, 0))
        for months, cutoff in ((12, (2025, 11, 7)), (None, (2025, 11, 7)), (3, (2026, 8, 7)), (36, (2023, 11, 7)),
                               (1, (2026, 8, 7)), (99, (2023, 11, 7))):
            self.assertEqual(rules.visitor_purge_cutoff(now, months).timetuple()[:3], cutoff, months)
        # A day the earlier month doesn't have: its last day
        may31 = timezone.make_aware(datetime.datetime(2026, 5, 31, 9, 0))
        self.assertEqual(rules.visitor_purge_cutoff(may31, 3).date(), datetime.date(2026, 2, 28))

    def test_purge_deletes_old_visitors_only(self):
        old_visitor = self.attend(400)
        new_visitor = self.attend(300)
        old_staff = self.attend(400, kind=SiteAttendance.KIND_STAFF, user=self.pat, name='Pat PM')
        old_portal = self.attend(400, kind=SiteAttendance.KIND_COLLABORATOR, user=self.sid, name='Sid Sub')
        incident = self.incident(occurred_at=timezone.now() - datetime.timedelta(days=900))
        out = io.StringIO()
        call_command('purge_signins', stdout=out)
        self.assertIn('Deleted 1 visitor sign-in.', out.getvalue())
        with org_context(self.org_a):
            left = set(SiteAttendance.objects.values_list('pk', flat=True))
            self.assertTrue(Incident.objects.filter(pk=incident.pk).exists())
        self.assertEqual(left, {new_visitor.pk, old_staff.pk, old_portal.pk})
        self.assertNotIn(old_visitor.pk, left)

    def test_each_company_has_its_own_period_and_dry_run_deletes_nothing(self):
        a = self.attend(120)
        with org_context(self.org_b):
            from projects.models import Project
            pb = Project.objects.get(pk=self.pb.pk)
        b = self.attend(120, org=self.org_b, project=pb)
        with org_context(self.org_a):
            s = SafetySettings.for_org(self.org_a)
            s.visitor_retention_months = 3
            s.save()
        out = io.StringIO()
        call_command('purge_signins', '--dry-run', stdout=out)
        self.assertIn('Would delete 1 visitor sign-in.', out.getvalue())
        with org_context(self.org_a):
            self.assertTrue(SiteAttendance.objects.filter(pk=a.pk).exists())
        call_command('purge_signins', stdout=io.StringIO())
        with org_context(self.org_a):
            self.assertFalse(SiteAttendance.objects.filter(pk=a.pk).exists())
        with org_context(self.org_b):
            self.assertTrue(SiteAttendance.objects.filter(pk=b.pk).exists())  # Bravo keeps 12 months

    def test_purge_needs_an_organisation(self):
        self.attend(400)
        with self.assertRaises(TenantContextMissing):
            purge_visitor_signins(self.org_a)
