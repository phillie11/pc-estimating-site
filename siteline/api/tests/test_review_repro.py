"""
Review of the read-only API (claude/r3b-api): each test reproduces one defect found in review, and
failed before its fix.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from api import auth
from api.models import ApiAccess
from projects.models import FileAccess
from rfis.models import RFI
from tenancy.context import org_context

from .base import ApiTestBase


class BadInputIsA400NotAServerErrorTests(ApiTestBase):
    """A date that is shaped right but doesn't exist, a huge number or year 1 crashed the call with a 500,
    and the call never reached the token's log."""

    def assert_400(self, path, **params):
        resp = self.api(path, self.text_a, **params)
        self.assertEqual(resp.status_code, 400, (path, params))
        self.assertIn('message', resp.json()['error'])
        with org_context(self.org_a):
            self.assertEqual(ApiAccess.objects.filter(token=self.token_a).first().status, 400)

    def test_dates_that_do_not_exist(self):
        self.assert_400('/api/v1/projects/', updated_since='2026-02-30')
        self.assert_400('/api/v1/rfis/', updated_since='2026-02-30T09:00:00')
        self.assert_400('/api/v1/sub-payments/', paid_since='2026-02-30')

    def test_dates_at_the_edge_of_the_calendar(self):
        # Past the last date there is, or before the first, once moved into UTC
        self.assert_400('/api/v1/projects/', updated_since='9999-12-31T23:59:59-23:59')
        self.assert_400('/api/v1/projects/', updated_since='0001-01-01T00:00:00+05:00')
        # A real but very early date means "everything"
        resp = self.api('/api/v1/projects/', self.text_a, updated_since='0001-01-02')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], self.api('/api/v1/projects/', self.text_a).json()['count'])

    def test_a_number_too_long_to_be_an_id(self):
        self.assert_400('/api/v1/rfis/', project='9' * 5000)

    def test_cvr_month_in_year_one(self):
        self.assert_400('/api/v1/cvr/', month='0001-01')
        self.assertEqual(self.api(f'/api/v1/cvr/{self.pa.pk}/', self.text_a, month='0001-01').status_code, 400)


class TokenTextNeverStoredInTheLogTests(ApiTestBase):
    """The call log kept the whole query string, so a token pasted into the URL (as well as sent in the
    header) was stored in full and shown to every owner and admin on the token's page for 90 days."""

    def test_token_in_the_query_string_is_blanked_in_the_log(self):
        prefix_b, secret_b = auth.parse(self.text_b)
        prefix_a, secret_a = auth.parse(self.text_a)
        self.client.get(f'/api/v1/projects/?api_key={self.text_b}&x={self.text_a}',
                        HTTP_AUTHORIZATION=f'Bearer {self.text_a}')
        with org_context(self.org_a):
            path = ApiAccess.objects.get(token=self.token_a).path
        for secret in (secret_a, secret_b):
            self.assertNotIn(secret, path)
        self.assertTrue(path.startswith('/api/v1/projects/?'))
        self.login(self.alice)
        page = self.client.get(reverse('api_token_detail', args=[self.org_a.slug, self.token_a.pk]))
        self.assertNotContains(page, secret_a)
        self.assertNotContains(page, secret_b)


class AccessLogRetentionTests(ApiTestBase):
    """Calls older than 90 days were only pruned when the same company made another API call, so a company
    that stopped using the API (or revoked its tokens) kept callers' addresses for ever."""

    def test_the_worker_prunes_every_companys_old_calls(self):
        from jobs.runner import Worker
        old = timezone.now() - timedelta(days=auth.KEEP_DAYS + 1)
        recent = timezone.now() - timedelta(days=auth.KEEP_DAYS - 1)
        rows = {}
        for org, token in ((self.org_a, self.token_a), (self.org_b, self.token_b)):
            with org_context(org):
                rows[org.pk] = (ApiAccess.objects.create(token=token, method='GET', path='/old', status=200, at=old,
                                                         ip='203.0.113.9'),
                                ApiAccess.objects.create(token=token, method='GET', path='/new', status=200,
                                                         at=recent))
        Worker().prune()  # no API call in between, no organisation current: the worker's hourly prune
        for org in (self.org_a, self.org_b):
            with org_context(org):
                gone, kept = rows[org.pk]
                self.assertFalse(ApiAccess.objects.filter(pk=gone.pk).exists())
                self.assertTrue(ApiAccess.objects.filter(pk=kept.pk).exists())


class RfiOverdueAgreesWithTheRfiTests(ApiTestBase):
    """The API called a responded RFI past its date "overdue"; the RFI's own page, Needs me and the overdue
    report count only RFIs still waiting for a response."""

    def test_responded_rfi_is_not_overdue(self):
        with org_context(self.org_a):
            rfi = RFI.objects.get(pk=self.a['rfi'].pk)
            self.assertTrue(rfi.is_overdue)
            RFI.objects.filter(pk=rfi.pk).update(status=RFI.STATUS_RESPONDED)
            rfi.refresh_from_db()
            self.assertFalse(rfi.is_overdue)
        body = self.api(f'/api/v1/rfis/{rfi.pk}/', self.text_a).json()
        self.assertEqual(body['status'], RFI.STATUS_RESPONDED)
        self.assertFalse(body['overdue'])
