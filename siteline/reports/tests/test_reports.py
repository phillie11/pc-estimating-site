"""
Company reports: who can open them, that every money figure equals what the job's own page shows
(cvr.figures, costs.report, cvr.cashflow), what counts as overdue, the safety counts, subcontractor
performance, CSVs and empty states. Set-up is api/tests/base.py: a job with money and one of every record.
"""
import csv
import io
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.views import create_organization
from api.tests.base import ApiTestBase
from changes.models import ChangeEvent
from changes.tests.base import make_user
from contracts.models import MainContract
from costs.report import cost_report
from cvr import cashflow, figures
from procurement.models import Submittal, SubmittalRevision
from projects.models import Issue, Project, ProjectMember
from quality import rules as q_rules
from quality.models import Permit
from reports import data
from safety import rules as s_rules
from safety.models import Incident
from subpayments.models import SubApplication
from tenancy.context import org_context

PAGES = ('reports_home', 'report_margin', 'report_cash', 'report_overdue', 'report_safety', 'report_subcontractors')


def rows_of(resp):
    return list(csv.reader(io.StringIO(resp.content.decode())))


class AccessTests(ApiTestBase):
    def test_owners_and_admins_only(self):
        expected = {'alice': 200, 'adam': 200, 'pat': 403, 'eve': 403, 'stan': 403, 'carla': 403, 'connie': 403,
                    'sid': 403, 'bea': 403, 'nora': 403, 'bob': 404}
        for name in PAGES:
            for who, code in expected.items():
                with self.subTest(page=name, who=who):
                    self.assertEqual(self.status_for(getattr(self, who), self.url(name)), code)

    def test_another_companys_jobs_never_show(self):
        self.login(self.bob)
        for name in PAGES:
            with self.subTest(name):
                resp = self.client.get(self.url(name, org=self.org_b))
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, 'Alpha job')
                self.assertNotContains(resp, 'Sid Joinery')

    def test_no_organisation_context_fails_closed(self):
        from tenancy.context import TenantContextMissing
        today, now = timezone.localdate(), timezone.now()
        for fn in (lambda: data.portfolio_margin(figures.rules.month_start(today)), lambda: data.cash_position(today),
                   lambda: data.overdue(today, now), lambda: data.safety(today), lambda: data.subcontractors(today)):
            with self.assertRaises(TenantContextMissing):
                fn()

    def test_nav_link_for_owners_and_admins_only(self):
        link = f'href="{self.url("reports_home")}"'
        self.login(self.alice)
        self.assertContains(self.client.get(self.url('dashboard'), follow=True), link)
        self.login(self.pat)
        self.assertNotContains(self.client.get(self.url('dashboard'), follow=True), link)


