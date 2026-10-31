"""
The independent review's reproductions of costs/ bugs, kept as regression tests. Each one now
asserts the fixed behaviour.
"""
import time
from decimal import Decimal as D

from django.contrib.messages import get_messages
from django.db import connection
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from costs import actions as cost_actions
from costs import ledger, services
from costs.models import Commitment, CommitmentAdjustment, CostCode, ForecastOverride, LedgerEntry
from costs.report import cost_report
from directory.models import Package
from projects.models import Project
from tenancy.context import org_context, unscoped
from tenancy.models import Membership

from .test_costs import CostTestBase


@override_settings(STEP_UP_SECONDS=10**9, RATELIMIT_ENABLED=False)
class ReviewRepro(CostTestBase):
    def test_1_project_with_only_costs_has_records_and_is_not_deleted(self):
        with self.a():
            job = Project.objects.create(name='Live, nothing else', stage=Project.STAGE_LIVE, value=D('1000'))
            services.place_order(job, self.pm, self.codes['500'], 'Speedy', D('100'))
            self.assertTrue(job.has_records())
        self.login(self.alice)
        from security.twofactor import AUTH_AT_KEY
        s = self.client.session
        s[AUTH_AT_KEY] = int(time.time())
        s.save()
        self.client.raise_request_exception = True
        r = self.client.post(reverse('project_delete', args=[self.org_a.slug, job.pk]), {'confirm': job.reference})
        self.assertEqual(r.status_code, 302)
        self.assertIn('can only be archived', ' '.join(str(m) for m in get_messages(r.wsgi_request)))
        with self.a():
            self.assertTrue(Project.objects.filter(pk=job.pk).exists())

    def test_2_a_committed_package_is_not_removed(self):
        with self.a():
            services.commit_package(self.package, self.codes['220'], self.pm)
        self.login(self.alice)
        self.client.raise_request_exception = True
        url = reverse('package_list', args=[self.org_a.slug, self.pa.pk])
        r = self.client.post(url, {'action': 'remove', 'package': self.package.pk})
        self.assertIn('This package has an order in Costs; adjust the order instead',
                      ' '.join(str(m) for m in get_messages(r.wsgi_request)))
        with self.a():
            self.assertTrue(Package.objects.filter(pk=self.package.pk).exists())
            # A committed package that isn't appointed any more still has no Remove button
            Package.objects.filter(pk=self.package.pk).update(status=Package.QUOTED)
        self.assertNotContains(self.client.get(url), 'value="remove"')

    def test_3_invoiced_to_date_nets_off_reversed_invoices(self):
        with self.a():
            c = services.place_order(self.pa, self.pm, self.codes['500'], 'Speedy', D('1000'))
            a = services.record_actual(self.pa, self.pm, self.codes['500'], 'INVOICE', 'Speedy', D('400'),
                                       commitment=c)
            services.record_actual(self.pa, self.pm, self.codes['500'], 'INVOICE', 'Speedy', D('150.50'),
                                   commitment=c)
            e = LedgerEntry.objects.get(source_id=a.pk, kind=ledger.ACTUAL)
            ledger.reverse(e, self.pm, 'wrong order')
        self.login(self.pm)
        r = self.client.get(c.get_absolute_url())
        self.assertEqual(r.context['invoiced'], D('150.50'))
        self.assertContains(r, 'Reversed')

    def test_4_margin_is_compared_unrounded(self):
        # contract 1,000,000; cost 900,400 => 9.96% margin, under a 10% threshold though it shows 10.0%
        self.budget(**{'100': '900400'}, margin='9.96')
        with self.a():
            rep = cost_report(self.pa)
        self.assertEqual(rep['margin'], D('99600.00'))
        self.assertEqual(rep['margin_pct'], D('10.0'))
        self.assertTrue(rep['below'])
        self.login(self.pm)
        self.assertEqual(len([i for i in self.needs_me(self.pm) if i.kind == 'cost_margin']), 1)

    def test_5_a_code_with_money_keeps_its_code_and_group(self):
        self.budget()
        code = self.codes['220']
        url = reverse('cost_code_edit', args=[self.org_a.slug, code.pk])
        self.login(self.alice)
        r = self.client.post(url, {'code': '999', 'name': 'Something else entirely', 'group': 'FEES',
                                   'is_active': 'on'})
        self.assertContains(r, 'so its code and group can')
        r = self.client.post(url, {'code': '220', 'name': 'Joinery', 'group': 'FEES', 'is_active': 'on'})
        self.assertContains(r, 'so its code and group can')
        # A correction to the name is allowed
        r = self.client.post(url, {'code': '220', 'name': 'Joinery and carpentry', 'group': 'PACKAGES',
                                   'is_active': 'on'})
        self.assertEqual(r.status_code, 302)
        with self.a():
            code = CostCode.objects.get(pk=code.pk)
        self.assertEqual((code.code, code.group, code.name), ('220', 'PACKAGES', 'Joinery and carpentry'))
        # A code with no money can still be renumbered
        spare = self.codes['265']
        self.client.post(reverse('cost_code_edit', args=[self.org_a.slug, spare.pk]),
                         {'code': '266', 'name': 'Signage', 'group': 'PACKAGES', 'is_active': 'on'})
        with self.a():
            self.assertEqual(CostCode.objects.get(pk=spare.pk).code, '266')

    def test_6_double_submit_records_once(self):
        self.login(self.pm)
        url = self.curl('cost_order_create')
        data = {'cost_code': self.codes['500'].pk, 'supplier': 'Speedy', 'value': '500', 'order_date': '2026-10-01'}
        self.client.post(url, data)
        self.client.post(url, data)
        with self.a():
            self.assertEqual(LedgerEntry.objects.filter(project=self.pa, kind='COMMITMENT').count(), 1)
        # The same page twice (same one-time key) records once; a fresh page is a new order
        page = self.client.get(url)
        key = page.context['form']['form_key'].value()
        self.client.post(url, {**data, 'supplier': 'Hire Co', 'form_key': key})
        self.client.post(url, {**data, 'supplier': 'Hire Co', 'form_key': key})
        key2 = self.client.get(url).context['form']['form_key'].value()
        self.assertNotEqual(key, key2)
        self.client.post(url, {**data, 'supplier': 'Hire Co', 'form_key': key2})
        with self.a():
            self.assertEqual(Commitment.objects.filter(supplier='Hire Co').count(), 2)
            order = Commitment.objects.first()
        adj = {'action': 'adjust', 'amount': '-50', 'reason': 'Shorter hire', 'form_key': 'abc'}
        self.client.post(order.get_absolute_url(), adj)
        self.client.post(order.get_absolute_url(), adj)
        with self.a():
            self.assertEqual(CommitmentAdjustment.objects.filter(commitment=order).count(), 1)
            self.assertEqual(order.value, D('450.00'))

    def test_7_archived_jobs_refuse_forecasts_with_a_message(self):
        self.budget()
        with self.a():
            self.pa.archived_at = timezone.now()
            self.pa.save()
            with self.assertRaises(services.CostError):
                services.set_forecast(self.pa, self.pm, self.codes['220'], D('1'), 'after archive')
            with self.assertRaises(services.CostError):
                services.record_actual(self.pa, self.pm, self.codes['220'], 'INVOICE', 'X', D('1'))
            self.assertFalse(ForecastOverride.objects.exists())

    def test_9_only_actual_costs_can_be_reversed(self):
        self.budget()
        with self.a():
            c = services.place_order(self.pa, self.pm, self.codes['500'], 'Speedy', D('100'))
            for entry in LedgerEntry.objects.exclude(kind=ledger.ACTUAL):
                with self.assertRaisesMessage(ledger.LedgerError, 'Only actual costs are reversed'):
                    ledger.reverse(entry, self.pm, 'try')
            self.assertEqual(c.value, D('100.00'))
            self.assertFalse(LedgerEntry.objects.filter(reverses__isnull=False).exists())

    def needs_me(self, user):
        req = RequestFactory().get('/')
        req.user, req.org = user, self.org_a
        with self.a():
            req.membership = Membership.objects.get(organization=self.org_a, user=user)
            return cost_actions.items(req)


