"""
Applications for payment through the views: who can open what (every role), submitting, the
client's payment and pay less notices and their deadlines, notices recorded on the client's
behalf with evidence, payments, Needs me, the PDF and its escaping, and tenancy.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from changes.tests.base import CommercialTestBase, make_user
from contracts.commercial import commercial_position
from contracts.models import MainContract
from core.access import visible_index
from core.models import AuditEntry, RecordIndex
from core.workflow import apply_transition
from projects.models import Project, ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership, Notification
from variations.models import Variation

from .. import actions as payment_actions
from .. import pdf as payment_pdf
from ..models import Application, Payment

ZERO = Decimal('0.00')


class PaymentsTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.stan = make_user('stan@a.test', 'Stan Staff')  # staff on live jobs, not this job's PM
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            self.contract = MainContract(project=self.pa, form='JCT_IC', edition='2016', valuation_day=28,
                                         administrator=self.carla_member)
            self.contract.apply_preset(creating=True)
            self.contract.save()

    def make(self, valuation=None, work='100000', submit=False, actor=None, project=None, org=None, **kw):
        with org_context(org or self.org_a):
            project = project or self.pa
            valuation = valuation or timezone.localdate() - timedelta(days=3)
            a = Application(project=project, valuation_date=valuation, work_done=Decimal(work),
                            title=f'Valuation to {valuation}', **kw)
            a.recalculate(MainContract.objects.filter(project=project).first())
            a.save(actor=actor or self.pat)
            if submit:
                apply_transition(a, 'submit', actor or self.pat)
            return a

    def detail(self, a):
        return self.url('application_detail', self.pa.pk, a.pk)

    def reload(self, a):
        with org_context(self.org_a):
            return Application.objects.get(pk=a.pk)


class RecordTests(PaymentsTestBase):
    def test_numbering_per_project_and_index(self):
        a, b = self.make(), self.make(valuation=timezone.localdate() - timedelta(days=40))
        with org_context(self.org_a):
            pa2 = Project.objects.create(name='Second', stage=Project.STAGE_LIVE)
        c = self.make(project=pa2)
        other = self.make(project=self.pb, org=self.org_b, actor=self.bob)
        self.assertEqual((a.ref, b.ref, c.ref, other.ref), ('AP-001', 'AP-002', 'AP-001', 'AP-001'))
        with org_context(self.org_a):
            idx = RecordIndex.objects.get(ref='AP-001', project=self.pa)
            self.assertEqual((idx.visibility, idx.record_type), ('INTERNAL', 'payments.application'))

    def test_submit_freezes_figures_dates_and_shares_with_the_client(self):
        a = self.make(valuation=timezone.localdate() - timedelta(days=2))
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'submit'})
        self.assertEqual(r.status_code, 302)
        a = self.reload(a)
        self.assertEqual((a.status, a.visibility, a.ball_in_court_user), ('SUBMITTED', 'SHARED', self.carla))
        self.assertEqual(a.due_date, a.payment_notice_by)
        self.assertIsNotNone(a.final_date)
        self.assertEqual((a.gross_valuation, a.retention, a.amount_due),
                         (Decimal('100000.00'), Decimal('5000.00'), Decimal('95000.00')))
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.carla, title__contains='AP-001').exists())
            self.assertTrue(AuditEntry.objects.filter(action='submit').exists())

    def test_previous_amounts_variations_and_lines(self):
        with org_context(self.org_a):
            vo = Variation(project=self.pa, title='Extra', description='x', value=Decimal('2000'))
            vo.save(actor=self.pat)
            Variation.objects.filter(pk=vo.pk).update(status='APPROVED', approved_value=Decimal('1800.00'))
        first = self.make(valuation=timezone.localdate() - timedelta(days=40), submit=True)
        self.assertEqual(first.variations_value, Decimal('1800.00'))
        second = self.make(work='150000')
        self.assertEqual(second.previous_certified, first.notified_sum)
        self.assertEqual(second.previous_certified, Decimal('96710.00'))   # (100,000 + 1,800) x 95%
        # Work done by section replaces the single figure
        self.login(self.pat)
        r = self.client.post(self.url('application_edit', self.pa.pk, second.pk), {
            'valuation_date': second.valuation_date.isoformat(), 'work_done': '1', 'materials_on_site': '0',
            'materials_off_site': '0', 'notes': '', 'version': second.version,
            'lines-TOTAL_FORMS': '2', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '60',
            'lines-0-section': 'Strip out', 'lines-0-value': '40000', 'lines-1-section': 'Drylining',
            'lines-1-value': '120000.50'})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context.get('form').errors)
        second = self.reload(second)
        self.assertEqual(second.work_done, Decimal('160000.50'))
        self.assertEqual(second.gross_valuation, Decimal('161800.50'))

    def test_one_application_per_valuation_date(self):
        a = self.make()
        self.login(self.pat)
        r = self.client.post(self.url('application_create', self.pa.pk), {
            'valuation_date': a.valuation_date.isoformat(), 'work_done': '5', 'materials_on_site': '0',
            'materials_off_site': '0', 'lines-TOTAL_FORMS': '0', 'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '60'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'is already made up to')

    def test_create_through_the_view(self):
        self.login(self.pat)
        r = self.client.post(self.url('application_create', self.pa.pk), {
            'valuation_date': '2026-09-28', 'work_done': '50000', 'materials_on_site': '1500',
            'materials_off_site': '0', 'notes': 'First one', 'lines-TOTAL_FORMS': '3', 'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '60'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            a = Application.objects.get()
        self.assertEqual((a.ref, a.status, a.gross_valuation, a.retention), ('AP-001', 'DRAFT', Decimal('51500.00'),
                                                                             Decimal('2575.00')))


class RoleTests(PaymentsTestBase):
    """Staff: owners, admins and the job's PM. Client: their own job's submitted ones. Nobody else."""

    def test_list_detail_pdf_by_role(self):
        draft, sent = self.make(), self.make(valuation=timezone.localdate() - timedelta(days=40), submit=True)
        urls = {
            'list': self.url('application_list', self.pa.pk),
            'draft': self.detail(draft), 'sent': self.detail(sent),
            'pdf': self.url('application_pdf', self.pa.pk, sent.pk),
            'new': self.url('application_create', self.pa.pk),
        }
        # (user, list, draft, submitted, pdf, new)
        table = [
            (self.alice, 200, 200, 200, 200, 200),   # owner
            (self.adam, 200, 200, 200, 200, 200),    # admin
            (self.pat, 200, 200, 200, 200, 200),     # the job's PM
            (self.stan, 403, 403, 403, 403, 403),    # other staff
            (self.eve, 404, 404, 404, 404, 404),     # estimating only: can't open live jobs
            (self.carla, 200, 404, 200, 200, 403),   # client: submitted only
            (self.connie, 403, 403, 403, 403, 403),  # consultant
            (self.sid, 403, 403, 403, 403, 403),     # subcontractor
            (self.bea, 403, 403, 403, 403, 403),     # tendering subcontractor
            (self.nora, 404, 404, 404, 404, 404),    # external, not on the job
            (self.bob, 404, 404, 404, 404, 404),     # another company
        ]
        for user, *codes in table:
            for key, code in zip(('list', 'draft', 'sent', 'pdf', 'new'), codes):
                with self.subTest(user=user.email, page=key):
                    self.assertEqual(self.status_for(user, urls[key]), code)

    def test_pm_on_the_project_team_counts(self):
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.stan, role=ProjectMember.ROLE_PM)
        self.assertEqual(self.status_for(self.stan, self.url('application_list', self.pa.pk)), 200)

    def test_client_list_shows_only_submitted(self):
        self.make(valuation=timezone.localdate() - timedelta(days=2))
        sent = self.make(valuation=timezone.localdate() - timedelta(days=40), submit=True)
        self.login(self.carla)
        r = self.client.get(self.url('application_list', self.pa.pk))
        self.assertContains(r, sent.reference)
        self.assertNotContains(r, 'AP-001')   # the draft
        self.assertNotContains(r, 'New application')

    def test_client_portal_tab_under_decisions(self):
        self.make(submit=True)
        self.login(self.carla)
        r = self.client.get(self.url('project_decisions', self.pa.pk))
        self.assertContains(r, 'Applications for payment')
        self.assertContains(r, 'Give your payment notice')
        self.login(self.connie)
        self.assertNotContains(self.client.get(self.url('project_decisions', self.pa.pk)), 'Applications for payment')

    def test_commercial_subnav(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('contract_detail', self.pa.pk)), 'Applications')
        self.login(self.stan)
        r = self.client.get(self.url('contract_detail', self.pa.pk))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Certified to date')
        self.assertNotIn(self.url('application_list', self.pa.pk), r.content.decode())

    def test_index_hides_applications_from_consultants_and_subs(self):
        self.make(submit=True)
        for user, expected in ((self.carla, 1), (self.connie, 0), (self.sid, 0)):
            with org_context(self.org_a):
                req = self.request_for(user)
                self.assertEqual(visible_index(req).filter(record_type='payments.application').count(), expected)

    def test_only_managers_submit_and_withdraw(self):
        a = self.make()
        for user in (self.carla, self.stan):
            self.status_for(user, self.detail(a), 'post', {'action': 'submit'})
        self.assertEqual(self.reload(a).status, 'DRAFT')
        self.login(self.carla)
        a = self.make(valuation=timezone.localdate() - timedelta(days=40), submit=True)
        self.assertEqual(self.client.post(self.detail(a), {'action': 'withdraw'}).status_code, 403)


class ClientNoticeTests(PaymentsTestBase):
    def test_client_gives_payment_notice_then_pay_less(self):
        a = self.make(submit=True)
        self.login(self.carla)
        r = self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '90000', 'basis': 'Ceilings at 80%'})
        self.assertEqual(r.status_code, 302)
        a = self.reload(a)
        self.assertEqual((a.status, a.notified_sum, a.payment_notice_on), ('NOTIFIED', Decimal('90000.00'),
                                                                           timezone.localdate()))
        self.assertEqual(a.notified_total, Decimal('108000.00'))
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.pat, title__contains='payment notice').exists())
        r = self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '85000', 'basis': 'Defective boarding'})
        a = self.reload(a)
        self.assertEqual((a.status, a.notified_sum), ('PAY_LESS', Decimal('85000.00')))

    def test_pay_less_one_day_late_is_refused_and_says_why(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.pay_less_by = timezone.localdate() - timedelta(days=1)
            a.save(actor=self.pat)
        self.login(self.carla)
        r = self.client.get(self.detail(a))
        self.assertContains(r, 'Too late')
        self.assertNotContains(r, 'Give pay less notice')
        r = self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '1', 'basis': 'x'})
        self.assertContains(r, 'Too late', status_code=200)
        self.assertContains(r, '1 day ago')
        self.assertEqual(self.reload(a).status, 'SUBMITTED')
        # Even straight through the workflow
        with org_context(self.org_a):
            from core.exceptions import InvalidTransition
            with self.assertRaises(InvalidTransition):
                apply_transition(self.reload(a), 'pay_less', self.carla, amount=Decimal('1'), basis='x')

    def test_pay_less_on_its_last_day_counts(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.pay_less_by = timezone.localdate()
            a.save(actor=self.pat)
        self.login(self.carla)
        self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '1000', 'basis': 'x'})
        self.assertEqual(self.reload(a).status, 'PAY_LESS')

    def test_no_payment_notice_stands_as_notified(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.payment_notice_by = timezone.localdate() - timedelta(days=1)
            a.save(actor=self.pat)
        self.login(self.pat)
        r = self.client.get(self.detail(a))
        self.assertContains(r, 'stands as the notified sum')
        self.assertContains(r, 'Advisory: check your contract.')
        self.assertTrue(self.reload(a).is_certified)

    def test_consultant_and_staff_cannot_give_client_notices(self):
        a = self.make(submit=True)
        self.login(self.connie)
        self.assertEqual(self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '1',
                                                           'basis': 'x'}).status_code, 403)
        self.login(self.pat)  # the PM is not an admin: they can't record it for the client either
        self.assertEqual(self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '1',
                                                           'basis': 'x'}).status_code, 403)
        self.assertEqual(self.reload(a).status, 'SUBMITTED')


class OnBehalfTests(PaymentsTestBase):
    def evidence(self):
        return SimpleUploadedFile('client-email.pdf', b'%PDF-1.4 notice', content_type='application/pdf')

    def test_needs_note_and_evidence(self):
        a = self.make(submit=True)
        self.login(self.adam)
        r = self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '90000', 'basis': 'x',
                                              'given_on': timezone.localdate().isoformat()})
        self.assertContains(r, "Attach the client&#x27;s email or letter")
        self.assertEqual(self.reload(a).status, 'SUBMITTED')

    def test_admin_records_payment_notice_for_the_client(self):
        a = self.make(submit=True)
        self.login(self.adam)
        r = self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '90000', 'basis': 'As emailed',
                                              'given_on': timezone.localdate().isoformat(),
                                              'note': 'Email from Carla, today', 'evidence': self.evidence()})
        self.assertEqual(r.status_code, 302)
        a = self.reload(a)
        self.assertEqual((a.status, a.payment_notice_on_behalf, a.payment_notice_given_by), ('NOTIFIED', True, self.adam))
        self.assertTrue(a.payment_notice_evidence.name.startswith(f'org_{self.org_a.pk}/payments/'))
        with org_context(self.org_a):
            entry = AuditEntry.objects.filter(action='payment_notice_on_behalf').get()
            self.assertIn('Recorded on behalf of client: Email from Carla', entry.note)
            self.assertTrue(Notification.objects.filter(user=self.carla, title__contains='recorded your payment notice')
                            .exists())
        # Whoever can see the application can open the evidence; the consultant can't
        ev = self.url('application_evidence', self.pa.pk, a.pk, 'payment-notice')
        self.assertEqual(self.status_for(self.carla, ev), 200)
        self.assertEqual(self.status_for(self.connie, ev), 403)

    def test_on_behalf_pay_less_after_the_deadline_is_refused(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.pay_less_by = timezone.localdate() - timedelta(days=2)
            a.submitted_at = timezone.now() - timedelta(days=5)
            a.save(actor=self.pat)
        self.login(self.alice)
        r = self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '1', 'basis': 'x', 'note': 'letter',
                                              'given_on': timezone.localdate().isoformat(), 'evidence': self.evidence()})
        self.assertContains(r, 'Too late')
        # ...but a notice the client gave in time, recorded later, counts
        r = self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '1000', 'basis': 'x', 'note': 'letter',
                                              'given_on': (timezone.localdate() - timedelta(days=2)).isoformat(),
                                              'evidence': self.evidence()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.reload(a).status, 'PAY_LESS')


class PaymentReceivedTests(PaymentsTestBase):
    def test_part_payments_then_paid(self):
        a = self.make(submit=True)   # 95,000 + VAT 19,000 = 114,000
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '100000', 'reference': 'BACS 1',
                                          'received_on': timezone.localdate().isoformat()})
        with org_context(self.org_a):
            a = self.reload(a)
            self.assertEqual((a.status, a.paid_total, a.outstanding), ('SUBMITTED', Decimal('100000.00'),
                                                                       Decimal('14000.00')))
        self.client.post(self.detail(a), {'action': 'payment', 'amount': '14000',
                                          'received_on': timezone.localdate().isoformat()})
        with org_context(self.org_a):
            a = self.reload(a)
            self.assertEqual((a.status, a.outstanding, a.paid_in_full_on), ('PAID', ZERO, timezone.localdate()))
            self.assertEqual(Payment.objects.filter(application=a).count(), 2)
            self.assertEqual(commercial_position(self.pa)['paid'], Decimal('114000.00'))

    def test_overdue_after_the_final_date(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            a.final_date = timezone.localdate() - timedelta(days=1)
            a.save(actor=self.pat)
        with org_context(self.org_a):
            self.assertTrue(self.reload(a).is_overdue)
        self.login(self.pat)
        r = self.client.get(self.url('application_list', self.pa.pk) + '?show=overdue')
        self.assertContains(r, 'Payment overdue')
        self.assertContains(r, a.reference)

    def test_client_cannot_record_payments(self):
        a = self.make(submit=True)
        self.login(self.carla)
        self.assertEqual(self.client.post(self.detail(a), {'action': 'payment', 'amount': '1',
                                                           'received_on': timezone.localdate().isoformat()}).status_code,
                         403)

    def test_certified_to_date(self):
        a = self.make(submit=True)
        with org_context(self.org_a):
            self.assertEqual(commercial_position(self.pa)['certified'], ZERO)
            apply_transition(a, 'payment_notice', self.carla, amount=Decimal('90000'), basis='x')
            self.assertEqual(commercial_position(self.pa)['certified'], Decimal('90000.00'))


class NeedsMeTests(PaymentsTestBase):
    def items(self, user):
        with org_context(self.org_a):
            return payment_actions.items(self.request_for(user))

    def test_client_items(self):
        a = self.make(submit=True)
        got = self.items(self.carla)
        self.assertEqual([(i.ref, i.action, i.due) for i in got], [(a.ref, 'Give your payment notice', a.payment_notice_by)])
        with org_context(self.org_a):
            apply_transition(a, 'payment_notice', self.carla, amount=Decimal('1000'), basis='x')
        a = self.reload(a)
        got = self.items(self.carla)
        self.assertEqual(got[0].due, a.pay_less_by)
        self.assertIn('pay less notice by', got[0].detail)
        self.assertEqual(self.items(self.connie), [])
        self.assertEqual(self.items(self.sid), [])

    def test_contractor_application_due_and_overdue(self):
        today = timezone.localdate()
        with org_context(self.org_a):
            self.contract.valuation_day = today.day if today.day <= 28 else 28
            self.contract.save()
        got = self.items(self.pat)
        self.assertEqual(len(got), 1)
        self.assertIn('Application for payment', got[0].title)
        self.assertEqual(self.items(self.stan), [])
        self.assertEqual(self.items(self.alice), [])   # the job has a PM: it's theirs
        a = self.make(valuation=got[0].due, submit=True)
        self.assertEqual(self.items(self.pat), [])
        with org_context(self.org_a):
            a.final_date = today - timedelta(days=1)
            a.save(actor=self.pat)
        got = self.items(self.pat)
        self.assertEqual([(i.ref, i.status_label, i.tone) for i in got], [(a.ref, 'Payment overdue', 'bad')])

    def test_registered_provider_reaches_the_needs_me_page(self):
        self.make(submit=True)
        self.login(self.carla)
        self.assertContains(self.client.get(self.url('needs_me')), 'Give your payment notice')


class PdfTests(PaymentsTestBase):
    def test_pdf_is_a_pdf_with_the_letterhead(self):
        a = self.make(submit=True)
        self.org_a.address = '1 Mill Lane\nLeeds'
        self.org_a.vat_number = 'GB123456789'
        self.org_a.save()
        self.login(self.carla)
        r = self.client.get(self.url('application_pdf', self.pa.pk, a.pk))
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_everything_typed_is_escaped(self):
        nasty = '<b>bold</b> & <img src="/etc/passwd"/> <font color=red>x'
        a = self.make(submit=True, notes=nasty)
        self.org_a.name = 'Alpha <i>Fitout</i> & Co'
        self.org_a.save()
        seen = []
        real = payment_pdf.Paragraph

        def spy(text, style, *args, **kwargs):
            seen.append(text)
            return real(text, style, *args, **kwargs)
        with org_context(self.org_a), mock.patch.object(payment_pdf, 'Paragraph', side_effect=spy):
            a = Application.objects.select_related('project', 'organization').get(pk=a.pk)
            data = payment_pdf.application_pdf(a, self.contract, {})
        self.assertTrue(data.startswith(b'%PDF'))
        joined = '\n'.join(seen)
        self.assertIn('&lt;b&gt;bold&lt;/b&gt; &amp; &lt;img src="/etc/passwd"/&gt;', joined)
        self.assertIn('Alpha &lt;i&gt;Fitout&lt;/i&gt; &amp; Co', joined)
        self.assertNotIn('<img', joined)
        self.assertNotIn('<b>', joined)

    def test_reverse_charge_shows_no_vat(self):
        with org_context(self.org_a):
            self.contract.vat_reverse_charge = True
            self.contract.save()
        a = self.make(submit=True)
        self.assertEqual((a.reverse_charge, a.vat, a.total), (True, ZERO, a.amount_due))
        self.login(self.pat)
        self.assertContains(self.client.get(self.detail(a)), 'Domestic reverse charge applies')


class TenancyTests(PaymentsTestBase):
    def test_other_company_gets_404(self):
        a = self.make(submit=True)
        self.login(self.bob)
        for url in (self.url('application_list', self.pa.pk), self.detail(a),
                    self.url('application_pdf', self.pa.pk, a.pk)):
            self.assertEqual(self.client.get(url).status_code, 404)
        # Bob's own workspace can't reach Alpha's application by id either
        self.assertEqual(self.client.get(self.url('application_detail', self.pb.pk, a.pk, org=self.org_b)).status_code,
                         404)

    def test_no_org_context_fails_closed(self):
        self.make()
        for model in (Application, Payment):
            with self.assertRaises(TenantContextMissing):
                model.objects.count()

    def test_items_stay_in_their_company(self):
        self.make(submit=True)
        with org_context(self.org_b):
            self.assertEqual(payment_actions.items(self.request_for(self.bob, org=self.org_b)), [])
