"""
Set-up for the close-out tests: the procurement test world (changes/tests/base.py and
procurement/tests/base.py: two companies, every role on Alpha's live job, Sid Joinery appointed on
the joinery package and Sue Ceilings on the ceilings package), with trades on those packages and a
JCT Intermediate contract (6-month Rectification Period) on the job.
"""
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile

from contracts.models import MainContract
from directory.models import Trade
from procurement.tests.base import ProcurementTestBase
from projects.models import Issue
from tenancy.context import org_context

from ..models import CompletionSection


def pdf(name='file.pdf', body=b'%PDF-1.4 test'):
    return SimpleUploadedFile(name, body, content_type='application/pdf')


def jpg(name='photo.jpg'):
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0 not really a jpeg', content_type='image/jpeg')


class CloseoutTestBase(ProcurementTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.joinery = Trade.objects.get_or_create(name='Joinery')[0]
            self.ceilings = Trade.objects.get_or_create(name='Suspended ceilings')[0]
            self.pkg.trade = self.joinery
            self.pkg.save()
            self.pkg2.trade = self.ceilings
            self.pkg2.save()
            self.contract = MainContract.objects.create(project=self.pa, form='JCT_IC', defects_period_months=6)

    def section(self, pc=date(2026, 6, 1), whole=True, org=None, project=None, **kw):
        with org_context(org or self.org_a):
            s = CompletionSection(project=project or self.pa, pc_date=pc, is_whole_works=whole,
                                  name=CompletionSection.WHOLE_WORKS if whole else kw.pop('name', 'Section 1'), **kw)
            s.save()
            return s

    def issue(self, org=None, project=None, **kw):
        kw.setdefault('title', 'Door sticks')
        kw.setdefault('created_by', self.pat)
        with org_context(org or self.org_a):
            i = Issue(project=project or self.pa, **kw)
            i.save()
            return i

    def fresh(self, obj, org=None):
        with org_context(org or self.org_a):
            model = type(obj)
            return (model.with_deleted if hasattr(model, 'with_deleted') else model.objects).get(pk=obj.pk)
