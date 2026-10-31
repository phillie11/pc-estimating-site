"""seed_demo pins three snags and an RFI on the Leeds GA, and each demo login sees the right ones."""
import io
import json
import re
import shutil
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from projects.models import Document
from tenancy.context import org_context
from tenancy.models import Organization
from viewer.models import DrawingPin


class SeedDemoPinsTests(TestCase):
    def test_demo_pins(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
            org = Organization.objects.get(slug='northbank-interiors')
            with org_context(org):
                ga = Document.objects.get(reference='CLL-NLD-ZZ-00-DR-A-1001')
                pins = list(DrawingPin.objects.filter(document=ga))
                current = ga.current
            self.assertEqual(sum(1 for p in pins if p.issue_id), 3)
            self.assertEqual(sum(1 for p in pins if p.rfi_id), 1)
            self.assertEqual(sorted(p.revision_code for p in pins), ['C01', 'C01', 'C01', 'P02'])
            url = reverse('drawing_viewer', args=[org.slug, ga.project_id, ga.pk, current.pk])
            seen = {}
            for email in ('pm@demo.test', 'sub@demo.test', 'client@demo.test'):
                self.client.force_login(User.objects.get(email=email),
                                        backend='django.contrib.auth.backends.ModelBackend')
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, email)
                cfg = json.loads(re.search(r'<script id="dv-config" type="application/json">(.*?)</script>',
                                           r.content.decode(), re.S).group(1))
                seen[email] = sorted(p['ref'] for p in cfg['pins'])
                pdf = self.client.get(cfg['pdf_url'])
                self.assertEqual(pdf.status_code, 200, email)
                self.assertTrue(b''.join(pdf.streaming_content).startswith(b'%PDF-'))
            self.assertEqual(seen['pm@demo.test'], ['IS-001', 'IS-002', 'IS-003', 'RFI-001'])
            self.assertEqual(seen['sub@demo.test'], ['IS-001', 'RFI-001'])
            self.assertEqual(seen['client@demo.test'], ['RFI-001'])
