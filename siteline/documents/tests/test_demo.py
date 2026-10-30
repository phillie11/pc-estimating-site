"""seed_demo builds a drawing register with transmittals the demo logins can walk through."""
import io
import shutil
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings

from documents.models import Transmittal
from projects.models import Document
from tenancy.context import org_context
from tenancy.models import Organization


class SeedDemoDocumentsTests(TestCase):
    def test_demo_register_and_transmittals(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
            org = Organization.objects.get(slug='northbank-interiors')
            with org_context(org):
                trs = {t.ref: t for t in Transmittal.objects.all()}
                self.assertEqual(trs['TR-001'].status, Transmittal.STATUS_ISSUED)  # the sub hasn't acknowledged yet
                self.assertTrue(trs['TR-001'].recipients.filter(user__email='client@demo.test',
                                                                acknowledged_at__isnull=False).exists())
                self.assertEqual(trs['TR-002'].ball_in_court_company.company_name, 'Lund Joinery Ltd')
                ga = Document.objects.get(reference='CLL-NLD-ZZ-00-DR-A-1001')
                self.assertEqual(ga.current.revision, 'C01')
                self.assertEqual(ga.revisions.filter(status='SUPERSEDED').count(), 2)
                self.assertEqual(Document.objects.get(reference='E-LTG-101').naming, Document.NAMING_UNPARSED)
            call_command('seed_demo', '--reset', stdout=io.StringIO())
