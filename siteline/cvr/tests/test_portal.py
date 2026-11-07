"""
Release 3 wave 2 defaults in commercial reporting (pending owner decision):

  - a month can't be signed off while an earlier month since the job's first CVR has no signed-off CVR,
    and the message names the missing month (CommercialSettings.cvr_every_month, on by default)
  - the client (or consultant) signs a daywork sheet in their portal instead of on our phone: the same
    signature path and checks, a Needs me item for them, hours only (never money)

Access for the portal: only the person asked sees and signs the sheet; other clients, subcontractors
and tendering subs are refused; another company gets a 404; no organisation fails closed.
"""
from datetime import date
from decimal import Decimal as D

from django.core.files.uploadedfile import SimpleUploadedFile

from changes.tests.base import make_user
from core.exceptions import InvalidTransition, TransitionForbidden
from core.workflow import apply_transition
from cvr import portal, rules, services
from cvr.models import DayworkLine, DayworkPhoto, DayworkSheet
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .base import CVRTestBase
from .test_views import png


class SkippedMonthTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()
        self.m3 = rules.add_months(self.m0, -3)
        with org_context(self.org_a):
            type(self.pa).objects.filter(pk=self.pa.pk).update(start_date=self.m3)
            self.pa.refresh_from_db()

    def signed(self, month):
        with org_context(self.org_a):
            p, _ = services.start_period(self.pa, month, self.pat, self.today)
            apply_transition(p, 'submit', self.pat, today=self.today)
            apply_transition(p, 'sign_off', self.alice)
        return p

    def submitted(self, month):
        with org_context(self.org_a):
            p, _ = services.start_period(self.pa, month, self.pat, self.today)
            apply_transition(p, 'submit', self.pat, today=self.today)
        return p

    def test_a_skipped_month_is_named(self):
        self.signed(self.m3)
        p1 = self.submitted(self.m1)  # m2 skipped
        with org_context(self.org_a), self.assertRaises(InvalidTransition) as e:
            apply_transition(p1, 'sign_off', self.alice)
        msg = str(e.exception)
        self.assertIn(f'There is no CVR for {rules.month_label(self.m2)}', msg)
        self.assertIn(f"first CVR ({rules.month_label(self.m3)})", msg)
        # Through the page, the same plain message
        self.login(self.alice)
        r = self.client.post(p1.get_absolute_url(), {'action': 'sign_off'}, follow=True)
        self.assertContains(r, f'There is no CVR for {rules.month_label(self.m2)}')
        # Doing the missing month lets it through
        self.signed(self.m2)
        with org_context(self.org_a):
            apply_transition(services.start_period(self.pa, self.m1, self.pat, self.today)[0], 'sign_off', self.alice)

    def test_the_first_cvr_starts_the_count(self):
        # Nothing before the job's first CVR is needed, however early the job started
        p2 = self.submitted(self.m2)
        with org_context(self.org_a):
            apply_transition(p2, 'sign_off', self.alice)

    def test_the_company_can_allow_a_skipped_month(self):
        with org_context(self.org_a):
            s = services.settings_for(self.org_a)
            self.assertTrue(s.cvr_every_month)
            s.cvr_every_month = False
            s.save()
        self.signed(self.m3)
        p1 = self.submitted(self.m1)
        with org_context(self.org_a):
            apply_transition(p1, 'sign_off', self.alice)

    def test_rule_table(self):
        jan, feb, mar, apr, may = (date(2026, m, 1) for m in range(1, 6))
        cases = [
            (jan, may, [jan, feb, mar, apr], []),
            (jan, may, [jan, mar, apr], [feb]),
            (jan, may, [jan], [feb, mar, apr]),
            (may, may, [], []),               # the first CVR itself
            (None, may, [], []),
            (jan, feb, [jan], []),
            (date(2025, 11, 1), feb, [date(2025, 11, 1), jan], [date(2025, 12, 1)]),  # across a year end
        ]
        for first, month, signed, missing in cases:
            self.assertEqual(rules.months_without_sign_off(first, month, signed), missing, (first, month, signed))


class PortalSigningTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.carl = make_user('carl@c.test', 'Carl Client Two')
        Membership.objects.create(organization=self.org_a, user=self.carl, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.carl, role=ProjectMember.ROLE_CLIENT)
            self.sheet = services.new_sheet(self.pa, self.stan, title='Move servery')
            DayworkLine.objects.create(sheet=self.sheet, kind='LABOUR', description='Joiner', quantity=D('8'),
                                       unit_rate=D('41.37'))
        self.detail = self.url('daywork_detail', self.pa.pk, self.sheet.pk)
        self.sign = self.url('daywork_portal_sign', self.pa.pk, self.sheet.pk)
        self.list = self.url('daywork_portal', self.pa.pk)

    def fresh(self):
        with org_context(self.org_a):
            return DayworkSheet.objects.get(pk=self.sheet.pk)

    def ask(self, signer=None):
        self.login(self.stan)
        return self.client.post(self.detail, {'action': 'ask_client', 'signer': (signer or self.carla).pk})

    def needs(self, user):
        from actions import registry
        with org_context(self.org_a):
            req = registry.request_for(user, Membership.objects.get(organization=self.org_a, user=user))
            return {(i.kind, i.ref) for i in registry.for_user(req)}

    def signature(self, marked=True):
        return SimpleUploadedFile('signature.png', png(marked=marked), content_type='image/png')

    def test_client_signs_in_the_portal(self):
        r = self.ask()
        self.assertRedirects(r, self.sheet.get_absolute_url())
        sheet = self.fresh()
        self.assertEqual((sheet.sign_requested_from, sheet.sign_requested_by), (self.carla, self.stan))
        self.assertIn(('daywork_sign', sheet.reference), self.needs(self.carla))
        self.assertNotIn(('daywork_sign', sheet.reference), self.needs(self.carl))
        self.login(self.stan)
        self.assertContains(self.client.get(self.detail), 'Waiting for Carla Client')
        self.login(self.carla)
        page = self.client.get(self.sign)
        self.assertContains(page, '<canvas')
        self.assertContains(page, 'Joiner')
        self.assertNotContains(page, '41.37')  # hours only: never rates or money
        self.assertNotContains(page, '£')
        self.assertContains(self.client.get(self.list), self.sign)
        # The same checks as the phone: a blank pad is refused
        r = self.client.post(self.sign, {'name': 'Carla Client', 'role': 'CLIENT', 'confirm': 'on',
                                         'version': sheet.version, 'signature': self.signature(False)})
        self.assertContains(r, 'The signature box is empty')
        r = self.client.post(self.sign, {'name': 'Carla Client', 'role': 'CLIENT', 'confirm': 'on',
                                         'version': sheet.version, 'signature': self.signature()})
        self.assertRedirects(r, self.list)
        sheet = self.fresh()
        self.assertEqual((sheet.status, sheet.signed_by_name, sheet.signature_recorded_by),
                         (DayworkSheet.SIGNED, 'Carla Client', self.carla))
        self.assertTrue(sheet.signature.name.startswith(f'org_{self.org_a.pk}/daywork-signatures/'))
        self.assertNotIn(('daywork_sign', sheet.reference), self.needs(self.carla))
        self.assertIn(('daywork', sheet.reference), self.needs(self.pat))  # on to the change register
        with org_context(self.org_a):
            self.assertIn('Signed in their portal by Carla Client',
                          services.change_description(sheet, sheet.price()))
        # Nothing left to sign
        r = self.client.get(self.sign, follow=True)
        self.assertContains(r, 'There is nothing to sign')

    def test_a_sheet_changed_since_it_was_opened_is_refused(self):
        self.ask()
        opened = self.fresh().version
        with org_context(self.org_a):
            DayworkLine.objects.create(sheet=self.sheet, kind='LABOUR', description='Labourer', quantity=D('2'),
                                       unit_rate=D('20'))
            s = self.fresh()
            s.save(actor=self.stan, audit_action='priced')
        self.login(self.carla)
        r = self.client.post(self.sign, {'name': 'Carla Client', 'role': 'CLIENT', 'confirm': 'on',
                                         'version': opened, 'signature': self.signature()}, follow=True)
        self.assertContains(r, 'changed')
        self.assertEqual(self.fresh().status, DayworkSheet.DRAFT)

    def test_only_the_person_asked_can_see_or_sign_it(self):
        self.ask()
        with org_context(self.org_a):
            DayworkPhoto.objects.create(sheet=self.sheet, image=SimpleUploadedFile('p.png', png()),
                                        thumbnail=SimpleUploadedFile('t.png', png()))
            photo = DayworkPhoto.objects.get(sheet=self.sheet)
        photo_url = self.url('daywork_portal_photo', self.pa.pk, self.sheet.pk, photo.pk, 'thumb')
        self.login(self.carla)
        self.assertNotEqual(self.client.get(photo_url).status_code, 404)
        for user, codes in ((self.carl, (404,)), (self.connie, (404,)), (self.sid, (403,)), (self.bea, (403,)),
                            (self.nora, (404,)), (self.bob, (404,))):
            self.login(user)
            self.assertIn(self.client.get(self.sign).status_code, codes, user)
            self.assertIn(self.client.get(photo_url).status_code, codes, user)
            self.assertIn(self.client.post(self.sign, {'name': 'x', 'role': 'CLIENT', 'confirm': 'on',
                                                       'version': self.fresh().version,
                                                       'signature': self.signature()}).status_code, codes, user)
        self.assertEqual(self.fresh().status, DayworkSheet.DRAFT)
        # The transition itself refuses anyone but staff and the person asked
        with org_context(self.org_a):
            for user in (self.carl, self.sid, self.connie):
                with self.assertRaises(TransitionForbidden):
                    apply_transition(self.fresh(), 'sign', user, signature=self.signature(), signer='x')

    def test_staff_cant_ask_a_subcontractor_and_can_stop_asking(self):
        r = self.ask(self.sid)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.fresh().sign_requested_from)
        self.ask(self.connie)  # a consultant (e.g. the contract administrator) can be asked
        self.assertEqual(self.fresh().sign_requested_from, self.connie)
        self.client.post(self.detail, {'action': 'stop_asking'})
        self.assertIsNone(self.fresh().sign_requested_from)
        self.assertNotIn(('daywork_sign', self.sheet.reference), self.needs(self.connie))

    def test_signed_on_the_phone_instead_clears_their_needs_me(self):
        self.ask()
        with org_context(self.org_a):
            apply_transition(self.fresh(), 'sign', self.stan, signature=self.signature(), signer='Carla on site',
                             role='CLIENT')
        self.assertNotIn(('daywork_sign', self.sheet.reference), self.needs(self.carla))

    def test_tab_shows_once_they_are_asked(self):
        self.login(self.carla)
        self.assertNotContains(self.client.get(self.url('project_decisions', self.pa.pk)), self.list)
        self.assertEqual(self.client.get(self.list).status_code, 200)  # an empty page that says why
        self.ask()
        self.login(self.carla)
        self.assertContains(self.client.get(self.url('project_decisions', self.pa.pk)), self.list)
        self.login(self.carl)
        self.assertNotContains(self.client.get(self.url('project_decisions', self.pa.pk)), self.list)

    def test_staff_are_sent_to_the_staff_page(self):
        self.login(self.stan)
        self.assertRedirects(self.client.get(self.list), self.url('daywork_list', self.pa.pk))

    def test_another_company_and_no_organisation(self):
        self.ask()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.list).status_code, 404)
        with self.assertRaises(TenantContextMissing):
            list(DayworkSheet.objects.filter(sign_requested_from=self.carla))
        from actions import registry
        req = registry.request_for(self.carla, Membership.objects.get(organization=self.org_a, user=self.carla))
        with self.assertRaises(TenantContextMissing):
            portal.client_items(req)
