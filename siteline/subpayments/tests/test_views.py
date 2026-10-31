"""
Subcontractor payments through the views: every role, the sub applying in the portal and seeing
only their own, our notices and their deadlines, the late-notice warning, payments posting to the
cost ledger and reconciling with costs.report, reversals, CIS details and the CIS summary CSV,
PDF escaping, Needs me, and tenancy.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from changes.tests.base import CommercialTestBase, make_user
from costs import ledger
from costs import services as cost_services
from costs.access import settings_for as cost_settings_for
from costs.models import CostCode
from costs.report import cost_report
from directory.models import Package, Subcontractor
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership, Notification

from .. import actions as subpay_actions
from .. import rules, services
from ..models import SubApplication, SubcontractorTax, SubPayment, subcontract_for

D = Decimal


class SubPayTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.stan = make_user('stan@a.test', 'Stan Staff')  # staff on live jobs, not this job's PM or QS
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        self.otto = make_user('otto@o.test', 'Otto Other')  # another subcontractor on the same job
        Membership.objects.create(organization=self.org_a, user=self.otto, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            cost_settings_for(self.org_a)
            self.code = CostCode.objects.get(code='220')
            self.pkg = Package.objects.create(project=self.pa, subcontractor=self.subco, status=Package.APPOINTED,
                                              quote_value=D('50000'))
            self.order = cost_services.commit_package(self.pkg, self.code, self.pat)
            self.other = Subcontractor.objects.create(company_name='Otto Electrical')
            self.other_pkg = Package.objects.create(project=self.pa, subcontractor=self.other,
                                                    status=Package.APPOINTED, quote_value=D('30000'))
            ProjectMember.objects.create(project=self.pa, user=self.otto, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.other)
            SubcontractorTax.objects.create(subcontractor=self.subco, cis_status=rules.CIS_REGISTERED,
                                            verification_number='V1234567890')
            self.sc = subcontract_for(self.pkg, self.pat)
            self.other_sc = subcontract_for(self.other_pkg, self.pat)

    def allow(self, sc, day):
        """Put a valuation date on the subcontract's listed dates (tests use dates relative to today)."""
        with org_context(self.org_a):
            sc.refresh_from_db()
            sc.cycle_kind = rules.CYCLE_DATES
            sc.valuation_dates = sorted({*sc.valuation_dates, day.isoformat()})
            sc.save()

    def receive(self, gross='20000', materials='4000', valuation=None, sc=None, actor=None, received=None):
        valuation = valuation or timezone.localdate() - timedelta(days=2)
        self.allow(sc or self.sc, valuation)
        with org_context(self.org_a):
            return services.receive_application(sc or self.sc, actor or self.sid, valuation, D(gross), D(materials),
                                                by_sub=True, received_on=received)

    def reload(self, a):
        with org_context(self.org_a):
            return SubApplication.objects.get(pk=a.pk)

    def detail(self, a):
        return self.url('subpay_application', self.pa.pk, a.pk)

    def pay(self, a, gross, materials=None, key='k1'):
        self.login(self.pat)
        data = {'action': 'payment', 'gross': gross, 'paid_on': timezone.localdate().isoformat(), 'form_key': key}
        if materials is not None:
            data['materials'] = materials
        return self.client.post(self.detail(a), data)


