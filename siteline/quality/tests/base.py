"""
Set-up for the quality tests: the commercial test world (two companies, every role on Alpha's live
job; changes/tests/base.py) plus an appointed drylining package for Sid, a site manager on the team
and helpers that make records through the models.

  alice owner   adam admin   pat PM (Project.manager)   sam site manager on the team (can't approve)
  eve estimator (can't open live jobs)   stan staff not on the job
  carla client  connie consultant  sid subcontractor  bea tendering sub  nora external not on the job
  bob owner of Bravo (another company)
"""
import datetime
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from changes.tests.base import CommercialTestBase, make_user
from directory.models import Package
from projects.models import ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

from .. import rules
from ..library import ensure_library
from ..models import ChecklistTemplate, Inspection, InspectionItem, Penetration, Permit

MEDIA = tempfile.mkdtemp()


def tearDownModule():  # imported into each test module
    shutil.rmtree(MEDIA, ignore_errors=True)


def jpeg(name='photo.jpg', size=(64, 48), colour='#808080'):
    buf = io.BytesIO()
    Image.new('RGB', size, colour).save(buf, 'JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=MEDIA)
class QualityTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.sam = make_user('sam@a.test', 'Sam Site')
        self.stan = make_user('stan@a.test', 'Stan Staff')
        Membership.objects.create(organization=self.org_a, user=self.sam, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.sam, role=ProjectMember.ROLE_SITE)
            self.pkg = Package.objects.create(project=self.pa, subcontractor=self.subco, status=Package.APPOINTED)
            ensure_library(self.org_a, self.alice)
            self.template = ChecklistTemplate.objects.get(name='Fire doors')

    def inspection(self, results=None, actor=None, project=None, org=None, package=None, **kw):
        org = org or self.org_a
        with org_context(org):
            if org == self.org_a:
                template = self.template
            else:
                ensure_library(org)
                template = ChecklistTemplate.objects.get(name='Fire doors')
            ins = Inspection(project=project or self.pa, template=template, title=kw.pop('title', 'Fire doors, L1'),
                             ball_in_court_user=kw.pop('inspector', self.pat), due_date=timezone.localdate(),
                             package=package, **kw)
            ins.save(actor=actor or self.pat)
            items = [InspectionItem.objects.create(inspection=ins, order=i.order, text=i.text,
                                                   result=(results[n] if results and n < len(results) else ''))
                     for n, i in enumerate(template.items.all())]
            return ins, items

    def permit(self, kind=rules.HOT_WORKS, actor=None, starts=None, hours=8, project=None, org=None, **kw):
        org = org or self.org_a
        start = starts or timezone.now() - datetime.timedelta(hours=1)
        with org_context(org):
            p = Permit(project=project or self.pa, permit_type=kind, title=kw.pop('title', 'Weld brackets'),
                       location='Bar', starts_at=start, expires_at=start + datetime.timedelta(hours=hours), **kw)
            from ..workflow import approver_for
            p.ball_in_court_user = approver_for(p.project)
            p.save(actor=actor or self.sam)
            return p

    def penetration(self, actor=None, project=None, org=None, **kw):
        with org_context(org or self.org_a):
            kw.setdefault('location', 'L1 riser wall')
            kw.setdefault('fire_rating', 60)
            p = Penetration(project=project or self.pa, **kw)
            p.title = p.default_title()
            p.save(actor=actor or self.pat)
            return p

    def fresh(self, obj):
        with org_context(obj.organization):
            model = type(obj)
            manager = getattr(model, 'with_deleted', model.objects)
            return manager.get(pk=obj.pk)

    def purl(self, name, *args, org=None, project=None):
        return self.url(name, (project or self.pa).pk, *args, org=org)
