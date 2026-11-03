"""
The CVR, cashflow and daywork pages for every role, another company and no organisation; the
daywork sheet from a phone through to the change register; Needs me.
"""
import io
from decimal import Decimal as D

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from changes.links import came_from
from changes.models import ChangeEvent
from core.workflow import apply_transition
from cvr import actions as cvr_actions
from cvr import services
from cvr.models import CVRPeriod, DayworkPhoto, DayworkSheet
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context

from .base import CVRTestBase

ALLOWED = (403, 404)


def png(marked=True, size=(300, 120)):
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', size, (255, 255, 255, 255))
    if marked:
        ImageDraw.Draw(img).line((20, 80, 120, 30, 200, 90), fill=(21, 25, 31, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class AccessTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()
        with org_context(self.org_a):
            self.period, _ = services.start_period(self.pa, self.m1, self.pat, self.today)
            self.sheet = services.new_sheet(self.pa, self.stan, title='Move servery')

    def expect(self, url, staff_ok):
        for name in ('alice', 'adam', 'pat', 'stan', 'eve', 'carla', 'connie', 'sid', 'bea', 'nora', 'bob'):
            with self.subTest(url=url, user=name):
                status = self.status_for(getattr(self, name), url)
                if name in staff_ok:
                    self.assertEqual(status, 200)
                elif name == 'bob':
                    self.assertEqual(status, 404)
                else:
                    self.assertIn(status, ALLOWED)

    def test_cvr_and_cashflow_are_for_the_jobs_cost_team(self):
        team = ('alice', 'adam', 'pat')
        for url in (self.url('cvr_list', self.pa.pk), self.url('cvr_period', self.pa.pk, self.period.pk),
                    self.url('cvr_cashflow', self.pa.pk)):
            self.expect(url, team)

    def test_dayworks_are_for_all_staff_on_the_job(self):
        staff = ('alice', 'adam', 'pat', 'stan')
        for url in (self.url('daywork_list', self.pa.pk), self.url('daywork_detail', self.pa.pk, self.sheet.pk),
                    self.url('daywork_create', self.pa.pk), self.url('daywork_rates', self.pa.pk),
                    self.url('daywork_sign', self.pa.pk, self.sheet.pk)):
            self.expect(url, staff)

    def test_company_cvr(self):
        url = self.url('company_cvr')
        self.assertEqual(self.status_for(self.alice, url), 200)
        self.assertEqual(self.status_for(self.carla, url), 403)
        self.assertEqual(self.status_for(self.bob, url), 404)
        self.login(self.pat)
        r = self.client.get(url + f'?month={self.m1:%Y-%m}')
        self.assertContains(r, 'Alpha job')
        # Staff who run no job (site staff, estimators) are refused (review: it opened empty for them)
        self.assertEqual(self.status_for(self.stan, url), 403)
        self.assertEqual(self.status_for(self.eve, url), 403)

    def test_settings_are_admins_only(self):
        url = self.url('cvr_settings')
        self.assertEqual(self.status_for(self.adam, url), 200)
        self.assertEqual(self.status_for(self.pat, url), 403)
        self.login(self.adam)
        self.client.post(url, {'cvr_due_day': '8', 'pm_can_sign_off': 'on', 'labour_pct': '90',
                               'materials_pct': '15', 'plant_pct': '10'})
        with org_context(self.org_a):
            s = services.settings_for(self.org_a)
        self.assertEqual((s.cvr_due_day, s.pm_can_sign_off, s.labour_pct), (8, True, D('90.00')))

    def test_another_company_cant_reach_any_of_it(self):
        self.login(self.bob)
        for url in (self.url('cvr_list', self.pa.pk), self.url('cvr_period', self.pa.pk, self.period.pk),
                    self.url('daywork_detail', self.pa.pk, self.sheet.pk), self.url('cvr_cashflow', self.pa.pk),
                    self.url('cvr_list', self.pa.pk, org=self.org_b)):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        # Bob's own project id under his company, with Alpha's record ids: still nothing
        self.assertEqual(self.client.get(self.url('cvr_period', self.pb.pk, self.period.pk, org=self.org_b))
                         .status_code, 404)

    def test_no_organisation_fails_closed(self):
        for model in (CVRPeriod, DayworkSheet):
            with self.subTest(model=model.__name__):
                with self.assertRaises(TenantContextMissing):
                    list(model.objects.all())

    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            from django.utils import timezone
            self.pa.archived_at = timezone.now()
            self.pa.save()
        self.login(self.alice)  # owners can still open it, read only
        self.assertEqual(self.client.get(self.url('cvr_period', self.pa.pk, self.period.pk)).status_code, 200)
        r = self.client.post(self.url('cvr_period', self.pa.pk, self.period.pk),
                             {'action': 'adjust', 'adj-kind': 'COST', 'adj-amount': '5', 'adj-reason': 'x'})
        self.assertIn(r.status_code, (403, 405))
        with org_context(self.org_a):
            self.assertEqual(self.period.adjustments.count(), 0)


class CVRPageTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()

    def test_start_adjust_submit_and_sign_off_through_the_pages(self):
        self.login(self.pat)
        r = self.client.get(self.url('cvr_list', self.pa.pk))
        self.assertContains(r, 'No monthly CVRs yet')
        self.assertContains(r, 'Start the CVR')
        r = self.client.post(self.url('cvr_list', self.pa.pk), {'month': self.m1.isoformat()})
        with org_context(self.org_a):
            period = CVRPeriod.objects.get(project=self.pa, month=self.m1)
        self.assertRedirects(r, period.get_absolute_url())
        page = self.client.get(period.get_absolute_url())
        self.assertContains(page, '£56,000.00')  # value to date, from AP-002
        self.assertContains(page, '£16,500.00')  # cost to date, from the ledger
        # The budget was set this month, so last month's forecast final cost is flagged as short of it
        self.assertContains(page, "budget went on the cost ledger on")
        # An adjustment with a reason; one with none is a form error
        r = self.client.post(period.get_absolute_url(), {'action': 'adjust', 'adj-kind': 'COST', 'adj-amount': '2500',
                                                         'adj-reason': 'Scaffold invoice not yet in'})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(period.get_absolute_url(), {'action': 'adjust', 'adj-kind': 'COST', 'adj-amount': '0',
                                                         'adj-reason': ''})
        self.assertContains(r, 'Enter an amount other than nil', status_code=200)
        page = self.client.get(period.get_absolute_url())
        self.assertContains(page, '£19,000.00')  # 16,500 + 2,500
        self.client.post(period.get_absolute_url(), {'action': 'submit'})
        # The PM can't sign off by default; the owner can
        self.client.post(period.get_absolute_url(), {'action': 'sign_off'})
        with org_context(self.org_a):
            self.assertEqual(CVRPeriod.objects.get(pk=period.pk).status, CVRPeriod.SUBMITTED)
        self.login(self.alice)
        page = self.client.get(period.get_absolute_url())
        self.assertContains(page, 'Sign off and lock')
        self.client.post(period.get_absolute_url(), {'action': 'sign_off'})
        with org_context(self.org_a):
            self.assertEqual(CVRPeriod.objects.get(pk=period.pk).status, CVRPeriod.SIGNED_OFF)
        page = self.client.get(period.get_absolute_url())
        self.assertContains(page, 'Signed off and locked')
        self.assertNotContains(page, 'Add an accrual or adjustment')
        # A post to a locked month changes nothing
        self.client.post(period.get_absolute_url(), {'action': 'adjust', 'adj-kind': 'COST', 'adj-amount': '1',
                                                     'adj-reason': 'late'})
        with org_context(self.org_a):
            self.assertEqual(period.adjustments.count(), 1)

    def test_csv_exports_are_formula_safe(self):
        with org_context(self.org_a):
            services.start_period(self.pa, self.m1, self.pat, self.today)
            services.new_sheet(self.pa, self.pat, title='=HYPERLINK("http://x")')
        self.login(self.pat)
        with self.settings(STEP_UP_SECONDS=10 ** 9):
            for url in (self.url('cvr_list', self.pa.pk), self.url('daywork_list', self.pa.pk),
                        self.url('cvr_cashflow', self.pa.pk), self.url('company_cvr')):
                with self.subTest(url=url):
                    r = self.client.get(url + '?export=csv', follow=True)
                    body = r.content.decode()
                    if 'text/csv' not in r.get('Content-Type', ''):
                        continue  # asked to confirm it's them first (step-up): checked in security's tests
                    self.assertNotIn('\n=HYPERLINK', body)
                    self.assertNotIn(',=HYPERLINK', body)

    def test_cashflow_page_has_the_chart_and_the_table(self):
        self.login(self.pat)
        r = self.client.get(self.url('cvr_cashflow', self.pa.pk))
        self.assertContains(r, '<svg class="cf-chart"')
        self.assertContains(r, 'role="img"')
        self.assertContains(r, 'Cashflow table')
        self.assertContains(r, '£19,000')  # received last month, ex VAT
        # drawn on the server: no chart library, nothing from another site
        self.assertNotContains(r, 'src="http')
        self.assertNotContains(r, 'chart.js')

    def test_cashflow_empty_state(self):
        with org_context(self.org_a):
            from projects.models import Project
            empty = Project.objects.create(name='Empty job', stage=Project.STAGE_LIVE, manager=self.pat)
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('cvr_cashflow', empty.pk)), 'No money in or out yet')

    def test_commercial_sub_navigation(self):
        self.login(self.pat)
        page = self.client.get(self.url('contract_detail', self.pa.pk)).content.decode()
        for label in ('>CVR<', '>Cashflow<', '>Dayworks<'):
            self.assertIn(label, page)
        self.login(self.stan)
        page = self.client.get(self.url('contract_detail', self.pa.pk)).content.decode()
        self.assertIn('>Dayworks<', page)
        self.assertNotIn('>CVR<', page)