class AccessTests(SubPayTestBase):
    def test_every_role_on_the_register_and_an_application(self):
        a = self.receive()
        for url in (self.url('subpay_list', self.pa.pk), self.detail(a), self.url('subpay_subcontract', self.pa.pk,
                                                                                  self.sc.pk)):
            expected = {'alice': 200, 'adam': 200, 'pat': 200, 'sid': 200, 'stan': 403, 'carla': 403,
                        'connie': 403, 'otto': 200 if url.endswith('sub-payments/') else 404, 'bea': None, 'eve': None, 'nora': None, 'bob': 404}
            for name, code in expected.items():
                with self.subTest(url=url, user=name):
                    status = self.status_for(getattr(self, name), url)
                    if code is None:
                        self.assertIn(status, (403, 404))
                    else:
                        self.assertEqual(status, code)
        self.assertEqual(self.status_for(None, self.detail(a)), 302)  # to the login page

    def test_otto_sees_the_register_but_not_sids_application(self):
        a = self.receive()
        self.assertEqual(self.status_for(self.otto, self.url('subpay_list', self.pa.pk)), 200)
        self.assertEqual(self.status_for(self.otto, self.detail(a)), 404)
        self.assertEqual(self.status_for(self.otto, self.url('subpay_subcontract', self.pa.pk, self.sc.pk)), 404)
        r = self.client.get(self.url('subpay_list', self.pa.pk))
        self.assertNotContains(r, a.reference)
        self.assertNotContains(r, 'Sid Joinery')

    def test_qs_on_the_team_is_staff(self):
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.stan, role=ProjectMember.ROLE_QS)
        self.assertEqual(self.status_for(self.stan, self.url('subpay_list', self.pa.pk)), 200)

    def test_sub_cannot_give_notices_or_pay(self):
        a = self.receive()
        self.login(self.sid)
        for data in ({'action': 'payment_notice', 'amount': '1', 'basis': 'x'},
                     {'action': 'payment', 'gross': '1', 'paid_on': timezone.localdate(), 'form_key': 'z'},
                     {'action': 'materials', 'amount': '0', 'reason': 'x'}):
            self.assertEqual(self.client.post(self.detail(a), data).status_code, 403)
        self.assertEqual(self.reload(a).status, SubApplication.RECEIVED)

    def test_company_pages_are_owners_and_admins_only(self):
        for name in ('subpay_settings', 'subpay_cis', 'subpay_tax_list'):
            for user, code in ((self.alice, 200), (self.adam, 200), (self.pat, 403), (self.sid, 403),
                               (self.bob, 404)):
                with self.subTest(page=name, user=user.email):
                    self.assertEqual(self.status_for(user, self.url(name)), code)
        self.assertEqual(self.status_for(self.pat, self.url('subpay_tax', self.subco.pk)), 403)
        self.assertEqual(self.status_for(self.adam, self.url('subpay_tax', self.subco.pk)), 200)

    def test_commercial_subnav_and_sub_decisions_tab(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('contract_detail', self.pa.pk)), 'Sub payments')
        self.login(self.sid)
        r = self.client.get(self.url('subpay_list', self.pa.pk))
        self.assertContains(r, 'Your payments')
        self.assertContains(r, 'Apply for payment')


class ApplyTests(SubPayTestBase):
    def test_sub_applies_through_the_portal(self):
        self.login(self.sid)
        valuation = timezone.localdate() - timedelta(days=1)
        self.allow(self.sc, valuation)
        data = {'valuation_date': valuation.isoformat(), 'gross_to_date': '20,000.00', 'materials': '4000',
                'notes': 'First fix complete', 'form_key': 'once'}
        r = self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk), data)
        self.assertEqual(r.status_code, 302)
        self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk), data)  # sent twice: one application
        data.pop('form_key')
        self.assertEqual(self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk), data).status_code, 400)
        with org_context(self.org_a):
            a = SubApplication.objects.get()
            self.assertEqual((a.ref, a.status, a.applied_by_sub, a.ball_in_court_user), ('SA-001', 'RECEIVED', True,
                                                                                         self.pat))
            # 20,000 less 5% retention
            self.assertEqual((a.gross_valuation, a.retention, a.amount_due, a.materials),
                             (D('20000.00'), D('1000.00'), D('19000.00'), D('4000.00')))
            self.assertEqual(a.due_date, a.payment_notice_by)
            self.assertTrue(Notification.objects.filter(user=self.pat, title__contains='SA-001').exists())

    def test_sub_cannot_apply_on_another_subs_package(self):
        self.login(self.sid)
        r = self.client.post(self.url('subpay_apply', self.pa.pk, self.other_pkg.pk),
                             {'valuation_date': timezone.localdate().isoformat(), 'gross_to_date': '100'})
        self.assertEqual(r.status_code, 404)

    def test_previous_amounts_and_approved_sub_variations(self):
        from delivery.models import SubVariation
        first = self.receive(gross='20000', valuation=timezone.localdate() - timedelta(days=40))
        with org_context(self.org_a):
            SubVariation.objects.create(project=self.pa, subcontractor=self.subco, package=self.pkg, title='Extra',
                                        description='x', value=D('3000'), status=SubVariation.APPROVED,
                                        approved_value=D('2500'), decided_at=timezone.now() - timedelta(days=5))
            SubVariation.objects.create(project=self.pa, subcontractor=self.subco, package=self.pkg, title='Later',
                                        description='x', value=D('900'), status=SubVariation.APPROVED,
                                        approved_value=D('900'), decided_at=timezone.now() + timedelta(days=5))
        second = self.receive(gross='30000', materials='0')
        self.assertEqual(second.variations_value, D('2500.00'))
        self.assertEqual(second.gross_valuation, D('32500.00'))
        self.assertEqual(second.previous_certified, first.amount_due)
        self.assertEqual(second.amount_due, D('32500.00') - D('1625.00') - D('19000.00'))
        with self.assertRaises(services.SubPayError):  # earlier than the last one
            self.receive(valuation=timezone.localdate() - timedelta(days=60))

    def test_withdraw_by_the_sub_before_our_notice(self):
        a = self.receive()
        self.login(self.sid)
        self.client.post(self.detail(a), {'action': 'withdraw'})
        self.assertEqual(self.reload(a).status, SubApplication.WITHDRAWN)


