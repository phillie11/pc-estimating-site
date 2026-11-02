"""
Regression tests for the usability sweep's fixes that touch behaviour rather than wording: dead links,
labels that pointed at the wrong input, and phone-sized filters.
"""
import re

from documents import services
from documents.models import Transmittal
from documents.tests.base import DocBase
from tenancy.context import org_context
from tenancy.models import ActivityEvent


class TransmittalFeedTests(DocBase):
    """A transmittal is for its recipients. Others on the project used to see its title in the feed, linking to a 404."""

    def test_issue_to_a_subcontractor_is_not_on_the_clients_feed(self):
        d, revs = self.doc('SLN-ABC-ZZ-00-DR-X-5001', 'Joinery details', ('P01',))
        with org_context(self.org_a):
            t = Transmittal(project=self.pa, title='Back-bar joinery for comment', purpose=Transmittal.PURPOSE_COMMENT)
            t.save(actor=self.staff)
            services.set_items(t, [revs['P01']])
            services.set_recipients(t, [], [self.joiners])
            services.issue(self.staff, t)
            self.assertTrue(ActivityEvent.objects.get(verb='issued').internal_only)
        self.login(self.cli)
        r = self.client.get(self.purl('project_detail'))
        self.assertNotContains(r, 'Back-bar joinery for comment')
        self.assertEqual(self.client.get(t.get_absolute_url()).status_code, 404)
        self.client.logout()
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('project_detail')), 'Back-bar joinery for comment')


class UniqueIdsTests(DocBase):
    def test_project_team_page_has_no_duplicate_ids(self):
        self.login(self.alice)
        html = self.client.get(self.purl('project_team')).content.decode()
        ids = re.findall(r'\sid="([^"]+)"', html)
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])
        self.assertIn('for="staff_role"', html)
