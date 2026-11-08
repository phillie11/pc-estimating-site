"""
The subcontractor's portal: their own permits (see them, ask for one) and fire-stopping on their own
packages (record it, photos, mark it sealed for the PM to sign off), plus the company setting that
whoever recorded a seal can't sign it off.

Access: an appointed sub sees only their own company's items; a second appointed sub on the same job
(Sue, Sparks Electrical) is refused theirs; clients, consultants and tendering subs get a 403; staff
are sent to the staff page; another company gets a 404; no organisation fails closed.
"""
from django.urls import reverse

import actions
from core.exceptions import InvalidTransition
from core.workflow import apply_transition, available_transitions
from directory.models import Package, Subcontractor
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .. import rules
from ..models import Penetration, Permit, QualityPhoto, QualitySettings
from ..photos import add_photo
from ..portal import sub_items
from .base import QualityTestBase, jpeg, make_user


class PortalBase(QualityTestBase):
    def setUp(self):
        super().setUp()
        self.sue = make_user('sue@e.test', 'Sue Sparks')
        Membership.objects.create(organization=self.org_a, user=self.sue, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.sparks = Subcontractor.objects.create(company_name='Sparks Electrical')
            ProjectMember.objects.create(project=self.pa, user=self.sue, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.sparks)
            self.sue_pkg = Package.objects.create(project=self.pa, subcontractor=self.sparks, status=Package.APPOINTED)

    def sid_permit(self, **kw):
        return self.permit(package=self.pkg, **kw)

    def sid_pen(self, **kw):
        kw.setdefault('package', self.pkg)
        return self.penetration(**kw)


class PermitPortalTests(PortalBase):
    def test_sub_sees_only_their_own_companys_permits(self):
        mine = self.sid_permit(title='Weld Sid brackets')
        theirs = self.permit(package=self.sue_pkg, title='Isolate Sparks board')
        ours = self.permit(title='Our own grinding')
        self.login(self.sid)
        r = self.client.get(self.purl('portal_permits'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.purl('portal_permit', mine.pk))
        self.assertNotContains(r, self.purl('portal_permit', theirs.pk))
        self.assertNotContains(r, self.purl('portal_permit', ours.pk))
        self.assertEqual(self.client.get(self.purl('portal_permit', mine.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.purl('portal_permit', theirs.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.purl('portal_permit', ours.pk)).status_code, 404)
        self.login(self.sue)
        self.assertEqual(self.client.get(self.purl('portal_permit', mine.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.purl('portal_permit', theirs.pk)).status_code, 200)

    def test_sub_asks_for_a_permit_and_the_pm_approves_it(self):
        self.login(self.sid)
        form = self.client.get(self.purl('portal_permit_create'))
        self.assertContains(form, 'value="Sid Sub"')  # who holds it defaults to them
        start = form.context['form'].initial['starts_at']
        end = form.context['form'].initial['expires_at']
        r = self.client.post(self.purl('portal_permit_create'), {
            'permit_type': rules.HOT_WORKS, 'title': 'Weld brackets', 'location': 'Bar', 'package': self.pkg.pk,
            'holder': 'Sid Sub', 'starts_at': start.strftime('%Y-%m-%dT%H:%M'),
            'expires_at': end.strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = Permit.objects.get()
            self.assertEqual((p.status, p.created_by, p.package, p.ball_in_court_user),
                             (rules.REQUESTED, self.sid, self.pkg, self.pat))
            self.assertEqual(r['Location'], self.purl('portal_permit', p.pk))
            # The PM approves it as any request; the sub can never approve their own
            self.assertNotIn('approve', {t.name for t in available_transitions(p, self.sid)})
            apply_transition(p, 'approve', self.pat, precautions=rules.precautions_for(rules.HOT_WORKS))
        self.assertEqual(self.fresh(p).status, rules.APPROVED)
        r = self.client.get(self.purl('portal_permit', p.pk))
        self.assertContains(r, 'Precautions confirmed')

    def test_sub_cant_ask_for_a_permit_on_someone_elses_package(self):
        self.login(self.sid)
        r = self.client.post(self.purl('portal_permit_create'), {
            'permit_type': rules.HEIGHT, 'title': 'Tower', 'location': 'Bar', 'package': self.sue_pkg.pk,
            'starts_at': '2030-01-01T08:00', 'expires_at': '2030-01-01T16:00'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Choose one of your own packages.')
        with org_context(self.org_a):
            self.assertFalse(Permit.objects.exists())

    def test_sub_with_no_package_is_told_to_ask_the_site_team(self):
        with org_context(self.org_a):
            Package.objects.filter(pk=self.pkg.pk).update(status=Package.QUOTED)
        self.login(self.sid)
        r = self.client.get(self.purl('portal_permit_create'), follow=True)
        self.assertContains(r, 'Ask the site team')
        self.assertNotContains(self.client.get(self.purl('portal_permits')), self.purl('portal_permit_create'))


class FirestopPortalTests(PortalBase):
    def test_sub_records_fire_stopping_and_it_goes_to_the_pm(self):
        self.login(self.sid)
        r = self.client.post(self.purl('portal_firestop_create'), {
            'location': 'L1 riser wall', 'element': 'WALL', 'fire_rating': '60', 'service': 'PIPE_PLASTIC',
            'size': '110 mm', 'system': 'Intumescent collar', 'package': self.pkg.pk, 'installer': 'Sid',
            'before_photo': jpeg('b.jpg'), 'after_photo': jpeg('a.jpg')})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = Penetration.objects.get()
        self.assertEqual((p.status, p.sealed_by, p.ball_in_court_user, p.created_by),
                         (Penetration.SEALED, self.sid, self.pat, self.sid))
        # The PM signs it off; the sub never can
        self.assertNotIn('sign_off', {t.name for t in available_transitions(p, self.sid)})
        with org_context(self.org_a):
            apply_transition(p, 'sign_off', self.pat)
        self.assertEqual(self.fresh(p).status, Penetration.SIGNED_OFF)
        self.assertContains(self.client.get(self.purl('portal_firestop_detail', p.pk)), 'Signed off')

    def test_photos_then_mark_sealed_then_reseal_after_a_fail(self):
        p = self.sid_pen(system='Collar')
        self.login(self.sid)
        r = self.client.post(self.purl('portal_firestop_action', p.pk), {'action': 'seal'}, follow=True)
        self.assertContains(r, 'Add a photo of the finished seal first')
        self.client.post(self.purl('portal_firestop_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()})
        self.client.post(self.purl('portal_firestop_action', p.pk), {'action': 'seal'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.sealed_by), (Penetration.SEALED, self.sid))
        # Photos are fixed while the site team has it
        r = self.client.post(self.purl('portal_firestop_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()},
                             follow=True)
        self.assertContains(r, 'with the site team')
        with org_context(self.org_a):
            apply_transition(p, 'fail', self.pat, remark='Gap behind the collar')
        # On the sub's Needs me, and the reason shows
        req = actions.request_for(self.sid, Membership.objects.get(organization=self.org_a, user=self.sid))
        with org_context(self.org_a):
            items = [i for i in actions.for_user(req) if i.kind == 'firestopping_sub']
        self.assertEqual([i.ref for i in items], [p.ref])
        self.assertEqual(items[0].url, self.purl('portal_firestop_detail', p.pk))
        self.assertContains(self.client.get(items[0].url), 'Gap behind the collar')
        self.client.post(self.purl('portal_firestop_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()})
        self.client.post(self.purl('portal_firestop_action', p.pk), {'action': 'reseal'})
        self.assertEqual(self.fresh(p).status, Penetration.SEALED)

    def test_no_photos_once_the_package_is_lost(self):
        p = self.sid_pen(system='Collar')
        self.login(self.sid)
        self.assertContains(self.client.get(self.purl('portal_firestop_detail', p.pk)), 'q-photo-form')
        with org_context(self.org_a):
            Package.objects.filter(pk=p.package_id).update(status=Package.UNSUCCESSFUL)
        r = self.client.post(self.purl('portal_firestop_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()})
        self.assertEqual(r.status_code, 403)
        with org_context(self.org_a):
            self.assertFalse(QualityPhoto.objects.filter(penetration=p).exists())
        self.assertNotContains(self.client.get(self.purl('portal_firestop_detail', p.pk)), 'q-photo-form')
        # Package back, but they are off the job: still refused
        with org_context(self.org_a):
            Package.objects.filter(pk=p.package_id).update(status=Package.APPOINTED)
            ProjectMember.objects.filter(project=self.pa, user=self.sid).delete()
        r = self.client.post(self.purl('portal_firestop_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()})
        self.assertIn(r.status_code, (403, 404))
        with org_context(self.org_a):
            self.assertFalse(QualityPhoto.objects.filter(penetration=p).exists())

    def test_sub_sees_only_their_own_companys_fire_stopping(self):
        mine = self.sid_pen(location='Sid wall')
        theirs = self.penetration(package=self.sue_pkg, location='Sparks riser')
        ours = self.penetration(location='Our ceiling')
        with org_context(self.org_a):
            photo = add_photo(self.pa, self.pat, jpeg(), penetration=theirs, stage='AFTER')
            own_photo = add_photo(self.pa, self.pat, jpeg(), penetration=mine, stage='AFTER')
        self.login(self.sid)
        r = self.client.get(self.purl('portal_firestop'))
        self.assertContains(r, 'Sid wall')
        self.assertNotContains(r, 'Sparks riser')
        self.assertNotContains(r, 'Our ceiling')
        for pen in (theirs, ours):
            self.assertEqual(self.client.get(self.purl('portal_firestop_detail', pen.pk)).status_code, 404)
            self.assertEqual(self.client.post(self.purl('portal_firestop_action', pen.pk),
                                              {'action': 'seal'}).status_code, 404)
            self.assertEqual(self.client.post(self.purl('portal_firestop_photo', pen.pk),
                                              {'stage': 'AFTER', 'photo': jpeg()}).status_code, 404)
        self.assertEqual(self.client.get(self.purl('portal_quality_photo', photo.pk, 'thumb')).status_code, 404)
        self.assertEqual(self.client.get(self.purl('portal_quality_photo', own_photo.pk, 'thumb')).status_code, 200)
        # Never the original (it keeps the camera's location data)
        self.assertEqual(self.client.get(self.purl('portal_quality_photo', own_photo.pk, 'original')).status_code, 404)
        self.assertEqual(self.fresh(theirs).status, Penetration.TO_SEAL)

    def test_sub_cant_seal_through_the_staff_pages(self):
        p = self.sid_pen()
        self.login(self.sid)
        self.assertEqual(self.client.post(self.purl('penetration_action', p.pk), {'action': 'seal'}).status_code, 403)
        self.assertEqual(self.client.get(self.purl('penetration_list')).status_code, 403)


class PortalAccessTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.ptw = self.sid_permit()
        self.fs = self.sid_pen()

    def pages(self):
        return [self.purl('portal_permits'), self.purl('portal_permit_create'), self.purl('portal_permit', self.ptw.pk),
                self.purl('portal_firestop'), self.purl('portal_firestop_create'),
                self.purl('portal_firestop_detail', self.fs.pk)]

    def test_clients_consultants_and_bidders_are_refused(self):
        for user in (self.carla, self.connie, self.bea):
            for url in self.pages():
                self.assertEqual(self.status_for(user, url), 403, (user.email, url))
            self.assertEqual(self.status_for(user, self.purl('portal_firestop_action', self.fs.pk), 'post',
                                             {'action': 'seal'}), 403)
        self.assertEqual(self.status_for(self.nora, self.purl('portal_permits')), 404)  # not on the job
        self.assertEqual(self.fresh(self.fs).status, Penetration.TO_SEAL)

    def test_staff_are_sent_to_the_staff_pages(self):
        self.login(self.pat)
        r = self.client.get(self.purl('portal_permits'))
        self.assertRedirects(r, self.purl('permit_list'))
        r = self.client.get(self.purl('portal_firestop_detail', self.fs.pk))
        self.assertRedirects(r, self.purl('penetration_list'))

    def test_another_company_gets_a_404(self):
        self.login(self.bob)
        for url in self.pages():
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post(self.purl('portal_firestop_action', self.fs.pk),
                                          {'action': 'seal'}).status_code, 404)
        # A subcontractor of Bravo's can't reach Alpha's records from Bravo's workspace either
        bravo_sub = make_user('bs@b.test', 'Bravo Sub')
        Membership.objects.create(organization=self.org_b, user=bravo_sub, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_b):
            co = Subcontractor.objects.create(company_name='Bravo Drylining')
            ProjectMember.objects.create(project=self.pb, user=bravo_sub, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=co)
        self.login(bravo_sub)
        for name, arg in (('portal_permit', self.ptw.pk), ('portal_firestop_detail', self.fs.pk)):
            url = reverse(name, args=[self.org_b.slug, self.pb.pk, arg])
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.fresh(self.fs).status, Penetration.TO_SEAL)

    def test_no_organisation_fails_closed(self):
        from types import SimpleNamespace
        with self.assertRaises(TenantContextMissing):
            list(Penetration.objects.filter(package__subcontractor=self.subco))
        req = actions.request_for(self.sid, Membership.objects.get(organization=self.org_a, user=self.sid))
        with self.assertRaises(TenantContextMissing):
            sub_items(req)
        self.assertEqual(sub_items(SimpleNamespace(user=self.sid, membership=None, org=self.org_a)), [])

    def test_tabs_show_for_an_appointed_sub_only(self):
        self.login(self.sid)
        # Your permits sits under Waiting on you, Fire-stopping under Snags
        self.assertContains(self.client.get(self.url('project_decisions', self.pa.pk)), self.purl('portal_permits'))
        self.assertContains(self.client.get(self.purl('issue_list')), self.purl('portal_firestop'))
        for user in (self.carla, self.connie):
            self.login(user)
            for page in (self.url('project_decisions', self.pa.pk), self.purl('issue_list')):
                r = self.client.get(page)
                self.assertNotContains(r, self.purl('portal_permits'))
                self.assertNotContains(r, self.purl('portal_firestop'))

    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.sid)
        self.assertEqual(self.client.post(self.purl('portal_firestop_action', self.fs.pk),
                                          {'action': 'seal'}).status_code, 404)


class OwnSealSignOffTests(PortalBase):
    def sealed_by(self, user):
        p = self.penetration(system='Collar')
        with org_context(self.org_a):
            add_photo(self.pa, user, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(p, 'seal', user)
        return self.fresh(p)

    def test_whoever_sealed_it_cant_sign_it_off(self):
        p = self.sealed_by(self.pat)
        # The PM sealed it, so it goes to an owner or admin to inspect, not back to the PM
        self.assertIn(p.ball_in_court_user, (self.alice, self.adam))
        self.assertNotIn('sign_off', {t.name for t in available_transitions(p, self.pat)})
        self.login(self.pat)
        r = self.client.get(p.get_absolute_url())
        self.assertContains(r, 'You recorded this seal')
        self.assertContains(r, 'value="fail"')
        r = self.client.post(self.purl('penetration_action', p.pk), {'action': 'sign_off'}, follow=True)
        self.assertContains(r, 'someone else on the job inspects and signs it off')
        self.assertEqual(self.fresh(p).status, Penetration.SEALED)
        with org_context(self.org_a), self.assertRaises(Exception):
            apply_transition(self.fresh(p), 'sign_off', self.pat)
        self.login(self.sam)
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'sign_off'})
        self.assertEqual(self.fresh(p).status, Penetration.SIGNED_OFF)

    def test_a_reseal_counts_as_recording_it(self):
        p = self.sealed_by(self.sam)
        with org_context(self.org_a):
            apply_transition(p, 'fail', self.pat, remark='Gap')
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(self.fresh(p), 'reseal', self.pat)
            p = self.fresh(p)
            self.assertEqual(p.sealed_by, self.pat)
            with self.assertRaises(Exception):
                apply_transition(p, 'sign_off', self.pat)
            apply_transition(p, 'sign_off', self.sam)
        self.assertEqual(self.fresh(p).status, Penetration.SIGNED_OFF)

    def test_the_company_can_turn_it_off(self):
        with org_context(self.org_a):
            s = QualitySettings.for_org(self.org_a)
            self.assertTrue(s.fs_independent_sign_off)  # on by default
            s.fs_independent_sign_off = False
            s.save()
        p = self.sealed_by(self.pat)
        self.assertEqual(p.ball_in_court_user, self.pat)
        with org_context(self.org_a):
            apply_transition(p, 'sign_off', self.pat)
        self.assertEqual(self.fresh(p).status, Penetration.SIGNED_OFF)

    def test_turning_it_back_on_moves_seals_left_with_the_person_who_sealed_them(self):
        with org_context(self.org_a):
            s = QualitySettings.for_org(self.org_a)
            s.fs_independent_sign_off = False
            s.save()
        stuck = self.sealed_by(self.pat)      # self sign-off allowed: the PM sealed it and it waits on the PM
        other = self.sealed_by(self.sam)      # waits on the PM, who didn't seal it: left alone
        self.assertEqual((stuck.ball_in_court_user, other.ball_in_court_user), (self.pat, self.pat))
        self.login(self.alice)
        r = self.client.post(self.url('quality_settings'), {'permit_hours': 8, 'expiry_warning_hours': 2,
                                                           'fire_watch_minutes': 60, 'fs_independent_sign_off': 'on'},
                             follow=True)
        self.assertContains(r, 'Seals waiting on the person who recorded them to sign off: 1')
        stuck, other = self.fresh(stuck), self.fresh(other)
        self.assertIn(stuck.ball_in_court_user, (self.alice, self.adam))
        self.assertEqual(other.ball_in_court_user, self.pat)
        with org_context(self.org_a):
            with self.assertRaises(Exception):
                apply_transition(stuck, 'sign_off', self.pat)
            apply_transition(self.fresh(stuck), 'sign_off', stuck.ball_in_court_user)
        self.assertEqual(self.fresh(stuck).status, Penetration.SIGNED_OFF)
        # Saving again with it already on moves nothing
        r = self.client.post(self.url('quality_settings'), {'permit_hours': 8, 'expiry_warning_hours': 2,
                                                           'fire_watch_minutes': 60, 'fs_independent_sign_off': 'on'},
                             follow=True)
        self.assertNotContains(r, 'Seals waiting on')

    def test_the_setting_is_on_the_quality_settings_page(self):
        self.login(self.alice)
        r = self.client.get(self.url('quality_settings'))
        self.assertContains(r, 'name="fs_independent_sign_off"')
        self.client.post(self.url('quality_settings'), {'permit_hours': 8, 'expiry_warning_hours': 2,
                                                       'fire_watch_minutes': 60})
        with org_context(self.org_a):
            self.assertFalse(QualitySettings.for_org(self.org_a).fs_independent_sign_off)

    def test_rule_table(self):
        # (sealed by, signing off, company setting on) -> allowed
        for sealed, signer, on, ok in ((1, 1, True, False), (1, 2, True, True), (None, 1, True, True),
                                       (1, 1, False, True), (2, 1, False, True)):
            self.assertEqual(rules.own_seal_problem(sealed, signer, on) is None, ok, (sealed, signer, on))

    def test_existing_seals_are_filled_from_the_audit_trail(self):
        import importlib

        from django.apps import apps as global_apps
        p = self.sealed_by(self.sam)
        with org_context(self.org_a):
            Penetration.objects.filter(pk=p.pk).update(sealed_by=None)
        from tenancy.context import unscoped
        migration = importlib.import_module('quality.migrations.0003_fill_sealed_by')
        with unscoped():
            migration.forwards(global_apps, None)
        self.assertEqual(self.fresh(p).sealed_by, self.sam)

    def test_invalid_transition_message_is_plain(self):
        p = self.sealed_by(self.pat)
        from ..workflow import _signed_off
        with org_context(self.org_a), self.assertRaises(InvalidTransition) as e:
            _signed_off(p, self.pat)
        self.assertIn('someone else on the job', str(e.exception))
