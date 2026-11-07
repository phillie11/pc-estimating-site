"""
What the endpoints serve: money reconciles to the module that owns it to the penny, nothing private
leaks, filters and paging work, and the OpenAPI document matches the endpoint definitions.
"""
import json
from datetime import timedelta
from decimal import Decimal as D

from django.urls import reverse
from django.utils import timezone

from api.resources import resources
from contracts.commercial import commercial_position
from costs.report import cost_report
from cvr import figures
from payments.models import Application
from payments.summary import certified_and_paid
from projects.models import Issue
from safety.models import Incident
from subpayments.models import SubcontractorTax
from tenancy.context import org_context

from .base import ApiTestBase


def money(v):
    return str(D(v).quantize(D('0.01')))


class ReconcileTests(ApiTestBase):
    def test_cost_report_totals_equal_the_cost_report(self):
        with org_context(self.org_a):
            rep = cost_report(self.pa)
        row = next(r for r in self.api('/api/v1/cost-reports/', self.text_a).json()['results']
                   if r['project_id'] == self.pa.pk)
        detail = self.api(f'/api/v1/cost-reports/{self.pa.pk}/', self.text_a).json()
        for got in (row, detail):
            self.assertEqual(got['budget'], money(rep['total'].budget))
            self.assertEqual(got['committed'], money(rep['total'].committed))
            self.assertEqual(got['actual'], money(rep['total'].actual))
            self.assertEqual(got['forecast_final_cost'], money(rep['final_cost']))
            self.assertEqual(got['forecast_final_account'], money(rep['final_account']))
            self.assertEqual(got['forecast_margin'], money(rep['margin']))
            self.assertEqual(got['forecast_margin_percent'], str(rep['margin_pct']))
            self.assertEqual(got['below_margin_warning'], rep['below'])
        self.assertEqual(row['actual'], '22750.00')  # 17,750 of costs and the 5,000 sub payment
        self.assertEqual(len(detail['lines']), len(rep['rows']))
        for line, r in zip(detail['lines'], rep['rows']):
            self.assertEqual((line['code'], line['forecast_final_cost'], line['variance']),
                             (r.code.code, money(r.forecast), money(r.variance)))
        self.assertNotIn('lines', row)

    def test_applications_add_up_to_certified_and_paid(self):
        with org_context(self.org_a):
            certified, paid = certified_and_paid(self.pa)
            apps = list(Application.objects.filter(project=self.pa))
        rows = self.api('/api/v1/applications/', self.text_a, project=self.pa.pk).json()['results']
        self.assertEqual(len(rows), len(apps))
        self.assertEqual(sum(D(r['notified_sum']) for r in rows if r['certified']), certified)
        self.assertEqual(sum(D(r['paid_ex_vat']) for r in rows), paid)
        ap1 = next(r for r in rows if r['id'] == self.money['ap1'].pk)
        self.assertEqual(ap1['paid_total'], '22800.00')
        self.assertEqual(ap1['status'], Application.STATUS_PAID)

    def test_cvr_this_month_equals_the_live_figures_and_the_cost_report(self):
        with org_context(self.org_a):
            f = figures.live_figures([self.pa], self.today)[self.pa.pk]
            rep = cost_report(self.pa)
            position = commercial_position(self.pa)
        row = self.api(f'/api/v1/cvr/{self.pa.pk}/', self.text_a).json()
        self.assertEqual(row['month'], f'{self.today:%Y-%m}')
        self.assertEqual(row['value'], money(f.value))
        self.assertEqual(row['cost'], money(f.cost))
        self.assertEqual(row['margin'], money(f.margin))
        self.assertEqual(row['forecast_final_account'], money(position['forecast']))
        self.assertEqual(row['forecast_final_cost'], money(rep['final_cost']))
        self.assertEqual(row['forecast_margin'], money(rep['margin']))
        self.assertEqual(row['cvr_status'], 'NONE')
        last = self.api(f'/api/v1/cvr/{self.pa.pk}/', self.text_a, month=f'{self.m1:%Y-%m}').json()
        self.assertEqual(last['month'], f'{self.m1:%Y-%m}')
        self.assertEqual(last['value'], '56000.00')  # AP-002, valued last month

    def test_change_event_totals_come_from_its_lines(self):
        from changes.models import ChangeEventLine
        with org_context(self.org_a):
            ChangeEventLine.objects.create(change_event=self.a['ce'], kind='COST', description='Cable', amount=D('120.50'))
            ChangeEventLine.objects.create(change_event=self.a['ce'], kind='REVENUE', description='Sockets',
                                           amount=D('300'))
        row = self.api(f'/api/v1/change-events/{self.a["ce"].pk}/', self.text_a).json()
        self.assertEqual((row['cost_total'], row['revenue_total'], row['margin']), ('120.50', '300.00', '179.50'))

    def test_sub_payments(self):
        pay = self.a['sub_payment']
        rows = self.api('/api/v1/sub-payments/', self.text_a).json()['results']
        row = next(r for r in rows if r['id'] == pay.pk)
        self.assertEqual(row['gross'], money(pay.gross))
        self.assertEqual(row['net'], money(pay.net))
        self.assertEqual(row['subcontractor'], 'Sid Joinery')
        self.assertFalse(row['reversed'])