@override_settings(MAX_UPLOAD_MB=5)
class DayworkFlowTests(CVRTestBase):
    def detail(self, sheet):
        return self.url('daywork_detail', self.pa.pk, sheet.pk)

    def test_from_the_phone_to_the_change_register(self):
        self.login(self.stan)
        r = self.client.get(self.url('daywork_list', self.pa.pk))
        self.assertContains(r, 'No daywork sheets yet')
        r = self.client.get(self.url('daywork_create', self.pa.pk))
        self.assertContains(r, 'Start the sheet')
        # Only the work is needed; the date is today
        r = self.client.post(self.url('daywork_create', self.pa.pk), {'title': 'Move bar servery 600mm',
                                                                      'work_date': self.today.isoformat()})
        with org_context(self.org_a):
            sheet = DayworkSheet.objects.get(project=self.pa)
        self.assertRedirects(r, sheet.get_absolute_url())
        self.assertEqual(sheet.reference, 'DW-001')
        # Site staff can't set rates; the PM can
        self.assertEqual(self.client.post(self.url('daywork_rates', self.pa.pk), {'action': 'add_rate'}).status_code,
                         403)
        self.login(self.pat)
        self.client.post(self.url('daywork_rates', self.pa.pk), {
            'action': 'add_rate', 'rate-kind': 'LABOUR', 'rate-name': 'Joiner', 'rate-unit': 'hour',
            'rate-rate': '42.50'})
        self.client.post(self.url('daywork_rates', self.pa.pk), {
            'action': 'terms', 'terms-labour_pct': '80', 'terms-materials_pct': '15', 'terms-plant_pct': '10',
            'terms-basis': 'Contract Particulars'})
        with org_context(self.org_a):
            joiner = self.pa.daywork_rates.get(name='Joiner')
        self.login(self.stan)
        # nothing to sign yet: no link to the signing page (its wording also appears in the help panel)
        self.assertNotContains(self.client.get(self.detail(sheet)), self.url('daywork_sign', self.pa.pk, sheet.pk))
        lines = [
            {'line-kind': 'LABOUR', 'line-rate': joiner.pk, 'line-quantity': '8'},
            {'line-kind': 'LABOUR', 'line-description': 'Labourer', 'line-quantity': '7.5', 'line-unit_rate': '28.33'},
            {'line-kind': 'MATERIALS', 'line-description': 'MDF sheets', 'line-quantity': '3', 'line-unit': 'sheets',
             'line-unit_rate': '12.99'},
            {'line-kind': 'PLANT', 'line-description': 'Scissor lift', 'line-quantity': '4', 'line-unit_rate': '15'},
        ]
        for data in lines:
            r = self.client.post(self.detail(sheet), {'action': 'add_line', **data})
            self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        # No rate and none chosen: told how to fix it
        r = self.client.post(self.detail(sheet), {'action': 'add_line', 'line-kind': 'LABOUR',
                                                  'line-description': 'Joiner', 'line-quantity': '2'})
        self.assertContains(r, 'Enter the rate, or choose one from the list')
        page = self.client.get(self.detail(sheet))
        self.assertContains(page, '£1,105.28')
        self.assertContains(page, self.url('daywork_sign', self.pa.pk, sheet.pk))
        self.assertContains(page, 'capture="environment"')
        # A photo through the normal upload path
        r = self.client.post(self.detail(sheet), {'action': 'add_photo', 'photo-caption': 'Before',
                                                  'photo-photo': SimpleUploadedFile('before.png', png(),
                                                                                    content_type='image/png')})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            photo = DayworkPhoto.objects.get(sheet=sheet)
        self.assertEqual(self.client.get(self.url('daywork_photo', self.pa.pk, sheet.pk, photo.pk, 'thumb'))
                         .status_code, 200)
        self.login(self.carla)
        self.assertIn(self.client.get(self.url('daywork_photo', self.pa.pk, sheet.pk, photo.pk, 'full')).status_code,
                      ALLOWED)
        self.login(self.stan)
        # The signature: a blank pad is refused, a signed one recorded
        sign_url = self.url('daywork_sign', self.pa.pk, sheet.pk)
        page = self.client.get(sign_url)
        self.assertContains(page, 'signature.js')
        self.assertContains(page, '<canvas')
        r = self.client.post(sign_url, {'name': 'Client rep', 'role': 'CLIENT', 'confirm': 'on',
                                        'signature': SimpleUploadedFile('signature.png', png(marked=False),
                                                                        content_type='image/png')})
        self.assertContains(r, 'The signature box is empty')
        r = self.client.post(sign_url, {'name': 'Client rep', 'role': 'CLIENT', 'confirm': 'on',
                                        'signature': SimpleUploadedFile('signature.html', b'<script>',
                                                                        content_type='text/html')})
        self.assertContains(r, '.html files are not allowed')
        r = self.client.post(sign_url, {'name': 'Client rep', 'role': 'CLIENT', 'confirm': 'on',
                                        'signature': SimpleUploadedFile('signature.png', png(),
                                                                        content_type='image/png')})
        self.assertRedirects(r, sheet.get_absolute_url())
        with org_context(self.org_a):
            sheet = DayworkSheet.objects.get(pk=sheet.pk)
        self.assertEqual((sheet.status, sheet.signed_by_name), (DayworkSheet.SIGNED, 'Client rep'))
        self.assertTrue(sheet.signature.name.startswith(f'org_{self.org_a.pk}/daywork-signatures/'))
        self.assertEqual(self.client.get(self.url('daywork_signature', self.pa.pk, sheet.pk)).status_code, 200)
        # Fixed once signed
        self.client.post(self.detail(sheet), {'action': 'add_line', **lines[0]})
        with org_context(self.org_a):
            self.assertEqual(sheet.lines.count(), 4)
        # Needs me: the PM (and whoever wrote it) to add it to the change register
        self.assertIn(('daywork', 'DW-001'), self.needs(self.pat))
        self.assertIn(('daywork', 'DW-001'), self.needs(self.stan))
        # One step onto the change register
        r = self.client.post(self.detail(sheet), {'action': 'send', 'send-change_event': ''})
        with org_context(self.org_a):
            ce = ChangeEvent.objects.get(project=self.pa)
            sheet = DayworkSheet.objects.get(pk=sheet.pk)
            lines_on_ce = {(ln.kind, ln.amount) for ln in ce.lines.all()}
            sources = {i.ref for i in came_from(ce)}
        self.assertRedirects(r, ce.get_absolute_url())
        self.assertEqual(lines_on_ce, {('REVENUE', D('1105.28')), ('COST', D('651.45'))})
        self.assertEqual((sheet.status, sheet.change_event_id), (DayworkSheet.IN_CHANGE, ce.pk))
        self.assertIn('DW-001', sources)
        self.assertIn('Signed on the day by Client rep', ce.description)
        self.assertNotIn(('daywork', 'DW-001'), self.needs(self.pat))
        # Sending again does nothing more
        self.client.post(self.detail(sheet), {'action': 'send', 'send-change_event': ''})
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.filter(project=self.pa).count(), 1)

    def test_onto_an_existing_change_event_and_unsigned(self):
        with org_context(self.org_a):
            ce = ChangeEvent(project=self.pa, title='Servery move', description='x', ball_in_court_user=self.pat)
            ce.save(actor=self.pat)
            sheet = services.new_sheet(self.pa, self.stan, title='Servery labour')
            from cvr.models import DayworkLine
            DayworkLine.objects.create(sheet=sheet, kind='LABOUR', description='Joiner', quantity=D('2'),
                                       unit_rate=D('40'))
        self.login(self.stan)
        self.client.post(self.detail(sheet), {'action': 'send', 'send-change_event': str(ce.pk)})
        with org_context(self.org_a):
            self.assertEqual(ce.lines.count(), 2)
            self.assertIn('Not signed by the client side', services.change_description(
                DayworkSheet.objects.get(pk=sheet.pk), {'prime': D('80'), 'additions': D('0'), 'total': D('80')}))

    def test_unsigned_sheet_chases_its_writer(self):
        from datetime import timedelta
        with org_context(self.org_a):
            sheet = services.new_sheet(self.pa, self.stan, title='Old work', work_date=self.today - timedelta(days=5))
        self.assertIn(('daywork', sheet.reference), self.needs(self.stan))
        self.assertNotIn(('daywork', sheet.reference), self.needs(self.pat))

    def test_cancelled_sheet_stays(self):
        with org_context(self.org_a):
            sheet = services.new_sheet(self.pa, self.stan, title='Mistake')
        self.login(self.stan)
        self.client.post(self.detail(sheet), {'action': 'cancel', 'note': 'Duplicate'})
        with org_context(self.org_a):
            self.assertEqual(DayworkSheet.objects.get(pk=sheet.pk).status, DayworkSheet.VOID)

    def needs(self, user):
        with org_context(self.org_a):
            from actions import registry
            from tenancy.context import close_request_memo, open_request_memo
            from tenancy.models import Membership
            memo = open_request_memo()
            try:
                req = registry.request_for(user, Membership.objects.get(organization=self.org_a, user=user))
                return {(i.kind, i.ref) for i in registry.for_user(req)}
            finally:
                close_request_memo(memo)


class NeedsMeTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()

    def items(self, user):
        return {(i.kind, i.title) for i in cvr_actions.cvr_items(self.request_for(user))}

    def test_prepare_then_sign_off(self):
        last = f'Prepare the CVR for {self.m1:%B %Y}'
        with org_context(self.org_a):
            self.assertIn(('cvr', last), self.items(self.pat))
            self.assertNotIn(('cvr', last), self.items(self.alice))  # owners aren't chased for jobs they don't run
            self.assertEqual(self.items(self.stan), set())
            self.assertEqual(cvr_actions.cvr_items(self.request_for(self.carla)), [])
            period, _ = services.start_period(self.pa, self.m1, self.pat, self.today)
            item = next(i for i in cvr_actions.cvr_items(self.request_for(self.pat)) if i.title == last)
            self.assertEqual(item.ref, period.reference)
            self.assertEqual(item.due, self.m0.replace(day=10))
            apply_transition(period, 'submit', self.pat, today=self.today)
            self.assertNotIn(('cvr', last), self.items(self.pat))
            sign = f'Sign off the CVR for {self.m1:%B %Y}'
            self.assertIn(('cvr', sign), self.items(self.alice))
            self.assertIn(('cvr', sign), self.items(self.adam))
            self.assertNotIn(('cvr', sign), self.items(self.pat))
            apply_transition(period, 'sign_off', self.alice)
            self.assertEqual(self.items(self.alice), set())

    def test_a_qs_on_the_team_prepares_it(self):
        from tenancy.models import Membership
        from changes.tests.base import make_user
        quinn = make_user('quinn@a.test', 'Quinn QS')
        Membership.objects.create(organization=self.org_a, user=quinn, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=quinn, role=ProjectMember.ROLE_QS)
            self.assertIn(('cvr', f'Prepare the CVR for {self.m1:%B %Y}'), self.items(quinn))
        self.assertEqual(self.status_for(quinn, self.url('cvr_list', self.pa.pk)), 200)
