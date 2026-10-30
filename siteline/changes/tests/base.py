"""
Set-up for the commercial tests (contracts, changes, variations): two companies and every role
on Alpha's live job, as in core/tests/base.py.

  alice   owner of Alpha            adam    admin of Alpha
  pat     staff, projects (PM)      eve     staff, estimating only (can't open live jobs)
  carla   client on pa              connie  consultant on pa
  sid     subcontractor on pa       bea     tendering subcontractor on pa
  nora    external member of Alpha, not on pa
  bob     owner of Bravo (another company)
"""
from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from accounts.views import create_organization
from directory.models import Subcontractor
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

PW = 'Correct-Horse-9'


def make_user(email, name='Test User'):
    return User.objects.create_user(email=email, password=PW, full_name=name)


class CommercialTestBase(TestCase):
    def setUp(self):
        u = make_user
        self.alice, self.adam, self.pat, self.eve = (u('alice@a.test', 'Alice A'), u('adam@a.test', 'Adam Admin'),
                                                     u('pat@a.test', 'Pat PM'), u('eve@a.test', 'Eve Estimator'))
        self.carla, self.connie, self.sid, self.bea, self.nora = (
            u('carla@c.test', 'Carla Client'), u('connie@c.test', 'Connie Consultant'), u('sid@s.test', 'Sid Sub'),
            u('bea@s.test', 'Bea Bidder'), u('nora@n.test', 'Nora Nobody'))
        self.bob = u('bob@b.test', 'Bob B')
        self.org_a = create_organization('Alpha Fitout', self.alice)
        self.org_b = create_organization('Bravo Build', self.bob)
        M = Membership
        M.objects.create(organization=self.org_a, user=self.adam, role=M.ROLE_ADMIN)
        M.objects.create(organization=self.org_a, user=self.pat, role=M.ROLE_MEMBER, area=M.AREA_DELIVERY)
        M.objects.create(organization=self.org_a, user=self.eve, role=M.ROLE_MEMBER, area=M.AREA_ESTIMATING)
        for ext in (self.carla, self.connie, self.sid, self.bea, self.nora):
            M.objects.create(organization=self.org_a, user=ext, role=M.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.pa = Project.objects.create(name='Alpha job', stage=Project.STAGE_LIVE, value=Decimal('100000.00'), manager=self.pat)
            self.subco = Subcontractor.objects.create(company_name='Sid Joinery')
            PM = ProjectMember
            self.carla_member = PM.objects.create(project=self.pa, user=self.carla, role=PM.ROLE_CLIENT,
                                                  company_name='Client Co')
            PM.objects.create(project=self.pa, user=self.connie, role=PM.ROLE_CONSULTANT)
            PM.objects.create(project=self.pa, user=self.sid, role=PM.ROLE_SUBCONTRACTOR, subcontractor=self.subco)
            PM.objects.create(project=self.pa, user=self.bea, role=PM.ROLE_BIDDER, subcontractor=self.subco)
        with org_context(self.org_b):
            self.pb = Project.objects.create(name='Bravo job', stage=Project.STAGE_LIVE)

    # Everyone who must never see contract, early warning or change event pages, and what they get
    EXTERNALS = ('carla', 'connie', 'sid', 'bea')

    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, *args, org=None):
        return reverse(name, args=[(org or self.org_a).slug, *args])

    def request_for(self, user, org=None):
        """A stand-in request for functions that take one (visible_index, needs_me, changes.actions.items)."""
        org = org or self.org_a
        with org_context(org):
            membership = Membership.objects.filter(organization=org, user=user, is_active=True).first()
        return SimpleNamespace(user=user, membership=membership, org=org)

    def status_for(self, user, url, method='get', data=None):
        self.client.logout()
        if user is not None:
            self.login(user)
        r = getattr(self.client, method)(url, data or {})
        return r.status_code