class PrivacyTests(ApiTestBase):
    def test_no_contact_details_tax_references_or_injured_peoples_details(self):
        with org_context(self.org_a):
            self.subco.email, self.subco.phone = 'office@sidjoinery.test', '07700 900123'
            self.subco.save()
            SubcontractorTax.objects.create(subcontractor=self.subco, utr='1234567890', verification_number='V9988776655')
            from subpayments.models import SubPayment
            SubPayment.objects.filter(pk=self.a['sub_payment'].pk).update(reference='BACS-REF-777',
                                                                           verification_number='V9988776655')
            Incident.objects.filter(pk=self.a['incident'].pk).update(
                anyone_hurt=True, injured_name='Jo Injured', injured_company='Hurt Ltd', injury_details='Cut hand')
        self.pat.phone = '07700 900456'
        self.pat.save()
        for r in resources():
            for url in (reverse(r.list_url_name()), ):
                with self.subTest(r.name):
                    body = self.api(url, self.text_a).content.decode()
                    rows = json.loads(body)['results']
                    for row in rows:
                        detail = self.api(reverse(r.detail_url_name(), args=[row.get('id', row.get('project_id'))]),
                                          self.text_a).content.decode()
                        body += detail
                    for secret in ('@', '07700', '1234567890', 'V9988776655', 'BACS-REF', 'Jo Injured', 'Hurt Ltd',
                                   'Cut hand'):
                        self.assertNotIn(secret, body, f'{r.name} leaks {secret}')

    def test_names_only_for_people(self):
        row = self.api(f'/api/v1/projects/{self.pa.pk}/', self.text_a).json()
        self.assertEqual(row['project_manager'], 'Pat PM')


