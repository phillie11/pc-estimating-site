"""seed_demo: the demo tokens, every report page on the demo company, and a fresh token reading Leeds."""
import io
import shutil
import tempfile

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from api import auth
from api.models import ApiToken
from tenancy.context import org_context
from tenancy.models import Organization


class SeedDemoApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.media = tempfile.mkdtemp()
        with cls.settings(cls, MEDIA_ROOT=cls.media):
            call_command('seed_demo', stdout=io.StringIO())
        cls.org = Organization.objects.get(slug='northbank-interiors')
        cls.owner = User.objects.get(email='owner@demo.test')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.media, ignore_errors=True)

    def test_demo_tokens_and_reports(self):
        with org_context(self.org):
            tokens = {t.name: t for t in ApiToken.objects.all()}
        self.assertTrue(tokens['Power BI dashboard'].is_live)
        self.assertTrue(tokens['Old spreadsheet'].is_revoked)
        self.client.force_login(self.owner, backend='django.contrib.auth.backends.ModelBackend')
        with self.settings(MEDIA_ROOT=self.media):
            for name in ('api_settings', 'api_docs', 'reports_home', 'report_margin', 'report_cash', 'report_overdue',
                         'report_safety', 'report_subcontractors'):
                with self.subTest(name):
                    self.assertEqual(self.client.get(reverse(name, args=[self.org.slug])).status_code, 200)
            resp = self.client.get(reverse('report_margin', args=[self.org.slug]))
            self.assertContains(resp, 'Call Lane')  # the Leeds bar job
            with org_context(self.org):
                _, text = auth.create_token(self.org, self.owner, 'Test', ['registers', 'commercial', 'safety'])
            for path in ('/api/v1/projects/', '/api/v1/rfis/', '/api/v1/cost-reports/', '/api/v1/cvr/',
                         '/api/v1/applications/', '/api/v1/sub-payments/', '/api/v1/incidents/'):
                with self.subTest(path):
                    resp = self.client.get(path, HTTP_AUTHORIZATION=f'Bearer {text}')
                    self.assertEqual(resp.status_code, 200)
                    self.assertGreater(resp.json()['count'], 0, path)
