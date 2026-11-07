"""
Review repro (stream I, wave 2): purge_visitor_signins(org) took the company as an argument but deleted
from whatever organisation was current. Called for one company inside another's context (a job, a shell,
a future caller looping differently from the command), it deleted the other company's visitors, using
the first company's retention period. Inside unscoped() it deleted every company's.
"""
import datetime

from django.test import override_settings
from django.utils import timezone

from tenancy.context import org_context

from ..models import SafetySettings, SiteAttendance
from ..retention import purge_visitor_signins
from .base import MEDIA, SafetyTestBase


@override_settings(MEDIA_ROOT=MEDIA)
class PurgeIsPinnedToItsCompanyTests(SafetyTestBase):
    def test_purge_for_one_company_never_touches_another(self):
        from projects.models import Project
        with org_context(self.org_b):
            pb = Project.objects.get(pk=self.pb.pk)
            b_old = SiteAttendance.objects.create(project=pb, kind=SiteAttendance.KIND_VISITOR, name='Bea',
                                                  signed_in_at=timezone.now() - datetime.timedelta(days=200))
        with org_context(self.org_a):
            a_old = SiteAttendance.objects.create(project=self.pa, kind=SiteAttendance.KIND_VISITOR, name='Vic',
                                                  signed_in_at=timezone.now() - datetime.timedelta(days=200))
            s = SafetySettings.for_org(self.org_a)
            s.visitor_retention_months = 3
            s.save()
        with org_context(self.org_b):  # the wrong company is current: nothing of Bravo's goes
            n, _ = purge_visitor_signins(self.org_a)
            self.assertTrue(SiteAttendance.objects.filter(pk=b_old.pk).exists())  # Bravo keeps 12 months
        self.assertEqual(n, 0)
        with org_context(self.org_a):  # in its own context, Alpha's goes
            self.assertEqual(purge_visitor_signins(self.org_a)[0], 1)
            self.assertFalse(SiteAttendance.objects.filter(pk=a_old.pk).exists())

    def test_purge_inside_unscoped_stays_in_its_company(self):
        from projects.models import Project
        from tenancy.context import unscoped
        with org_context(self.org_b):
            pb = Project.objects.get(pk=self.pb.pk)
            b_old = SiteAttendance.objects.create(project=pb, kind=SiteAttendance.KIND_VISITOR, name='Bea',
                                                  signed_in_at=timezone.now() - datetime.timedelta(days=500))
        with unscoped():  # e.g. called from a platform-admin loop over every company
            n, _ = purge_visitor_signins(self.org_a)
        self.assertEqual(n, 0)
        with org_context(self.org_b):
            self.assertTrue(SiteAttendance.objects.filter(pk=b_old.pk).exists())