class MarginTests(ApiTestBase):
    def test_margin_equals_the_cvr_and_the_cost_report(self):
        with org_context(self.org_a):
            rep = data.portfolio_margin(figures.rules.month_start(self.today))
            cost = cost_report(self.pa)
            live = figures.live_figures([self.pa], self.today)[self.pa.pk]
        row = next(r for r in rep['rows'] if r['project'].pk == self.pa.pk)
        self.assertEqual(row['f'].forecast_margin, cost['margin'])
        self.assertEqual(row['f'].final_cost, cost['final_cost'])
        self.assertEqual(row['f'].final_account, cost['final_account'])
        self.assertEqual(row['f'].value, live.value)
        last = figures.live_figures  # movement: this month's forecast margin less last month's
        with org_context(self.org_a):
            prev = last([self.pa], self.m1)[self.pa.pk]
        self.assertEqual(row['move'], cost['margin'] - prev.forecast_margin)
        self.assertEqual(rep['total']['forecast_margin'],
                         sum(r['f'].forecast_margin for r in rep['rows'] if r['has_figures']))

    def test_jobs_with_no_figures_are_not_in_the_totals(self):
        with org_context(self.org_a):
            Project.objects.create(name='Just won', stage=Project.STAGE_WON, value=D('500000'))
            rep = data.portfolio_margin(figures.rules.month_start(self.today))
        new = next(r for r in rep['rows'] if r['project'].name == 'Just won')
        self.assertFalse(new['has_figures'])
        self.assertEqual(rep['counted'], 1)
        self.assertEqual(rep['total']['final_account'],
                         next(r for r in rep['rows'] if r['project'].pk == self.pa.pk)['f'].final_account)

    def test_page_and_csv(self):
        self.login(self.alice)
        with org_context(self.org_a):
            margin = cost_report(self.pa)['margin']
        resp = self.client.get(self.url('report_margin'))
        self.assertContains(resp, 'Alpha job')
        self.assertContains(resp, 'role="img"')
        csv_resp = self.client.get(self.url('report_margin'), {'export': 'csv'})
        self.assertEqual(csv_resp['Content-Type'], 'text/csv')
        body = rows_of(csv_resp)
        self.assertEqual(body[0][0], 'Job ref')
        line = next(r for r in body if r[1] == 'Alpha job')
        self.assertEqual(D(line[9]), margin)

    def test_only_live_jobs(self):
        with org_context(self.org_a):
            done = Project.objects.create(name='Finished job', stage=Project.STAGE_COMPLETE, value=D('1000'))
            bid = Project.objects.create(name='A bid', stage=Project.STAGE_TENDERING)
            rep = data.portfolio_margin(figures.rules.month_start(self.today))
        names = {r['project'].name for r in rep['rows']}
        self.assertIn('Alpha job', names)
        self.assertNotIn(done.name, names)
        self.assertNotIn(bid.name, names)


class CashTests(ApiTestBase):
    def test_cells_are_the_jobs_own_cashflow(self):
        with org_context(self.org_a):
            rep = data.cash_position(self.today)
            built = {m.month: m for m in cashflow.build(self.pa, self.today)['months']}
        row = next(r for r in rep['rows'] if r['project'].pk == self.pa.pk)
        self.assertEqual(len(row['cells']), 3)
        self.assertEqual(row['cells'][0]['month'], figures.rules.month_start(self.today))
        for c in row['cells']:
            m = built.get(c['month'])
            self.assertEqual(c['in'], m.money_in if m else D('0'))
            self.assertEqual(c['out'], m.money_out if m else D('0'))
            self.assertEqual(c['net'], c['in'] - c['out'])
        for i, t in enumerate(rep['totals']):
            self.assertEqual(t['net'], sum(r['cells'][i]['net'] for r in rep['rows']))
        self.assertEqual(rep['grand']['net'], sum(r['net'] for r in rep['rows']))

    def test_page_and_csv(self):
        self.login(self.adam)
        self.assertContains(self.client.get(self.url('report_cash')), self.url('cvr_cashflow', self.pa.pk))
        body = rows_of(self.client.get(self.url('report_cash'), {'export': 'csv'}))
        self.assertEqual(body[-1][1], 'Total')
        self.assertEqual(len(body[0]), 2 + 9 + 1)


class OverdueTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        with org_context(self.org_a):
            SubApplication.objects.filter(pk=self.a['sub_app'].pk).update(
                payment_notice_by=self.today - timedelta(days=1), payment_notice_on=None)
            self.permit = Permit(project=self.pa, title='Hot works at the bar', permit_type=q_rules.HOT_WORKS,
                                 location='Bar', starts_at=now - timedelta(hours=10), expires_at=now - timedelta(hours=1))
            self.permit.save(actor=self.pat)
            Permit.objects.filter(pk=self.permit.pk).update(status=q_rules.APPROVED)
            self.fresh = Permit(project=self.pa, title='Still running', permit_type=q_rules.HOT_WORKS, location='Bar',
                                starts_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=3))
            self.fresh.save(actor=self.pat)
            Permit.objects.filter(pk=self.fresh.pk).update(status=q_rules.APPROVED)
            contract = MainContract.objects.get(project=self.pa)
            contract.form = 'NEC4_ECC'
            contract.save()
            self.late_ce = ChangeEvent(project=self.pa, title='Late notification', description='x',
                                       aware_on=self.today - timedelta(weeks=9))
            self.late_ce.save(actor=self.pat)

    def sections(self):
        with org_context(self.org_a):
            return {s.key: s for s in data.overdue(self.today, timezone.now())}

    def test_what_counts(self):
        s = self.sections()
        self.assertEqual([i.ref for i in s['rfis'].items], [self.a['rfi'].reference])
        self.assertEqual(s['rfis'].items[0].days_late, 3)
        self.assertEqual([i.ref for i in s['defects'].items], [f'IS-{self.a["defect"].number:03d}'])
        self.assertEqual([i.ref for i in s['permits'].items], [self.permit.reference])
        self.assertEqual([i.ref for i in s['notices'].items], [self.a['sub_app'].reference])
        clocks = s['clocks'].items
        self.assertEqual([i.ref for i in clocks], [self.late_ce.reference])
        self.assertEqual(clocks[0].due, self.today - timedelta(weeks=9) + timedelta(weeks=8))
        self.assertTrue(s['clocks'].advisory)

    def test_done_things_drop_off(self):
        with org_context(self.org_a):
            ChangeEvent.objects.filter(pk=self.late_ce.pk).update(notified_on=self.today)
            Issue.objects.filter(pk=self.a['defect'].pk).update(status=Issue.STATUS_CLOSED)
            Permit.objects.filter(pk=self.permit.pk).update(status=q_rules.CLOSED)
            from rfis.models import RFI
            RFI.objects.filter(pk=self.a['rfi'].pk).update(status=RFI.STATUS_RESPONDED)
        s = self.sections()
        for key in ('rfis', 'defects', 'permits', 'clocks'):
            self.assertEqual(s[key].count, 0, key)

    def test_archived_jobs_are_left_out(self):
        self.archive(self.pa, self.org_a)
        self.assertEqual(sum(s.count for s in self.sections().values()), 0)

    def test_page_and_csv(self):
        self.login(self.alice)
        resp = self.client.get(self.url('report_overdue'))
        self.assertContains(resp, self.permit.get_absolute_url())
        self.assertContains(resp, self.late_ce.get_absolute_url())
        body = rows_of(self.client.get(self.url('report_overdue'), {'export': 'csv'}))
        self.assertEqual(len(body), 1 + 5)


class SafetyTests(ApiTestBase):
    def test_counts_by_month_kind_and_riddor(self):
        with org_context(self.org_a):
            for kind, riddor in ((Incident.KIND_INJURY, s_rules.RIDDOR_OVER_7), (Incident.KIND_INJURY, s_rules.RIDDOR_OVER_3),
                                 ('DAMAGE', '')):
                inc = Incident(project=self.pa, title='x', description='x', location='x', kind=kind,
                               riddor_category=riddor)
                inc.save(actor=self.pat)
            old = Incident(project=self.pa, title='old', description='x', location='x',
                           occurred_at=timezone.now() - timedelta(days=500))
            old.save(actor=self.pat)
            rep = data.safety(self.today)
        this = rep['rows'][-1]
        kinds = [k for k, _ in rep['kinds']]
        self.assertEqual(this['total'], 4)  # base.py's near miss and the three above; not the old one
        self.assertEqual(this['by_kind'][kinds.index(Incident.KIND_INJURY)], 2)
        self.assertEqual(this['by_kind'][kinds.index(Incident.KIND_NEAR_MISS)], 1)
        self.assertEqual(this['riddor'], 1)  # over 3 days is recorded, not reported
        self.assertEqual(rep['total'], 4)
        self.assertEqual(len(rep['rows']), 12)

    def test_page_and_csv(self):
        self.login(self.alice)
        self.assertContains(self.client.get(self.url('report_safety')), 'RIDDOR reportable')
        body = rows_of(self.client.get(self.url('report_safety'), {'export': 'csv'}))
        self.assertEqual(len(body), 13)