class CascadeChecks(CostTestBase):
    def test_user_and_org_deletion_and_cross_org_reverse(self):
        with self.a():
            c = services.commit_package(self.package, self.codes['220'], self.pm)
            a = services.record_actual(self.pa, self.pm, self.codes['220'], 'INVOICE', 'Lund', D('10'), commitment=c)
            e = LedgerEntry.objects.get(source_id=a.pk, kind='ACTUAL')
        self.pm.delete()  # SET_NULL through the append-only querysets
        with self.a():
            e = LedgerEntry.objects.get(pk=e.pk)
            self.assertIsNone(e.actor_id)
            self.assertEqual(e.actor_name, 'Pat PM')
        with org_context(self.org_b):
            with self.assertRaises(ledger.LedgerError):
                ledger.reverse(LedgerEntry.all_objects.get(pk=e.pk), self.bob, 'cross org')
        with unscoped():
            self.org_a.delete()
            self.assertFalse(LedgerEntry.all_objects.filter(pk=e.pk).exists())


class NeedsMeQueryCount(CostTestBase):
    """Item 8: the margin check on Needs me runs on every page, so it must not grow with the number of jobs."""

    def jobs_with_costs(self, n):
        with self.a():
            while Project.objects.filter(cost_ledger__isnull=False).distinct().count() < n:
                j = Project.objects.create(name=f'Job {Project.objects.count()}', stage=Project.STAGE_LIVE,
                                           value=D('100'))
                services.place_order(j, self.pm, self.codes['500'], 'S', D('95'))
                services.set_forecast(j, self.pm, self.codes['500'], D('96'), 'Hire extended')

    def count(self):
        req = RequestFactory().get('/')
        req.user, req.org = self.alice, self.org_a
        with self.a():
            req.membership = Membership.objects.get(organization=self.org_a, user=self.alice)
            with CaptureQueriesContext(connection) as q:
                items = cost_actions.items(req)
        return len(q), items

    def test_queries_do_not_grow_with_jobs(self):
        self.jobs_with_costs(1)
        one, items = self.count()
        self.assertEqual(len(items), 1)  # margin 4% on £100
        self.jobs_with_costs(10)
        ten, items = self.count()
        self.assertEqual(len(items), 10)
        self.assertEqual(one, ten)

    def test_same_answer_as_the_cost_report(self):
        self.budget(**{'100': '850000', '220': '60000'}, margin='9')
        with self.a():
            services.set_forecast(self.pa, self.pm, self.codes['220'], D('50001'), 'Saving agreed')
            rep = cost_report(self.pa)
        items = self.count()[1]
        self.assertEqual(rep['below'], bool(items))
        self.assertEqual(items[0].amount, rep['margin'])