class NoticeTests(SubPayTestBase):
    def test_payment_notice_then_pay_less(self):
        a = self.receive()
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '17500', 'basis': 'Doors short'})
        self.assertEqual(r.status_code, 302)
        a = self.reload(a)
        self.assertEqual((a.status, a.notified_sum, a.basis), ('NOTIFIED', D('17500.00'), 'payment_notice'))
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.sid, title__contains='payment notice').exists())
        self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '16000', 'basis': 'Snagging'})
        a = self.reload(a)
        self.assertEqual((a.status, a.notified_sum), ('PAY_LESS', D('16000.00')))

    def test_late_notice_red_warning_for_staff_only(self):
        a = self.receive(valuation=timezone.localdate() - timedelta(days=20),
                         received=timezone.localdate() - timedelta(days=19))
        self.assertTrue(self.reload(a).stands_as_notified)
        self.login(self.pat)
        r = self.client.get(self.detail(a))
        self.assertContains(r, 'Our payment notice is late')
        self.assertContains(r, 'bad-banner')
        self.assertContains(self.client.get(self.url('subpay_list', self.pa.pk)), 'stands as the notified sum')
        self.login(self.sid)
        self.assertNotContains(self.client.get(self.detail(a)), 'Our payment notice is late')
        # A late payment notice is recorded but the application still stands
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '100', 'basis': 'late'})
        a = self.reload(a)
        self.assertEqual((a.status, a.notified_sum, a.basis), ('NOTIFIED', a.amount_due, 'default'))

    def test_pay_less_one_day_late_is_refused(self):
        a = self.receive()
        with org_context(self.org_a):
            SubApplication.objects.filter(pk=a.pk).update(pay_less_by=timezone.localdate() - timedelta(days=1))
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'pay_less', 'amount': '1', 'basis': 'x'})
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(self.reload(a).status, SubApplication.RECEIVED)


