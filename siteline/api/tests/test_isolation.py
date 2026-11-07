"""
Every endpoint, from both companies: a token reads only its own company. Another company's id is a 404
on the single-record endpoint and as a ?project= filter, its rows never appear in a list, and with no
organisation current every resource's rows fail closed. Loops over api.resources.resources(), so an
endpoint added later is covered without a new test.
"""
from django.urls import reverse

from api.resources import Context, resources
from tenancy.context import TenantContextMissing, org_context

from .base import ApiTestBase


def ids_for(org_records, project, money=None):
    """{resource name: an id of a row in that company} from the records base.py made."""
    r = org_records
    out = {'projects': project.pk, 'rfis': r['rfi'].pk, 'change-events': r['ce'].pk, 'documents': r['doc'].pk,
           'snags': r['snag'].pk, 'cost-reports': project.pk, 'sub-payments': r['sub_payment'].pk,
           'cvr': project.pk, 'incidents': r['incident'].pk}
    if money:
        out['variations'] = money['vo'].pk
        out['applications'] = money['ap1'].pk
    return out


class IsolationTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.ids_a = ids_for(self.a, self.pa, self.money)
        self.ids_b = ids_for(self.b, self.pb, self.b)

    def test_every_endpoint_is_covered_by_this_test(self):
        names = {r.name for r in resources()}
        self.assertEqual(names, set(self.ids_a), 'Add the new endpoint to ids_for()')

    def test_own_company_reads_its_own_rows(self):
        for r in resources():
            with self.subTest(r.name):
                resp = self.api(reverse(r.list_url_name()), self.text_a)
                self.assertEqual(resp.status_code, 200, resp.content)
                got = {row.get('id', row.get('project_id')) for row in resp.json()['results']}
                self.assertIn(self.ids_a[r.name], got)
                resp = self.api(reverse(r.detail_url_name(), args=[self.ids_a[r.name]]), self.text_a)
                self.assertEqual(resp.status_code, 200, resp.content)

    def test_another_companys_token_gets_404_on_every_record(self):
        for r in resources():
            with self.subTest(r.name):
                resp = self.api(reverse(r.detail_url_name(), args=[self.ids_a[r.name]]), self.text_b)
                self.assertEqual(resp.status_code, 404, resp.content)
                self.assertNotIn(b'Alpha', resp.content)
                # and the other way round
                resp = self.api(reverse(r.detail_url_name(), args=[self.ids_b[r.name]]), self.text_a)
                self.assertEqual(resp.status_code, 404, resp.content)

    def test_another_companys_rows_never_appear_in_a_list(self):
        for r in resources():
            with self.subTest(r.name):
                resp = self.api(reverse(r.list_url_name()), self.text_b)
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertNotIn('Alpha', body)
                got = {row.get('id', row.get('project_id')) for row in resp.json()['results']}
                if r.name not in ('projects', 'cost-reports', 'cvr'):
                    self.assertNotIn(self.ids_a[r.name], got)
                for row in resp.json()['results']:
                    if 'project_id' in row:
                        self.assertEqual(row['project_id'], self.pb.pk)

    def test_another_companys_project_as_a_filter_is_404(self):
        for r in resources():
            if not any(f.name == 'project' for f in r.filters):
                continue
            with self.subTest(r.name):
                resp = self.api(reverse(r.list_url_name()), self.text_b, project=self.pa.pk)
                self.assertEqual(resp.status_code, 404, resp.content)

    def test_another_companys_document_download_is_404(self):
        rev = self.a['doc'].revisions.model.all_objects.filter(document=self.a['doc']).first()
        url = reverse('api_document_download', args=[self.a['doc'].pk, rev.pk])
        self.assertEqual(self.api(url, self.text_b).status_code, 404)
        self.assertEqual(self.api(url, self.text_a).status_code, 200)

    def test_no_organisation_context_fails_closed(self):
        from django.test import RequestFactory
        ctx = Context(self.token_a, RequestFactory().get('/api/v1/'))
        for r in resources():
            with self.subTest(r.name):
                with self.assertRaises(TenantContextMissing):
                    list(r.queryset(ctx)[:1])
                with org_context(self.org_a):
                    self.assertTrue(r.queryset(ctx).filter(pk=self.ids_a[r.name]).exists())

    def test_archived_jobs_are_left_out(self):
        self.archive(self.pa, self.org_a)
        for r in resources():
            with self.subTest(r.name):
                resp = self.api(reverse(r.detail_url_name(), args=[self.ids_a[r.name]]), self.text_a)
                self.assertEqual(resp.status_code, 404, resp.content)
