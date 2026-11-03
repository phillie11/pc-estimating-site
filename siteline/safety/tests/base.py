"""
Set-up for the safety tests: changes.tests.base (two companies, every role on Alpha's live job)
plus sam (site manager on the job's team) and stan (projects staff, not on this job).

  alice owner · adam admin · pat PM (the job's manager) · sam site team · stan staff not on the job
  eve estimator · carla client · connie consultant · sid subcontractor · bea tendering sub
  nora external, not on the job · bob owner of another company
"""
import io
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from changes.tests.base import CommercialTestBase, make_user
from projects.models import ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

from ..models import Incident, SiteGate

MEDIA = tempfile.mkdtemp(prefix='safety-tests-')


def jpeg(name='scene.jpg', size=(640, 480)):
    buf = io.BytesIO()
    Image.new('RGB', size, '#996655').save(buf, 'JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=MEDIA)
class SafetyTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.sam, self.stan = make_user('sam@a.test', 'Sam Site'), make_user('stan@a.test', 'Stan Staff')
        for u in (self.sam, self.stan):
            Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.sam, role=ProjectMember.ROLE_SITE)
            self.pa.start_date = timezone.localdate() - timedelta(days=10)
            self.pa.save()

    # Who can open the Safety tab, and who can't (and what they get)
    STAFF_ON_JOB = ('alice', 'adam', 'pat', 'sam')
    REFUSED = (('stan', 403), ('eve', 404), ('carla', 403), ('connie', 403), ('sid', 403), ('bea', 403),
               ('nora', 404), ('bob', 404))

    def incident(self, project=None, org=None, actor=None, **kw):
        org = org or self.org_a
        project = project or self.pa
        with org_context(org):
            fields = {'description': 'Board fell off trolley', 'location': 'Lift lobby',
                      'ball_in_court_user': project.manager or actor or self.pat, **kw}
            i = Incident(project=project, **fields)
            i.save(actor=actor or self.pat)
            return i

    def gate(self, project=None, org=None):
        with org_context(org or self.org_a):
            return SiteGate.for_project(project or self.pa)

    def u(self, name, *args, org=None):
        return self.url(name, *args, org=org)