class SubcontractorTests(ApiTestBase):
    def test_on_time_snags_and_days_to_close(self):
        pkg = self.money['pkg']
        with org_context(self.org_a):
            def submittal(title, due, made_on):
                s = Submittal(project=self.pa, title=title, package=pkg, due_date=due)
                s.save(actor=self.pat)
                if made_on:
                    SubmittalRevision.objects.create(submittal=s, number=0, submitted_by=self.sid,
                                                     submitted_at=timezone.make_aware(
                                                         timezone.datetime.combine(made_on, timezone.datetime.min.time()))
                                                     + timedelta(hours=12))
                return s
            submittal('On time', self.today - timedelta(days=5), self.today - timedelta(days=6))
            submittal('Late', self.today - timedelta(days=5), self.today - timedelta(days=2))
            submittal('Missed', self.today - timedelta(days=1), None)
            submittal('Not due', self.today + timedelta(days=10), None)
            now = timezone.now()
            a = Issue.objects.create(project=self.pa, title='Gap', issue_type='SNAG', assignee=self.sid)
            b = Issue.objects.create(project=self.pa, title='Chip', issue_type='SNAG', assignee=self.sid)
            Issue.objects.filter(pk=a.pk).update(status=Issue.STATUS_CLOSED, created_at=now - timedelta(days=4),
                                                 closed_at=now)
            Issue.objects.filter(pk=b.pk).update(status=Issue.STATUS_CLOSED, created_at=now - timedelta(days=1),
                                                 closed_at=now)
            Issue.objects.create(project=self.pa, title='Not theirs', issue_type='SNAG', assignee=self.pat)
            rows = data.subcontractors(self.today)
        row = next(r for r in rows if r['name'] == 'Sid Joinery')
        self.assertEqual(row['packages'], 1)
        self.assertEqual((row['submittals_due'], row['on_time']), (3, 1))
        self.assertEqual(row['on_time_pct'], D('33.3'))
        self.assertEqual((row['snags'], row['closed'], row['per_package']), (2, 2, D('2.0')))
        self.assertEqual(row['avg_days'], D('2.5'))

    def test_bidders_and_other_jobs_dont_count(self):
        with org_context(self.org_a):
            other = Project.objects.create(name='Other job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=other, user=self.sid, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.subco)
            Issue.objects.create(project=other, title='Elsewhere', issue_type='SNAG', assignee=self.sid)
            rows = data.subcontractors(self.today)
        row = next(r for r in rows if r['name'] == 'Sid Joinery')
        self.assertEqual(row['snags'], 0)  # not appointed on the other job

    def test_page_search_and_csv(self):
        self.login(self.alice)
        resp = self.client.get(self.url('report_subcontractors'))
        self.assertContains(resp, 'Sid Joinery')
        self.assertContains(resp, self.url('sub_detail', self.subco.pk))
        self.assertNotContains(self.client.get(self.url('report_subcontractors'), {'q': 'zzz'}), 'Sid Joinery')
        body = rows_of(self.client.get(self.url('report_subcontractors'), {'export': 'csv'}))
        self.assertEqual(body[1][0], 'Sid Joinery')

    def test_csv_is_formula_safe(self):
        with org_context(self.org_a):
            self.subco.company_name = '=HYPERLINK("http://x")'
            self.subco.save()
        self.login(self.alice)
        body = rows_of(self.client.get(self.url('report_subcontractors'), {'export': 'csv'}))
        self.assertTrue(body[1][0].startswith("'="))


class EmptyCompanyTests(TestCase):
    def test_every_report_explains_an_empty_company(self):
        owner = make_user('o@x.test')
        org = create_organization('Empty Co', owner)
        self.client.force_login(owner, backend='django.contrib.auth.backends.ModelBackend')
        for name in PAGES:
            with self.subTest(name):
                resp = self.client.get(reverse(name, args=[org.slug]))
                self.assertEqual(resp.status_code, 200)
                if name != 'reports_home':
                    self.assertContains(resp, 'class="empty')
                    self.assertNotContains(resp, 'export=csv')
