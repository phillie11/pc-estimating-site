"""
Set-up for the procurement tests: the commercial test world (two companies, every role on Alpha's
live job; changes/tests/base.py) plus two subcontractors appointed on different packages.

  sid    Sid Joinery, appointed on the joinery package (pkg)
  sue    Sue Ceilings, a second subcontractor on the same job, appointed on the ceilings package (pkg2)
  connie consultant on pa         carla client on pa         bea tendering sub on pa
"""
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile

from changes.tests.base import CommercialTestBase, make_user
from core.workflow import apply_transition
from directory.models import Package, Subcontractor
from projects.models import ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

from ..models import ScheduleLine, Submittal, SubmittalRevision


def pdf(name='drawing.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


class ProcurementTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.sue = make_user('sue@s2.test', 'Sue Ceilings')
        Membership.objects.create(organization=self.org_a, user=self.sue, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.pkg = Package.objects.create(project=self.pa, subcontractor=self.subco, status=Package.APPOINTED)
            self.subco2 = Subcontractor.objects.create(company_name='Sue Ceilings Ltd')
            self.pkg2 = Package.objects.create(project=self.pa, subcontractor=self.subco2, status=Package.APPOINTED)
            ProjectMember.objects.create(project=self.pa, user=self.sue, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.subco2)

    def line(self, project=None, org=None, **kw):
        kw.setdefault('title', 'Bar joinery')
        kw.setdefault('required_on_site', date(2026, 12, 14))
        kw.setdefault('lead_time_weeks', 6)
        kw.setdefault('approval_weeks', 2)
        with org_context(org or self.org_a):
            line = ScheduleLine(project=project or self.pa, **kw)
            line.save()
            return line

    def submittal(self, package='default', line=None, actor=None, org=None, project=None, **kw):
        kw.setdefault('title', 'Bar shop drawings')
        with org_context(org or self.org_a):
            s = Submittal(project=project or self.pa, line=line, **kw)
            s.package = self.pkg if package == 'default' else package
            if s.package is not None:
                s.ball_in_court_company = s.package.subcontractor
            else:
                s.ball_in_court_user = self.pat
            s.save(actor=actor or self.pat)
            return s

    def submitted(self, **kw):
        """A submittal with rev 00 in, waiting on Pat's review (made through the model, not the views)."""
        s = self.submittal(**kw)
        with org_context(self.org_a):
            SubmittalRevision.objects.create(submittal=s, number=0, submitted_by=self.sid)
            apply_transition(s, 'submit', self.sid)
        return s

    def fresh(self, obj):
        with org_context(self.org_a):
            return type(obj).objects.get(pk=obj.pk) if not hasattr(type(obj), 'with_deleted') \
                else type(obj).with_deleted.get(pk=obj.pk)

    def detail(self, s):
        return self.url('submittal_detail', self.pa.pk, s.pk)