class FilterAndPagingTests(ApiTestBase):
    def test_paging(self):
        with org_context(self.org_a):
            for i in range(5):
                Issue.objects.create(project=self.pa, title=f'Snag {i}', issue_type='SNAG')
        first = self.api('/api/v1/snags/', self.text_a, page_size=3).json()
        self.assertEqual((first['count'], first['pages'], first['page']), (7, 3, 1))
        self.assertIsNone(first['previous'])
        self.assertIn('page=2', first['next'])
        self.assertIn('page_size=3', first['next'])
        last = self.client.get(first['next'].replace('page=2', 'page=3'),
                               HTTP_AUTHORIZATION=f'Bearer {self.text_a}').json()
        self.assertEqual(len(last['results']), 1)
        self.assertIsNone(last['next'])
        ids = [r['id'] for r in first['results']]
        self.assertEqual(ids, sorted(ids))

    def test_filters(self):
        rows = self.api('/api/v1/snags/', self.text_a, type='DEFECT').json()['results']
        self.assertEqual([r['id'] for r in rows], [self.a['defect'].pk])
        rows = self.api('/api/v1/rfis/', self.text_a, status='DRAFT,CLOSED').json()['results']
        self.assertEqual(rows, [])
        rows = self.api('/api/v1/rfis/', self.text_a, status='open,RESPONDED').json()['results']
        self.assertEqual([r['id'] for r in rows], [self.a['rfi'].pk])
        self.assertTrue(rows[0]['overdue'])
        later = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, updated_since=later).json()['count'], 0)
        self.assertEqual(self.api('/api/v1/rfis/', self.text_a, updated_since='2020-01-01').json()['count'], 1)
        rows = self.api('/api/v1/projects/', self.text_a, stage='LIVE').json()['results']
        self.assertEqual([r['id'] for r in rows], [self.pa.pk])

    def test_detail_fields_only_on_the_single_record(self):
        listed = self.api('/api/v1/rfis/', self.text_a).json()['results'][0]
        one = self.api(f'/api/v1/rfis/{self.a["rfi"].pk}/', self.text_a).json()
        self.assertNotIn('question', listed)
        self.assertEqual(one['question'], 'What depth is the void at grid C?')

    def test_documents_carry_revisions_and_download_links(self):
        one = self.api(f'/api/v1/documents/{self.a["doc"].pk}/', self.text_a).json()
        self.assertEqual(one['current_revision']['revision'], 'P01')
        self.assertEqual(len(one['revisions']), 1)
        self.assertTrue(one['current_revision']['download_url'].endswith(
            f'/api/v1/documents/{self.a["doc"].pk}/revisions/{self.a["rev"].pk}/download/'))

    def test_money_is_text_with_two_decimal_places(self):
        row = self.api(f'/api/v1/applications/{self.money["ap2"].pk}/', self.text_a).json()
        for name in ('gross_valuation', 'retention', 'amount_due', 'vat', 'total'):
            self.assertRegex(row[name], r'^-?\d+\.\d{2}$')


class OpenApiTests(ApiTestBase):
    def test_document_is_public_and_matches_the_endpoints(self):
        resp = self.client.get('/api/v1/openapi.json')
        self.assertEqual(resp.status_code, 200)
        doc = resp.json()
        self.assertEqual(doc['openapi'], '3.0.3')
        self.assertEqual(doc['components']['securitySchemes']['bearer']['scheme'], 'bearer')
        for r in resources():
            with self.subTest(r.name):
                path = f'/{r.name}/'
                self.assertIn(path, doc['paths'])
                self.assertIn(f'/{r.name}/{{id}}/', doc['paths'])
                key = ''.join(p.capitalize() for p in r.name.split('-'))
                schema = doc['components']['schemas'][key + 'Detail']
                self.assertEqual(set(schema['properties']), {f.name for f in r.fields})
                listed = {p['name'] for p in doc['paths'][path]['get']['parameters']}
                self.assertEqual(listed, {f.name for f in r.filters} | {'page', 'page_size'})
                # every field a real call returns is described, and nothing more
                body = self.api('/api/v1' + path, self.text_a).json()['results'][0]
                self.assertEqual(set(body), set(doc['components']['schemas'][key]['properties']))
        self.assertIn('/documents/{id}/revisions/{revision_id}/download/', doc['paths'])
        self.assertNotIn('Alpha', resp.content.decode())

    def test_docs_page_lists_every_resource(self):
        self.login(self.alice)
        resp = self.client.get(reverse('api_docs', args=[self.org_a.slug]))
        self.assertEqual(resp.status_code, 200)
        for r in resources():
            self.assertContains(resp, f'id="{r.name}"')