class PaymentTests(SubPayTestBase):
    def test_payment_posts_gross_before_cis_to_the_ledger_and_reconciles(self):
        a = self.receive(gross='20000', materials='4000')  # due 19,000
        r = self.pay(a, '19000')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = SubPayment.objects.get()
            # 19,000 gross; 4,000 materials; 15,000 labour; CIS 20% = 3,000; VAT 3,800; net 19,800
            self.assertEqual((p.gross, p.materials, p.labour, p.cis_deduction, p.vat, p.net),
                             (D('19000.00'), D('4000.00'), D('15000.00'), D('3000.00'), D('3800.00'),
                              D('19800.00')))
            self.assertEqual((p.ledger_entry.kind, p.ledger_entry.amount, p.ledger_entry.cost_code_id),
                             (ledger.ACTUAL, D('19000.00'), self.code.pk))
            self.assertEqual(ledger.totals(self.pa)[self.code.pk][ledger.ACTUAL], D('19000.00'))
            row = next(r for r in cost_report(self.pa)['rows'] if r.code.pk == self.code.pk)
            self.assertEqual(row.actual, D('19000.00'))
            self.assertEqual(SubApplication.objects.get(pk=a.pk).status, SubApplication.PAID)
            self.assertTrue(Notification.objects.filter(user=self.sid, title__contains='Payment advice').exists())

    def test_same_form_twice_records_once_and_no_key_is_refused(self):
        a = self.receive()
        self.pay(a, '1000', key='same')
        self.pay(a, '1000', key='same')
        with org_context(self.org_a):
            self.assertEqual(SubPayment.objects.count(), 1)
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'payment', 'gross': '10', 'paid_on': timezone.localdate()})
        self.assertEqual(r.status_code, 400)

    def test_no_overpayment(self):
        a = self.receive()
        self.pay(a, '19000.01')
        with org_context(self.org_a):
            self.assertFalse(SubPayment.objects.exists())

    def test_reversal_reverses_the_ledger_and_reopens(self):
        a = self.receive()
        self.pay(a, '19000')
        with org_context(self.org_a):
            p = SubPayment.objects.get()
        self.login(self.pat)  # the PM can't reverse
        self.assertEqual(self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk,
                                                           'reason': 'x'}).status_code, 403)
        self.login(self.adam)
        self.client.post(self.detail(a), {'action': 'reverse_payment', 'payment': p.pk, 'reason': 'Wrong job'})
        with org_context(self.org_a):
            p.refresh_from_db()
            self.assertTrue(p.is_reversed)
            self.assertEqual(p.reversal_entry.amount, D('-19000.00'))
            self.assertEqual(ledger.totals(self.pa)[self.code.pk][ledger.ACTUAL], D('0.00'))
            row = next(r for r in cost_report(self.pa)['rows'] if r.code.pk == self.code.pk)
            self.assertEqual(row.actual, D('0.00'))
        self.assertEqual(self.reload(a).status, SubApplication.RECEIVED)

    def test_part_payments_split_materials_and_unverified_rate(self):
        with org_context(self.org_a):
            SubcontractorTax.objects.filter(subcontractor=self.subco).update(cis_status=rules.CIS_UNVERIFIED,
                                                                            reverse_charge=True)
        a = self.receive(gross='20000', materials='4000')
        self.pay(a, '10000', materials='3000', key='a')
        self.pay(a, '9000', key='b')
        with org_context(self.org_a):
            p1, p2 = SubPayment.objects.order_by('pk')
            self.assertEqual((p1.cis_deduction, p1.vat, p1.net), (D('2100.00'), D('0.00'), D('7900.00')))
            self.assertEqual((p2.materials, p2.cis_deduction), (D('1000.00'), D('2400.00')))
            self.assertEqual(SubApplication.objects.get(pk=a.pk).status, SubApplication.PAID)

    def test_payment_needs_an_order(self):
        a = self.receive('1000', '0', valuation=timezone.localdate(), sc=self.other_sc, actor=self.otto)
        self.pay(a, '950')
        with org_context(self.org_a):
            self.assertFalse(SubPayment.objects.exists())

    def test_retention_release_goes_through_the_same_flow(self):
        from contracts.models import MainContract
        a = self.receive(gross='20000', valuation=timezone.localdate() - timedelta(days=30))
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, practical_completion_on=timezone.localdate() - timedelta(days=3),
                                        defects_period_months=12)
            rel = services.release_retention(self.sc, self.pat, timezone.localdate() - timedelta(days=3))
            self.assertEqual((rel.kind, rel.retention, rel.amount_due), ('RELEASE', D('500.00'), D('500.00')))
            self.assertEqual(rel.previous_certified, a.amount_due)
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('subpay_subcontract', self.pa.pk, self.sc.pk)),
                            'Released at practical completion')


class AdviceAndCSVTests(SubPayTestBase):
    def test_advice_pdf_escapes_everything_typed(self):
        from payments import pdf as payment_pdf

        from .. import pdf as subpay_pdf
        nasty = '<b>bold</b> & <img src="/etc/passwd"/>'
        with org_context(self.org_a):
            Subcontractor.objects.filter(pk=self.subco.pk).update(company_name=nasty)
        a = self.receive()
        self.pay(a, '1000', key='x')
        with org_context(self.org_a):
            SubPayment.objects.update(reference='<font color=red>BACS</font>')
            p = SubPayment.objects.get()
        seen = []
        real = payment_pdf.Paragraph

        def spy(text, style):
            seen.append(text)
            return real(text, style)
        with org_context(self.org_a), mock.patch.object(payment_pdf, 'Paragraph', side_effect=spy):
            data = subpay_pdf.advice_pdf(SubPayment.objects.get(pk=p.pk))
        self.assertTrue(data.startswith(b'%PDF'))
        joined = '\n'.join(seen)
        self.assertIn('&lt;b&gt;bold&lt;/b&gt; &amp; &lt;img src="/etc/passwd"/&gt;', joined)
        self.assertIn('&lt;font color=red&gt;BACS&lt;/font&gt;', joined)
        self.assertNotIn('<img', joined)

    def test_advice_for_the_sub_only(self):
        a = self.receive()
        self.pay(a, '1000')
        with org_context(self.org_a):
            p = SubPayment.objects.get()
        url = self.url('subpay_advice', self.pa.pk, p.pk)
        self.assertEqual(self.status_for(self.sid, url), 200)
        self.assertEqual(self.status_for(self.otto, url), 404)
        self.assertEqual(self.status_for(self.carla, url), 403)

    def test_cis_summary_csv_is_formula_safe_and_by_tax_month(self):
        with org_context(self.org_a):
            SubcontractorTax.objects.filter(subcontractor=self.subco).update(utr='=HYPERLINK("x")')
            Subcontractor.objects.filter(pk=self.subco.pk).update(company_name='@SUM(A1)')
        a = self.receive()
        self.pay(a, '19000')
        month = rules.tax_month(timezone.localdate())
        self.login(self.alice)
        r = self.client.get(self.url('subpay_cis') + f'?month={month.key}&export=csv', follow=True)
        body = r.content.decode()
        self.assertTrue(r['Content-Type'].startswith('text/csv'))
        self.assertIn("'@SUM(A1)", body)
        self.assertIn("'=HYPERLINK", body)
        self.assertIn('not a submission', body)
        self.assertIn('19000.00', body)
        self.assertIn('3000.00', body)
        page = self.client.get(self.url('subpay_cis') + f'?month={month.key}')
        self.assertContains(page, 'Not a submission')
        self.assertContains(page, '3,000.00')
        other = rules.tax_month(month.start - timedelta(days=40))
        self.assertNotContains(self.client.get(self.url('subpay_cis') + f'?month={other.key}'), '@SUM(A1)')

    def test_cis_summary_leaves_out_reversed_payments(self):
        from ..views import cis_summary
        a = self.receive()
        self.pay(a, '19000')
        with org_context(self.org_a):
            services.reverse_payment(SubPayment.objects.get(), self.adam, 'mistake')
            rows, totals = cis_summary(rules.tax_month(timezone.localdate()))
        self.assertEqual((rows, totals['cis']), ([], D('0.00')))

    def test_tax_details_need_a_verification_number_for_gross(self):
        self.login(self.alice)
        r = self.client.post(self.url('subpay_tax', self.other.pk), {'cis_status': rules.CIS_GROSS, 'vat_rate': '20'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(self.url('subpay_tax', self.other.pk), {'cis_status': rules.CIS_GROSS, 'vat_rate': '20',
                                                                     'verification_number': 'V9'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(SubcontractorTax.objects.get(subcontractor=self.other).cis_status, rules.CIS_GROSS)


class NeedsMeAndTenancyTests(SubPayTestBase):
    def items(self, user):
        with org_context(self.org_a):
            return subpay_actions.items(self.request_for(user))

    def test_staff_items(self):
        a = self.receive()
        items = self.items(self.pat)
        self.assertEqual([(i.ref, i.status_label, i.due) for i in items],
                         [(a.reference, 'Payment notice due', a.payment_notice_by)])
        late = self.receive(valuation=timezone.localdate() - timedelta(days=20), sc=self.other_sc, actor=self.otto,
                            received=timezone.localdate() - timedelta(days=19))
        labels = {i.ref: i.status_label for i in self.items(self.pat)}
        self.assertEqual(labels[late.reference], 'No payment notice given: stands as notified')
        self.assertEqual(self.items(self.stan), [])
        self.assertEqual(self.items(self.carla), [])

    def test_pay_by_final_date_item(self):
        a = self.receive()
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '19000', 'basis': 'ok'})
        with org_context(self.org_a):
            SubApplication.objects.filter(pk=a.pk).update(pay_less_by=timezone.localdate() - timedelta(days=1))
        items = self.items(self.pat)
        self.assertEqual([i.status_label for i in items], ['To pay'])

    def test_sub_apply_prompt(self):
        with org_context(self.org_a):
            self.sc.valuation_day = (timezone.localdate() + timedelta(days=2)).day
            self.sc.save()
        items = self.items(self.sid)
        self.assertTrue(items and items[0].action == 'Apply for payment')

    def test_another_company_gets_404_and_no_org_fails_closed(self):
        a = self.receive()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.detail(a)).status_code, 404)
        self.assertEqual(self.client.get(self.url('subpay_list', self.pa.pk, org=self.org_b)).status_code, 404)
        with self.assertRaises(TenantContextMissing):
            list(SubApplication.objects.all())
        with self.assertRaises(TenantContextMissing):
            list(SubPayment.objects.all())
        with org_context(self.org_b):
            self.assertFalse(SubApplication.objects.exists())
            self.assertFalse(SubcontractorTax.objects.exists())


class ReviewFixTests(SubPayTestBase):
    def apply(self, user, day, gross='20000', **extra):
        self.login(user)
        return self.client.post(self.url('subpay_apply', self.pa.pk, self.pkg.pk),
                                {'valuation_date': day.isoformat(), 'gross_to_date': gross, 'form_key': str(day),
                                 **extra})

    def test_valuation_date_must_be_this_cycle_or_the_one_before(self):
        today = timezone.localdate()
        with org_context(self.org_a):
            self.sc.refresh_from_db()
            allowed = self.sc.allowed_dates(today)
        self.assertEqual(len(allowed), 2)
        self.assertEqual(self.apply(self.sid, allowed[0] - timedelta(days=31)).status_code, 200)  # two cycles back
        self.assertEqual(self.apply(self.sid, today + timedelta(days=1)).status_code, 200)        # the future
        self.assertEqual(self.apply(self.sid, allowed[0]).status_code, 302)                       # one cycle back
        with org_context(self.org_a):
            a = SubApplication.objects.get()
        # Received today, so our deadlines run from today, not from the old valuation date
        self.assertGreaterEqual(a.payment_notice_by, today)
        self.assertFalse(a.stands_as_notified)

    def test_gross_above_three_times_the_order_needs_a_staff_reason(self):
        day = timezone.localdate() - timedelta(days=1)
        self.allow(self.sc, day)
        self.assertEqual(self.apply(self.sid, day, gross='150000.01').status_code, 200)
        self.assertEqual(self.apply(self.pat, day, gross='150000.01').status_code, 200)
        r = self.apply(self.pat, day, gross='150000.01', received_on=day.isoformat(),
                       override_reason='Order to be increased: client instruction CI-12')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(SubApplication.objects.get().gross_to_date, D('150000.01'))

    def test_payment_notice_lowering_the_sum_caps_materials(self):
        a = self.receive('20000', '15000')
        self.login(self.pat)
        self.client.post(self.detail(a), {'action': 'payment_notice', 'amount': '10000', 'basis': 'Valued lower'})
        self.assertEqual(self.reload(a).materials, D('10000.00'))

    def test_part_payments_vat_adds_up(self):
        with org_context(self.org_a):
            SubcontractorTax.objects.filter(subcontractor=self.subco).update(vat_rate=D('17.5'))
        a = self.receive('20000', '0')  # 19,000 due
        for i, part in enumerate(('0.03', '0.03', '18999.94')):
            self.pay(a, part, key=f'p{i}')
        with org_context(self.org_a):
            vats = [p.vat for p in SubPayment.objects.order_by('pk')]
        self.assertEqual(sum(vats), rules.vat_on(D('19000'), D('17.5')))

    def test_payment_date_rules_and_earlier_tax_month_warning(self):
        a = self.receive(received=timezone.localdate() - timedelta(days=1),
                         valuation=timezone.localdate() - timedelta(days=2))
        self.login(self.pat)
        r = self.client.post(self.detail(a), {'action': 'payment', 'gross': '100', 'form_key': 'e',
                                              'paid_on': (timezone.localdate() - timedelta(days=3)).isoformat()})
        self.assertEqual(r.status_code, 200)  # before it was received
        with org_context(self.org_a):
            self.assertFalse(SubPayment.objects.exists())
        self.assertTrue(services.earlier_tax_month(timezone.localdate() - timedelta(days=40)))
        self.assertFalse(services.earlier_tax_month(timezone.localdate()))

    def test_reversal_in_a_later_tax_month_is_an_adjustment(self):
        from ..views import cis_summary
        a = self.receive()
        self.pay(a, '19000')
        with org_context(self.org_a):
            p = SubPayment.objects.get()
            earlier = timezone.localdate() - timedelta(days=45)
            SubPayment.objects.filter(pk=p.pk).update(paid_on=earlier)
            services.reverse_payment(p, self.adam, 'Paid to the wrong sub')
            then, _ = cis_summary(rules.tax_month(earlier))
            now, totals = cis_summary(rules.tax_month(timezone.localdate()))
        self.assertEqual([(r['gross'], r['cis'], r['adjustment']) for r in then],
                         [(D('19000.00'), D('3000.00'), False)])
        self.assertEqual([(r['gross'], r['cis'], r['adjustment']) for r in now],
                         [(D('-19000.00'), D('-3000.00'), True)])
        self.login(self.alice)
        csv_text = self.client.get(self.url('subpay_cis') + '?export=csv').content.decode()
        self.assertIn('earlier return may need amending', csv_text)

    def test_order_invoiced_to_date_includes_sub_payments_and_form_hides_the_order(self):
        a = self.receive()
        self.pay(a, '19000')
        self.login(self.pat)
        r = self.client.get(self.url('cost_commitment_detail', self.pa.pk, self.order.pk))
        self.assertEqual(r.context['invoiced'], D('19000.00'))
        r = self.client.get(self.url('cost_actual_create', self.pa.pk))
        self.assertNotIn(self.order, list(r.context['form'].fields['commitment'].queryset))

    def test_hand_entered_actual_blocks_sub_payment(self):
        from costs.models import ActualCost
        with org_context(self.org_a):
            ActualCost.objects.create(project=self.pa, cost_code=self.code, supplier='Sid', amount=D('100'),
                                      commitment=self.order)
            from costs import ledger as cl
            act = ActualCost.objects.get()
            cl.post(self.pa, self.code, cl.ACTUAL, D('100'), source=act)
        a = self.receive()
        self.pay(a, '1000')
        with org_context(self.org_a):
            self.assertFalse(SubPayment.objects.exists())

    def test_verification_number_on_the_advice(self):
        from .. import pdf as subpay_pdf
        a = self.receive()
        self.pay(a, '1000')
        with org_context(self.org_a):
            p = SubPayment.objects.get()
        seen = []
        real = subpay_pdf._p

        def spy(text, style):
            seen.append(str(text))
            return real(text, style)
        from unittest import mock as m
        for show in (True, False):
            seen.clear()
            with org_context(self.org_a), m.patch.object(subpay_pdf, '_p', side_effect=spy):
                subpay_pdf.advice_pdf(SubPayment.objects.get(pk=p.pk), show_verification=show)
            self.assertEqual(any('V1234567890' in t for t in seen), show)
        url = self.url('subpay_advice', self.pa.pk, p.pk)
        for user in (self.sid, self.adam, self.pat):
            self.login(user)
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_no_writes_on_get_and_utr(self):
        from ..models import Subcontract, SubPaymentSettings
        with org_context(self.org_a):
            Subcontract.objects.filter(pk=self.other_sc.pk).delete()
            SubPaymentSettings.objects.all().delete()
        self.login(self.otto)
        self.assertEqual(self.client.get(self.url('subpay_apply', self.pa.pk, self.other_pkg.pk)).status_code, 200)
        self.login(self.alice)
        self.client.get(self.url('subpay_settings'))
        self.client.get(self.url('subpay_open', self.pa.pk, self.other_pkg.pk))
        with org_context(self.org_a):
            self.assertFalse(Subcontract.objects.filter(package=self.other_pkg).exists())
            self.assertFalse(SubPaymentSettings.objects.exists())
        r = self.client.post(self.url('subpay_tax', self.other.pk), {'cis_status': rules.CIS_UNVERIFIED,
                                                                     'vat_rate': '20', 'utr': '12345'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(self.url('subpay_tax', self.other.pk), {'cis_status': rules.CIS_UNVERIFIED,
                                                                     'vat_rate': '20', 'utr': '12345 67890'})
        self.assertEqual(r.status_code, 302)
